"""Unit submission phases: intent recheck, persistence, isolation, slurm, pipeline, code.

Python 3.8+, standard library only; dependencies never point to the facade.
"""

import os
import shlex
import subprocess
import time
from pathlib import Path

import child_environment as CE
import codex_home as CH
import coordinator_paths as CP
import unit as U
import worktree as W
import swarm_code_launch as SCL
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
    if rc == 0:
        existing_agent, _note = SW.reconcile_orphan(unit_dir, kind="code")
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
        ownership_problem = SW._paseo_path_ownership_problem(reuse_workspace)
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
