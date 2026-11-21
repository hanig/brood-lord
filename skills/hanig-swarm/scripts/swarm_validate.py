"""Plan validation phases: schema, fields, canary, retry, limits, DAG and scopes.

Python 3.8+, standard library only; dependencies never point to the facade.
"""

import os
import shutil

import unit as U
import swarm_authors as SA
import swarm_outbox as SO
import swarm_plan_rules as SPR
from swarm_types import PlanError

KINDS = U.KINDS
DEFAULT_MAX_ATTEMPTS = 1
CHARGE_METRICS = ("gpu_hours", "cpu_hours", "read_bytes", "wall_seconds",
                  "dollars")
RETRY_MODES = ("restart", "resume")


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
