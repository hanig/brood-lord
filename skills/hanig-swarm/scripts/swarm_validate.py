"""Plan validation phases: schema, fields, outputs, runtimes, inputs, DAG and checks.

Python 3.8+, standard library only; dependencies never point to the facade.
"""

import glob
import os
import re
import shutil

import unit as U
import swarm_authors as SA
import swarm_code_protocol as SCP
import swarm_outbox as SO
import swarm_plan_rules as SPR
import swarm_preflight as SP
import swarm_routing as SR
from swarm_types import PlanError

KINDS = U.KINDS
DEFAULT_MAX_ATTEMPTS = 1
CHARGE_METRICS = ("gpu_hours", "cpu_hours", "read_bytes", "wall_seconds",
                  "dollars")
RETRY_MODES = ("restart", "resume")
FINDINGS_FILE = "findings.json"
QOS_CAVEAT = (
    "Note that `qos_grptres` in the survey resolves the PARTITION QOS only: "
    "an account or association QOS can impose a GrpTRES nothing here can "
    "see, so a partition that accepts this account is still not a promise "
    "the job will run.")
_AS_FIELDS = ("provider", "mode", "model", "thinking", "env", "cwd",
              "title", "background", "json")


def _validate_unit_fields(units):
    """Validate per-unit declared metadata, list fields and workspace policy."""
    # `sbatch` is read as [str(a) for a in (u.get("sbatch") or [])]. Hand it
    # the string "--partition=cpu_batch" and Python iterates it CHARACTER by
    # character, so declared_partition() finds nothing, the unit silently
    # lands on the cluster default, and validate prints "declares no
    # partition" about a unit that plainly declares one. Refuse strings loudly
    # for all iterated list fields.
    for u in units:
        if not isinstance(u, dict):
            continue
        SPR.declared_seed(u)
        SPR.declared_scope(u)
        SA.declared_author_history(u)
        if "tracker" in u and not SO._nonblank_text(u["tracker"]):
            raise PlanError(
                f"unit {u.get('id', '?')!r} has tracker={u['tracker']!r}; "
                "tracker must be a non-empty issue string")
        deadline = u.get("deadline_s")
        try:
            finite_deadline = (float(deadline) if deadline is not None
                               and not isinstance(deadline, bool)
                               and isinstance(deadline, (int, float))
                               else None)
        except (OverflowError, ValueError):
            finite_deadline = None
        if (deadline is not None
                and (finite_deadline is None
                     or finite_deadline != finite_deadline
                     or finite_deadline in (float("inf"), float("-inf"))
                     or finite_deadline <= 0)):
            raise PlanError(
                f"unit {u.get('id','?')!r} has deadline_s={deadline!r}; "
                f"it must be a positive, finite number of seconds measured "
                f"from the coordinator-written allocated_at timestamp.")
        for field in ("needs", "inputs", "outputs", "sbatch",
                      "requires_verification"):
            val = u.get(field)
            if val is None or isinstance(val, list):
                continue
            raise PlanError(
                f"unit {u.get('id','?')!r} has {field}={val!r}, a "
                f"{type(val).__name__}, but {field} must be a list. A string "
                f"here is not rejected by the code that reads it: it is "
                f"iterated one character at a time, so the value is silently "
                f"lost and the unit runs with a default instead. Write "
                f'["--partition=cpu_batch"], not "--partition=cpu_batch".')

        policy = u.get("workspace_policy")
        if policy is not None and not isinstance(policy, dict):
            raise PlanError(
                f"unit {u.get('id','?')!r} has workspace_policy={policy!r}; "
                f"it must be an object")
        if policy and (policy.get("requires_clean_git")
                       or policy.get("clean_git")):
            workspace = (u.get("execution_workspace") or policy.get("path")
                         or u.get("repo"))
            if not workspace:
                raise PlanError(
                    f"unit {u.get('id','?')!r} requires a clean Git workspace "
                    f"but declares no workspace_policy.path, "
                    f"execution_workspace, or repo")


def _validate_canary(plan, units):
    """Validate optional plan-level canary and return effective unit list."""
    if "canary" not in plan:
        return units
    canary = plan["canary"]
    if not isinstance(canary, str):
        raise PlanError("plan 'canary' must be a unit id string")
    probe = next((u for u in units if isinstance(u, dict)
                  and u.get("id") == canary), None)
    if probe is None:
        raise PlanError(f"plan 'canary' {canary!r} names no unit")
    if probe.get("needs"):
        raise PlanError(f"plan 'canary' {canary!r} must be a root "
                        "with no 'needs'")
    return SPR._units_with_canary(plan)


def _validate_convergence_paseo_promote(units, which=None):
    """Validate convergence specs, local paseo availability, and promote_to."""
    for u in units:
        if not isinstance(u, dict):
            continue
        problem = SPR.converge_problem(u)
        if problem:
            raise PlanError(problem)

    resolve_bin = shutil.which if which is None else which
    if any(isinstance(u, dict) and u.get("kind") == "code" for u in units):
        if not resolve_bin("paseo"):
            ids = ", ".join(sorted(u.get("id", "?") for u in units
                                   if isinstance(u, dict)
                                   and u.get("kind") == "code"))
            raise PlanError(
                f"unit(s) {ids} are kind=code, which runs a coding agent "
                f"through paseo, and paseo is not on PATH on "
                f"{os.uname().nodename}. Either run this plan from a machine "
                f"where you run agents, or declare the work as kind=pipeline "
                f"or kind=slurm and invoke the tool directly. Installing "
                f"paseo on a shared login node is usually the wrong answer: "
                f"it would run the agent processes there.")

    for u in units:
        if isinstance(u, dict) and u.get("promote_to"):
            _, derr = SPR.resolve_promote_to(u.get("promote_to"))
            if derr:
                raise PlanError(
                    f"unit {u.get('id', '?')!r} declares promote_to "
                    f"{u['promote_to']!r}, but {derr}")


def _validate_retry_exposure(plan, units, charge_metrics=None,
                             default_max_attempts=None, retry_modes=None):
    """Validate declared retry_limits, max_attempts and retry.max_lost."""
    metrics = CHARGE_METRICS if charge_metrics is None else charge_metrics
    default_attempts = (DEFAULT_MAX_ATTEMPTS if default_max_attempts is None
                        else default_max_attempts)
    modes = RETRY_MODES if retry_modes is None else retry_modes

    limits = (plan.get("retry_limits") or {})
    for k, v in limits.items():
        if isinstance(v, bool) or not isinstance(v, (int, float)) \
                or v != v or v in (float("inf"), float("-inf")) or v < 0:
            raise PlanError(f"retry_limits[{k!r}]={v!r}; a limit must be a "
                            f"non-negative, finite number.")
        if k not in metrics:
            raise PlanError(
                f"retry_limits names {k!r}, which is not a known metric. "
                f"Use one of: {', '.join(metrics)}.")
    for u in units:
        if not isinstance(u, dict):
            continue
        uid = u.get("id", "?")
        attempts = u.get("max_attempts", default_attempts)
        if isinstance(attempts, bool) or not isinstance(attempts, (int, float)) \
                or attempts != attempts or attempts in (float("inf"),
                                                        float("-inf")) \
                or attempts != int(attempts) or int(attempts) < 0:
            raise PlanError(f"unit {uid!r} has max_attempts={attempts!r}; it "
                            f"must be a whole number of at least 0.")
        attempts = max(1, int(attempts))     # 0 and 1 both mean "run once"
        retry = u.get("retry")
        if attempts == 1:
            continue
        if not isinstance(retry, dict):
            raise PlanError(
                f"unit {uid!r} asks for {attempts} attempts but declares no "
                f"'retry' contract. A retry starts in a FRESH EMPTY attempt "
                f"directory, so it redoes the whole unit unless a tested "
                f"resume path says otherwise. Declare what an interruption "
                f"costs:\n"
                f'      "retry": {{"mode": "restart", '
                f'"max_lost": {{"read_bytes": <n>}}}}\n'
                f"    or set max_attempts to 1.")
        mode = retry.get("mode")
        if mode not in modes:
            raise PlanError(f"unit {uid!r} has retry.mode={mode!r}; use "
                            f"{' or '.join(repr(m) for m in modes)}.")
        if mode == "resume":
            raise PlanError(
                f"unit {uid!r} declares retry.mode='resume', which is NOT "
                f"SUPPORTED yet. Cross-attempt handoff is not built and has "
                f"not passed a forced-preemption test, and shipping the claim "
                f"before the mechanism is how a false pass gets made. Use "
                f"'restart' with a max_lost you can afford, or split the unit.")
        lost = retry.get("max_lost")
        if not isinstance(lost, dict) or not lost:
            raise PlanError(
                f"unit {uid!r} declares retry.mode='restart' without "
                f"'max_lost'. State what ONE interruption costs, in a metric "
                f"the project also limits: {', '.join(metrics)}.")
        for metric, value in lost.items():
            if metric not in metrics:
                raise PlanError(f"unit {uid!r} max_lost names {metric!r}, "
                                f"which is not a known metric. Use one of: "
                                f"{', '.join(metrics)}.")
            if isinstance(value, bool) or not isinstance(value, (int, float)) \
                    or value != value \
                    or value in (float("inf"), float("-inf")) or value < 0:
                raise PlanError(f"unit {uid!r} max_lost[{metric!r}]="
                                f"{value!r}; it must be a non-negative number.")
            cap = limits.get(metric)
            if cap is None:
                raise PlanError(
                    f"unit {uid!r} declares it can lose {value} {metric} per "
                    f"interruption, but the plan sets no retry_limits for "
                    f"{metric!r}, so nothing says whether that is acceptable. "
                    f"Add: \"retry_limits\": {{\"{metric}\": <n>}}")
            if value > cap:
                raise PlanError(
                    f"unit {uid!r} can lose {value} {metric} per interruption, "
                    f"over the project limit of {cap}. Split it into smaller "
                    f"units, or raise the limit deliberately.")


def _validate_limits_and_pools(plan, units):
    """Validate plan-level concurrency limits and declared unit pools."""
    lim = plan.get("limits") or {}
    if not isinstance(lim, dict):
        raise PlanError("'limits' must be an object")
    mr = lim.get("max_running")
    if mr is not None and (not isinstance(mr, int) or isinstance(mr, bool)
                           or mr < 1):
        raise PlanError(f"limits.max_running={mr!r}; it must be an integer of "
                        f"at least 1.")
    pools = lim.get("pools") or {}
    if not isinstance(pools, dict):
        raise PlanError("limits.pools must be an object of name -> integer")
    for name, cap in pools.items():
        if not isinstance(cap, int) or isinstance(cap, bool) or cap < 1:
            raise PlanError(f"limits.pools[{name!r}]={cap!r}; it must be an "
                            f"integer of at least 1.")
    for u in units:
        pool = isinstance(u, dict) and u.get("pool")
        if pool and pool not in pools:
            raise PlanError(
                f"unit {u.get('id','?')!r} joins pool {pool!r}, which is not "
                f"declared in limits.pools. A pool with no cap bounds nothing.")


def _validate_code_contracts(units):
    """Validate provider_resume, continuation and requires_verification rules."""
    for u in units:
        if not isinstance(u, dict):
            continue
        if "provider_resume" in u:
            resume = u["provider_resume"]
            mx = resume.get("max") if isinstance(resume, dict) else None
            if type(mx) is not int or not 0 <= mx <= 3:
                raise PlanError(f"unit {u.get('id', '?')!r}: provider_resume "
                                "must be an object with max an integer from 0 to 3")
            if u.get("kind") != "code":
                raise PlanError(f"unit {u.get('id', '?')!r}: provider_resume "
                                "is only supported for code units")
        cont = u.get("continuation")
        if cont is not None:
            if not isinstance(cont, dict):
                raise PlanError(
                    f"unit {u.get('id','?')!r} has continuation={cont!r}; it "
                    f"must be an object with a 'max'.")
            mx = cont.get("max")
            if not isinstance(mx, int) or isinstance(mx, bool) or mx < 1:
                raise PlanError(
                    f"unit {u.get('id','?')!r} has continuation.max={mx!r}; "
                    f"it must be a whole number of at least 1. A bound that "
                    f"is not a number is not a bound.")
            if u.get("kind") != "code":
                raise PlanError(
                    f"unit {u.get('id','?')!r} is kind={u.get('kind')!r} and "
                    f"declares 'continuation'. A continuation resumes a "
                    f"conversational turn, which only a code agent has. A "
                    f"Slurm job that exits is retried, not prodded.")
        for claim in (u.get("requires_verification") or []):
            if not str(claim).strip():
                raise PlanError(
                    f"unit {u.get('id','?')!r} declares an empty entry in "
                    f"'requires_verification'. Name the claim a verifier must "
                    f"establish, e.g. \"tests-pass\".")
        if u.get("requires_verification") and u.get("kind") != "code":
            raise PlanError(
                f"unit {u.get('id','?')!r} is kind={u.get('kind')!r} and "
                f"declares 'requires_verification'. Verification binds to a "
                f"produced commit, which only a code unit has. A slurm or "
                f"pipeline unit is judged by its declared outputs.")


def _validate_ids_and_dag(units, kinds=None):
    """Validate unit identity, required kind/outputs/command, DAG cycles and write scopes."""
    valid_kinds = KINDS if kinds is None else kinds
    seen, by_id = set(), {}
    for i, u in enumerate(units):
        if not isinstance(u, dict):
            raise PlanError(f"unit {i} is not an object")
        uid = u.get("id")
        if not isinstance(uid, str) or not uid.strip():
            raise PlanError(f"unit {i} has no 'id'; every unit needs a stable id")
        if uid in seen:
            raise PlanError(f"duplicate unit id {uid!r}; ids must be unique "
                            f"because state is keyed on them")
        seen.add(uid)
        by_id[uid] = u
        if u.get("kind") not in valid_kinds:
            raise PlanError(f"unit {uid!r} has kind {u.get('kind')!r}; use one "
                            f"of {', '.join(valid_kinds)}")
        if not u.get("outputs"):
            raise PlanError(f"unit {uid!r} declares no outputs, so it can never "
                            f"be judged done. Add 'outputs'.")
        if u["kind"] != "code" and not u.get("command"):
            raise PlanError(f"unit {uid!r} is kind {u['kind']} with no "
                            f"'command' to submit")

    # Dependencies must exist and must not cycle.
    for uid, u in by_id.items():
        for dep in (u.get("needs") or []):
            if dep not in by_id:
                raise PlanError(f"unit {uid!r} needs {dep!r}, which is not in "
                                f"the plan")
            if dep == uid:
                raise PlanError(f"unit {uid!r} depends on itself")
    state = {}

    def visit(uid, path):
        if state.get(uid) == "done":
            return
        if state.get(uid) == "open":
            cut = path.index(uid)
            raise PlanError("unit dependency cycle: "
                            + " -> ".join([*path[cut:], uid]))
        state[uid] = "open"
        for dep in (by_id[uid].get("needs") or []):
            visit(dep, [*path, uid])
        state[uid] = "done"

    for uid in by_id:
        visit(uid, [])

    # No two concurrent units may write overlapping scopes.
    def ordered(a, b):
        seen_ = set()

        def reaches(x, target):
            if x == target:
                return True
            if x in seen_:
                return False
            seen_.add(x)
            return any(reaches(d, target)
                       for d in (by_id[x].get("needs") or []))
        return reaches(a, b) or reaches(b, a)

    ids = sorted(by_id)
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            if ordered(a, b):
                continue
            for sa in (by_id[a].get("write_scopes") or []):
                for sb in (by_id[b].get("write_scopes") or []):
                    if SPR._scopes_overlap(sa, sb):
                        raise PlanError(
                            f"units {a!r} and {b!r} can run concurrently and "
                            f"their write scopes overlap ({sa} / {sb}). Two "
                            f"units that can write the same place cannot both "
                            f"have an exclusive write root, which is what makes "
                            f"the done-predicate conclusive. Give them disjoint "
                            f"scopes, or order them with 'needs'.")
    return by_id


def _validate_outputs_and_findings(units, new_attempts=True, findings_file=None):
    """Validate relative output paths, slurm output rules, and findings.json promotion."""
    findings_name = FINDINGS_FILE if findings_file is None else findings_file
    for u in units:
        if not isinstance(u, dict):
            continue
        for out in (u.get("outputs") or []):
            text = str(out).strip()
            if not text:
                raise PlanError(
                    f"unit {u.get('id','?')!r} declares an empty output. Name "
                    f"the artifact, relative to the attempt's write root.")
            if os.path.isabs(text):
                raise PlanError(
                    f"unit {u.get('id','?')!r} declares the output {text!r} as "
                    f"an ABSOLUTE path. Declared outputs are looked for inside "
                    f"the attempt's exclusive write root and nowhere else, so "
                    f"this one can never be found and the unit can never "
                    f"close, however well the work goes. Declare it relative "
                    f"to the write root; use $SWARM_UNIT_DIR in the command if "
                    f"the tool needs an absolute path. To publish somewhere "
                    f"shared, declare 'promote_to' instead.")
            if text.startswith("..") or "/../" in text:
                raise PlanError(
                    f"unit {u.get('id','?')!r} declares the output {text!r}, "
                    f"which climbs out of the attempt's write root. The write "
                    f"root is exclusive so that finding an artifact there is "
                    f"conclusive; an output above it is neither exclusive nor "
                    f"findable.")
        problem = (U.slurm_output_problem(u.get("outputs") or [])
                   if new_attempts and u.get("kind") == "slurm" else None)
        if problem:
            raise PlanError(f"unit {u.get('id', '?')!r}: {problem}")

    for u in units:
        if not isinstance(u, dict):
            continue
        for out in (u.get("outputs") or []):
            if os.path.basename(str(out).strip()) != findings_name:
                continue
            if u.get("promote_to"):
                break
            raise PlanError(
                f"unit {u.get('id','?')!r} declares the output {str(out)!r} "
                f"and no 'promote_to'. Declared outputs stay inside the "
                f"attempt's exclusive write root, and the only reader of "
                f"{findings_name} -- report.py's \"Findings reported by this "
                f"project\" section -- reads it from the PROJECT directory. "
                f"Written there and never promoted, it is an artifact nobody "
                f"sees, which is indistinguishable from not writing it. "
                f"Declare \"promote_to\": \"/abs/path/to/the/project\", or "
                f"drop the output if these findings are not being published.")


def _validate_runtimes_and_isolation(plan, units):
    """Validate declared execution runtimes and optional OS-backed isolation."""
    SPR._validate_runtimes(plan, units)
    for u in units:
        if not isinstance(u, dict):
            continue
        problem = SPR.isolation_problem(u)
        if problem:
            raise PlanError(problem)


def _validate_inputs_and_scheduler(units, survey=None, qos_caveat=None):
    """Validate declared inputs, upstream ordering, partitions, memory and accounts."""
    caveat = QOS_CAVEAT if qos_caveat is None else qos_caveat
    by_id = {u.get("id"): u for u in units if isinstance(u, dict)}
    produced_by = {}
    for u in units:
        if not isinstance(u, dict):
            continue
        for out in (u.get("outputs") or []):
            produced_by.setdefault(out, set()).add(u.get("id"))
    unresolved = []
    for u in units:
        if not isinstance(u, dict):
            continue
        for raw in (u.get("inputs") or []):
            text = str(raw).strip()
            if not text:
                unresolved.append((u.get("id", "?"), "<empty>"))
                continue
            if SPR._looks_unresolved(text):
                unresolved.append((u.get("id", "?"), text))
                continue
            makers = produced_by.get(text)
            if makers:
                uid_here = u.get("id")
                if makers & SPR._ancestors(uid_here, by_id):
                    continue                # an UPSTREAM unit makes it
                named = ", ".join(sorted(m for m in makers if m))
                unresolved.append((
                    uid_here,
                    f"{text} (produced by {named}, which is not upstream of "
                    f"it, so nothing orders them)"))
                continue
            if any(ch in text for ch in "*?["):
                if os.path.isabs(text) and not glob.glob(text):
                    unresolved.append((u.get("id", "?"),
                                       f"{text} (matches nothing)"))
            elif os.path.isabs(text) and not os.path.exists(text):
                unresolved.append((u.get("id", "?"), f"{text} (does not exist)"))
    if unresolved:
        listed = "; ".join(f"{uid} needs {what}" for uid, what in unresolved)
        raise PlanError(
            f"this plan cannot run: {listed}.\n"
            f"    An input that is missing, empty or still a placeholder is "
            f"not a detail to settle later: the plan will be built, issues "
            f"will be filed for it, and it will then sit waiting for a value "
            f"nobody was asked for. Settle it before dispatching.")

    bad_parts = SPR.partition_problems(units)
    if bad_parts:
        known = sorted(SPR._known_partitions() or [])
        listed = "; ".join(f"{uid} wants {name!r}" for uid, name in bad_parts)
        raise PlanError(
            f"this plan names partition(s) that {os.uname().nodename} does "
            f"not have: {listed}. This cluster offers: {', '.join(known)}. A "
            f"project runs on ONE server and its plan carries that server's "
            f"sbatch flags, so this is usually a plan written for a different "
            f"cluster. Refusing here beats a half-dispatched DAG and an "
            f"sbatch error per unit.")

    starved = SPR.memory_flag_problems(units, survey)
    if starved:
        raise PlanError(
            f"unit(s) {', '.join(sorted(starved))} are kind=slurm and request "
            f"no memory, but the survey of this cluster reports "
            f"mem_flag_required: DefMemPerNode is UNLIMITED, so there is no "
            f"default per-node memory to fall back on. A unit that says "
            f"nothing here is not accepting a sensible default -- there is "
            f"none -- and what it gets instead is a site-dependent fallback "
            f"nothing in the plan records. Add a memory request to the unit's "
            f"'sbatch' list: \"--mem=64G\". `--mem-per-cpu` or `--mem-per-gpu` "
            f"count too, since sbatch refuses `--mem` alongside either.")

    refused = SPR.account_problems(units, survey)
    if refused:
        listed = "; ".join(
            f"{uid} charges {acct!r} on partition {part!r}, but {why}"
            for uid, acct, part, why in refused)
        raise PlanError(
            f"this plan charges an account its partition will not accept: "
            f"{listed}.\n"
            f"    This is the failure the per-partition survey was added "
            f"for: hours went into QOSGrpCpuLimit while a 736-CPU partition "
            f"sat 202 CPUs idle, because nothing joined the account to the "
            f"partition. Move the unit to a partition this account is allowed "
            f"in, or charge an account that partition takes. {caveat}")


def _validate_commands_and_code_units(units):
    """Validate slurm submission nesting, code prompt flags, completion protocol, and code fields."""
    for u in units:
        if not isinstance(u, dict) or u.get("kind") != "slurm":
            continue
        cmd = str(u.get("command") or "").strip()
        first = (cmd.split() or [""])[0]
        if os.path.basename(first) in ("sbatch", "srun", "salloc"):
            raise PlanError(
                f"unit {u.get('id','?')!r} has a command starting with "
                f"{os.path.basename(first)!r}. The coordinator submits this "
                f"command ITSELF, so a submission here nests one job inside "
                f"another: the outer job exits in seconds having queued an "
                f"inner job nothing is bound to, and Slurm reports COMPLETED "
                f"with ExitCode 0:0 for work that never ran. Give the command "
                f"that does the work, and put scheduler flags in 'sbatch'.")

    for u in units:
        if not isinstance(u, dict) or u.get("kind") != "code":
            continue
        text = str(u.get("prompt") or u.get("command") or "")
        head = text.split()[:1]
        if not head or not head[0].startswith("--"):
            continue
        flag = head[0][2:].split("=")[0].replace("-", "_")
        if flag in _AS_FIELDS:
            raise PlanError(
                f"unit {u.get('id','?')!r} is kind=code and its prompt BEGINS "
                f"with {head[0]!r}. A code unit's prompt is the LAST "
                f"positional argument to the agent runner, so a flag written "
                f"here is not configuration: it is a sentence the agent is "
                f"asked to read. Set {flag!r} as a field on the unit instead, "
                f"alongside 'prompt'.")

    validation_intent = {
        "repo": "/__swarm_protocol_validation_repo__",
        "branch": "swarm-protocol-validation-attempt",
        "target_branch": "main",
        "repository_remote": "ssh://git@example.invalid/project.git",
        "judgment_ref": "refs/heads/swarm-protocol-validation-attempt",
        "base_commit": "0" * 40,
    }
    for u in units:
        if not isinstance(u, dict) or u.get("kind") != "code":
            continue
        unit_intent = dict(validation_intent)
        if "seed" in u:
            unit_intent["seed"] = u["seed"]
        assembled = SCP._dispatch_prompt(u, unit_intent)
        problem = SCP._code_protocol_problem(assembled, unit_intent)
        if problem:
            raise PlanError(
                f"unit {u.get('id','?')!r} is kind=code but the coordinator "
                f"would dispatch it without the required completion protocol "
                f"({problem}). Refusing a code unit that could finish its "
                f"edits yet never produce the merged-PR evidence that closes "
                f"it.")

    for u in units:
        if not isinstance(u, dict) or u.get("kind") != "code":
            continue
        uid = u.get("id", "?")
        if not u.get("repo"):
            raise PlanError(
                f"unit {uid!r} is kind=code and declares no 'repo'. A code "
                f"unit is closed by a merged pull request, so with no "
                f"repository there is nothing to open one from and the unit "
                f"can never reach DONE. Declare the repository it changes, or "
                f"make it kind=pipeline if it is not changing code.")

        unattended = SP._unattended_mode_example(u.get("provider"))
        if "mode" in u and not str(u.get("mode") or "").strip():
            raise PlanError(
                f"unit {uid!r} declares mode={u.get('mode')!r}, which is not a "
                f"value. An absent or empty mode omits the flag entirely, so "
                f"the agent runs on DEFAULT permissions, stops at its first "
                f"write and waits for a person. Write the mode you want, e.g. "
                f"\"{unattended}\" or \"default\". Modes are "
                f"provider-specific; `paseo run --help` and the provider name "
                f"the set it accepts.")
        if "mode" not in u:
            raise PlanError(
                f"unit {uid!r} is kind=code and declares no 'mode'. An agent "
                f"under default permissions stops at its first write and "
                f"waits for a person, so unattended it runs forever doing "
                f"nothing. The coordinator will not pick permissions on your "
                f"behalf: say what this unit needs, e.g. "
                f"\"mode\": \"{unattended}\" for unattended work on "
                f"{u.get('provider') or SR.DEFAULT_AGENT_PROVIDER}, or "
                f"\"mode\": \"default\" to accept the stall deliberately. "
                f"Modes are provider-specific.")

        think = u.get("thinking")
        if isinstance(think, str) and think.strip().lower() in (
                "null", "none", "nil", "false"):
            raise PlanError(
                f"unit {uid!r} has thinking={think!r} as a STRING. paseo would "
                f"receive that as a thinking id, not find it, and return an "
                f"errored agent. To suppress the flag write JSON null or an "
                f"empty string; to set a level write the id, e.g. \"high\".")
        target = str(u.get("target_branch") or "").strip()
        if not target:
            legacy = (" The legacy 'branch' field is not used as a fallback."
                      if u.get("branch") else "")
            raise PlanError(
                f"unit {uid!r} is kind=code on repo {u['repo']!r} and declares "
                f"no 'target_branch'. The coordinator creates the source "
                f"branch, but it cannot open a mergeable pull request without "
                f"the plan naming its destination.{legacy}")


def _validate_command_paths(plan, units):
    """Validate visible absolute paths and globs referenced in non-code commands."""
    produced_anywhere = {str(o) for u in units if isinstance(u, dict)
                         for o in (u.get("outputs") or [])}
    for u in units:
        if not isinstance(u, dict) or u.get("kind") == "code":
            continue
        cmd = str(u.get("command") or "")
        declared = {str(i) for i in (u.get("inputs") or [])}
        tokens = cmd.split()
        rest = " ".join(tokens[1:]) if len(tokens) > 1 else ""
        rt, _ref = SPR.resolve_runtime(plan, u)
        entry = str((rt or {}).get("entrypoint") or "") if isinstance(
            rt, dict) else ""
        for raw in re.findall(r"[^\s'\"=,;:()]+", rest):
            tok = raw.strip().rstrip(",;")
            if not tok.startswith("/") or len(tok) < 4:
                continue
            if "$" in tok or "{" in tok:
                continue                       # expanded at run time
            if tok in declared or tok in produced_anywhere:
                continue
            if entry and tok == entry:
                continue               # the declared runtime, not a data path
            probe = tok.rstrip("/") or tok
            parent = os.path.dirname(probe)
            visible = bool(parent) and os.path.isdir(parent)

            if any(ch in tok for ch in "*?["):
                if glob.glob(tok):
                    continue
                if not visible:
                    continue           # a mount this host cannot see
                raise PlanError(
                    f"unit {u.get('id','?')!r} names the pattern {tok!r} in "
                    f"its command and nothing in {parent!r} matches it. This "
                    f"host can see that directory, so this is not a mount "
                    f"that differs on the compute node.")
            if os.path.exists(tok):
                continue
            if not visible:
                continue
            raise PlanError(
                f"unit {u.get('id','?')!r} names {tok!r} in its command. "
                f"{parent!r} exists on this host and does not contain it, so "
                f"this is not a mount that differs on the compute node: it is "
                f"the FileNotFoundError you would meet after dispatching. "
                f"Declare it in 'inputs' if something else creates it, or fix "
                f"the path.")


def _validate_slurm_arrays(units):
    """Refuse malformed --array flags and Slurm array units that declare outputs."""
    for u in units:
        if not isinstance(u, dict) or u.get("kind") != "slurm":
            continue
        arr = None
        args = [str(a) for a in (u.get("sbatch") or [])]
        for i, a in enumerate(args):
            if a.startswith("--array="):
                arr = a.split("=", 1)[1]
            elif a in ("--array", "-a") and i + 1 < len(args):
                nxt = args[i + 1]
                if nxt.startswith("-"):
                    raise PlanError(
                        f"unit {u.get('id','?')!r} has {a} followed by "
                        f"{nxt!r}, which is another flag rather than a task "
                        f"range. Slurm would reject this at submission.")
                arr = nxt
            elif a.startswith("-a") and len(a) > 2 and not a.startswith("--"):
                arr = a[2:]
        if arr and (u.get("outputs") or []):
            raise PlanError(
                f"unit {u.get('id','?')!r} declares --array {arr!r} AND "
                f"outputs {', '.join(u['outputs'])}. Every array task shares "
                f"this unit's single attempt directory, so the first task to "
                f"finish writes the artifacts record over a partially "
                f"complete result and the unit reads DONE on a fraction of "
                f"the work. A dry run will not show it, because a dry run "
                f"does not fan out. Make each shard its own unit, or have the "
                f"array write into per-task paths and declare a separate "
                f"merge unit that produces the outputs.")

