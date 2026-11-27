"""Unit submission phases: intent recheck, persistence, isolation, slurm, pipeline, code.

Python 3.8+, standard library only; dependencies never point to the facade.
"""

import json
import os
import shlex
import subprocess
import time
from pathlib import Path

import child_environment as CE
import codex_home as CH
import coordinator_paths as CP
import cli_io as CIO
import lor_io as LIO
import unit as U
import worktree as W
import swarm_code_launch as SCL
import swarm_code_protocol as SCP
import swarm_dispatch as SD
import swarm_evidence as SE
import swarm_plan_rules as SPR
import swarm_preflight as SP
import swarm_state as SS
import swarm_workspaces as SW
from swarm_types import PlanError


def _recheck_existing_code_intent(u, attempt, existing_intent,
                                  dispatch_source=None):
    """Re-verify admission for a recorded code launch intent before re-dispatch."""
    anchor_err = SCL._code_launch_intent_problem(existing_intent, u, attempt)
    if not anchor_err and (("seed" in u) != ("seed" in existing_intent)
                           or u.get("seed") != existing_intent.get("seed")):
        anchor_err = (f"unit {u['id']!r}: seed differs from recorded "
                      "launch intent; allocate a fresh attempt")
    if not anchor_err:
        try:
            seed = SPR.declared_seed(u)
        except PlanError as exc:
            anchor_err = str(exc)
        else:
            anchor_err = SP._seed_reachability_problem(
                seed, existing_intent["repo"],
                existing_intent.get("repository_remote_raw"),
                existing_intent.get("repository_remote"))
    if not anchor_err and dispatch_source is None:
        target, anchor_err = SP._resolve_dispatch_target(u)
        if not anchor_err:
            dispatch_source, anchor_err = SP._dispatch_source_identity(
                u, target)
    if (not anchor_err and existing_intent["base_commit"] !=
            dispatch_source["target_commit"]):
        anchor_err = SP._dispatch_base_refusal(
            u.get("id"), dispatch_source["repo"],
            f"recorded launch base {existing_intent['base_commit']} "
            f"differs from resolved target "
            f"{dispatch_source['target_commit']}; allocate a fresh "
            f"attempt rather than replacing its launch authority")
    if not anchor_err:
        anchor_err = SP._repeat_stash_preflight(u)
    anchored_base = (None if anchor_err else {
        "base": existing_intent["base_commit"],
        "intent": existing_intent,
    })
    return anchor_err, anchored_base


def _persist_anchored_launch(u, unit_dir, kind, dry_run, state, state_dir,
                             anchored_base):
    """Record trusted pre-launch authority in coordinator state and flush to disk."""
    if state is not None and anchored_base:
        us = state.setdefault("units", {}).setdefault(u["id"], {})
        if anchored_base.get("base"):
            us.setdefault("attempt_bases", {})[Path(unit_dir).name] = (
                anchored_base["base"])
        if anchored_base.get("intent"):
            us.setdefault("attempt_launch_intents", {})[
                Path(unit_dir).name] = anchored_base["intent"]
        if anchored_base.get("facts"):
            us.setdefault("attempt_launch_facts", {})[
                Path(unit_dir).name] = anchored_base["facts"]
        if anchored_base.get("seal"):
            us.setdefault("attempt_record_seals", {})[Path(unit_dir).name] = (
                anchored_base["seal"])
    if kind == "code" and not dry_run and state_dir is not None:
        SS.save_state(state_dir, state)
    if SP._requires_clean_workspace(u) and kind != "code" and not dry_run:
        facts = SS.trusted_launch_facts(state or {}, u["id"], unit_dir)
        if not facts:
            return (
                f"unit {u['id']!r}: coordinator state has no complete launch "
                f"snapshot for attempt {Path(unit_dir).name!r}. Re-dispatch "
                f"into a fresh attempt; do not reconstruct trusted facts "
                f"from the agent-writable launch record.")
        stored = SE.trusted_record_seal(state or {}, u["id"], unit_dir)
        if stored:
            _audit, audit_err = W.read_sealed_launch_record(unit_dir, stored)
            audit = (state.setdefault("units", {}).setdefault(u["id"], {})
                     .setdefault("attempt_record_audit", {}))
            if audit_err:
                audit[Path(unit_dir).name] = audit_err
            else:
                audit.pop(Path(unit_dir).name, None)
        if state_dir is not None:
            SS.save_state(state_dir, state)
    return None


def _prepare_isolation_command(u, unit_dir, state, state_dir):
    """Build and durably record OS-backed isolation submission wrapper."""
    prior = (((state.get("units") or {}).get(u["id"]) or {})
             .get("attempt_isolation_facts") or {}).get(Path(unit_dir).name)
    submission_command, isolation_facts, isolation_error = (
        SD._isolation_submission(u, unit_dir, prior))
    if isolation_error:
        return None, isolation_error
    isolation_error = SD._clear_isolation_marker(isolation_facts)
    if isolation_error:
        return None, isolation_error
    isolation_error = SD._record_isolation_facts(
        state, u, unit_dir, isolation_facts)
    if isolation_error:
        return None, isolation_error
    SS.save_state(state_dir, state)
    return submission_command, None


def _submit_slurm(u, unit_dir, state, record_enabled, submission_command):
    """Render job.sbatch, persist submitted.json, and submit via sbatch."""
    deps = SD._dep_env(u, state or {})
    script = Path(unit_dir) / "job.sbatch"
    attempt_id = Path(unit_dir).name
    body = ["#!/bin/bash", f"#SBATCH --job-name=swarm-{attempt_id}",
            "set -euo pipefail",
            f"cd {shlex.quote(str(unit_dir))}",
            f"export SWARM_UNIT_ID={shlex.quote(u['id'])}",
            f"export SWARM_UNIT_DIR={shlex.quote(str(unit_dir))}"]
    body += [f"export {n}={shlex.quote(v)}" for n, v in deps]
    if record_enabled:
        body.append(SD._slurm_exit_trap(unit_dir))
    body += [submission_command, ""]
    for extra in (u.get("sbatch") or []):
        body.insert(1, f"#SBATCH {extra}")
    werr = U.write_json(Path(unit_dir) / "submitted.json",
                        {"script": "job.sbatch"})
    if werr:
        return None, werr
    try:
        script.write_text("\n".join(body))
    except OSError as e:
        return None, f"cannot write {script}: {e}"
    rc, out, err = U.run(["sbatch", "--parsable", str(script)],
                         cwd=str(unit_dir), timeout=120)
    if rc != 0:
        return None, f"sbatch refused it: {(err or out).strip()[:200]}"
    return out.split(";")[0].strip(), None


def _submit_pipeline(u, unit_dir, state, submission_command):
    """Launch a detached pipeline engine in a filtered child environment."""
    log = Path(unit_dir) / "engine.log"
    try:
        fh = open(log, "ab")
    except OSError as e:
        return None, f"cannot open {log}: {e}"
    try:
        wrapped = (f'(\n{submission_command}\nrc=$?\nwait\nexit $rc\n)\n'
                   f'printf %s "$?" > {U.ENGINE_RC}\n')
        proc = subprocess.Popen(
            ["sh", "-c", wrapped], cwd=str(unit_dir), env=CE.child_env({
                "SWARM_UNIT_ID": str(u["id"]),
                "SWARM_UNIT_DIR": str(unit_dir),
                **dict(SD._dep_env(u, state or {})),
            }),
            stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            start_new_session=True)
    except OSError as e:
        fh.close()
        return None, f"engine launch failed: {e}"
    fh.close()
    werr = U.write_json(Path(unit_dir) / "engine.json",
                        {"pid": proc.pid, "command": u["command"],
                         "host": os.uname().nodename,
                         "launched_at": time.time(),
                         "log": "engine.log"})
    if werr:
        return None, f"launched pid {proc.pid} but {werr}"
    return f"engine-{proc.pid}", None


def _prepare_code_worktree(u, unit_dir, state, state_dir, attempt, intent):
    """Recover an orphan agent/worktree or create and verify the attempt worktree."""
    source_repo = intent["repo"]
    rc, _out, _err = SP._git(
        source_repo, "show-ref", "--verify", "--quiet",
        f"refs/heads/{intent['branch']}")
    native = intent.get("backend") in LIO.NATIVE_BACKENDS
    if rc == 0:
        existing_agent, note = SW.reconcile_orphan(
            unit_dir, kind="code", intent=intent)
        if native and not existing_agent and note == "UNKNOWN":
            # Paseo is guarded below by its registry ownership check; a Lor
            # or direct-CLI attempt skips that, so an unanswerable lookup
            # must refuse here rather than start a second agent for one
            # attempt.
            return None, None, (
                f"attempt {attempt!r} has branch {intent['branch']!r}, but "
                f"{intent.get('backend')} could not be asked whether an agent "
                f"already exists for it; refusing to start another. Retry "
                f"when it answers")
        if existing_agent:
            recovery_error = SW._recover_code_launch(
                state if state is not None else {}, u, unit_dir,
                existing_agent)
            if recovery_error:
                return None, None, (
                    f"attempt {attempt!r} already has agent "
                    f"{existing_agent}, branch {intent['branch']!r}, and "
                    f"a worktree, but recovery refused it: "
                    f"{recovery_error}. Inspect the named agent/worktree; "
                    f"do not re-run this attempt until they agree with "
                    f"the recorded launch intent")
            if state_dir is not None:
                SS.save_state(state_dir, state)
            return str(existing_agent), None, None
        candidates = SP._git_worktrees_on_branch(source_repo, intent["branch"])
        if not candidates or len(candidates) != 1:
            return None, None, (
                f"attempt {attempt!r} has branch {intent['branch']!r} "
                f"but no registered agent and {len(candidates or [])} "
                f"matching Git worktrees. Inspect `git worktree list` "
                f"and `paseo workspace ls`; the coordinator reuses only "
                f"one unambiguous worktree that still matches its trusted "
                f"launch intent")
        reuse_workspace = candidates[0]
        # A Lor or direct-CLI attempt never touches Paseo: the lookup above
        # already established that no agent of its own backend owns it.
        ownership_problem = (None if native else
                             SW._paseo_path_ownership_problem(reuse_workspace))
        if ownership_problem:
            return None, None, (
                f"attempt {attempt!r} will not launch an agent in "
                f"pre-existing worktree {reuse_workspace!r}: "
                f"{ownership_problem}")
        reuse_error = SCL._complete_code_launch(
            state, u, unit_dir, reuse_workspace, workspace_owner="coordinator")
        if reuse_error:
            return None, None, (
                f"attempt {attempt!r} has an unowned worktree, but it "
                f"does not match the trusted launch intent: {reuse_error}")
        SS.save_state(state_dir, state)
        return None, reuse_workspace, None

    reuse_workspace, git_pointer_sha256, create_error = (
        SCL._create_code_worktree(state_dir, intent))
    if reuse_workspace:
        workspace_meta = SCL._register_code_workspace(
            state, u, unit_dir, reuse_workspace, workspace_owner="coordinator")
        SS.save_state(state_dir, state)
    if create_error:
        return None, None, create_error
    complete_error = SCL._complete_code_launch(
        state, u, unit_dir, reuse_workspace,
        workspace_owner="coordinator",
        git_pointer_sha256=git_pointer_sha256)
    if complete_error:
        workspace_meta["verification"] = "refused"
        workspace_meta["cleanup_pending"] = True
        workspace_meta["verification_problem"] = complete_error
        SS.save_state(state_dir, state)
        return None, None, (f"coordinator-created worktree could not be "
                            f"verified: {complete_error}")
    SS.save_state(state_dir, state)
    return None, reuse_workspace, None


def _prepare_codex_home(u, unit_dir, state, state_dir, attempt, auth):
    """Allocate, persist, and populate a coordinator-owned per-attempt CODEX_HOME."""
    us = SS._unit_state(state, u["id"])
    had_homes = "attempt_codex_homes" in us
    previous_homes = us.get("attempt_codex_homes")
    homes = dict(previous_homes) if had_homes else {}
    try:
        home, home_identity = CH.allocate(state_dir, attempt, unit_dir, u)
        try:
            home_meta = {"path": str(home), "removed": False,
                         "identity": home_identity}
            homes[attempt] = home_meta
            us["attempt_codex_homes"] = homes
            SS.save_state(state_dir, state)
        except BaseException:
            try:
                CH.remove(state_dir, attempt, str(home), home_identity)
            finally:
                if had_homes:
                    us["attempt_codex_homes"] = previous_homes
                else:
                    us.pop("attempt_codex_homes", None)
            raise
        try:
            CH.populate(home, auth)
        except CH.HomeError:
            home_meta["unlaunched"] = True
            SS.save_state(state_dir, state)
            raise
    except (CH.HomeError, CP.PathPolicyError) as exc:
        return None, str(exc)
    return str(home), None


def _finalize_code_launch(u, unit_dir, state, state_dir, attempt,
                          reuse_workspace, rc, out, err):
    """Parse Paseo run output, register workspace ownership, and verify identity."""
    if rc != 0:
        workspace_meta = SCL._register_code_workspace(
            state, u, unit_dir, reuse_workspace, workspace_owner="coordinator")
        workspace_meta["verification"] = "refused"
        workspace_meta["cleanup_pending"] = True
        workspace_meta["verification_problem"] = "paseo run failed"
        SS.save_state(state_dir, state)
        return None, f"paseo run failed: {SW._paseo_error(out, err)}"
    rec = SW._paseo_json(out) or {}
    agent = (rec.get("agentId") or rec.get("AgentId")
             or rec.get("id") or rec.get("Id"))
    if agent:
        SS._unit_state(state, u["id"]).setdefault(
            "attempt_cleanup_agents", {})[attempt] = str(agent)
    reported_workspace = rec.get("cwd") or rec.get("Cwd")
    workspace = reuse_workspace
    workspace_id = (SCL._paseo_workspace_id(out)
                    or rec.get("workspaceId") or rec.get("WorkspaceId"))
    workspace_meta = SCL._register_code_workspace(
        state if state is not None else {}, u, unit_dir, workspace,
        workspace_id=workspace_id, workspace_owner="coordinator")
    if state_dir is not None:
        SS.save_state(state_dir, state)
    if not agent:
        workspace_meta["verification"] = "refused"
        workspace_meta["cleanup_pending"] = True
        workspace_meta["verification_problem"] = "Paseo returned no agent id"
        if state_dir is not None:
            SS.save_state(state_dir, state)
        return None, (f"paseo run returned no agent id: "
                      f"{SW._paseo_error(out, err)}")
    if reuse_workspace:
        facts = SS.trusted_launch_facts(state, u["id"], unit_dir)
        identity_error = W.workspace_identity_problem(U.run, facts)
        if (reported_workspace is not None
                and str(Path(reported_workspace).resolve()) != reuse_workspace):
            identity_error = (f"Paseo attached the recovered agent to "
                              f"{reported_workspace!r}, not authenticated worktree "
                              f"{reuse_workspace!r}")
        if identity_error:
            workspace_meta["verification"] = "refused"
            workspace_meta["cleanup_pending"] = True
            workspace_meta["verification_problem"] = identity_error
            SS.save_state(state_dir, state)
            return None, (f"paseo run returned agent {agent}, but reused "
                          f"worktree verification failed: {identity_error}")
    complete_err = SCL._complete_code_launch(
        state if state is not None else {}, u, unit_dir, workspace,
        workspace_id=workspace_id, recovery=True,
        workspace_owner="coordinator")
    if complete_err:
        workspace_meta["verification"] = "refused"
        workspace_meta["cleanup_pending"] = True
        workspace_meta["verification_problem"] = complete_err
        if state_dir is not None:
            SS.save_state(state_dir, state)
        return None, (f"paseo run returned agent {agent}, but its "
                      f"worktree could not be recorded: {complete_err}")
    if state_dir is not None:
        SS.save_state(state_dir, state)
    return str(agent), None


def _write_attempt_context(unit_dir, pairs):
    """Write the allowlisted attempt context, owner-only, beside the attempt.

    It lives in the exclusive attempt directory, never in the git worktree,
    so the worker's `git status` stays clean. Audit and convenience only.
    """
    path = Path(unit_dir) / SCP.ATTEMPT_CONTEXT_FILE
    tmp = path.with_suffix(path.suffix + ".tmp")
    record = {"schema_version": 1,
              "values": {name: value for name, value in pairs},
              "order": [name for name, _value in pairs]}
    try:
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            json.dump(record, handle, indent=1, sort_keys=True)
            handle.write("\n")
        os.replace(str(tmp), str(path))
    except OSError as exc:
        return f"cannot write attempt context {path}: {exc}"
    return None


def _secret_in(text, environ=None):
    """Name of a coordinator credential whose VALUE appears in ``text``."""
    source = os.environ if environ is None else environ
    for name, value in source.items():
        if not value or len(value) < 12:
            continue
        sensitive = (name in CE.DENIED_ENV_NAMES
                     or name.endswith(("_TOKEN", "_API_KEY", "_SECRET",
                                       "_SECRET_ACCESS_KEY", "_PROOF",
                                       "_PASSWORD")))
        if sensitive and value in text:
            return name
    return None


def _refuse_lor_workspace(state, state_dir, u, unit_dir, workspace, problem):
    meta = SCL._register_code_workspace(
        state, u, unit_dir, workspace, workspace_owner="coordinator")
    meta["verification"] = "refused"
    meta["cleanup_pending"] = True
    meta["verification_problem"] = problem
    SS.save_state(state_dir, state)


def _launch_lor_agent(u, unit_dir, state, state_dir, attempt, intent,
                      workspace, plan_path=None):
    """Start a native Lor coding subagent in the verified attempt worktree.

    No CODEX_HOME, no Paseo workspace and no detached watcher holding an
    expiring token: Lor's parent-session wake and `swarm.py advance` observe
    it. Context reaches the agent only through an allowlisted prompt block
    and an owner-only context.json outside the worktree.
    """
    # `lor session start` parents the subagent to the ambient LOR_SESSION_ID,
    # while every later poll, bind and lookup uses the frozen intent's parent.
    # Starting under another parent would orphan the subagent from its own
    # attempt, so a mismatch refuses before any Lor side effect.
    recorded = str(intent.get("parent_session_id") or "").strip()
    ambient = str(os.environ.get("LOR_SESSION_ID") or "").strip()
    if ambient != recorded:
        return None, (
            f"unit {u['id']!r} attempt {attempt!r}: the recorded Lor launch "
            f"intent belongs to a different parent session ({recorded!r}) "
            f"than this process's LOR_SESSION_ID ({ambient!r}); refusing to "
            f"start a subagent the recorded parent could never observe. "
            f"Re-dispatch from parent session {recorded!r}, or allocate a "
            f"fresh attempt from this session")
    try:
        unit_real = str(Path(unit_dir).resolve())
        work_real = str(Path(workspace).resolve())
        if os.path.commonpath((unit_real, work_real)) in (unit_real, work_real):
            return None, (f"attempt directory {unit_real} and worktree "
                          f"{work_real} overlap; refusing to write attempt "
                          f"context where git would see it")
    except ValueError:
        pass
    try:
        pairs = SCP.attempt_context_values(
            u["id"], unit_dir, SD._dep_env(u, state or {}))
    except PlanError as exc:
        return None, str(exc)
    prompt_intent = dict(intent)
    if plan_path is not None:
        prompt_intent.update(scope_plan=str(Path(plan_path).resolve()),
                             scope_state_dir=str(Path(state_dir).resolve()))
    prompt = SCP._dispatch_prompt(
        u, prompt_intent, attempt_context=SCP.render_attempt_context(pairs))
    protocol_problem = SCP._code_protocol_problem(prompt, prompt_intent)
    if protocol_problem:
        return None, (f"unit {u['id']!r} code prompt has no valid completion "
                      f"protocol ({protocol_problem}); refusing an unclosable "
                      f"agent launch")
    leaked = _secret_in(prompt) or _secret_in(json.dumps(pairs))
    if leaked:
        return None, (f"unit {u['id']!r}: the rendered Lor task would contain "
                      f"the value of {leaked}; refusing to launch")
    context_err = _write_attempt_context(unit_dir, pairs)
    if context_err:
        return None, context_err
    canonical = intent.get("canonical_model_id")
    parent = intent.get("parent_session_id")
    session_id, start_err = LIO.start_session(
        canonical_model_id=canonical, working_directory=str(workspace),
        title=f"[swarm] {u['id']} {attempt}", task=prompt,
        effort=intent.get("effort"))
    if start_err:
        _refuse_lor_workspace(state, state_dir, u, unit_dir, workspace,
                              "lor session start failed")
        return None, start_err
    us = SS._unit_state(state, u["id"])
    us.setdefault("attempt_cleanup_agents", {})[attempt] = session_id
    # Lor tracks this attempt natively: the parent session is woken when the
    # subagent finishes, and `swarm.py advance` (or `wait_event.py
    # --poll-lor`) judges it. Recording this BEFORE any watcher logic runs
    # means no detached process holding a short-lived LOR_SESSION_TOKEN is
    # ever spawned for a Lor attempt.
    us.setdefault("code_terminal_watches", {})[attempt] = {
        "agent_id": session_id, "backend": LIO.LOR_BACKEND,
        "parent_session_id": parent, "host": os.uname().nodename,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "status": "lor_native_parent_wake",
        "reason": ("no detached watcher: Lor wakes the parent session on "
                   "finish; run `swarm.py advance` or `wait_event.py "
                   "--poll-lor` to judge")}
    SCL._register_code_workspace(state, u, unit_dir, workspace,
                                 workspace_owner="coordinator")
    SS.save_state(state_dir, state)
    facts = SS.trusted_launch_facts(state, u["id"], unit_dir)
    identity_error = W.workspace_identity_problem(U.run, facts)
    if not identity_error:
        obs = LIO.poll_session(session_id, parent_session_id=parent)
        reported = obs.get("working_directory")
        if reported and not str(reported).startswith("~"):
            try:
                if str(Path(reported).resolve()) != str(Path(workspace).resolve()):
                    identity_error = (f"Lor started session {session_id} in "
                                      f"{reported!r}, not authenticated "
                                      f"worktree {workspace!r}")
            except OSError as exc:
                identity_error = f"cannot resolve Lor working directory: {exc}"
    if identity_error:
        LIO.stop_session(session_id)
        _refuse_lor_workspace(state, state_dir, u, unit_dir, workspace,
                              identity_error)
        return None, (f"lor session {session_id} was started, but worktree "
                      f"verification failed and it was stopped: "
                      f"{identity_error}")
    complete_err = SCL._complete_code_launch(
        state, u, unit_dir, workspace, recovery=True,
        workspace_owner="coordinator")
    if complete_err:
        _refuse_lor_workspace(state, state_dir, u, unit_dir, workspace,
                              complete_err)
        return None, (f"lor session {session_id} was started, but its "
                      f"worktree could not be recorded: {complete_err}")
    SS.save_state(state_dir, state)
    return session_id, None


def _launch_cli_agent(u, unit_dir, state, state_dir, attempt, intent,
                      workspace, plan_path=None):
    """Start a direct-CLI (claude/codex/pi) worker in the verified worktree.

    The run id bound to the unit is the attempt id; its run directory is
    STATE_DIR/cli-runs/<attempt>, frozen in the intent. Context reaches the
    worker only through the allowlisted prompt block and an owner-only
    context.json outside the worktree, exactly as for a Lor subagent.
    """
    backend = intent.get("backend")
    runs_root = intent.get("cli_runs_root")
    if not runs_root or Path(runs_root).parent.resolve() != Path(state_dir).resolve():
        return None, (f"unit {u['id']!r} attempt {attempt!r}: the frozen "
                      f"direct-CLI run root {runs_root!r} is not under this "
                      f"state directory; refusing to start a run the "
                      f"coordinator could not observe")
    # The placement frozen at dispatch is binding in both directions: an
    # attempt frozen onto Slurm never falls back to running the CLI on
    # whatever host retries it, and one frozen local is never moved onto
    # Slurm (its node-local worktree may be unreachable) or run on a host
    # whose own placement is Slurm (a login or agent host).
    environ = dict(os.environ)
    frozen = intent.get("cli_placement")
    if frozen == CIO.SLURM:
        environ[CIO.MODE_ENV] = "on"
    elif frozen == CIO.LOCAL:
        here, problem = CIO.placement(environ)
        if problem:
            # Placement refused on this host (as cli_io.resume refuses): a
            # host that cannot say where the CLI belongs never runs it.
            _refuse_lor_workspace(state, state_dir, u, unit_dir, workspace,
                                  f"{backend} placement refused")
            return None, problem
        if here == CIO.SLURM:
            _refuse_lor_workspace(state, state_dir, u, unit_dir, workspace,
                                  f"{backend} placement refused")
            return None, (
                f"unit {u['id']!r} attempt {attempt!r} was frozen to local "
                f"{backend} placement at dispatch, but this host places "
                f"coding CLIs on Slurm (a Slurm login or agent host, or "
                f"{CIO.MODE_ENV}=on). Refusing both to run the CLI on this "
                f"host and to submit a node-local worktree to Slurm. Retry "
                f"from the dispatching host, or set {CIO.MODE_ENV}=off to "
                f"run it here deliberately")
    try:
        unit_real = str(Path(unit_dir).resolve())
        work_real = str(Path(workspace).resolve())
        if os.path.commonpath((unit_real, work_real)) in (unit_real, work_real):
            return None, (f"attempt directory {unit_real} and worktree "
                          f"{work_real} overlap; refusing to write attempt "
                          f"context where git would see it")
    except ValueError:
        pass
    try:
        pairs = SCP.attempt_context_values(
            u["id"], unit_dir, SD._dep_env(u, state or {}))
    except PlanError as exc:
        return None, str(exc)
    prompt_intent = dict(intent)
    if plan_path is not None:
        prompt_intent.update(scope_plan=str(Path(plan_path).resolve()),
                             scope_state_dir=str(Path(state_dir).resolve()))
    prompt = SCP._dispatch_prompt(
        u, prompt_intent, attempt_context=SCP.render_attempt_context(pairs))
    protocol_problem = SCP._code_protocol_problem(prompt, prompt_intent)
    if protocol_problem:
        return None, (f"unit {u['id']!r} code prompt has no valid completion "
                      f"protocol ({protocol_problem}); refusing an unclosable "
                      f"agent launch")
    leaked = _secret_in(prompt) or _secret_in(json.dumps(pairs))
    if leaked:
        return None, (f"unit {u['id']!r}: the rendered {backend} prompt would "
                      f"contain the value of {leaked}; refusing to launch")
    context_err = _write_attempt_context(unit_dir, pairs)
    if context_err:
        return None, context_err
    run, start_err = CIO.launch(
        backend, intent.get("cli_model"), intent.get("effort"), str(workspace),
        prompt, state_dir, attempt, env=environ)
    if start_err:
        _refuse_lor_workspace(state, state_dir, u, unit_dir, workspace,
                              f"{backend} launch failed")
        return None, start_err
    us = SS._unit_state(state, u["id"])
    us.setdefault("attempt_cleanup_agents", {})[attempt] = attempt
    us.setdefault("code_terminal_watches", {})[attempt] = {
        "agent_id": attempt, "backend": backend, "host": os.uname().nodename,
        "cli_mode": run["mode"], "slurm_job_id": run.get("slurm_job_id"),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "status": "cli_turn_receipt",
        "reason": ("no watcher process: the turn wrapper writes an atomic "
                   "exit receipt; run `swarm.py advance` or `wait_event.py "
                   "--poll-cli` to judge")}
    SCL._register_code_workspace(state, u, unit_dir, workspace,
                                 workspace_owner="coordinator")
    SS.save_state(state_dir, state)
    facts = SS.trusted_launch_facts(state, u["id"], unit_dir)
    identity_error = W.workspace_identity_problem(U.run, facts)
    if not identity_error:
        try:
            if str(Path(run["worktree"]).resolve()) != str(Path(workspace).resolve()):
                identity_error = (f"{backend} run {attempt} works in "
                                  f"{run['worktree']!r}, not authenticated "
                                  f"worktree {workspace!r}")
        except OSError as exc:
            identity_error = f"cannot resolve the {backend} run worktree: {exc}"
    if identity_error:
        CIO.cancel(state_dir, attempt)
        _refuse_lor_workspace(state, state_dir, u, unit_dir, workspace,
                              identity_error)
        return None, (f"{backend} run {attempt} was started, but worktree "
                      f"verification failed and it was cancelled: "
                      f"{identity_error}")
    complete_err = SCL._complete_code_launch(
        state, u, unit_dir, workspace, recovery=True,
        workspace_owner="coordinator")
    if complete_err:
        _refuse_lor_workspace(state, state_dir, u, unit_dir, workspace,
                              complete_err)
        return None, (f"{backend} run {attempt} was started, but its "
                      f"worktree could not be recorded: {complete_err}")
    SS.save_state(state_dir, state)
    return attempt, None
