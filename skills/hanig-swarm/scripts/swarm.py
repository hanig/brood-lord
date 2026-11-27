#!/usr/bin/env python3
"""swarm.py: the coordinator: dispatch, bound, detach, advance the DAG.

Step 2 of docs/plan-swarm.md. This is the CENTRE OF GRAVITY of the system, per
the committee's drift guard: "the center of gravity is the coordinator and the
human interface, not the predicate."

WHAT IT DOES. Reads a plan of polymorphic units (slurm | pipeline | code) with
dependencies. Validates that the DAG is acyclic and that no two units can write
the same place. Then, for each unit whose dependencies are DONE: allocates an
exclusive write root via unit.py, submits, records the binding, and DETACHES.

WHY IT DETACHES. A coordinator that babysits is a coordinator that dies with its
terminal. Cluster jobs run for hours or days; an ssh drop must not lose the DAG.
So `run` dispatches and exits, and `advance` -- idempotent, safe to run from a
Paseo schedule or cron -- reads durable state, re-checks units, and moves the
DAG forward. Every state transition is on disk before it is acted on.

WHAT IT REFUSES TO DO. It does not judge units; `unit.py check` does, and its
exit code is the only input. It does not re-run a completed unit. It does not
reuse a write root: a retry mints a NEW attempt, because reuse is exactly what
makes a predicate inconclusive.

Adapted from Shreshth's `start-a-sprint`, whose plan validator already enforced
the two invariants that matter here: acyclic dependencies, and disjoint write
scopes between concurrent workers. His scope-overlap logic is the direct
ancestor of `_scopes_overlap` below.

Python 3.8+, stdlib only, login-node safe.
"""

import argparse
import d5_activation as D5
import repair_guidance as DI
import datetime as dt
import errno
import fcntl
import fnmatch
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
import unit as U  # noqa: E402  same skill, installed together
import child_environment as CE  # noqa: E402
import paseo_io as PIO
import lor_io as LIO
import cli_adapters as CA
import cli_io as CIO
import worktree as W  # noqa: E402
import verify as V  # noqa: E402
import converge as CV  # noqa: E402  the declared-convergence gate
import coordinator_paths as CP  # noqa: E402
import recovery as R  # noqa: E402  audit-only worktree preservation
import codex_home as CH  # noqa: E402  local filesystem only

KINDS = U.KINDS

import swarm_routing as SR
import swarm_state as SS
import swarm_journal as SJ
import swarm_evidence as SE
import swarm_outbox as SO
import swarm_plan_rules as SPR
import swarm_validate as SV
import swarm_preflight as SP
import swarm_code_protocol as SCP
import swarm_code_launch as SCL
import swarm_workspaces as SW
import swarm_dispatch as SD
import swarm_submit as SSB
import swarm_advance as SADV
import swarm_continue as SC
import swarm_provider_resume as SPRS
import swarm_authors as SA
import swarm_provider_stops as SPS
from swarm_dispatch import (
    _allocate, _dep_env, _isolation_submission, _record_isolation_facts,
    _clear_isolation_marker, _slurm_exit_trap, _bind,
    _CONTAINER_BIND_ENV,
)
from swarm_workspaces import (
    _paseo_json,
    _paseo_error,
    _paseo_workspace_records,
    _paseo_workspace_for_path,
    _paseo_path_ownership_problem,
    _registered_attempt_workspace,
    _recover_code_launch,
    _report_retained_worktrees,
    _report_would_archive_code_worktree,
    _worktree_cleanup_failed,
    _cleanup_codex_home,
    _archive_code_worktree,
    _preserve_and_archive_code_worktree,
    reconcile_orphan,
    _reconcile_lor_orphan,
    _recover_lor_code_launch,
    _reconcile_cli_orphan,
    _recover_cli_code_launch,
    WORKTREE_ARCHIVE_MAX_ATTEMPTS,
    WORKTREE_CLEANUP_SUMMARY_PREFIX,
)
from swarm_code_launch import (
    LAUNCH_RECORD, _capture_code_launch, _code_launch_intent_problem,
    _paseo_workspace_id, _code_launch_record_payload, _create_code_worktree,
    _register_code_workspace, _write_code_launch_record, _complete_code_launch,
    _write_launch_record, _frozen_agent_fields, _frozen_agent_view,
)
from swarm_code_protocol import (
    _seed_carry_forward, _code_completion_protocol, _dispatch_prompt,
    _code_protocol_problem, CODE_COMPLETION_PROTOCOL_MARKER,
    attempt_context_values, render_attempt_context,
)
from swarm_preflight import (
    _git, _unattended_mode_example, _workspace_policy, _requires_clean_workspace,
    _execution_workspace, PreflightRefusal, _plan_workspace, _dirty_refusal,
    _repeat_launch_preflight, _inside_dir, _output_destinations, _claim_dir,
    _claim_liveness, _claim_refusal, _claim_record, _own_claim, _take_output_claims,
    _drop_claims, _release_output_claims, _note_claimed_attempt,
    OUTPUT_CLAIMS_DIRNAME, CLAIM_FILE, CLAIM_LIVE, CLAIM_FREE, CLAIM_UNKNOWN,
    DRY_PREFIX,
    _code_worktree_names,
    _git_worktrees_on_branch,
    _stash_refusal,
    _stash_preflight,
    _repeat_stash_preflight,
    _git_push_destination,
    _exact_remote_ref_head,
    _dispatch_base_refusal,
    _resolve_dispatch_target,
    _dispatch_source_identity,
    _dispatch_target_for_advance,
    _installed_skill_source_version,
    _installed_skill_snapshot,
    _warn_installed_skill_drift,
    _seed_reachability_problem,
)
from swarm_plan_rules import (
    _unquoted_shell_syntax,
    _isolation_command_argv,
    isolation_problem,
    _ancestors,
    resolve_runtime,
    _clean_abs_parts,
    shared_env_reason,
    _env_path_contains,
    _interpreter_belongs_to_env,
    _conda_dist_env_info,
    _is_native_env_launcher,
    _looks_py_env_root,
    _inferred_env_root,
    resolve_environment,
    _looks_unresolved,
    _runtime_identity,
    _validate_environments,
    _validate_runtimes,
    converge_problem,
    _units_with_canary,
    plan_digest,
    RESOLUTIONS,
    ENV_KINDS,
    ENV_KEYS,
    CONDA_ROOT_NAMES,
    SHARED_ENV_NAMES,
    ISOLATION_BACKENDS,
    ISOLATION_KEYS,
    ISOLATION_ROOT_TOKENS,
    ISOLATION_SHELL_EXECUTABLES,
    ISOLATION_MULTICALL_EXECUTABLES,
    ISOLATION_LAUNCHER_EXECUTABLES,
    _NEEDS_RUNTIME,
    _PLACEHOLDER_RE,
    _PLACEHOLDER_EXACT,
    _PLACEHOLDER_PREFIX,
    CONVERGE_UNIT_KEYS,
    COSMETIC_FIELDS,
    declared_seed,
    declared_scope,
    _norm_scope,
    _scopes_overlap,
    declared_account,
    declared_partition,
    _known_partitions,
    partition_problems,
    read_survey,
    discover_survey,
    _scheduler_facts,
    surveyed_partition,
    limit_state,
    _account_names,
    declared_memory,
    memory_flag_problems,
    account_problems,
    resolve_promote_to,
    _LOOK_IT_UP,
    SURVEY_FILE,
    LIMIT_SET,
    LIMIT_OPEN,
    LIMIT_UNKNOWN,
    MEM_FLAGS,
)
from swarm_outbox import (
    _unique_pairs,
    _read_sequence,
    _write_sequence,
    _sequence_history,
    _allocate_sequence,
    set_sequence_floor,
    OUTBOX_SEQ,
    emit_intent,
    backfill_tracker_intents,
    _parse_receipt_text,
    _read_receipts_raw,
    _receipt_shape_problem,
    load_acknowledgments,
    _receipt_grade,
    _append_receipt_atomic,
    record_receipt,
    acknowledgment_status,
    read_outbox,
    load_outbox_contract,
    OUTBOX,
    RECEIPT_SCHEMA_VERSION,
    TRACKER_EVENTS,
    RECEIPTS,
    RECEIPT_CONFIRMED,
    RECEIPT_CONFIRMING_SOURCES,
    UNACKNOWLEDGED,
    ATTESTED_UNSPECIFIED,
    ATTESTED_CONFIRMED,
    ACKNOWLEDGED,
    CONFLICT,
    closing_evidence_for,
    outbox_key,
    _intent_evidence_digest,
    _intent_attempt_identity,
    _intent_envelope,
    normalize_intent,
    _nonblank_text,
    _schema_is,
    validate_intent,
    validate_observation,
    require_valid_intent,
    _receipt_admissible,
    reconcile_observation,
    _has_attested_merge_receipt,
    _has_bound_merge_evidence,
    superseded_evidence,
    _intent_key,
    _with_intent_envelope,
    INTENT_SCHEMA_VERSION,
    OBSERVATION_SCHEMA_VERSION,
    RECONCILIATION_SCHEMA_VERSION,
    INTENT_CONNECTOR_CAPABILITY,
    INTENT_OPERATIONS,
    OPERATION_ACCEPTED,
    ASYNC_COMPLETED,
    CONFIRMED_BY_READBACK,
    UNKNOWN,
    OBSERVATION_OUTCOMES,
    MUTATION_RESPONSE,
    A2A_LIFECYCLE,
    RECEIVER_READBACK,
    RECEIVER_DEDUPLICATION,
    CONFIRMING_SOURCES,
    OBSERVATION_SOURCES,
    CLOSING_EVIDENCE,
)
from swarm_evidence import (
    trusted_base,
    trusted_produced_head,
    _capture_artifact_basis,
    raw_artifact_basis,
    trusted_artifact_basis,
    trusted_isolation_facts,
    _reported_check_result,
    _record_receipt_provenance,
    trusted_record_seal,
    admit_merge,
    _authority_result_sink,
    _check,
    CHECK_RESULT_PREFIX,
    RECEIPT_PROVENANCE_LIMIT,
    ISOLATION_MARKER_NAME,
    _merge_shape_problem,
    _read_merge_raw,
    load_merge_receipts,
    _same_repo,
    _verify_shape_problem,
    _read_verify_raw,
    load_verifications,
    admit_verification,
    _receipt_reason,
    attested_receipt,
    MERGE_RECEIPTS,
    MERGE_METHODS,
    _MERGE_REQUIRED,
    VERIFY_RECEIPTS,
    _VERIFY_REQUIRED,
)
from swarm_journal import (
    _heal_jsonl_tail, _fsync_append, _RawJournal, fatal_problems, _fsync_directory,
)
from swarm_state import (
    _holds_the_path,
    _StateSnapshot,
    _read_state_epoch,
    STATE_FILE,
    STATE_EPOCH_FILE,
    LEASE,
    LOCK,
    _LOCK_FDS,
    _LOCK_EPOCHS,
    empty_agent_overrides,
    agent_overrides_problem,
    load_agent_overrides,
    save_agent_overrides,
    attempt_launch_intent,
    attempt_backend,
    attempt_parent_session,
    attempt_cli_run_dir,
)
from swarm_routing import (
    _routing_token, _routing_model, _orchestrator_token,
    _model_matches_substrings, _provider_forbidden_for_rule,
    _parse_agent_routing, load_agent_routing, _plan_fingerprint,
    _plan_orchestrator, _plan_routing_entry, effective_default_provider,
    forbid_unit_provider_problem, _model_family, apply_agent_resolution,
    default_thinking_for, normalize_agent_spec, lor_mode_refusal,
    _unit_pins_agent, plan_code_agent, resolve_effective_agent,
    effective_agent_problem, _spec_options, unit_agent_spec,
)
from swarm_types import (
    PlanError, OutboxError, DONE, RUNNING, FAILED, PREEMPTED, INCOMPLETE,
    NEEDS_HUMAN, NAME, EXIT_OK, EXIT_HALTED, EXIT_FAILED_UNIT, EXIT_USAGE,
    EXIT_CONFLICT, EXIT_SCOPE_OUTSIDE, EXIT_SCOPE_UNCHECKED,
)


def _d5_state(state_dir, plan=None, extra_repos=()):
    if state_dir is not None:
        return Path(state_dir).expanduser().absolute()
    if isinstance(plan, (str, os.PathLike)):
        plan, error = U.read_json(plan)
        if error or not isinstance(plan, dict):
            raise SystemExit("D5 cannot resolve operated state: unreadable plan")
    return CP.resolve_paths(plan=plan, extra_repos=extra_repos)[0]


def _d5_bytecode_advisory(state_dir, plan=None):
    """Report an interpreter precondition; U8 may enforce or waive it."""
    try:
        directory = _d5_state(state_dir, plan)
    except SystemExit:
        # Observations retain their own plan diagnostics. Mutation dispatch
        # independently resolves the state and still refuses before effects.
        return
    if D5.d5_active(directory) and not sys.dont_write_bytecode:
        try:
            diagnostic = sys.stderr
            if diagnostic is not None:
                print("D5 advisory: bytecode writing is enabled; use -B or "
                      "PYTHONDONTWRITEBYTECODE=1. Bytecode and host temporary-directory "
                      "readiness remain advisory preconditions for U8 to enforce or waive.",
                      file=diagnostic)
        except Exception:
            # An unavailable diagnostic stream must not turn advice into refusal.
            pass


def _d5_refuse(route, state_dir, plan=None, extra_repos=()):
    """U4.1: legacy effect routes have no authority in the first active profile."""
    if D5.d5_active(_d5_state(state_dir, plan, extra_repos)):
        raise SystemExit(DI.refusal_message("D5 active: swarm {} is outside the supported merge profile".format(route)))


def _d5_dispatch(args):
    # Default refusal also covers new commands and aliases. Observations retain
    # their legacy constraints; validate's optional seed check can fetch.
    if args.fn not in (cmd_schema, cmd_status, cmd_scope_check, cmd_validate):
        _d5_refuse(getattr(args, "command", "unknown command"), getattr(args, "state_dir", None), plan=getattr(args, "plan", None))
    elif args.fn is cmd_validate and getattr(args, "check_seeds", False):
        _d5_refuse("validate --check-seeds", getattr(args, "state_dir", None), plan=getattr(args, "plan", None))


def __getattr__(name):
    # Read state helpers through their owner so patches reach every consumer;
    # the lock owner PID is rebound there after acquisition and fork detection.
    if name in {
        "acquire_lease", "bump_lease_epoch", "renew_lease", "release_lease", "load_state",
        "_LOCK_OWNER_PID",
        "save_state",
        "_unit_state",
        "_set_unit_state",
        "_normalise_state_clock",
        "_deadline_host_evaluation",
        "_deadline_exceeded",
        "_mark_deadline_exceeded",
        "_clear_deadline_breach",
        "_deadline_attempt_is_unresolved",
        "_occupies_live_resources",
        "trusted_launch_facts",
        "trusted_launch_host_anchor",
        "LIVE_STATES", "OPERATOR_TERMINAL_STATES",
    }:
        return getattr(SS, name)
    # Routing resolution rebinds defaults. Read through to their owner so
    # legacy swarm.<name> callers see the current plan's routing, not a copy.
    if name in {
        "AGENTS_FILE", "DEFAULT_AGENT_PROVIDER", "DEFAULT_AGENT_THINKING",
        "THINKING_BY_MODEL", "PINNED_AGENT_PROVIDER", "PINNED_AGENT_THINKING",
        "PINNED_THINKING_BY_MODEL", "ORCHESTRATOR_DEFAULTS",
        "FORBID_WHEN_ORCHESTRATOR", "VALID_ORCHESTRATORS",
        "ACTIVE_ORCHESTRATOR",
    }:
        return getattr(SR, name)
    raise AttributeError("module %r has no attribute %r" % (__name__, name))


# --- plan validation ------------------------------------------------------
# The one artifact hanig-project's report renders as the project's own
# claims. Named here because validate now has an opinion about it; see the
# findings block in validate_plan for what that opinion is and is not.
FINDINGS_FILE = "findings.json"

# The caveat that must ride along with every one of these judgements. Said
# in the refusal itself, because that is the message someone reads while
# choosing the partition they will move the unit to.
QOS_CAVEAT = (
    "Note that `qos_grptres` in the survey resolves the PARTITION QOS only: "
    "an account or association QOS can impose a GrpTRES nothing here can "
    "see, so a partition that accepts this account is still not a promise "
    "the job will run.")


# --- the declared convergence gate ---------------------------------------
# `unit.py` answers existence and terminal state. For a training run that is
# not enough: a job that executed its whole step budget, exited 0 and wrote a
# checkpoint is DONE under that predicate even if the loss was flat for the
# last three quarters of it. `converge.py` scores a declared criterion over
# the metrics SERIES and tells "it converged" apart from "it stopped".
#
# It is OPT-IN per unit and it gates DONE, in the same shape as
# `requires_verification`: undeclared, nothing changes, because a gate
# everybody must satisfy is one everybody learns to satisfy trivially.
#
# THE CRITERION COMES FROM THE PLAN, whose digest is frozen, and never from
# the attempt directory. That is not a detail. The unit spec lives inside the
# write root the job itself writes to, so reading the criterion from there
# would let a run rewrite the standard it is judged against -- the same
# laundering the launch record and the receipt were demoted to audit-only for.
def converge_verdict(u, attempt_dir):
    """Judge a unit's declared criterion over its attempt's metrics.

    Returns (state_name, [reasons]) straight from `converge.py`. It reads the
    metrics file the PLAN named, inside this attempt's exclusive write root,
    and nothing else: it writes nothing, consults neither the launch record
    nor the receipt, and re-derives no pinned value. The metrics series is
    primary evidence observed where it lies, not a pinned fact asked again.
    """
    spec = u.get("converge") or {}
    return CV.judge(str(Path(attempt_dir) / str(spec.get("metrics") or "")),
                    spec.get("criterion") or {},
                    spec.get("diverge") or [],
                    spec.get("budget"),
                    bool(spec.get("sparse_metric")))


def validate_plan(plan, survey=None, new_attempts=True):
    """Raise PlanError, or return a summary. Refuses BEFORE anything is
    dispatched: a plan that cannot be run should not half-run.

    `survey` is hanig-project's recorded observation of this cluster, or None
    when there is none to read. None is UNKNOWN, not "no restrictions": every
    check that consults it stays silent rather than guessing."""
    if not isinstance(plan, dict):
        raise PlanError("the plan is not a JSON object")
    units = plan.get("units")
    if not isinstance(units, list) or not units:
        raise PlanError("the plan declares no units; add a 'units' list")

    SV._validate_unit_fields(units)
    SV._validate_orchestrator(plan, units)
    units = SV._validate_canary(plan, units)
    SV._validate_convergence_paseo_promote(units, which=shutil.which, plan=plan)
    SV._validate_retry_exposure(
        plan, units,
        charge_metrics=CHARGE_METRICS,
        default_max_attempts=DEFAULT_MAX_ATTEMPTS,
        retry_modes=RETRY_MODES,
    )
    SV._validate_limits_and_pools(plan, units)
    SV._validate_code_contracts(units)
    SV._validate_outputs_and_findings(
        units, new_attempts=new_attempts, findings_file=FINDINGS_FILE)
    SV._validate_runtimes_and_isolation(plan, units)
    SV._validate_inputs_and_scheduler(
        units, survey=survey, qos_caveat=QOS_CAVEAT)
    SV._validate_ids_and_dag(units, kinds=KINDS)
    SV._validate_commands_and_code_units(units, plan=plan)
    SV._validate_command_paths(plan, units)
    SV._validate_slurm_arrays(units)

    return {"units": len(units),
            "with_deps": sum(1 for u in units if u.get("needs")),
            # Which slurm units say nothing about where they run. The
            # partition check reads u["sbatch"], so an empty one means it
            # examined nothing -- and a validator silent about what it skipped
            # is indistinguishable from one that checked and approved.
            # Units that will make exactly ONE attempt because they said
            # nothing. Reported as a FACT about the policy being applied, not
            # as a guess about whether that is wise: inferring "you probably
            # wanted retries" from a partition name is precisely what this
            # code refuses to do elsewhere.
            "default_attempts": sorted(
                u["id"] for u in units
                if isinstance(u, dict) and "max_attempts" not in u),
            "without_partition": sorted(
                u["id"] for u in units
                if u.get("kind", "slurm") == "slurm"
                and not SPR.declared_partition(u))}


# --- safety for unattended running ----------------------------------------
# `advance` was called "idempotent and safe to re-enter" before it was audited.
# It was not. Four holes, all of which matter the moment a scheduler runs it
# rather than a human:
#
#   1. Two concurrent advances could both load old state and submit one unit.
#   2. A crash between sbatch and bind left a job running that nothing owned.
#   3. INCOMPLETE could stay live forever, so a lost job never became terminal.
#   4. Nothing detected the plan file changing while units were live.
#
# A human typing `advance` notices all four. A cron job does not.
# older than this was abandoned by a controller that died.
SETTLE_S = 600             # accounting lag before a missing row becomes terminal

# Repetition is OPT-IN. This defaulted to 3, so every unit silently carried
# three times its stated exposure: a 1.42 TiB read on a preemptible partition
# was really a 4 TiB worst case, and nobody had asked for that. A retry starts
# in a FRESH EMPTY attempt directory, so a retry is a redo unless a tested
# resume contract says otherwise.
DEFAULT_MAX_ATTEMPTS = 1

# Metrics a plan may budget or declare as retry exposure. A small fixed
# vocabulary, so "read_bytes" means one thing everywhere and a typo is caught
# rather than silently ignored.
CHARGE_METRICS = ("gpu_hours", "cpu_hours", "read_bytes", "wall_seconds",
                  "items")
RETRY_MODES = ("restart", "resume")

# --- dispatch -------------------------------------------------------------
def _resolve_launch_agent(u, state_dir, plan=None):
    """Resolve and check the agent a NEW code attempt freezes. (agent, error)."""
    overrides, problem = SS.load_agent_overrides(state_dir)
    if problem:
        return None, f"unit {u['id']!r}: {problem}"
    agent = SR.resolve_effective_agent(plan, u, overrides)
    problem = SR.effective_agent_problem(plan, agent)
    if problem:
        return None, problem
    if agent["backend"] == LIO.LOR_BACKEND:
        problem = LIO.lor_availability()
        if problem:
            return None, f"unit {u['id']!r} resolves to {agent['provider']!r}: {problem}"
        agent["parent_session_id"] = os.environ["LOR_SESSION_ID"].strip()
    elif CA.is_cli_backend(agent["backend"]):
        # Direct CLI: binary, key (pi+openrouter) and placement are checked
        # before any attempt directory, worktree or claim exists.
        problem = CIO.availability(agent["backend"], agent["cli_model"])
        if problem:
            return None, f"unit {u['id']!r} resolves to {agent['provider']!r}: {problem}"
        agent["cli_placement"] = CIO.placement()[0]
        agent["cli_runs_root"] = (str(Path(state_dir).absolute() / CIO.RUNS_DIR)
                                  if state_dir is not None else None)
    return agent, None


def _submit(u, unit_dir, dry_run, state=None, state_dir=None,
            dispatch_source=None, plan_path=None, plan=None):
    """Submit, and return (job_id, error). Dispatch differs per kind; judging
    does not.

    --dry-run records what WOULD be submitted, so the DAG logic is testable
    without a scheduler. A coordinator that can only be tested on a live
    cluster does not get tested."""
    _d5_refuse('_submit', state_dir)
    kind = u["kind"]
    record_enabled, problem = U.slurm_record_policy(
        unit_dir, u, SE.raw_artifact_basis(state or {}, u["id"], unit_dir))
    if problem:
        return None, problem
    anchored_base = None
    backend = LIO.PASEO_BACKEND
    if kind == "code":
        attempt = Path(unit_dir).name
        existing_intent = ((((state or {}).get("units") or {}).get(u["id"]) or {})
                           .get("attempt_launch_intents") or {}).get(attempt)
        if existing_intent:
            # The attempt already froze its agent; never re-resolve it.
            backend = SS.attempt_backend(
                ((state or {}).get("units") or {}).get(u["id"]), attempt)
            u = SCL._frozen_agent_view(u, existing_intent)
            if backend == LIO.LOR_BACKEND:
                lor_problem = LIO.lor_availability()
                if lor_problem:
                    return None, f"unit {u['id']!r}: {lor_problem}"
            elif CA.is_cli_backend(backend):
                cli_problem = CIO.availability(backend, existing_intent.get("cli_model"))
                if cli_problem:
                    return None, f"unit {u['id']!r}: {cli_problem}"
            anchor_err, anchored_base = SSB._recheck_existing_code_intent(
                u, attempt, existing_intent, dispatch_source=dispatch_source)
        else:
            # Resolved once, here, under the project lock, BEFORE any
            # worktree or agent exists. A Lor agent that cannot be started
            # from this process is refused now with no side effect.
            agent, agent_err = _resolve_launch_agent(u, state_dir, plan)
            if agent_err:
                return None, agent_err
            u, backend = agent["unit"], agent["backend"]
            anchor_err, anchored_base = SCL._capture_code_launch(
                unit_dir, u, dispatch_source=dispatch_source,
                unit_state=((state or {}).get("units") or {}).get(u["id"]),
                agent=agent)
        if anchor_err:
            return None, anchor_err
        SP._warn_installed_skill_drift(
            anchored_base["intent"].get("installed_skills"),
            anchored_base["base"], u.get("id"))
    elif SP._requires_clean_workspace(u):
        anchor_err, anchored_base = SCL._write_launch_record(unit_dir, u)
        if anchor_err:
            return None, anchor_err
    persist_err = SSB._persist_anchored_launch(
        u, unit_dir, kind, dry_run, state, state_dir, anchored_base)
    if persist_err:
        return None, persist_err
    if dry_run:
        return f"dry-{os.urandom(3).hex()}", None
    if state is None or state_dir is None:
        return None, (f"refusing non-dry dispatch for unit {u['id']!r} "
                      f"without coordinator state and state_dir: launch "
                      f"authority must be durable before an external job or "
                      f"agent is created")
    submission_command = u.get("command")
    if (not dry_run and kind in _NEEDS_RUNTIME
            and u.get("isolation") is not None):
        submission_command, isolation_error = SSB._prepare_isolation_command(
            u, unit_dir, state, state_dir)
        if isolation_error:
            return None, isolation_error
    if kind == "slurm":
        return SSB._submit_slurm(
            u, unit_dir, state, record_enabled, submission_command)
    if kind == "pipeline":
        return SSB._submit_pipeline(u, unit_dir, state, submission_command)
    if kind == "code" and backend in LIO.NATIVE_BACKENDS:
        attempt = Path(unit_dir).name
        intent = (((state or {}).get("units", {}).get(u["id"], {})
                   .get("attempt_launch_intents") or {}).get(attempt))
        intent_err = SCL._code_launch_intent_problem(intent, u, attempt)
        if intent_err:
            return None, intent_err
        recovered_agent, reuse_workspace, wt_err = SSB._prepare_code_worktree(
            u, unit_dir, state, state_dir, attempt, intent)
        if wt_err:
            return None, wt_err
        if recovered_agent:
            return recovered_agent, None
        launcher = (SSB._launch_lor_agent if backend == LIO.LOR_BACKEND
                    else SSB._launch_cli_agent)
        return launcher(
            u, unit_dir, state, state_dir, attempt, intent, reuse_workspace,
            plan_path=plan_path)
    if kind == "code":
        attempt = Path(unit_dir).name
        codex = (u.get("provider") or SR.DEFAULT_AGENT_PROVIDER).split("/", 1)[0] == "codex"
        if codex:
            try:
                CH.check_effective_uid()
            except CH.HomeError as exc:
                return None, str(exc)
            if any(str(kv).partition("=")[0] == "CODEX_HOME"
                   for kv in (u.get("env") or [])):
                return None, "CODEX_HOME is coordinator-owned; remove it from the unit env"
            try:
                auth = CH.validate_source()
            except CH.HomeError as exc:
                return None, str(exc)
        intent = (((state or {}).get("units", {}).get(u["id"], {})
                   .get("attempt_launch_intents") or {}).get(attempt))
        intent_err = SCL._code_launch_intent_problem(intent, u, attempt)
        if intent_err:
            return None, intent_err
        recovered_agent, reuse_workspace, wt_err = SSB._prepare_code_worktree(
            u, unit_dir, state, state_dir, attempt, intent)
        if wt_err:
            return None, wt_err
        if recovered_agent:
            return recovered_agent, None
        argv = ["paseo", "run", "--background", "--json",
                "--cwd", str(reuse_workspace)]
        provider = u.get("provider") or SR.DEFAULT_AGENT_PROVIDER
        default_model = None
        if not u.get("provider"):
            provider, separator, default_model = provider.partition("/")
            if not separator:
                default_model = None
        argv += ["--provider", provider,
                 "--title", f"[swarm] {u['id']} {Path(unit_dir).name}",
                 "--env", f"SWARM_UNIT_ID={u['id']}",
                 "--env", f"SWARM_UNIT_DIR={unit_dir}"]
        if u.get("mode"):
            argv += ["--mode", str(u["mode"])]
        if u.get("model") or default_model:
            argv += ["--model", str(u.get("model") or default_model)]
        thinking = u.get("thinking", SR.default_thinking_for(u))
        if thinking:
            argv += ["--thinking", str(thinking)]
        for kv in (u.get("env") or []):
            argv += ["--env", str(kv)]
        prompt_intent = dict(intent)
        if plan_path is not None:
            prompt_intent.update(scope_plan=str(Path(plan_path).resolve()),
                                 scope_state_dir=str(Path(state_dir).resolve()))
        prompt = SCP._dispatch_prompt(u, prompt_intent)
        protocol_problem = SCP._code_protocol_problem(prompt, prompt_intent)
        if protocol_problem:
            return None, (
                f"unit {u['id']!r} code prompt has no valid completion "
                f"protocol ({protocol_problem}); refusing an unclosable "
                f"agent launch")
        if codex:
            home, home_err = SSB._prepare_codex_home(
                u, unit_dir, state, state_dir, attempt, auth)
            if home_err:
                return None, home_err
            argv += ["--env", f"CODEX_HOME={home}"]
        argv.append(prompt)
        rc, out, err = U.run(argv, timeout=180)
        return SSB._finalize_code_launch(
            u, unit_dir, state, state_dir, attempt, reuse_workspace,
            rc, out, err)
    return None, f"unknown kind {kind!r}"


def maybe_continue(state_dir, uid, u, us, report, state=None):
    """Send a bounded continuation to a code agent that settled empty-handed.

    NOT a unit state, NOT a retry mode, and there is deliberately no Slurm
    analogue. A conversational turn is not a retry boundary: a planning-only
    turn can settle while the session, worktree, launch identity, attempt root
    and budget all remain valid, so minting a fresh attempt would discard the
    context and spend a retry on what is really a provider liveness defect.

    So this stays INSIDE the attempt: same attempt id, same write root, same
    anchor. What it must never become is a correction loop. It answers exactly
    one condition, "settled and produced nothing", and the bound is declared
    in the plan rather than passed by whoever happens to run advance.

    Returns True if a continuation was sent.
    """
    _d5_refuse('maybe_continue', state_dir)
    cfg = u.get("continuation") or {}
    limit = cfg.get("max") or 0
    if not limit:
        return False
    if u.get("kind") != "code":
        return False
    # ONLY this reason. A permission block is NEEDS_HUMAN and a person must
    # answer it; a failure is FAILED and prodding it repeats the failure; a
    # verifier rejection is a correction loop, which is a different thing
    # wearing the same clothes.
    if (SE._receipt_reason(state or {}, uid, us.get("attempt_dir") or "")
            != U.REASON_NO_OUTPUTS):
        return False
    agent = us.get("job_id")
    if not agent:
        return False

    if state is None or state_dir is None:
        report.append(
            f"{uid}: refusing to send an unrecorded continuation: durable "
            f"coordinator state and state_dir are required before Paseo is "
            f"contacted")
        return False

    prompt = str(cfg.get("prompt") or
                 "Your turn ended without producing the declared outputs. "
                 "Continue the work you planned.")
    prompt_digest = hashlib.sha256(prompt.encode()).hexdigest()[:16]
    continuations = us.setdefault("continuations", [])
    pending = next((entry for entry in continuations
                    if isinstance(entry, dict)
                    and entry.get("status") == "pending"
                    and entry.get("sent") is None), None)
    if pending is not None:
        if pending.get("agent_id") != str(agent):
            report.append(f"{uid}: retained: continuation agent mismatch")
            return False
        if pending.get("prompt_sha256") != prompt_digest:
            report.append(
                f"{uid}: refusing to retry pending continuation "
                f"{pending.get('n')}: its recorded prompt no longer matches "
                f"the plan")
            return False
        entry = pending
        used = int(entry.get("n") or len(continuations)) - 1
    else:
        used = len(continuations)
        if used >= limit:
            return False
        entry = {"at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                 "n": used + 1, "of": limit,
                 "agent_id": str(agent),
                 "prompt_sha256": prompt_digest,
                 "sent": None, "status": "pending"}
        continuations.append(entry)
        # A continuation is an external, non-idempotent act. Persist the
        # pending entry before sending it. If the coordinator dies before
        # Paseo is contacted, the same entry is retried rather than consuming
        # an unsent slot. If it dies after Paseo accepts the message but
        # before the result is saved, Paseo offers no idempotency key, so a
        # retry can duplicate the prompt; exactly-once delivery is not
        # available across that boundary.
    ambiguous_retry = pending is not None
    entry["delivery_unacknowledged"] = True
    entry["active_since_send"] = False
    entry["settled_since_send"] = False
    entry["settle_recovered"] = False
    backend = SS.attempt_backend(us, us.get("attempt_dir"))
    entry["backend"] = backend
    SS.save_state(state_dir, state)
    if backend == LIO.LOR_BACKEND:
        # Queued, so a message never interrupts a turn; same bounded,
        # pending-first bookkeeping as Paseo, different transport.
        sent, detail = LIO.send_continuation(str(agent), prompt, timeout=120)
        rc, out, err = (0, detail, "") if sent else (1, "", detail)
    elif CA.is_cli_backend(backend):
        # A direct-CLI continuation is turn N+1 of the same session in the
        # same worktree; cli_io refuses while a turn is still running.
        run, detail = CIO.resume(state_dir, Path(us["attempt_dir"]).name, prompt)
        rc, out, err = ((0, f"turn {run['turn']}", "") if run is not None
                        else (1, "", detail))
    else:
        rc, out, err = U.run(["paseo", "send", str(agent), prompt], timeout=120)
    entry["delivery_unacknowledged"] = ambiguous_retry or rc != 0
    entry["sent"] = rc == 0
    entry["status"] = "sent" if rc == 0 else "failed"
    if rc != 0:
        entry["error"] = (err or out or "").strip()[:200]
    else:
        us["incomplete_since"] = time.time()
        SS._set_unit_state(us, "RUNNING")
    SS.save_state(state_dir, state)
    if rc != 0:
        report.append(f"{uid}: continuation {used + 1}/{limit} could not be "
                      f"sent: {entry['error']}")
        return False
    report.append(
        f"{uid}: settled without producing; sent continuation "
        f"{used + 1}/{limit} to agent {agent} in the SAME attempt. After "
        f"{limit} the unit fails for missing production evidence.")
    return True


# --- the DAG --------------------------------------------------------------
RETRYABLE = {PREEMPTED}          # a preemption is not a failure
TERMINAL_BAD = {FAILED}
WORKTREE_CLEANUP_STATES = frozenset(
    ("READY_FOR_PR", "DONE", "FAILED", "FAILED_EVIDENCE", "PREEMPTED"))


def advance(plan, state, state_dir, root, dry_run, max_new=None,
            accept_plan_change=False, plan_path=None):
    """Idempotent. Re-check every live unit, then dispatch whatever is ready.

    Safe to run from a Paseo schedule or cron every few minutes: it never
    re-submits a unit that has an attempt recorded, and it persists before it
    acts."""
    _d5_refuse('advance', state_dir)
    U.SACCT_UNAVAILABLE = False
    units = {u["id"]: u for u in SPR._units_with_canary(plan)}
    report, dispatched, halted = [], 0, state.get("halted")
    SPS.report_retained(state, report)
    advance_observed_at = time.time()
    dispatch_targets = {}
    dispatch_refusal = None

    # A DRY RUN MUST NOT CONTAMINATE A REAL PROJECT.
    if dry_run:
        refusal = SADV._check_dry_run_contamination(units, state, report)
        if refusal is not None:
            return refusal
    before_states = {uid: (SS._unit_state(state, uid) or {}).get("state")
                     for uid in units}

    SADV._normalise_persisted_state(
        units, state, state_dir, dry_run, advance_observed_at, report)
    SADV._cleanup_worktrees(
        units, state, state_dir, dry_run, WORKTREE_CLEANUP_STATES, report)

    # The plan must not change under a live run. A mid-flight edit silently
    # redefines what the recorded attempts were for.
    digest = SPR.plan_digest(plan)
    if state.get("plan_digest") is None:
        state["plan_digest"] = digest
    elif state["plan_digest"] != digest and accept_plan_change:
        prior = state["plan_digest"]
        state["plan_digest"] = digest
        state.setdefault("ratified_edits", []).append(
            {"from": prior, "to": digest,
             "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
             "by": os.environ.get("USER", "?")})
        report.append(f"plan change RATIFIED: {prior[:12]} -> {digest[:12]}. "
                      f"Recorded attempts are kept.")
        SS.save_state(state_dir, state)
    elif state["plan_digest"] != digest:
        # The old remedy here was "start a new state directory", which
        # re-dispatches DONE units and duplicates live jobs: a refusal whose
        # named action is worse than the fault. Raising a budget ceiling is a
        # DESIGNED human intervention, so there must be a way to say yes.
        return (report + [f"REFUSING to advance: the plan file changed while units are "
                 f"live (digest {state['plan_digest'][:12]} -> {digest[:12]}). "
                 f"A mid-flight edit redefines what the recorded attempts were "
                 f"for.",
                 f"If the edit was intended, ratify it explicitly:",
                 f"    python3 swarm.py advance <plan> --accept-plan-change",
                 f"which records the new digest and keeps every recorded "
                 f"attempt. Units already DONE are not re-dispatched."], 0,
                "plan changed mid-flight")

    def _submit_live(*args, **kwargs):
        kwargs.setdefault("plan", plan)
        return _submit(*args, **kwargs)

    def _agent_preflight(u):
        attempt_state = SS._unit_state(state, u["id"])
        if attempt_state.get("attempt_dir"):
            return None
        _agent, problem = _resolve_launch_agent(u, state_dir, plan)
        return problem

    SADV._evaluate_deadlines(
        units, state, state_dir, dry_run, advance_observed_at, report)
    SADV._recover_unbound_attempts(
        units, state, state_dir, dry_run, dispatch_targets, plan_path,
        report, submit_fn=_submit_live)

    # 1. Re-check anything with a live attempt. The coordinator does not judge;
    #    unit.py does, and its exit code is the whole input.
    accounting_job_id = None
    for uid, u in units.items():
        # Each `_check` can take its own timeout, and a synchronous pipeline
        # engine takes far longer. Renewing between units keeps mutual
        # exclusion for as long as we are actually making progress, instead of
        # losing it on a fixed clock while still running.
        # None is "cannot tell" and must NOT be treated as loss: one
        # transient NFS read used to halt a healthy project until a human
        # edited durable state. Only an explicit False stops us.
        if not dry_run and SS.renew_lease(state_dir) is False:
            # We LOST the lease. Three reviewers found this discarded, which
            # is the worst outcome in the file: a controller deposed mid-run
            # keeps checking, dispatching and writing swarm-state.json
            # alongside its successor, which is the "one unit becomes two
            # jobs" that renewal exists to prevent.
            report.append("STOPPING: this controller no longer holds the "
                          "lease; another has taken it over. Nothing further "
                          "is dispatched from here. Whatever is already "
                          "submitted keeps running and will be judged by "
                          "whoever holds the lease.")
            state["halted"] = "lease lost mid-advance"
            SS.save_state(state_dir, state)
            return report, dispatched, state["halted"]
        us = SS._unit_state(state, uid)
        if (us.get("state") in SS.OPERATOR_TERMINAL_STATES
                or SPS.retained(us, us.get("attempt_dir"))):
            continue
        if not us["attempt_dir"]:
            continue
        attempt = us["attempt_dir"]
        launch_facts = SS.trusted_launch_facts(state, uid, attempt)
        host_problem = W.launch_host_problem(
            SS.trusted_launch_host_anchor(state, uid, attempt))
        if host_problem:
            us["host_judgment"] = "UNJUDGEABLE_HERE"
            us.pop("incomplete_since", None)
            us.pop("launch_recovery_problem", None)
            report.append(f"{uid}: UNJUDGEABLE_HERE -- {host_problem}")
            SS.save_state(state_dir, state)
            continue
        if us.get("launch_recovery_problem"):
            report.append(f"{uid}: NEEDS_HUMAN -- "
                          f"{us['launch_recovery_problem']}")
            continue
        us.pop("host_judgment", None)
        if us["state"] == "DONE":
            continue
        (rc, stdout, stderr, protocol_problem, checked_state,
         accounting_job_id) = SADV._check_unit_attempt(
            uid, u, us, state, state_dir, attempt, launch_facts,
            accounting_job_id, report)
        previous = us.get("state")
        previous_state_changed_at = us.get("state_changed_at")
        previous_state_changed_at_basis = us.get("state_changed_at_basis")

        if SADV._handle_post_check_deadline_and_resume(
                uid, u, us, state, state_dir, rc, protocol_problem,
                checked_state, previous, dry_run, WORKTREE_CLEANUP_STATES,
                report):
            continue

        SADV._apply_convergence_gate(
            uid, u, us, attempt, converge_verdict, report)
        SADV._admit_code_closure(uid, u, us, state, state_dir, report)
        report.append(f"{uid}: {us['state']}")

        if SADV._settle_incomplete_attempt(
                uid, u, us, state, state_dir, rc, protocol_problem,
                SETTLE_S, maybe_continue, report):
            continue
        SADV._apply_retry_and_teardown(
            uid, u, us, state, state_dir, attempt, rc, previous,
            previous_state_changed_at, previous_state_changed_at_basis,
            dry_run, RETRYABLE, DEFAULT_MAX_ATTEMPTS, WORKTREE_CLEANUP_STATES,
            _current_code_terminal_watch, report)
    SS.save_state(state_dir, state)

    # 1b. Release the output claims of units that are no longer live (B6).
    SADV._release_completed_output_claims(
        units, state, state_dir, root, dry_run, report)

    # 2 & 3. Enforce budget/concurrency caps and dispatch ready units.
    dispatched, halted, dispatch_refusal = SADV._dispatch_ready_units(
        plan, units, state, state_dir, root, dry_run, max_new, plan_path,
        dispatch_targets, halted, report, submit_fn=_submit_live,
        agent_preflight=_agent_preflight)

    # Emit tracker intents from the FINAL state of the advance.
    project = plan.get("name") or "swarm"
    if not dry_run:
        SO.backfill_tracker_intents(state_dir, project, units)
    existing_outbox_keys = {
        rec.get("key") for rec in SO.read_outbox(state_dir)
        if isinstance(rec, dict)} if dry_run else set()
    for uid in sorted(units):
        us = SS._unit_state(state, uid)
        SADV._emit_unit_final_intent(
            state_dir, project, uid, us, units, state, dry_run,
            existing_outbox_keys, report, _emit_current_intent)
    return report, dispatched, halted or dispatch_refusal


def _load_plan(path):
    plan, err = U.read_json(path)
    if err:
        sys.exit(f"error: no readable plan at {path}: {err}")
    # Dispatch validates against the survey too, not just `validate`. A
    # refusal that only fires when someone runs the optional command is a
    # refusal that fires after the DAG is live.
    survey, _note = SPR.discover_survey(path)
    try:
        validate_plan(plan, survey, new_attempts=False)
    except PlanError as e:
        sys.exit(f"error: invalid plan: {e}")
    except RecursionError:
        sys.exit("error: the dependency graph is too deep to validate; it is "
                 "probably cyclic in a way the checker could not unwind.")
    return plan


CODE_TERMINAL_WATCH_LOG = "code-terminal-watch.log"


def _code_terminal_outcome_path(state_dir, uid, attempt, agent):
    # The record belongs to the coordinator, outside the worker's write root.
    key = json.dumps([uid, attempt, str(agent)], separators=(",", ":"))
    digest = hashlib.sha256(key.encode()).hexdigest()
    return Path(state_dir) / ("code-terminal-outcome-" + digest + ".json")


def _write_code_terminal_outcome(args, outcome):
    """Retain diagnostics even while another controller holds the lock.

    This per-watcher record is never judgment authority. Only its diagnostic
    fields are imported into swarm-state.json under the project lock.
    """
    path = _code_terminal_outcome_path(
        args.state_dir, args.unit, args.attempt, args.agent)
    record = {"unit": args.unit, "attempt": args.attempt,
              "agent_id": str(args.agent), "outcome": outcome}
    err = U.write_json(path, record)
    if err:
        print(f"cannot record terminal-watch outcome: {err}", flush=True)


def _code_terminal_log_tail(path):
    """A bounded diagnostic tail; a replaced FIFO must not stall status."""
    import stat
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as fh:
            if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
                return "[log is not a regular file]"
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - 4000))
            return fh.read(4000).decode("utf-8", "replace")
    except OSError as exc:
        return f"[log unavailable: {exc}]"


def _code_terminal_process(watch, attempt):
    """Local diagnostic liveness, never a fact used to judge the unit."""
    if watch.get("host") != os.uname().nodename:
        return "unknown", "watcher host is different or unrecorded"
    pid = watch.get("pid")
    if type(pid) is not int or pid <= 0:
        return "unknown", "watcher launch has no recorded pid"
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "absent", "watcher process no longer exists"
    except OSError as exc:
        return "unknown", f"cannot inspect watcher process: {exc}"
    rc, out, _err = U.run(
        ["ps", "-ww", "-p", str(pid), "-o", "stat=", "-o", "command="],
        timeout=5)
    fields = out.split(None, 1)
    if rc != 0 or len(fields) != 2:
        return "unknown", "ps did not establish watcher liveness"
    if fields[0].startswith("Z"):
        return "absent", "watcher process is a zombie"
    # ps renders argv without preserving argument boundaries. Never infer a
    # mismatch from a value its display cannot represent unambiguously.
    if any(char.isspace() for value in (attempt, str(watch.get("agent_id")))
           for char in value):
        return "unknown", "ps cannot distinguish watcher arguments containing whitespace"
    # Check the complete ps output, not a shortened display, so an unrelated
    # reused pid does not read as a live watcher just because kill succeeded.
    tokens = fields[1].split()
    pairs = list(zip(tokens, tokens[1:]))
    if ("watch-code-terminal" not in tokens
            or ("--attempt", attempt) not in pairs
            or ("--agent", str(watch.get("agent_id"))) not in pairs):
        return "absent", "recorded pid now names a different command"
    return "present", "watcher process present; this is not work progress"


def _read_code_terminal_outcome(state_dir, uid, attempt, agent):
    record, err = U.read_json(_code_terminal_outcome_path(
        state_dir, uid, attempt, agent))
    if err == "missing":
        return {}, None
    if err or not isinstance(record, dict):
        return {}, "unreadable terminal-watch outcome ignored"
    if (record.get("unit") != uid or record.get("attempt") != attempt
            or record.get("agent_id") != agent):
        return {}, "stale terminal-watch outcome ignored: unit/attempt/agent mismatch"
    if not isinstance(record.get("outcome"), dict):
        return {}, "malformed terminal-watch outcome ignored"
    # Deliberately no produced_head, job id, unit state or other authority.
    return {key: record["outcome"][key] for key in CODE_TERMINAL_DIAGNOSTICS
            if key in record["outcome"]}, None


CODE_TERMINAL_DIAGNOSTICS = (
    "status", "observed_at", "wait_exit_code", "reason", "log_tail", "checked_at")


def _current_code_terminal_watch(state, uid, attempt, agent):
    """Use only the coordinator's current attempt and exact agent binding."""
    us = state.get("units", {}).get(uid) or {}
    current = Path(us["attempt_dir"]).name if us.get("attempt_dir") else None
    watch = (us.get("code_terminal_watches") or {}).get(attempt)
    if (attempt != current or agent != us.get("job_id")
            or not isinstance(watch, dict) or watch.get("agent_id") != agent):
        return None
    return watch


def _observe_code_terminal_watch(state_dir, uid, attempt, watch):
    """Return a diagnostic snapshot without mutating state or taking a lease."""
    result = dict(watch)
    outcome, ignored = _read_code_terminal_outcome(
        state_dir, uid, attempt, watch.get("agent_id"))
    result.update(outcome)
    # The outcome describes wait, while this field records a later locked
    # coordinator check. Re-reading the retained outcome must not undo it.
    if watch.get("status") == "checked":
        result["status"] = "checked"
        result["checked_at"] = watch.get("checked_at")
    if ignored:
        result["outcome_ignored"] = ignored
    if result.get("status") in ("starting", "waiting"):
        liveness, reason = _code_terminal_process(watch, attempt)
        result["process_state"] = liveness
        result["process_reason"] = reason
        result["process_observed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        if liveness == "absent":
            # Publication and exit can happen between the read and the probe.
            outcome, ignored = _read_code_terminal_outcome(
                state_dir, uid, attempt, watch.get("agent_id"))
            result.update(outcome)
            if ignored:
                result["outcome_ignored"] = ignored
            if result.get("status") in ("starting", "waiting"):
                result.update({
                    "status": "exited_without_idle", "wait_exit_code": None,
                    "observed_at": result["process_observed_at"],
                    "reason": reason + "; no idle observation or exit code recorded",
                    "log_tail": _code_terminal_log_tail(watch.get("log")),
                })
    return result


def _refresh_code_terminal_watches(state, state_dir, report):
    """Import diagnostics inside cmd_run's lease spanning load through save."""
    changed = False
    for uid, us in state.get("units", {}).items():
        if us.get("state") in SS.OPERATOR_TERMINAL_STATES:
            continue
        for attempt, watch in (us.get("code_terminal_watches") or {}).items():
            if not isinstance(watch, dict):
                continue
            agent = watch.get("agent_id")
            if _current_code_terminal_watch(state, uid, attempt, agent) is None:
                if _code_terminal_outcome_path(state_dir, uid, attempt, agent).exists():
                    report.append(f"{uid}: stale terminal-watch outcome ignored "
                                  f"for attempt {attempt}; attempt/agent is no longer current")
                continue
            observed = _observe_code_terminal_watch(state_dir, uid, attempt, watch)
            ignored = observed.pop("outcome_ignored", None)
            if ignored:
                report.append(f"{uid}: {ignored}")
            if observed != watch:
                watch.update(observed)
                changed = True
    return changed


def _code_terminal_status(state, state_dir, uid, attempt):
    us = state.get("units", {}).get(uid) or {}
    watch = (us.get("code_terminal_watches") or {}).get(attempt)
    if not isinstance(watch, dict):
        return None
    if _current_code_terminal_watch(state, uid, attempt, watch.get("agent_id")) is None:
        return dict(watch, outcome_ignored="stale terminal-watch record ignored: agent mismatch",
                    import_pending=False)
    observed = _observe_code_terminal_watch(state_dir, uid, attempt, watch)
    observed["import_pending"] = any(
        observed.get(key) != watch.get(key) for key in CODE_TERMINAL_DIAGNOSTICS)
    return observed


def _start_code_terminal_watchers(plan, state, args, report):
    """Start one best-effort event-driven checker for each live code attempt.

    The watcher waits in Paseo, then invokes a separate coordinator process
    for the ordinary advance path under the same project lock. It has no separate judgment authority: unit.py remains
    the only checker and the resulting head still crosses the normal
    coordinator-controlled result channel.
    """
    _d5_refuse('_start_code_terminal_watchers', getattr(args, "state_dir", None), plan=getattr(args, "plan", None))
    units = {u["id"]: u for u in plan.get("units") or []}
    started = False
    for uid, u in sorted(units.items()):
        if u.get("kind") != "code":
            continue
        us = SS._unit_state(state, uid)
        attempt_dir = us.get("attempt_dir")
        job_id = us.get("job_id")
        if (not attempt_dir or not job_id or us.get("state") not in SS.LIVE_STATES
                or SE.trusted_produced_head(state, uid, attempt_dir)):
            continue
        attempt = Path(attempt_dir).name
        watches = us.setdefault("code_terminal_watches", {})
        if isinstance(watches.get(attempt), dict):
            continue
        if SS.attempt_backend(us, attempt) == LIO.LOR_BACKEND:
            # Never a detached process holding a short-lived Lor token: the
            # Lor parent session is woken natively; advance judges.
            watches[attempt] = {
                "agent_id": str(job_id), "backend": LIO.LOR_BACKEND,
                "parent_session_id": SS.attempt_parent_session(us, attempt),
                "host": os.uname().nodename,
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "status": "lor_native_parent_wake"}
            SS.save_state(args.state_dir, state)
            report.append(f"{uid}: lor session {job_id} is tracked by Lor "
                          "parent wake + `swarm.py advance`; no watcher "
                          "process started")
            continue
        if CA.is_cli_backend(SS.attempt_backend(us, attempt)):
            # No watcher process: the turn wrapper's atomic exit receipt is
            # the terminal signal; advance (or wait_event --poll-cli) reads it.
            watches[attempt] = {
                "agent_id": str(job_id), "backend": SS.attempt_backend(us, attempt),
                "host": os.uname().nodename,
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "status": "cli_turn_receipt"}
            SS.save_state(args.state_dir, state)
            report.append(f"{uid}: {SS.attempt_backend(us, attempt)} run {job_id} "
                          "is tracked by its turn receipt + `swarm.py "
                          "advance`; no watcher process started")
            continue
        log_path = Path(attempt_dir) / CODE_TERMINAL_WATCH_LOG
        # Persist the one watcher intent before starting its process. A crash
        # after spawn must not make the next advance launch an unbounded set
        # of duplicate waiters. This record is diagnostic and deduplication
        # state only; it never supplies a judgment fact.
        watch = {
            "agent_id": str(job_id),
            "host": os.uname().nodename,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "log": str(log_path),
            "status": "starting",
        }
        watches[attempt] = watch
        SS.save_state(args.state_dir, state)
        try:
            log = open(log_path, "ab")
        except OSError as exc:
            watch.update({"status": "start_failed", "reason": str(exc),
                          "observed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
            SS.save_state(args.state_dir, state)
            report.append(f"{uid}: could not open terminal-watch log: {exc}. "
                          "Scheduled advance remains the fallback.")
            continue
        argv = [
            sys.executable, str(Path(__file__).resolve()),
            "watch-code-terminal", str(Path(args.plan).resolve()),
            "--state-dir", str(args.state_dir),
            "--root", str(args.root),
            "--unit", uid,
            "--attempt", attempt,
            "--agent", str(job_id),
        ]
        try:
            proc = subprocess.Popen(
                argv, stdin=subprocess.DEVNULL, stdout=log,
                stderr=subprocess.STDOUT, env=CE.child_env(),
                start_new_session=True)
        except OSError as exc:
            log.close()
            watch.update({"status": "start_failed", "reason": str(exc),
                          "observed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
            SS.save_state(args.state_dir, state)
            report.append(f"{uid}: could not start terminal watcher: {exc}. "
                          "Scheduled advance remains the fallback.")
            continue
        log.close()
        watch["pid"] = proc.pid
        watch["status"] = "waiting"
        SS.save_state(args.state_dir, state)
        report.append(f"{uid}: watching agent {job_id} for an immediate "
                      "terminal judgment")
        started = True
    return started


# This watcher waits with NO DEADLINE, and that is deliberate.
#
# It previously passed `timeout=30 * 24 * 60 * 60`. That reaches
# `selectors.PollSelector.poll()`, whose timeout is milliseconds in a C int,
# so 2592000000 ms exceeded 2147483647 and raised OverflowError before the
# child was ever waited on. The terminal watch therefore never observed an
# agent: a `code` unit was never judged the moment it went idle, and waited
# for the next scheduled advance instead. It failed invisibly because the
# traceback goes to a per-attempt log nothing reads.
#
# A smaller finite value merely moves the deadline. GPT-Astra, asked to
# adjudicate a 20-day replacement: "'this agent has run suspiciously long'
# supports escalation or an explicit workload-lifetime policy. It does not
# naturally support abandoning observation while the agent continues running.
# That is precisely when observation may be most valuable." No duration
# requirement exists to justify any particular ceiling.
#
# `None` is the honest expression of "watch until the agent is idle". It is
# also the only unbounded option: `poll()` blocks indefinitely on None, while
# every finite value is capped at 24.855 days by that same C int. This is the
# one caller that wants to block, and it runs as its own detached watcher
# process rather than inside the coordinator.
#
# The watcher publishes only its own outcome. Status observes without writing;
# a locked advance imports diagnostics, including killed-watcher observations.


def cmd_watch_code_terminal(args):
    """Publish only this watcher's outcome; a separate coordinator checks it."""
    _d5_refuse('cmd_watch_code_terminal', getattr(args, "state_dir", None), plan=getattr(args, "plan", None))
    try:
        rc, out, err = U.run(
            ["paseo", "wait", str(args.agent), "--json"],
            timeout=None)
    except Exception as exc:
        rc, out, err = None, "", f"{type(exc).__name__}: {exc}"
    outcome = {
        "status": "idle_observed" if rc == 0 else "exited_without_idle",
        "observed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "wait_exit_code": rc,
        "reason": ("paseo wait observed idle" if rc == 0 else
                   f"paseo wait ended without an idle observation (exit {rc}): "
                   f"{(err or out)[-400:]}"),
        "log_tail": (out + "\n" + err)[-4000:].strip(),
    }
    _write_code_terminal_outcome(args, outcome)
    if rc != 0:
        print(outcome["reason"], flush=True)
        return EXIT_HALTED

    # Never load, lock or save coordinator state in the watcher. The ordinary
    # coordinator owns the whole load -> import -> check -> save transaction,
    # and checks this binding again under its lease. Contention leaves the
    # published observation for status and the next scheduled advance.
    try:
        check_rc, out, err = U.run([
            sys.executable, str(Path(__file__).resolve()),
            "advance-code-terminal", str(args.plan),
            "--state-dir", str(args.state_dir), "--root", str(args.root),
            "--unit", args.unit, "--attempt", args.attempt,
            "--agent", str(args.agent)], timeout=None)
    except Exception as exc:
        check_rc, out, err = None, "", f"{type(exc).__name__}: {exc}"
    if check_rc != EXIT_OK:
        outcome["reason"] += (
            f"; immediate coordinator exited {check_rc}: {(err or out)[-400:]}; "
            "scheduled advance remains the fallback")
        _write_code_terminal_outcome(args, outcome)
        print(outcome["reason"], flush=True)
        return EXIT_HALTED
    if out:
        print(out, end="" if out.endswith("\n") else "\n")
    return EXIT_OK


def _prepare_command_paths(args, plan=None, extra_repos=(), need_root=False,
                           read_only=False):
    """Apply the one external path policy before any command can write."""
    if not read_only:
        _d5_refuse('_prepare_command_paths', getattr(args, "state_dir", None),
                   plan=plan, extra_repos=extra_repos)
    raw_state = getattr(args, "state_dir", None)
    raw_root = getattr(args, "root", None)
    try:
        state, root, _worktrees = CP.resolve_paths(
            raw_state, raw_root, plan=plan, cwd=os.getcwd(),
            extra_repos=extra_repos, need_root=(need_root or raw_state is None))
    except CP.PathPolicyError as exc:
        sys.exit(f"error: {exc}")

    # Only implicit defaults opt into implicit legacy migration. Explicit
    # destinations are never populated from a guessed source.
    if read_only and raw_state is None:
        # Status may inspect an unmigrated legacy snapshot, but only a writer
        # may copy it to the external default. Keep status free of writes even
        # on the first invocation after an upgrade.
        legacy = CP.project_context() / ".swarm" / "state"
        if not (state / STATE_FILE).exists() and (legacy / STATE_FILE).is_file():
            state = legacy
    elif raw_state is None and root is not None:
        try:
            CP.migrate_legacy_defaults(state, root, cwd=os.getcwd())
        except OSError as exc:
            sys.exit(f"error: cannot copy legacy coordinator state to the "
                     f"external default: {exc}. Legacy files were retained.")
    args.state_dir = str(state)
    if need_root:
        args.root = str(root)
    return state, root


def cmd_continue(args):
    _d5_refuse('cmd_continue', getattr(args, "state_dir", None), plan=getattr(args, "plan", None))
    args.check_seeds = True
    return SC.continue_plan(args, lambda plan: cmd_validate(args, plan))


def cmd_validate(args, plan=None):
    if getattr(args, "check_seeds", False):
        _d5_refuse('cmd_validate', getattr(args, "state_dir", None), plan=getattr(args, "plan", None))
    SR.apply_agent_resolution(args.plan, plan=plan)
    if plan is None:
        plan, err = U.read_json(args.plan)
        if err:
            sys.exit(f"error: no readable plan at {args.plan}: {err}")
    explicit = getattr(args, "survey", None)
    if explicit:
        # NAMED, so unreadable is an error rather than a shrug. Degrading a
        # survey the operator pointed at into "unknown" would silently drop
        # every check they asked for.
        survey, serr = SPR.read_survey(explicit)
        if serr:
            sys.exit(f"error: no readable survey at {explicit}: {serr}")
        note = explicit
    else:
        survey, note = SPR.discover_survey(args.plan)
    try:
        summary = validate_plan(plan, survey)
    except PlanError as e:
        sys.exit(f"error: invalid plan: {e}")
    if getattr(args, "check_seeds", False):
        problems = []
        for u in plan["units"]:
            if "seed" not in u:
                continue
            target, problem = SP._resolve_dispatch_target(u)
            if not problem:
                source, problem = SP._dispatch_source_identity(u, target)
            if not problem:
                problem = SP._seed_reachability_problem(
                    u["seed"], source["repo"], source["repository_remote_raw"],
                    source["repository_remote"])
            if problem:
                # Render one diagnostic line per unit, after the decision.
                detail = " ".join(problem.splitlines())
                problems.append(f"unit {u['id']!r}: {detail}")
        if problems:
            sys.exit("error: invalid plan:\n" + "\n".join(problems))
    print(f"plan is valid: {summary['units']} unit(s), "
          f"{summary['with_deps']} with dependencies")
    # WHICH CLUSTER FACTS WERE CONSULTED, in both directions. A pass here
    # means "the survey did not contradict this plan", and with no survey it
    # means nothing at all, so neither may be printed as approval.
    if survey is None:
        print(f"  NOT CHECKED: {note}. Nothing here examined this cluster's "
              f"memory policy or per-partition account rules.")
    else:
        print(f"  survey: {note} -- memory policy and per-partition account "
              f"rules were checked against it. {QOS_CAVEAT}")
    # NAME WHAT WAS NOT VERIFIED, so silence is never read as approval. A real
    # run declared cpu_preemptible with 32 CPUs in its prose, ran on the
    # default partition with 2, and this printed "plan is valid" -- because
    # the partition check reads u["sbatch"], which was empty, so it examined
    # nothing and said nothing.
    # A DEFAULT CHANGED UNDER EXISTING PLANS. max_attempts was 3 and is now
    # 1, so a plan that still validates may behave differently than it used
    # to. Say which units that applies to rather than letting it be
    # discovered by a preemption.
    once = summary.get("default_attempts") or []
    if once:
        print(f"  retry policy: {len(once)} unit(s) declare no max_attempts "
              f"and will make ONE attempt each. A retry starts in a fresh "
              f"empty directory, so repetition is opt-in: declare "
              f"max_attempts with a 'retry' contract if a redo is acceptable.")

    missing = summary.get("without_partition") or []
    if missing:
        known = sorted(SPR._known_partitions() or [])
        print(f"  NOT CHECKED: {len(missing)} slurm unit(s) declare no "
              f"partition, so nothing here verified where they run: "
              f"{', '.join(missing)}")
        print(f"  They will use this cluster's DEFAULT partition"
              + (f", of {len(known)} available" if known else "")
              + ". If the plan's prose names a partition, it is describing "
                "something this file does not request.")
    return EXIT_OK


def cmd_run(args):
    """Dispatch what is ready, then EXIT. Does not babysit."""
    _d5_bytecode_advisory(getattr(args, "state_dir", None), plan=getattr(args, "plan", None))
    _d5_refuse('cmd_run', getattr(args, "state_dir", None), plan=getattr(args, "plan", None))
    SR.apply_agent_resolution(args.plan)
    plan = _load_plan(args.plan)
    _prepare_command_paths(args, plan=plan, need_root=True)
    # ONE WRITER. Two schedulers firing at once, or a human running `advance`
    # while cron does, would both read old state and submit the same unit.
    ok, holder = SS.acquire_lease(args.state_dir)
    if not ok:
        print(f"cannot take this project's lock: {holder}")
        # The old text here said "pass --force if you are certain it is dead"
        # and "a stale lease expires on its own after 900s". Both became false
        # when the lock moved to the kernel: --force was accepted and silently
        # ignored, and there is no expiry to wait out. Telling an operator to
        # type a flag that does nothing is worse than offering nothing.
        print("  A live controller cannot be forced off, and a dead one frees "
              "the project immediately, so there is nothing to wait out and "
              "nothing to override.")
        print("  If nothing is really running, the lock is already free: "
              "check with `swarm.py status`.")
        return EXIT_HALTED
    try:
        state = SS.load_state(args.state_dir)
        if (getattr(args, "terminal_watch", False)
                and _current_code_terminal_watch(
                    state, args.unit, args.attempt, args.agent) is None):
            print("obsolete terminal watcher: attempt/agent no longer current; no check run")
            return EXIT_OK
        watch_report = []
        if (not args.dry_run
                and _refresh_code_terminal_watches(state, args.state_dir, watch_report)):
            SS.save_state(args.state_dir, state)
        report, dispatched, halted = advance(
            plan, state, args.state_dir, args.root, args.dry_run,
            args.max_new_dispatches,
            accept_plan_change=getattr(args, "accept_plan_change", False),
            plan_path=str(Path(args.plan).resolve()))
        report[:0] = watch_report
        if (not args.dry_run
                and _start_code_terminal_watchers(plan, state, args, report)):
            SS.save_state(args.state_dir, state)
    finally:
        SS.release_lease(args.state_dir)
    for line in report:
        print(f"  {line}")
    print(f"\ndispatched {dispatched} unit(s); coordinator exiting. Advance "
          f"with:\n  swarm.py advance {args.plan} --state-dir "
          f"{args.state_dir} --root {args.root}")
    if halted:
        print(f"HALTED: {halted}")
        return EXIT_HALTED
    return EXIT_OK


def cmd_advance(args):
    return cmd_run(args)


def _emit_current_intent(state_dir, project, uid, us, unit, evidence=None):
    """Both advancement and operator commands emit from their durable result."""
    return SO.emit_intent(state_dir, project, uid, us.get("state"), us, evidence,
                          kind=unit.get("kind"), tracker=unit.get("tracker"))


def _stop_lor_attempt(state_dir, state, uid, us):
    """Stop the current attempt's Lor session after a terminal disposition.

    Paseo attempts keep their existing retention semantics. A Lor session is
    stopped (idempotently) and, only when the CLI confirms, recorded as having
    released its live resources; a failed stop leaves the attempt retaining
    them, never presumed dead.
    """
    attempt_dir = us.get("attempt_dir")
    agent = us.get("job_id")
    backend = SS.attempt_backend(us, attempt_dir) if attempt_dir else None
    if (not attempt_dir or not agent
            or backend not in LIO.NATIVE_BACKENDS):
        return
    release = us.get("terminal_resource_release") or {}
    if (release.get("attempt_dir") == attempt_dir
            and release.get("job_id") == agent):
        return
    if backend == LIO.LOR_BACKEND:
        stopped, detail = LIO.stop_session(str(agent))
        stops, label = us.setdefault("attempt_lor_stops", {}), "lor session"
        record = {"session_id": str(agent)}
    else:
        stopped, detail = CIO.cancel(state_dir, Path(attempt_dir).name)
        stops, label = us.setdefault("attempt_cli_stops", {}), f"{backend} run"
        record = {"run_id": str(agent), "backend": backend}
    record.update(ok=stopped, detail=detail,
                  at=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    stops[Path(attempt_dir).name] = record
    if stopped:
        us["terminal_resource_release"] = {
            "attempt_dir": attempt_dir, "job_id": agent,
            "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "basis": f"{label} stop {agent} succeeded"}
        print(f"{uid}: stopped {label} {agent}")
    else:
        print(f"{uid}: could not stop {label} {agent}: {detail}; the "
              f"attempt still retains its resources", file=sys.stderr)
    SS.save_state(state_dir, state)


def cmd_stop_unit(args):
    """Record an approver's terminal disposition; preserve every attempt."""
    _d5_refuse('cmd_stop_unit', getattr(args, "state_dir", None), plan=getattr(args, "plan", None))
    if not args.approver or not args.approver.strip():
        print("error: a non-blank approver is required", file=sys.stderr)
        return 4
    if not args.reason or not args.reason.strip():
        print("error: a non-blank reason is required", file=sys.stderr)
        return 4
    # Disposition needs no dispatcher runtime.
    plan, error = U.read_json(args.plan)
    if error or not isinstance(plan, dict) or not isinstance(plan.get("units"), list):
        print(f"error: no plan with a units list at {args.plan}: {error or 'invalid shape'}", file=sys.stderr)
        return 4
    units = {}
    for unit in plan["units"]:
        uid = unit.get("id") if isinstance(unit, dict) else None
        if not isinstance(uid, str) or not uid.strip() or uid in units:
            print("error: plan units require unique non-blank ids", file=sys.stderr)
            return 4
        units[uid] = unit
    if args.unit not in units:
        print(f"error: unknown unit {args.unit!r}", file=sys.stderr)
        return 4
    successor = getattr(args, "by", None)
    if args.command == "supersede" and successor not in units:
        print(f"error: unknown successor unit {successor!r} in this plan", file=sys.stderr)
        return 4
    _prepare_command_paths(args, plan=plan)
    ok, holder = SS.acquire_lease(args.state_dir, bump_epoch=False)
    if not ok:
        print(f"error: cannot take this project's lock: {holder}", file=sys.stderr)
        return 4
    try:
        state = SS.load_state(args.state_dir)
        if not state.get("plan_digest"):
            print("error: coordinator state is missing plan_digest; ratify the intended plan first", file=sys.stderr)
            return 4
        if state["plan_digest"] != SPR.plan_digest(plan):
            print("error: plan differs from coordinator state; ratify the intended plan first", file=sys.stderr)
            return 4
        us = SS._unit_state(state, args.unit)
        target = "ABANDONED" if args.command == "abandon" else "SUPERSEDED"
        request = {"state": target, "approver": args.approver,
                   "reason": args.reason, "successor": successor}
        decision = us.get("terminal_decision") or {}
        if us.get("state") in SS.OPERATOR_TERMINAL_STATES:
            if (us.get("state") != target
                    or any(decision.get(k) != v for k, v in request.items())):
                print("error: conflicting terminal decision", file=sys.stderr)
                return 4
            # Repair a crash between persistence and append without changing
            # the decision, its timestamp, or the state epoch.
        else:
            if us.get("state") == "DONE":
                print(f"error: unit {args.unit!r} is DONE", file=sys.stderr)
                return 4
            receipt = None
            if target == "SUPERSEDED":
                next_us = state.get("units", {}).get(successor) or {}
                if next_us.get("state") != "DONE":
                    print(f"error: successor {successor!r} is not DONE", file=sys.stderr)
                    return 4
                receipt = next_us.get("merge_receipt")
                if not SO._has_bound_merge_evidence(successor, next_us, {"receipt": receipt}):
                    print(f"error: successor {successor!r} has no bound merge receipt", file=sys.stderr)
                    return 4
            decision = dict(request, prior_state=us.get("state"),
                            at=dt.datetime.now(dt.timezone.utc).isoformat(),
                            retains_live_resources=SS._occupies_live_resources(units[args.unit], us))
            if receipt is not None:
                decision["merge_receipt"] = dict(receipt)
            SS.bump_lease_epoch(args.state_dir)
            state = SS.load_state(args.state_dir)
            us = SS._unit_state(state, args.unit)
            if target == "ABANDONED":
                SS._set_unit_state(us, "ABANDONED")
            else:
                SS._set_unit_state(us, "SUPERSEDED")
            us["reason"] = args.reason
            us["terminal_decision"] = decision
            SS.save_state(args.state_dir, state)
        _stop_lor_attempt(args.state_dir, state, args.unit, us)
        _emit_current_intent(args.state_dir, plan.get("name") or "swarm",
                             args.unit, us, units[args.unit])
        print(f"{args.unit}: {target} -- {args.reason}" +
              (f" (successor: {successor})" if successor else ""))
        retention_report = []
        SPS.report_retained(state, retention_report, args.unit)
        for line in retention_report:
            print(line)
        return EXIT_OK
    finally:
        SS.release_lease(args.state_dir)


# --- gated promotion ------------------------------------------------------
# Outputs live in the exclusive write root. Getting them into a shared
# canonical tree is a SEPARATE, human-approved, recorded step, because a swarm
# that silently writes a shared path is a swarm that gets switched off.
#
# A unit that declares no `promote_to` never touches a shared path at all.
PROMOTIONS = "promotions.jsonl"


# --- runtime code-agent switching ---------------------------------------
# NOT a plan edit. plan.json and its digest stay byte-identical; the switch is
# a revisioned record in coordinator state that applies to attempts created
# AFTER it. Every existing attempt keeps the backend it froze at launch.
SET_AGENT_TERMINAL = ("DONE", "FAILED", "FAILED_EVIDENCE", "ABANDONED",
                      "SUPERSEDED")


def _agent_view(agent):
    return {key: agent.get(key) for key in (
        "backend", "provider", "model", "canonical_model_id", "thinking",
        "mode", "source")}


def _set_agent_rows(plan, state, overrides, only=None):
    """Effective agent of every code unit, and what a switch would touch."""
    rows = []
    for u in plan.get("units") or []:
        if not isinstance(u, dict) or u.get("kind") != "code":
            continue
        if only is not None and u.get("id") != only:
            continue
        us = (state.get("units") or {}).get(u["id"]) or {}
        attempt = us.get("attempt_dir")
        st = us.get("state")
        if st in SET_AGENT_TERMINAL:
            disposition = "terminal: unchanged"
        elif attempt and st not in (None, "PREFLIGHT_REFUSED"):
            disposition = ("in-flight: keeps its frozen backend; only a later "
                           "fresh attempt would use the new agent")
        else:
            disposition = "undispatched: the next attempt uses this agent"
        current = None
        if attempt:
            intent = SS.attempt_launch_intent(us, attempt)
            current = {"attempt": Path(attempt).name, "state": st,
                       "backend": SS.attempt_backend(us, attempt),
                       "provider": intent.get("provider"),
                       "model": intent.get("model"),
                       "agent_id": us.get("job_id")}
        rows.append({"unit": u["id"], "state": st,
                     "pinned_in_plan": SR._unit_pins_agent(u),
                     "effective": _agent_view(
                         SR.resolve_effective_agent(plan, u, overrides)),
                     "current_attempt": current,
                     "disposition": disposition})
    return rows


def _set_agent_target(args):
    """Build the requested override spec from flags. (spec, problem)."""
    provider, backend, model = args.provider, args.backend, args.model
    if provider is None and backend is None and model is None:
        return None, ("set-agent needs --provider PROVIDER, or --backend "
                      "{lor,paseo,claude,codex,pi} with --model MODEL (or "
                      "--clear / --show)")
    if model is not None and backend is None and provider is None:
        return None, "--model needs --backend (or use --provider)"
    if backend == LIO.PASEO_BACKEND and provider is None:
        provider, model = model, None
    raw = {}
    if backend is not None:
        raw["backend"] = backend
    if provider is not None:
        raw["provider"] = provider
    if model is not None:
        raw["model"] = model
    if args.thinking is not None:
        raw["thinking"] = args.thinking
    if args.mode is not None:
        raw["mode"] = args.mode
    return SR.normalize_agent_spec(raw, "set-agent")


def _print_set_agent(result, as_json):
    if as_json:
        print(json.dumps(result, indent=2, sort_keys=True))
        return
    print(f"agent overrides revision {result['revision']}"
          + (f" ({result['action']})" if result.get("action") else ""))
    if result.get("project_override"):
        print(f"  project override: {result['project_override']}")
    for uid, spec in sorted((result.get("unit_overrides") or {}).items()):
        print(f"  unit override {uid}: {spec}")
    for row in result["units"]:
        eff = row["effective"]
        effort = eff["thinking"] if eff["thinking"] else "(none)"
        print(f"  {row['unit']}: {eff['backend']} {eff['provider']}"
              + (f" model {eff['model']}" if eff.get("model") else "")
              + f" effort {effort} mode {eff['mode'] or '-'} "
              f"[{eff['source']}{', pinned in plan' if row['pinned_in_plan'] else ''}]")
        print(f"      {row['disposition']}")
        cur = row.get("current_attempt")
        if cur:
            print(f"      current attempt {cur['attempt']} ({cur['state']}): "
                  f"backend {cur['backend']}, agent {cur['agent_id']}")
    for line in result.get("notes") or []:
        print(f"  {line}")


def cmd_set_agent(args):
    """Switch the coding agent for attempts not yet created; plan.json untouched."""
    plan_path = Path(args.plan)
    try:
        plan_bytes = plan_path.read_bytes()
        plan = json.loads(plan_bytes.decode("utf-8"))
    except (OSError, ValueError) as exc:
        print(f"error: no readable plan at {args.plan}: {exc}", file=sys.stderr)
        return EXIT_USAGE
    if not isinstance(plan, dict) or not isinstance(plan.get("units"), list):
        print(f"error: {args.plan} has no units list", file=sys.stderr)
        return EXIT_USAGE
    digest_before = SPR.plan_digest(plan)
    SR.apply_agent_resolution(args.plan, plan=plan)
    units = {u.get("id"): u for u in plan["units"] if isinstance(u, dict)}
    if args.unit is not None:
        u = units.get(args.unit)
        if u is None:
            print(f"error: unknown unit {args.unit!r}", file=sys.stderr)
            return EXIT_USAGE
        if u.get("kind") != "code":
            print(f"error: unit {args.unit!r} is kind={u.get('kind')!r}; "
                  f"only code units have a coding agent", file=sys.stderr)
            return EXIT_USAGE
    _prepare_command_paths(args, plan=plan, read_only=args.show)
    if args.show:
        overrides, problem = SS.load_agent_overrides(args.state_dir)
        if problem:
            print(f"error: {problem}", file=sys.stderr)
            return EXIT_USAGE
        state = SS.load_state(args.state_dir)
        _print_set_agent({
            "revision": overrides["revision"], "plan_digest": digest_before,
            "project_override": overrides.get("project"),
            "unit_overrides": overrides.get("units") or {},
            "units": _set_agent_rows(plan, state, overrides, args.unit)},
            args.json)
        return EXIT_OK
    if args.clear and any(v is not None for v in (
            args.provider, args.backend, args.model, args.thinking, args.mode)):
        print("error: --clear takes no agent flags", file=sys.stderr)
        return EXIT_USAGE
    spec = None
    if not args.clear:
        spec, problem = _set_agent_target(args)
        if problem:
            print(f"error: {problem}", file=sys.stderr)
            return EXIT_USAGE
    ok, holder = SS.acquire_lease(args.state_dir, bump_epoch=False)
    if not ok:
        print(f"error: cannot take this project's lock: {holder}", file=sys.stderr)
        return EXIT_HALTED
    try:
        overrides, problem = SS.load_agent_overrides(args.state_dir)
        if problem:
            print(f"error: {problem}", file=sys.stderr)
            return EXIT_USAGE
        state = SS.load_state(args.state_dir)
        if args.unit is not None:
            st = ((state.get("units") or {}).get(args.unit) or {}).get("state")
            if st in SET_AGENT_TERMINAL:
                print(f"error: unit {args.unit!r} is {st}; it will not "
                      f"dispatch again, so an override would never apply",
                      file=sys.stderr)
                return EXIT_USAGE
        scope = f"unit:{args.unit}" if args.unit else "project"
        previous = ((overrides.get("units") or {}).get(args.unit)
                    if args.unit else overrides.get("project"))
        if args.clear and not previous:
            print(f"no {scope} override to clear; revision "
                  f"{overrides['revision']} unchanged")
            return EXIT_OK
        proposed = json.loads(json.dumps(overrides))
        if args.unit:
            if args.clear:
                proposed["units"].pop(args.unit, None)
            else:
                proposed["units"][args.unit] = spec
        else:
            proposed["project"] = None if args.clear else spec
        # FAIL EARLY: every non-terminal code unit must still be
        # dispatchable under the proposed resolution.
        problems = []
        if spec is not None:
            problem = SR.forbid_unit_provider_problem(
                plan, {"id": scope, "kind": "code", "provider": spec["provider"]})
            if problem:
                problems.append(problem)
            if spec["backend"] == LIO.LOR_BACKEND:
                problem = LIO.lor_availability()
                if problem:
                    problems.append(problem)
            elif CA.is_cli_backend(spec["backend"]):
                problem = CIO.availability(
                    spec["backend"], CA.parse_provider(spec["backend"], spec["provider"]))
                if problem:
                    problems.append(problem)
        for uid, u in sorted(units.items()):
            if u.get("kind") != "code" or (args.unit and uid != args.unit):
                continue
            st = ((state.get("units") or {}).get(uid) or {}).get("state")
            if st in SET_AGENT_TERMINAL:
                continue
            agent = SR.resolve_effective_agent(plan, u, proposed)
            problem = SR.effective_agent_problem(plan, agent)
            if problem:
                problems.append(problem)
        if problems:
            for problem in dict.fromkeys(problems):
                print(f"error: {problem}", file=sys.stderr)
            print("error: nothing was changed", file=sys.stderr)
            return EXIT_USAGE
        proposed["revision"] = int(overrides["revision"]) + 1
        proposed["history"].append({
            "revision": proposed["revision"], "scope": scope,
            "action": "clear" if args.clear else "set",
            "agent": spec, "previous": previous,
            "at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "by": os.environ.get("USER", "?")})
        error = SS.save_agent_overrides(args.state_dir, proposed)
        if error:
            print(f"error: {error}", file=sys.stderr)
            return EXIT_HALTED
        if (plan_path.read_bytes() != plan_bytes
                or SPR.plan_digest(plan) != digest_before):
            print("error: plan.json changed during set-agent; it must not",
                  file=sys.stderr)
            return EXIT_CONFLICT
        _print_set_agent({
            "revision": proposed["revision"], "plan_digest": digest_before,
            "action": f"{'cleared' if args.clear else 'set'} {scope}",
            "project_override": proposed.get("project"),
            "unit_overrides": proposed.get("units") or {},
            "units": _set_agent_rows(plan, state, proposed, args.unit),
            "notes": ["plan.json and its digest are unchanged; in-flight "
                      "attempts keep their frozen backend."]}, args.json)
        return EXIT_OK
    finally:
        SS.release_lease(args.state_dir)


def _append_promotion_receipt(state_dir, record):
    """Durably append one idempotent promotion receipt; return an error."""
    path = Path(state_dir) / PROMOTIONS
    key = record.get("promotion_key")
    try:
        if path.is_file() and key:
            for line in path.read_text().splitlines():
                try:
                    existing = json.loads(line)
                except ValueError:
                    continue
                if existing.get("promotion_key") == key:
                    # This may be recovery from a crash after the file fsync
                    # but before its directory fsync. Anchor the visible entry
                    # before allowing the pending intent to complete.
                    SJ._fsync_directory(path.parent)
                    return None
        created = not path.exists()
        with path.open("a") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        if created:
            # fsync(contents) does not make a newly-created filename durable.
            # Completion may be saved only after the parent records the entry.
            SJ._fsync_directory(path.parent)
        return None
    except OSError as exc:
        return str(exc)


def _redigest(attempt_dir, rel, recorded, accept_weak=False):
    """Re-derive one output's fingerprint NOW and compare with the receipt.

    The receipt is evidence about a moment; promotion happens later. Comparing
    like for like matters: a size-mtime record cannot certify content, so a
    match against one is reported as the weak thing it is."""
    src = Path(attempt_dir) / rel
    try:
        st = src.stat()
    except OSError as e:
        return False, f"{rel}: cannot stat it now ({e})"
    method = str(recorded.get("method") or "")
    # PROMOTION REFUSES WEAK EVIDENCE. Everywhere else a weak fingerprint is
    # reported as weak and allowed through; here it is not, because this is
    # the one place the tool writes to a path other people read. Four
    # reviewers found the old fallback: a size+mtime match cannot see a file
    # edited in place within the same mtime second, and for a DIRECTORY output
    # it cannot see anything at all.
    if "WEAK" in method or not recorded.get("sha256"):
        shown = method or "an unknown fingerprint"
        if not accept_weak:
            return False, (f"{rel}: the receipt holds only {shown}, which "
                           f"cannot establish that the content is unchanged. "
                           f"Promotion will not publish on that basis. "
                           f"Re-check the attempt to mint a stronger receipt, "
                           f"or pass --accept-weak-evidence to publish on "
                           f"size and mtime alone, which is recorded.")
        # Two reviewers found that this branch returned success without
        # comparing ANYTHING: st was bound from src.stat() and discarded, so a
        # file replaced with different content and a different size was
        # published while the record asserted a "size and mtime" match that
        # was never performed. A false statement in the audit trail of the one
        # outward-facing surface is worse than no record at all.
        if st.st_size != recorded.get("size"):
            return False, (f"{rel}: size changed since the verdict "
                           f"({recorded.get('size')} -> {st.st_size}). "
                           f"--accept-weak-evidence lowers the standard to "
                           f"size and mtime; it does not waive them.")
        if int(st.st_mtime) != recorded.get("mtime"):
            return False, (f"{rel}: mtime changed since the verdict. "
                           f"--accept-weak-evidence lowers the standard to "
                           f"size and mtime; it does not waive them.")
        return True, (f"{rel}: size and mtime match ({shown}). This does NOT "
                      f"establish the content is unchanged; accepted because "
                      f"--accept-weak-evidence was passed.")
    if recorded.get("sha256") and method.startswith("tree-digest"):
        now = U._tree_digest(src)
        if now.get("sha256") != recorded["sha256"]:
            return False, (f"{rel}: the directory tree changed since the "
                           f"verdict ({recorded['sha256'][:12]} -> "
                           f"{now.get('sha256','?')[:12]})")
        return True, f"{rel}: directory tree digest matches"
    if recorded.get("sha256") and method.startswith("content-digest"):
        try:
            digest, truncated = U.sha256_file(src)
        except OSError as e:
            return False, f"{rel}: cannot digest it now ({e})"
        if truncated:
            return False, (f"{rel}: the receipt holds a full content digest "
                           f"but the file now digests as truncated")
        if digest != recorded["sha256"]:
            return False, (f"{rel}: CONTENT CHANGED since the verdict "
                           f"({recorded['sha256'][:12]} -> {digest[:12]})")
        return True, f"{rel}: content digest matches"
    shown = method or "no usable fingerprint"
    return False, (f"{rel}: the receipt records {shown}, which promotion "
                   f"cannot verify against.")


def promote(plan, state, state_dir, uid, approver, approve,
            accept_weak=False):
    """Returns (lines, ok). Refuses loudly; copies only on explicit approval."""
    _d5_refuse('promote', state_dir)
    units = {u["id"]: u for u in plan["units"]}
    u = units.get(uid)
    if not u:
        return [f"no unit {uid!r} in this plan"], False
    dest_root, derr = SPR.resolve_promote_to(u.get("promote_to"),
                                         state.get("root"))
    if u.get("promote_to") and derr:
        return [f"REFUSING: unit {uid} declares promote_to but {derr}"], False
    if not dest_root:
        return ([f"unit {uid} declares no 'promote_to', so it has no shared "
                 f"destination and nothing to promote. Its outputs stay in the "
                 f"exclusive write root, which is the safe default."], False)

    us = state.get("units", {}).get(uid) or {}
    if us.get("state") != "DONE":
        return ([f"REFUSING: unit {uid} is {us.get('state') or 'unstarted'}, "
                 f"not DONE. Only a unit whose predicate returned DONE may be "
                 f"promoted; promoting on any weaker basis is the false pass "
                 f"this repo exists to prevent."], False)
    attempt = us.get("attempt_dir")
    # ATTESTED. Promotion copies artifacts into a shared canonical tree on a
    # human's approval, and this receipt is both the evidence it rests on and
    # what that human is shown. Read unattested, an agent could write its own
    # DONE receipt and satisfy the gate with a claim about its own work.
    receipt, err = SE.attested_receipt(state, uid, attempt)
    if err or not isinstance(receipt, dict):
        return ([f"REFUSING: no admissible receipt for unit {uid} ({err}). "
                 f"The receipt is the evidence promotion rests on, and it "
                 f"counts only when this coordinator caused the check that "
                 f"wrote it."], False)
    if receipt.get("state") != "DONE":
        return ([f"REFUSING: the receipt for unit {uid} says "
                 f"{receipt.get('state')!r}, not DONE."], False)

    recorded = receipt.get("outputs") or {}
    if not recorded:
        return ([f"REFUSING: the receipt for unit {uid} records no output "
                 f"fingerprints, so a change since the verdict could not be "
                 f"detected. Re-run `unit.py check` on the attempt to record "
                 f"them, then promote."], False)

    lines, ok = [f"unit {uid}, attempt {Path(attempt).name}", ""], True
    weak = False
    for rel, rec in sorted(recorded.items()):
        good, why = _redigest(attempt, rel, rec, accept_weak)
        lines.append(f"  {'ok ' if good else 'NO '} {why}")
        ok = ok and good
        weak = weak or (good and "does NOT establish" in why)
    if not ok:
        lines += ["", f"REFUSING to promote {uid}: the outputs are not what "
                      f"the receipt describes. Re-run the unit, or re-check "
                      f"the attempt to mint a receipt for what is there now."]
        return lines, False

    # Versioned directory + one pointer swap. Renaming into place is NOT
    # atomic across filesystems, and a shared canonical tree is usually a
    # different mount from the run root, so a half-copied output would appear
    # under the canonical name. Copy into a version, then swap a symlink.
    dest = Path(dest_root) / uid
    version = dest / Path(attempt).name
    current = dest / "current"
    lines += ["", f"  destination : {version}",
              f"  pointer     : {current} -> {Path(attempt).name}"]
    if weak:
        lines.append("  NOTE: at least one output matched only on size and "
                     "mtime, which does not establish unchanged content.")
    if not approve:
        lines += ["", "DRY RUN. Nothing was copied. Approve with:",
                  f"    swarm.py promote <plan> --unit {uid} --approve "
                  f"--approver <name>"]
        return lines, True
    if not approver:
        lines += ["", "REFUSING: --approve requires --approver <name>. The "
                      "record must name who accepted this."]
        return lines, False

    promotion_key = hashlib.sha256(
        f"{uid}\x00{Path(attempt).name}\x00{version}".encode()).hexdigest()[:16]
    promotion_intent = {
        "key": promotion_key, "unit": uid,
        "attempt": Path(attempt).name, "destination": str(version),
        "current": str(current), "approver": approver,
        "status": "pending",
    }
    state.setdefault("promotion_intents", {})[promotion_key] = promotion_intent
    # Copying into a shared tree and swapping its public pointer are the one
    # promotion act that cannot be rolled back by coordinator state. Persist
    # the exact approved destination first; a crash can then resume the same
    # idempotent version rather than leaving an unexplained publication.
    SS.save_state(state_dir, state)

    try:
        missing_directories = []
        cursor = version.parent
        while not cursor.exists() and cursor != cursor.parent:
            missing_directories.append(cursor)
            cursor = cursor.parent
        version.parent.mkdir(parents=True, exist_ok=True)
        # mkdir(parents=True) can create both the promotion root and the unit
        # directory. Persist every new directory entry from the first created
        # ancestor down; fsyncing only the leaf does not anchor its own name.
        for created in reversed(missing_directories):
            SJ._fsync_directory(created.parent)
        if version.exists():
            # ALREADY PUBLISHED. The earlier version of this deleted it and
            # recopied, which destroys data other people may already be
            # reading and leaves `current` pointing at nothing during the gap.
            # A version directory is named by attempt id and its contents were
            # digest-verified when it was written, so re-promoting the same
            # attempt is a no-op. Re-point and stop.
            lines.append(f"  already published at {version}; not rewriting it")
        else:
            staging = dest / f".staging-{Path(attempt).name}-{os.getpid()}"
            if staging.exists():
                shutil.rmtree(staging)
            staging.mkdir(parents=True)
            for rel in sorted(recorded):
                src, dst = Path(attempt) / rel, staging / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                if src.is_dir():
                    shutil.copytree(src, dst)
                else:
                    shutil.copy2(src, dst)
            # RE-VERIFY THE COPY. Fingerprints were checked before copying,
            # and an ordinary concurrent writer on a shared filesystem can
            # change a source file in between, so what landed in staging is
            # the only thing worth trusting. Reviewers found this window; it
            # is closed by checking the copy rather than by hoping.
            for rel, rec in sorted(recorded.items()):
                good, why = _redigest(staging, rel, rec, accept_weak)
                if not good:
                    shutil.rmtree(staging, ignore_errors=True)
                    return lines + ["", f"REFUSING: the copy does not match "
                                        f"the receipt, so the source changed "
                                        f"while it was being read. {why}",
                                    "Nothing was published and the canonical "
                                    "pointer did not move."], False
            # Fully populated AND verified before it takes the canonical name,
            # so a reader never sees a partial or altered version directory.
            # `staging` and `version` are siblings by construction, making
            # this one same-filesystem atomic rename. If the filesystem
            # refuses that guarantee (for example EXDEV), os.replace fails
            # and the canonical version is not published; there is no
            # cross-filesystem copy fallback that could expose a half-copy.
            os.replace(staging, version)      # same directory: atomic
        # Persist either the new version rename or a version recovered from a
        # crash after rename but before this fsync.
        SJ._fsync_directory(version.parent)
        tmp_link = dest / f".current-{os.getpid()}"
        if tmp_link.is_symlink() or tmp_link.exists():
            tmp_link.unlink()
        os.symlink(Path(attempt).name, tmp_link)
        os.replace(tmp_link, current)         # atomic pointer swap
        SJ._fsync_directory(current.parent)
    except OSError as e:
        return lines + ["", f"REFUSING: promotion failed or could not be "
                            f"made durable ({e}). No completion was recorded; "
                            f"inspect version/current and rerun this exact "
                            f"promotion."], False

    record = {"unit": uid, "attempt": Path(attempt).name,
              "promotion_key": promotion_key,
              "promoted_to": str(version), "approver": approver,
              "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
              "outputs": recorded,
              "digest_basis": ("size-mtime only for at least one output" if weak
                               else "content digest for every output")}
    receipt_error = _append_promotion_receipt(state_dir, record)
    if receipt_error:
        # Publication already happened, so do not claim it did not. Keep the
        # durable intent pending and return failure: a rerun recognizes the
        # version, records this same keyed receipt, then marks it complete.
        lines += [f"  WARNING: promoted, but its receipt could not be "
                  f"persisted: {receipt_error}",
                  "  The promotion intent remains pending; rerun this exact "
                  "promotion to finish its audit record."]
        return lines, False
    promotion_intent["status"] = "complete"
    promotion_intent["completed_at"] = record["at"]
    SS.save_state(state_dir, state)
    lines += ["", f"PROMOTED {uid} -> {version}", f"approved by {approver}"]
    return lines, True


def cmd_promote(args):
    _d5_refuse('cmd_promote', getattr(args, "state_dir", None), plan=getattr(args, "plan", None))
    plan, err = U.read_json(args.plan)
    if err:
        sys.exit(f"error: no readable plan at {args.plan}: {err}")
    _prepare_command_paths(args, plan=plan)
    state = SS.load_state(args.state_dir)
    lines, ok = promote(plan, state, args.state_dir, args.unit,
                        args.approver, args.approve,
                        getattr(args, "accept_weak_evidence", False))
    for line in lines:
        print(f"  {line}" if line else "")
    return EXIT_OK if ok else EXIT_FAILED_UNIT


# Every field a unit may carry, what requires it, and what it couples to.
# ONE read instead of five successive refusals.
#
# Getting one moved unit valid took five dispatch attempts: repo, then branch,
# then the account/canary mismatch, then the canary-ancestor rule, then
# overlapping write scopes. Each message was precise about its own rule and
# said nothing about the next one, so the coupling was learned serially, and a
# field that does not exist got invented along the way from guessing at shape.
# Error messages teach one rule at a time by construction; a schema teaches the
# shape at once.
SCHEMA_PLAN_FIELDS = [
    ("canary", "all", "optional",
     "string id of one root unit (no needs). Every other unit implicitly "
     "needs it: wait for DONE; FAILED, FAILED_EVIDENCE or HELD holds the "
     "others through ordinary upstream-failure handling. Omit for normal "
     "fan-out. This does not replace runtime verification."),
    ("environments", "all", "optional",
     "object of env_id -> {path, python, lock_file, build[, model_line, kind]}. "
     "Declares one dedicated Python environment per model/code line plus one "
     "dedicated orchestration environment (kind='orchestration' or key "
     "'orchestration', where lock_file may be 'none' for stdlib-only scripts). "
     "Shared personal/system envs (conda base, workbook, /usr/bin, etc.) and "
     "two model lines sharing one env are refused."),
    ("orchestrator", "all", "optional",
     "orchestrator host identity (claude | codex | antigravity). When present "
     "and mapped in agents.json default_by_orchestrator, code units omitting "
     "provider default to that orchestrator's seat (and its resolved project "
     "snapshot). When absent, fails closed to agents.json default.provider and "
     "preserves legacy plan digests. Refuses code units violating "
     "agents.json forbid_when_orchestrator."),
]

SCHEMA_FIELDS = [
    ("id", "all", "required", "unique; names the attempt directory and the "
     "env var SWARM_DEP_<ID>"),
    ("kind", "all", "required", "slurm | pipeline | code; fixes what closes "
     "the unit and cannot be overridden per plan"),
    ("tracker", "all", "optional",
     "non-empty issue string, e.g. ARC-698. Labels tracker intents only; "
     "never admission, closure or DONE authority. Omission preserves the "
     "plan digest and intent keys"),
    ("command", "slurm, pipeline", "required",
     "the WORK. Never sbatch/srun/salloc: the coordinator submits it. An "
     "absolute path or glob in its ARGUMENTS is refused only when its "
     "parent directory is visible on THIS host and does not contain a match; "
     "a path under a mount only the compute node has is not refused, and the "
     "program in the first token is never checked"),
    ("prompt", "code", "required",
     "the agent's instruction, and the runner's last positional argument. A "
     "paseo flag at its start is refused; configuration goes in fields"),
    ("outputs", "all", "required",
     "RELATIVE to the attempt write root. The predicate looks nowhere else. "
     "Refused with --array, which fans N writers into one directory"),
    ("inputs", "all", "optional",
     "checked for existence, placeholders and upstream production"),
    ("needs", "all", "optional",
     "DAG edges; a unit dispatches only after every dependency is DONE"),
    ("repo", "code", "required", "closure is a merged PR; without it DONE is "
     "unreachable"),
    ("target_branch", "code", "required",
     "the destination of the attempt's pull request. The coordinator creates "
     "a separate swarm-<attempt> source branch; legacy branch is not reused"),
    ("seed", "code", "optional",
     "JSON object: ref (refs/heads/...), base and head (full 40/64-hex commit "
     "ids), optional evidence path. Dispatch requires base <= head <= the "
     "fetched origin push-destination ref. Raw provenance is recorded in "
     "coordinator launch intent; the worker fetches and cherry-picks -x, "
     "skipping empty commits. No whole-file checkout. The coordinator does "
     "not replay commits; judging, closure and scope-check are unchanged. "
     "Relative evidence paths use the source repository directory"),
    ("mode", "code", "required",
     "no default on purpose. Absent or empty means default permissions, so "
     "the agent stalls at its first write"),
    ("provider", "code", "optional",
     "default is agents.json default_by_orchestrator[orchestrator] when plan "
     "declares orchestrator, else agents.json default.provider"),
    ("model", "code", "optional", "overrides the provider's default"),
    ("backend", "code", "optional",
     "paseo, lor, claude, codex or pi. A direct CLI (claude/codex/pi) is "
     "selected only here, in code_agent or by set-agent --backend: provider "
     "claude/<m> alone stays Paseo. pi takes model <provider>/<model>; "
     "provider openrouter needs OPENROUTER_API_KEY coordinator-side. 'mode' "
     "is optional and a restrictive one is refused"),
    ("author_history", "code", "optional",
     "JSON list of objects with exactly unit, attempt (non-empty strings), "
     "and identity (provider/model or null). Coordinator-recorded potential "
     "contributors; carried across attempts and continuations. Metadata only; "
     "no effect on admission, judgment or rendered prompts"),
    ("thinking", "code", "optional",
     "default from agents.json. JSON null or \"\" suppresses the flag; the STRING "
     "\"null\" is refused"),
    ("env", "code", "optional", "list of KEY=VALUE passed to the agent"),
    ("continuation", "code", "optional",
     '{"max": N, "prompt": "..."}; bounded nudges when it settles without '
     "producing. Exhaustion FAILS the unit"),
    ("provider_resume", "code", "optional",
     '{"max": N}; integer 0..3, default off. Same-attempt provider-error sends; '
     "5 minute initial / 15 minute later backoff, no replay or review-round reset"),
    ("requires_verification", "code", "optional",
     "claims an authorized verifier must establish before closing. The "
     "reserved integration-tests claim runs in a disposable candidate merge "
     "and also binds the pre-merge target commit"),
    ("runtime", "slurm, pipeline", "required",
     'inline or a "runtimes" id, or the literal "none". Declares resolution, '
     "entrypoint, probe and verified_by; optional environment/model_line"),
    ("environment", "all", "optional",
     'id from plan "environments" (or "none" for non-Python code units). '
     "Required on code units when plan declares 'environments'. Must not "
     "target the orchestration env or a shared personal/system env"),
    ("model_line", "all", "optional",
     "logical model or code line name (for example STAGE or MaxToki); "
     "validate refuses two distinct model lines sharing one environment"),
    ("isolation", "slurm, pipeline", "optional",
     '{"kind": "container", "backend": "apptainer", "image": "...", '
     '"writable": ["$SWARM_UNIT_DIR"], "read_only": [...]}. Restricts the '
     "dispatched workload's writable HOST binds to the attempt root and "
     "binds every declared input read-only. It does not create another Unix "
     "identity or isolate other same-UID host processes, PIDs, or networking"),
    ("sbatch", "slurm", "optional",
     "a LIST of scheduler flags. A string is iterated character by "
     "character. --mem is required when the survey reports "
     "mem_flag_required, and --account is checked against the surveyed "
     "allow/deny lists of the partition --partition names"),
    ("write_scopes", "all", "optional",
     "must not overlap between concurrent units. Names FILES; does NOT "
     "isolate a code unit's repository"),
    ("scope", "all", "optional",
     "JSON list of repo-relative, case-sensitive fnmatch globs; no absolute "
     "paths or '..' segments. Wildcards, including **, span any depth. "
     "An empty list allows no changed paths. scope-check compares the code "
     "attempt's recorded base and judged head locally; it is an advisory "
     "merge precondition and never blocks advance or changes closure"),
    ("workspace_policy", "slurm, pipeline", "optional",
     '{"requires_clean_git": true, "path": "/checkout"}; opt-in launch '
     "preflight for a non-code unit"),
    ("promote_to", "all", "optional",
     "where verified outputs are published. Needs a named approver. "
     "REQUIRED of a unit whose outputs include findings.json, which the "
     "report reads from the project directory rather than the write root"),
    ("max_attempts", "all", "optional",
     "default 1. Above 1 requires a retry contract with max_lost"),
    ("deadline_s", "all", "optional",
     "positive finite seconds measured from allocated_at, the coordinator-"
     "written allocation timestamp. A breach becomes NEEDS_HUMAN with "
     "reason deadline_exceeded; it does not cancel work, release an output "
     "claim, or mint a retry"),
    ("retry", "all", "optional",
     '{"mode": "restart", "max_lost": {...}}. "resume" is REFUSED'),
    ("gpu_hours", "all", "optional", "charged against the plan's budget"),
    ("pool", "all", "optional", "must be declared in limits.pools"),
    ("converge", "slurm, pipeline", "optional",
     '{"metrics": "metrics.jsonl", "criterion": {...}, "diverge": [...], '
     '"budget": N}. Scores a criterion over the metrics SERIES and gates '
     "DONE: a run that spent its budget without meeting it is NEEDS_HUMAN, "
     "not DONE, so it closes no ticket and satisfies no dependent. The "
     "metrics file must also be a declared output, and the criterion is read "
     "from the plan, never from the attempt directory"),
]

# What couples to what, stated once. These are the rules that only announce
# themselves as a refusal.
SCHEMA_COUPLINGS = [
    "A runtime canary must match runtime identity, partition AND account, "
    "and be a DAG ancestor of it. A plan spanning two partitions needs one "
    "canary per partition.",
    "A runtime canary must run the runtime's declared probe command verbatim; a "
    "canary running `true` establishes nothing.",
    "Concurrent units must have disjoint write_scopes; order them with needs "
    "if they overlap.",
    "max_attempts above 1 needs retry.max_lost in a metric the plan also caps "
    "in retry_limits.",
    "An input is satisfied by an upstream unit's output only when that unit "
    "is an ancestor.",
    "converge.metrics must also appear in outputs, so the predicate checks "
    "the file exists before convergence is judged over it.",
    "converge is refused on kind=code: a code unit is closed by a merged pull "
    "request and has no metrics series.",
    "A slurm unit must state its memory (--mem, --mem-per-cpu or "
    "--mem-per-gpu) when the survey reports mem_flag_required. With no "
    "survey to read, that fact is unknown and nothing is refused on it.",
    "A slurm unit's --account must not be denied by, nor missing from a set "
    "allow_accounts of, the partition it names, as the survey recorded them. "
    "unknown on a field decides nothing; the other field still decides.",
    "A unit declaring findings.json must declare promote_to. Outputs stay in "
    "the attempt write root and the report reads findings.json from the "
    "project directory, so an unpromoted one is read by nobody.",
    "Nothing may run in a shared personal/system Python env (such as conda "
    "base or workbook): validate refuses shared interpreter paths, units "
    "resolving outside declared plan environments, workload units targeting "
    "the orchestration environment, and two model lines sharing one env.",
]


def cmd_schema(args):
    """Print plan/unit fields, when required, and what they couple to."""
    if args.json:
        print(json.dumps(
            {"fields": [{"field": f, "kinds": k, "requirement": r,
                         "notes": n} for f, k, r, n in SCHEMA_FIELDS],
             "plan_fields": [{"field": f, "kinds": k, "requirement": r,
                              "notes": n} for f, k, r, n in SCHEMA_PLAN_FIELDS],
             "couplings": SCHEMA_COUPLINGS}, indent=2))
        return EXIT_OK
    print("  PLAN FIELDS\n")
    for field, kinds, req, note in SCHEMA_PLAN_FIELDS:
        print(f"  {field}  {req}  {kinds}")
        for line in _wrap(note, 66):
            print(f"    {line}")
        print()
    width = max(len(f) for f, _k, _r, _n in SCHEMA_FIELDS)
    print("  UNIT FIELDS\n")
    for field, kinds, req, note in SCHEMA_FIELDS:
        print(f"  {field:<{width}}  {req:<8}  {kinds}")
        for line in _wrap(note, 66):
            print(f"  {'':<{width}}  {'':<8}  {line}")
        print()
    print("  COUPLINGS between fields\n")
    for c in SCHEMA_COUPLINGS:
        lines = _wrap(c, 72)
        print(f"  - {lines[0]}")
        for line in lines[1:]:
            print(f"    {line}")
    return EXIT_OK


def _wrap(text, width):
    words, lines, cur = text.split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return lines or [""]


def _scope_git(repo, *args):
    """Read local Git objects, preserving filename bytes and denying fetches."""
    env_program = shutil.which("env", path=os.defpath)
    if not env_program:
        raise PlanError("system env executable is unavailable")
    argv = [env_program]
    for key in os.environ:
        if key.startswith("GIT_"):
            argv.extend(("-u", key))
    argv.extend(("GIT_CONFIG_NOSYSTEM=1", f"GIT_CONFIG_GLOBAL={os.devnull}",
                 "GIT_NO_LAZY_FETCH=1", "GIT_ALLOW_PROTOCOL=",
                 "GIT_TERMINAL_PROMPT=0", "git", "--no-replace-objects",
                 "-C", repo))
    try:
        result = subprocess.run(
            argv + list(args),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=CE.child_env(), timeout=60, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise PlanError(f"local Git read unavailable: {exc}")
    if result.returncode:
        raise PlanError("local Git read failed: " + W.render_git_diagnostic(
            result.returncode, result.stderr.decode("utf-8", "replace")))
    return result.stdout.decode("utf-8", "surrogateescape")


def _scope_changed_paths(repo, base, head):
    """Read both rename endpoints; no quoting, whitespace folding or filters."""
    for commit in (base, head):
        if _scope_git(repo, "cat-file", "-t", commit).strip() != "commit":
            raise PlanError("scope-check basis is not a commit object")
    raw = _scope_git(repo, "diff", "--name-status", "-z", "-M",
                     "--no-ext-diff", "--no-textconv", "--no-relative",
                     "--ignore-submodules=none", "--no-color",
                     base, head, "--")
    if not raw:
        return [], []
    if not raw.endswith("\0"):
        raise PlanError("incomplete local Git name-status output")
    fields = iter(raw[:-1].split("\0"))
    changed, removed = [], []
    try:
        for status in fields:
            if not re.fullmatch(r"[ADMTUXB]|[RC][0-9]+|M[0-9]+", status):
                raise PlanError(f"unknown local Git change status {status!r}")
            path = next(fields)
            changed.append(path)
            if status == "D" or status.startswith("R"):
                removed.append(path)
            if status.startswith(("R", "C")):
                changed.append(next(fields))
    except StopIteration:
        raise PlanError("incomplete local Git name-status record")
    return changed, removed


def cmd_scope_check(args):
    """Advisory merge precondition; reads state, never judges or mutates it."""
    worker_head = getattr(args, "head", None)
    report = {"unit": args.unit, "status": "unchecked", "attempt": None,
              "base": None, "head": None, "scope": None,
              "head_source": "worker" if worker_head is not None else "judged",
              "repository": None, "target": None, "state_epoch": None,
              "out_of_scope": [], "deletions_out_of_scope": []}
    code = EXIT_SCOPE_UNCHECKED
    try:
        plan, error = U.read_json(args.plan)
        if error or not isinstance(plan, dict):
            raise PlanError(f"no readable plan: {error or 'not an object'}")
        units = plan.get("units")
        if not isinstance(units, list):
            raise PlanError("plan units must be a JSON list")
        matches = [u for u in units if isinstance(u, dict)
                   and u.get("id") == args.unit]
        if len(matches) != 1:
            raise PlanError("scope-check needs one matching plan unit")
        u = matches[0]
        patterns = SPR.declared_scope(u)
        report["scope"] = patterns
        if u.get("kind") != "code":
            raise PlanError("unit has no code-attempt judgment")
        # Explicit state-dir: validate containment without default migration.
        state_dir, _root, _trees = CP.resolve_paths(
            args.state_dir, plan=plan, cwd=os.getcwd())
        epoch, error = _read_state_epoch(state_dir)
        if error:
            raise PlanError(error)
        report["state_epoch"] = epoch
        state, error = U.read_json(state_dir / STATE_FILE)
        if error or not isinstance(state, dict):
            raise PlanError(f"no readable coordinator state: "
                            f"{error or 'not an object'}")
        us = (state.get("units") or {}).get(args.unit) or {}
        attempt_dir = us.get("attempt_dir")
        if not isinstance(attempt_dir, str) or not attempt_dir:
            raise PlanError("no current attempt in coordinator state")
        attempt = Path(attempt_dir).name
        report["attempt"] = attempt
        intent = (us.get("attempt_launch_intents") or {}).get(attempt)
        problem = SCL._code_launch_intent_problem(intent, u, attempt)
        if problem:
            raise PlanError(problem)
        base, repo = intent["base_commit"], intent["repo"]
        head = SE.trusted_produced_head(state, args.unit, attempt_dir)
        if worker_head is not None:
            # Resolve in the recorded repository, never the operator's cwd.
            # This advisory observation is not written to produced-head state.
            CP.resolve_paths(args.state_dir, plan=plan, cwd=os.getcwd(),
                             extra_repos=[repo])
            head = _scope_git(repo, "rev-parse", "--verify",
                              worker_head + "^{commit}").strip()
        report.update({"base": base, "head": head,
                       "repository": intent.get("repository_remote"),
                       "target": intent["target_branch"]})
        if patterns is None:
            raise PlanError("unit declares no scope")
        if (not isinstance(head, str)
                or not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", head)):
            raise PlanError("no valid judged head for this attempt in "
                            "coordinator state")
        if not isinstance(repo, str) or not repo:
            raise PlanError("no repository in coordinator launch intent")
        CP.resolve_paths(args.state_dir, plan=plan, cwd=os.getcwd(),
                         extra_repos=[repo])
        changed, removed = _scope_changed_paths(repo, base, head)
        outside = lambda path: not any(fnmatch.fnmatchcase(path, pattern)
                                       for pattern in patterns)
        report["out_of_scope"] = sorted(set(filter(outside, changed)))
        report["deletions_out_of_scope"] = sorted(set(filter(outside, removed)))
        report["status"] = ("out_of_scope" if report["out_of_scope"]
                            else "in_scope")
        code = EXIT_SCOPE_OUTSIDE if report["out_of_scope"] else EXIT_OK
    except (PlanError, CP.PathPolicyError, TypeError, ValueError,
            AttributeError) as exc:
        report["reason"] = str(exc)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(f"scope-check {args.unit!r}: {report['status']}")
        if report.get("reason"):
            print(f"  unchecked: {report['reason']}")
        for path in report["out_of_scope"]:
            print("  out of scope: " + json.dumps(path))
        for path in report["deletions_out_of_scope"]:
            print("  deletion or rename-away out of scope: " + json.dumps(path))
    return code


def _integration_verification_fence(state_dir, unit, receipt=None):
    """Check coordinator merge intents under the lease, optionally publishing.

    The generic verifier does not interpret resolution chains. Once this unit
    has a merge intent, use the connected operator to resolve it and verify.
    Check both before execution and at publication, never holding the lease
    across the verifier. No connected/network module enters the coordinator.
    """
    _d5_refuse('_integration_verification_fence', state_dir)
    ok, holder = SS.acquire_lease(state_dir)
    if not ok:
        return "integration verification cannot acquire coordinator lock: " + str(holder)
    try:
        for path in sorted(Path(state_dir).glob("merge-unit-*.json")):
            intent, error = U.read_json(path)
            if (error or not isinstance(intent, dict)
                    or not isinstance(intent.get("binding"), dict)
                    or not isinstance(intent["binding"].get("unit"), str)):
                return "integration verification cannot read merge intent: " + str(path)
            if intent["binding"]["unit"] == unit:
                return "integration verification refused: merge intent exists for unit " + unit
        if receipt is not None:
            SE.load_verifications(state_dir)
            SJ._fsync_append(Path(state_dir) / SE.VERIFY_RECEIPTS, receipt)
    except (OSError, OutboxError) as exc:
        return "integration verification publication refused: " + str(exc)
    finally:
        SS.release_lease(state_dir)
    return None


def cmd_verify(args):
    """Run an authorized, content-pinned verifier and record what it said.

    Everything that makes this admissible happens HERE, in one place, and the
    receipt records all of it: the policy came from the anchored base commit,
    the verifier's bytes hashed to what the policy authorized, those exact
    bytes ran, and the head it ran against is the one this attempt produced.
    """
    _d5_refuse('cmd_verify', getattr(args, "state_dir", None), plan=getattr(args, "plan", None))
    _prepare_command_paths(args)
    if args.claim == V.INTEGRATION_CLAIM:
        problem = _integration_verification_fence(args.state_dir, args.unit)
        if problem:
            sys.stderr.write(f"error: {problem}\n")
            return EXIT_CONFLICT
    state = SS.load_state(args.state_dir) or {}
    launch_facts = SS.trusted_launch_facts(
        state, args.unit, args.attempt)
    if not launch_facts:
        sys.stderr.write(
            f"error: coordinator state records no complete launch snapshot "
            f"for unit {args.unit!r}, attempt {Path(args.attempt).name!r}. "
            f"Re-dispatch it; verification may not reconstruct repository "
            f"identity or a base from the launch record.\n")
        return EXIT_USAGE
    repo = launch_facts["repo"]
    base = launch_facts["base_commit"]
    _prepare_command_paths(args, extra_repos=[repo])
    try:
        SE.load_verifications(args.state_dir)
    except OutboxError as exc:
        sys.stderr.write(f"  VERIFICATION JOURNAL: {exc}\n")
        return EXIT_CONFLICT

    if not repo:
        sys.stderr.write("error: this attempt anchored no repository, so "
                         "there is no base to read a policy from.\n")
        return EXIT_USAGE

    # Attempt identity is part of the lookup. Check it before touching a
    # verifier path so a request for the wrong attempt fails for the actual
    # authority defect and cannot be obscured by an unrelated file error.
    produced = SE.trusted_produced_head(state, args.unit, args.attempt)
    if not produced:
        sys.stderr.write(
            f"error: coordinator state records no judged produced commit for "
            f"attempt {Path(args.attempt).name!r}. Re-run that attempt; do "
            f"not recover a basis from another attempt, its receipt, or the "
            f"current branch.\n")
        return EXIT_USAGE
    runner = U.run
    executables = None
    if args.claim == V.INTEGRATION_CLAIM:
        try:
            execution_policy, execution_digest = V.RV.read_policy(args.state_dir)
            if execution_policy.get("verification_host"):
                sys.stderr.write("error: remote merge verification requires merge_unit.py --verify-integration\n")
                return EXIT_USAGE
            for executable in execution_policy.get("local", {}).values():
                if CP._inside(executable, repo):
                    sys.stderr.write("error: verification executable is inside the operated repository\n")
                    return EXIT_USAGE
            executables = V.RV.resolve_executables(execution_policy.get("local"), excluded_roots=(repo,))
            runner = V.RV.GitRunner(U.run, executables)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            sys.stderr.write("error: verification executables unavailable: {}\n".format(exc))
            return EXIT_USAGE
    basis_problem = W.validate_pinned_head(runner, launch_facts, produced)
    if basis_problem:
        sys.stderr.write(f"error: {basis_problem}\n")
        return EXIT_USAGE

    policy, policy_digest, perr = V.read_policy(runner, repo, base)
    if perr:
        sys.stderr.write(f"error: {perr}\n")
        return EXIT_USAGE

    digest, _size, derr = V.digest_file(args.path)
    if derr:
        sys.stderr.write(f"error: {derr}\n")
        return EXIT_USAGE

    entry, refusal = V.authorized(policy, args.verifier, digest, args.claim)
    if refusal:
        sys.stderr.write(f"error: {refusal}\n")
        return EXIT_USAGE

    corpus_evidence, refusal = V.corpus_evidence(
        runner, repo, base, produced, entry)
    if refusal:
        sys.stderr.write(f"error: {refusal}\n")
        return EXIT_USAGE
    refusal = V.corpus_change_refusal(entry, corpus_evidence, args.claim)
    if refusal:
        sys.stderr.write(f"error: {refusal}\n")
        return EXIT_FAILED_UNIT

    merge_evidence = {}
    if args.claim == V.INTEGRATION_CLAIM:
        target_commit = getattr(args, "target_commit", None)
        if not target_commit:
            sys.stderr.write(
                "error: integration-tests requires --target-commit. Supply "
                "that commit's Git object locally; this command never "
                "contacts a forge.\n")
            return EXIT_USAGE
        outcome, merge_evidence, rerr = V.run_in_candidate_merge(
            runner, repo, produced, target_commit, args.path, digest,
            args=args.arg, timeout=args.timeout, executables=executables,
            state_dir=args.state_dir, unit=args.unit,
            journal_entries=SE.load_verifications(args.state_dir)[0])
        if V.RV.read_policy(args.state_dir)[1] != execution_digest:
            sys.stderr.write("error: execution policy changed during verification\n")
            return EXIT_CONFLICT
    else:
        if getattr(args, "target_commit", None):
            sys.stderr.write(
                "error: --target-commit applies only to the "
                "integration-tests claim. Ordinary verification remains "
                "bound only to the produced head.\n")
            return EXIT_USAGE
        outcome, rerr = V.run_in_checkout(
            U.run, repo, produced, args.path, digest, args=args.arg,
            timeout=args.timeout)
    if rerr:
        sys.stderr.write(f"error: {rerr}\n")
        return EXIT_FAILED_UNIT

    rec = {"unit": args.unit, "claim": args.claim, "verifier": args.verifier,
           "verifier_sha256": digest, "policy_sha256": policy_digest,
           "subject_head": produced,
           **V.outcome_result(outcome),
           "exit_code": outcome["exit_code"],
           "stdout_tail": outcome["stdout"], "stderr_tail": outcome["stderr"],
           "by": os.environ.get("USER") or "?",
           "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "schema_version": 1}
    rec.update(corpus_evidence)
    rec.update(merge_evidence)
    if outcome.get("execution") is not None:
        rec.update(execution=outcome["execution"], schema_version=2)
    bad = SE._verify_shape_problem(rec)
    if bad:
        sys.stderr.write(f"error: this would not be admissible: {bad}\n")
        return EXIT_USAGE
    if args.claim == V.INTEGRATION_CLAIM:
        problem = _integration_verification_fence(args.state_dir, args.unit, rec)
        if problem:
            sys.stderr.write(f"error: {problem}\n")
            return EXIT_CONFLICT
    else:
        SJ._fsync_append(Path(args.state_dir) / SE.VERIFY_RECEIPTS, rec)
    print(f"  {args.claim}: {rec['result'].upper()} (exit "
          f"{outcome['exit_code']}) for {produced[:12]}")
    print(f"  verifier {args.verifier} {digest[:12]}, policy "
          f"{policy_digest[:12]} from base {str(base)[:12]}")
    return EXIT_OK if rec["result"] == "pass" else EXIT_FAILED_UNIT


def cmd_merge(args):
    """Record that a PR for this unit was observed merged.

    Run from a machine that can see the PR. This is an ATTESTATION, like the
    tracker acknowledgment: nothing here checked GitHub, and nothing can. What
    the coordinator checks is that the head you pin is the head it judged this
    attempt to have produced.
    """
    _d5_refuse('cmd_merge', getattr(args, "state_dir", None), plan=getattr(args, "plan", None))
    _prepare_command_paths(args)
    try:
        SE.load_merge_receipts(args.state_dir)     # refuse to extend a bad journal
    except OutboxError as exc:
        sys.stderr.write(f"  MERGE JOURNAL: {exc}\n")
        return EXIT_CONFLICT

    rec = {"unit": args.unit, "repo": args.repo or "", "pr": args.pr,
           "target": args.target, "head": args.head,
           "merged_as": args.merged_as, "method": args.method,
           "merged": True, "attested": True,
           "by": os.environ.get("USER") or "?",
           "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "schema_version": 1}
    target_commit = getattr(args, "target_commit", None)
    if target_commit:
        rec["target_commit"] = target_commit
    integration_status = getattr(args, "integration_status", None)
    if integration_status:
        # Audit label supplied by the connected merge operator, never new
        # verification authority. Claim admission still reads verify receipts.
        rec["integration_status"] = integration_status
    bad = SE._merge_shape_problem(rec)
    if bad:
        sys.stderr.write(f"error: this would not be admissible: {bad}\n")
        return EXIT_USAGE
    SJ._fsync_append(Path(args.state_dir) / SE.MERGE_RECEIPTS, rec)
    print(f"  recorded: {args.unit} merged as {args.merged_as[:12]} "
          f"(head {args.head[:12]}, {args.method})")
    print("  This is the attester's word. The coordinator will admit it only "
          "if\n  that head is the one this attempt produced.")
    return EXIT_OK


def cmd_seq_floor(args):
    """Record a sequence floor under lease."""
    _d5_refuse('cmd_seq_floor', getattr(args, "state_dir", None), plan=getattr(args, "plan", None))
    _prepare_command_paths(args)
    ok, holder = SS.acquire_lease(args.state_dir)
    if not ok:
        print(f"cannot take this project's lock: {holder}", file=sys.stderr)
        return EXIT_HALTED
    try:
        record = SO.set_sequence_floor(
            args.state_dir, args.floor, args.approver, args.reason)
    except (OSError, ValueError, OutboxError) as exc:
        print(f"error: seq-floor refused: {exc}", file=sys.stderr)
        return EXIT_USAGE
    finally:
        SS.release_lease(args.state_dir)
    print(json.dumps(record, sort_keys=True))
    return EXIT_OK


def cmd_outbox(args):
    """Show tracker intents and whether each was acknowledged.

    Draining happens elsewhere, on a machine that can reach the tracker. This
    command exists so a human on the cluster can see exactly what WOULD be
    sent before anything is, and afterwards what the drainer confirmed landed.
    """
    _d5_refuse('cmd_outbox', getattr(args, "state_dir", None), plan=getattr(args, "plan", None))
    _prepare_command_paths(args)
    intents = SO.read_outbox(args.state_dir)

    try:
        return _cmd_outbox_inner(args, intents)
    except OutboxError as exc:
        # One handler for every path into the journal. Previously each branch
        # decided separately whether to care, and one of them always forgot.
        if args.json:
            print(json.dumps({"error": str(exc)}, indent=2, sort_keys=True))
        else:
            sys.stderr.write(f"  RECEIPT JOURNAL: {exc}\n")
        return EXIT_CONFLICT


def _cmd_outbox_inner(args, intents):
    if args.record_receipt:
        selected = next((i for i in intents
                         if i.get("key") == args.record_receipt), None)
        if selected is None:
            sys.stderr.write(
                f"error: no intent with key {args.record_receipt!r} in this "
                f"outbox. A receipt for an unknown key would acknowledge "
                f"something that was never intended.\n")
            return EXIT_USAGE
        if not args.ref:
            sys.stderr.write(
                "error: --ref is required. The receipt records the tracker's "
                "own reference for the operation the drainer confirmed by "
                "read-back; without it there is nothing to check later.\n")
            return EXIT_USAGE
        source = getattr(args, "source", None)
        matched = getattr(args, "matched", False)
        if source or matched:
            sys.stderr.write(
                "error: this compatibility command records only the weaker "
                "legacy attestation. Reconcile a complete observation file "
                "with drain_contract.py --state-dir to record the stronger "
                "receiver-confirmed grade.\n")
            return EXIT_USAGE
        rec = SO.record_receipt(args.state_dir, args.record_receipt, args.ref,
                             op=args.op)
        print(f"  recorded: {rec['key']} -> {rec['ref']}")
        if SO._receipt_grade(rec) == SO.ATTESTED_UNSPECIFIED:
            print("  grade: attested (compatible legacy form; "
                  "no read-back basis was supplied)")
        return EXIT_OK

    status, problems = SO.acknowledgment_status(args.state_dir)

    # FAIL CLOSED. Corruption is not the same as an interrupted tail: a
    # truncated last line is a local write that did not finish, and repeating
    # reconciliation fixes it. A bad line in the middle means the journal
    # cannot be read in
    # full, so no status derived from it can be trusted, including the
    # comfortable ones.
    for i in intents:
        st, rs = status.get(i.get("key"), (SO.UNACKNOWLEDGED, []))
        i["ack_status"] = st
        i["ack_refs"] = sorted({r.get("ref") for r in rs})

    unack = [i for i in intents if i["ack_status"] == SO.UNACKNOWLEDGED]
    conflicts = [i for i in intents if i["ack_status"] == SO.CONFLICT]

    if args.json:
        print(json.dumps(
            {"note": "ack_status 'attested' is an unspecified legacy claim; "
                     "'attested_confirmed' is bound to receiver read-back or "
                     "receiver deduplication. Neither is independently "
                     "verified tracker state. 'unacknowledged' means no "
                     "receipt either way, NOT that nothing was filed.",
             "intents": intents if args.all else unack},
            indent=2, sort_keys=True))
        return EXIT_CONFLICT if conflicts else EXIT_OK

    if not intents:
        print("  no tracker intents recorded")
        print("  Intents appear as units change state. Nothing is ever sent "
              "from here:\n  the coordinator runs on a login node and cannot "
              "reach a tracker.")
        return EXIT_OK

    for note in problems:
        print(f"  RECEIPT JOURNAL [{note['kind']}]: {note['detail']}")
    if problems:
        print()

    show = intents if args.all else unack
    print(f"  {len(unack)} unacknowledged of {len(intents)} intent(s)"
          + (f", {len(conflicts)} in CONFLICT" if conflicts else "") + "\n")
    for i in show:
        ev = "with evidence" if i.get("evidence") else "no evidence"
        label = {SO.ATTESTED_UNSPECIFIED: "attested",
                 SO.ATTESTED_CONFIRMED: "confirmed",
                 SO.CONFLICT: "CONFLICT",
                 SO.UNACKNOWLEDGED: "unack"}[i["ack_status"]]
        # same string in both modes; see the note on ACKNOWLEDGED
        print(f"  [{label:8}] {i['verb']:6} {i['unit']:12} "
              f"{i['unit_state']:16} {ev}")
        print(f"      {i['why']}  key={i['key']}")
        if "tracker" in i:
            print(f"      tracker: {i['tracker']!r}")
        if i["ack_refs"]:
            print(f"      tracker ref: {', '.join(i['ack_refs'])}")

    if conflicts:
        print("\n  CONFLICT means two receipts claim different tracker refs "
              "for one\n  intent. Something was filed twice, in two places. "
              "Resolve by hand;\n  this tool will not pick a winner.")

    print("\n  Drain from a machine that can reach the tracker, then record "
          "what\n  landed:  swarm.py outbox --state-dir DIR "
          "--record-receipt KEY --ref ID")
    print("  UNACKNOWLEDGED does NOT mean 'not filed'. It means this machine "
          "has\n  no receipt either way. After an ambiguous drain, do not "
          "replay: resolve\n  it with receiver-side deduplication or "
          "read-back.")
    print("  ATTESTED is a compatible legacy claim with no recorded basis; "
          "CONFIRMED\n  is bound to receiver read-back. Neither is "
          "independent verification.")
    return EXIT_CONFLICT if conflicts else EXIT_OK


def _elapsed_seconds(observed_at, started_at):
    """A non-negative whole-second age, or None when history is unknown."""
    if started_at is None or isinstance(started_at, bool):
        return None
    try:
        started = float(started_at)
    except (TypeError, ValueError, OverflowError):
        return None
    if started != started or started in (float("inf"), float("-inf")):
        return None
    return max(0, int(float(observed_at) - started))


def _status_rows(plan, state, state_dir, observed_at=None):
    """Read durable state plus explicitly labelled watcher observations.

    Reads no scheduler and starts no workers; local ps probes are diagnostic
    only. Rendering never imports observations into coordinator state.
    """
    observed_at = time.time() if observed_at is None else float(observed_at)
    units = {u["id"]: u for u in SPR._units_with_canary(plan)}
    promoted = {}
    try:
        for line in (Path(state_dir) / PROMOTIONS).read_text().splitlines():
            if not line.strip():
                continue
            # Skip a BAD LINE, never the whole file. A crash during append
            # leaves a partial record, and discarding everything showed
            # already-promoted units as "NOT promoted", inviting the operator
            # to publish a second time.
            try:
                r = json.loads(line)
                promoted[r["unit"]] = r
            except (ValueError, KeyError, TypeError):
                continue
    except OSError:
        pass
    rows = []
    for uid, u in units.items():
        us = state.get("units", {}).get(uid) or {}
        st = us.get("state") or "-"
        deadline_host_local = None
        deadline_problem = None
        deadline_evaluation = None
        if u.get("deadline_s") is not None and us.get("attempt_dir"):
            deadline_host_local, deadline_problem = \
                SS._deadline_host_evaluation(state, uid, us)
            deadline_evaluation = (
                "evaluable_here" if deadline_host_local is True
                else "not_evaluable_here")
        held_by = []
        if st == "HELD":
            held_by = [d for d in (u.get("needs") or [])
                       if (state.get("units", {}).get(d) or {}).get("state")
                       in ("FAILED", "HELD", "FAILED_EVIDENCE",
                            "ABANDONED", "SUPERSEDED")]
        rows.append({
            "id": uid, "kind": u.get("kind", "?"), "state": st,
            "terminal_watch": (_code_terminal_status(
                state, state_dir, uid, Path(us["attempt_dir"]).name)
                if u.get("kind") == "code" and us.get("attempt_dir") else None),
            "job_id": us.get("job_id"), "attempt_dir": us.get("attempt_dir"),
            "backend": (SS.attempt_backend(us, us["attempt_dir"])
                        if u.get("kind") == "code" and us.get("attempt_dir")
                        else None),
            "allocated_at": us.get("allocated_at"),
            "state_changed_at": us.get("state_changed_at"),
            "state_changed_at_basis": us.get("state_changed_at_basis"),
            "age_s": _elapsed_seconds(observed_at, us.get("allocated_at")),
            "state_age_s": _elapsed_seconds(
                observed_at, us.get("state_changed_at")),
            "reason": us.get("reason"),
            "successor": (us.get("terminal_decision") or {}).get("successor"),
            "deadline_evaluation": deadline_evaluation,
            "deadline_host_local": deadline_host_local,
            "deadline_problem": deadline_problem,
            "attempts": len(us.get("attempts") or []),
            "gpu_hours": float(us.get("gpu_hours") or 0),
            "needs": u.get("needs") or [],
            "held_by": held_by,
            # Why a unit that has not started is not starting. A bare "-" in
            # the status table left a human unable to tell a DAG that is
            # waiting from one that has stalled forever.
            "waiting_on": ([d for d in (u.get("needs") or [])
                            if (state.get("units", {}).get(d) or {}).get(
                                "state") != "DONE"]
                           if not us.get("attempt_dir") and st in ("-", None)
                           else []),
            "promotable": bool(u.get("promote_to")),
            "promoted": promoted.get(uid, {}).get("promoted_to"),
            "promoted_by": promoted.get(uid, {}).get("approver"),
        })
        if plan.get("canary") in rows[-1]["waiting_on"]:
            rows[-1]["waiting_reason"] = f"waiting on canary {plan['canary']}"
        if us.get("provider_resumes"):
            rows[-1]["provider_resume"] = SPRS.status(us, u)
    rows.sort(key=lambda r: r["id"])
    return rows


def status_report(plan, state, state_dir, observed_at=None):
    observed_at = time.time() if observed_at is None else float(observed_at)
    rows = _status_rows(plan, state, state_dir, observed_at=observed_at)
    observation = time.strftime("%Y-%m-%dT%H:%M:%S%z",
                                time.localtime(observed_at))
    declared = (plan.get("budget") or {}).get("gpu_hours")
    spent = sum(r["gpu_hours"] for r in rows)
    # READY_FOR_PR belongs here: no mechanism can leave that state today, so
    # a DAG parked in it is stalled, not progressing. Without this, `status`
    # exits 0 and a cron wrapper polling it reports a healthy project forever.
    attention = [r for r in rows
                 if r["state"] in ("NEEDS_HUMAN", "FAILED", "FAILED_EVIDENCE",
                                   "READY_FOR_PR", "PREFLIGHT_REFUSED")]
    return {
        "project": plan.get("name"),
        "observed_at": observation,
        "generated_at": observation,
        "halted": state.get("halted"),
        "budget": {"declared_gpu_hours": declared, "spent_gpu_hours": spent,
                   "remaining_gpu_hours": (None if declared is None
                                           else float(declared) - spent)},
        "counts": {s: sum(1 for r in rows if r["state"] == s)
                   for s in sorted({r["state"] for r in rows})},
        "needs_attention": [r["id"] for r in attention],
        "units": rows,
    }


def cmd_usage(args):
    import usage_raw
    _prepare_command_paths(args, read_only=True)
    result = usage_raw.report(args.state_dir, SS.load_state(args.state_dir),
                              args.unit, args.since, args.tz)
    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        print("Usage (agent-attested): %d measured, %d unknown" %
              (result['measured_count'], result['unknown_count']))
        print("Measured totals: " + json.dumps(result['totals'], sort_keys=True))
        for row in result['unknown']:
            print("%s/%s: unknown (%s)" % (row['unit'], row['attempt'], row['reason']))
    return 0


def cmd_status(args):
    plan, err = U.read_json(args.plan)
    if err:
        sys.exit(f"error: no readable plan at {args.plan}: {err}")
    _prepare_command_paths(args, plan=plan, read_only=True)
    state = SS.load_state(args.state_dir)
    rep = status_report(plan, state, args.state_dir)

    retention_report = []
    SPS.report_retained(state, retention_report)
    if retention_report:
        rep["retained_provider_stops"] = retention_report
    if args.json:
        print(json.dumps(rep, indent=2, sort_keys=True))
    else:
        for line in retention_report:
            print(line)
        rows = rep["units"]
        w = max([len(r["id"]) for r in rows] + [4])
        print(f"  observed at {rep['observed_at']}")
        print(f"  {'unit'.ljust(w)}  {'kind':9} {'state':13} {'job':14} "
              f"{'gpuh':>5} att {'age':>8} {'in-state':>8}")
        for r in rows:
            job = str(r["job_id"] or "-")
            age = "-" if r["age_s"] is None else f"{r['age_s']}s"
            state_age = ("-" if r["state_age_s"] is None
                         else f"{r['state_age_s']}s")
            if (r["state_age_s"] is not None
                    and r["state_changed_at_basis"] == "first_observed"):
                state_age = ">=" + state_age
            print(f"  {r['id'].ljust(w)}  {r['kind']:9} {r['state']:13} "
                  f"{job[:14]:14} {r['gpu_hours']:>5g} {r['attempts']} "
                  f"{age:>8} {state_age:>8}")
            watch = r["terminal_watch"]
            resume = r.get("provider_resume")
            if resume:
                print(f"  {'':{w}}    resumed {resume['resumed']}/{resume['max']} "
                      f"after provider error; observed {resume['observed_at']} "
                      f"(delivery unconfirmed, not progress)")
            if watch and watch.get("status") == "lor_native_parent_wake":
                print(f"  {'':{w}}    backend lor: session {r['job_id']} is "
                      f"tracked by Lor parent-session wake + `swarm.py "
                      f"advance` (no watcher process); `wait_event.py "
                      f"--poll-lor` waits for it")
                watch = None
            if watch and watch.get("status") == "cli_turn_receipt":
                print(f"  {'':{w}}    backend {r.get('backend')}: run "
                      f"{r['job_id']} is judged from its turn receipt by "
                      f"`swarm.py advance` (no watcher process); "
                      f"`wait_event.py --poll-cli` waits for it")
                watch = None
            if watch:
                process = (watch.get("process_state") if watch.get("status")
                           in ("starting", "waiting") else None)
                suffix = f" (process {process})" if process else ""
                if watch.get("import_pending"):
                    suffix += " (not yet imported)"
                observation = (
                    f"checked {watch.get('checked_at') or 'unknown'}"
                    if watch.get("status") == "checked" else
                    f"observed {watch.get('observed_at') or watch.get('process_observed_at') or watch.get('started_at') or 'unknown'}")
                print(f"  {'':{w}}    terminal watcher: "
                      f"{watch.get('status', 'unknown')}{suffix}; {observation}")
                if watch.get("outcome_ignored"):
                    print(f"  {'':{w}}      {watch['outcome_ignored']}")
                if watch.get("reason") or watch.get("process_reason"):
                    print(f"  {'':{w}}      "
                          f"{watch.get('reason') or watch.get('process_reason')}")
                if watch.get("log_tail"):
                    print(f"  {'':{w}}      log tail: {watch['log_tail']!r}")
                if watch.get("status") != "checked":
                    print(f"  {'':{w}}      Scheduled advance remains the fallback.")
            if r["reason"]:
                print(f"  {'':{w}}    reason: {r['reason']}")
            if r["successor"]:
                print(f"  {'':{w}}    successor: {r['successor']}")
            if r["deadline_evaluation"] == "not_evaluable_here":
                print(f"  {'':{w}}    deadline: NOT EVALUABLE HERE; "
                      f"no escalation -- {r['deadline_problem']}")
            # A held unit must say WHY, or the operator reads it as "waiting".
            if r["held_by"]:
                print(f"  {'':{w}}    held by {', '.join(r['held_by'])}, which "
                      f"will not complete")
            if r["state"] == "READY_FOR_PR":
                # Say plainly that this cannot advance on its own. A unit
                # parked in a state no mechanism can leave is a stalled DAG,
                # and a human reading `status` must not have to know that.
                print(f"  {'':{w}}    the agent finished and its outputs "
                      f"exist, but a code unit is closed by a MERGED PULL "
                      f"REQUEST.")
                print(f"  {'':{w}}    Nothing records merges yet, so this "
                      f"will not advance on its own and anything below it "
                      f"waits. Open the PR and close it by hand, or make "
                      f"this a kind=slurm or kind=pipeline unit.")
            if r["waiting_on"]:
                waiting = [f"canary {d}" if d == plan.get("canary") else d
                           for d in r["waiting_on"]]
                print(f"  {'':{w}}    waiting on "
                      f"{', '.join(waiting)}")
            if r["promoted"]:
                print(f"  {'':{w}}    promoted to {r['promoted']} "
                      f"(approved by {r['promoted_by']})")
            elif r["promotable"] and r["state"] == "DONE":
                print(f"  {'':{w}}    NOT promoted; approve with `swarm.py "
                      f"promote <plan> --unit {r['id']} --approve`")
        b = rep["budget"]
        if b["declared_gpu_hours"] is not None:
            print(f"\n  budget: {b['spent_gpu_hours']:g} of "
                  f"{b['declared_gpu_hours']:g} GPU-hours committed, "
                  f"{b['remaining_gpu_hours']:g} left")
        if rep["halted"]:
            # On the page, not only in a log.
            print(f"\n  HALTED: {rep['halted']}")
        if rep["needs_attention"]:
            print(f"\n  NEEDS YOU: {', '.join(rep['needs_attention'])}")

    # Exit code is the notification channel. The coordinator has no network by
    # design, so a cron wrapper reads this and decides whether to wake anyone.
    if rep["needs_attention"]:
        return EXIT_FAILED_UNIT
    return EXIT_HALTED if rep["halted"] else EXIT_OK


def main():
    ap = argparse.ArgumentParser(
        prog="swarm.py", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)

    def common(p):
        p.add_argument("plan")
        p.add_argument("--state-dir", default=None,
                       help="external coordinator state directory. Default: "
                            "a per-project directory under XDG_STATE_HOME or "
                            "~/.local/state")
        p.add_argument("--root", default=None,
                       help="external attempt root beside the default state "
                            "directory")
        p.add_argument("--dry-run", action="store_true",
                       help="allocate and record, but do not submit. The DAG "
                            "logic is testable without a scheduler.")
        p.add_argument("--max-new-dispatches", type=int, default=None)
        p.add_argument("--accept-plan-change", action="store_true",
                       help="ratify an intentional mid-flight plan edit. "
                            "Records the new digest and KEEPS every recorded "
                            "attempt, so nothing already DONE is re-dispatched. "
                            "Use when raising a budget ceiling or correcting a "
                            "pending unit; not to paper over an accidental "
                            "edit to a unit that is already running.")

    v = sub.add_parser("validate", help="acyclic deps, disjoint write scopes")
    v.add_argument("plan")
    v.add_argument("--check-seeds", action="store_true",
                   help="check every declared seed against its dispatch "
                        "source and origin push destination using Git; "
                        "does not allocate attempts or write state")
    v.add_argument("--survey", default=None,
                   help="the survey.py JSON to check this plan's memory "
                        "requests and account/partition pairs against. "
                        "Without it, .swarm/survey.json beside the plan and "
                        "then in the working directory, and if neither is "
                        "there those cluster facts are UNKNOWN and nothing "
                        "is refused on them.")
    v.set_defaults(fn=cmd_validate)

    c = sub.add_parser("continue", help="derive a fresh code unit from an attempt")
    c.add_argument("plan")
    c.add_argument("--state-dir", required=True)
    c.add_argument("--unit", required=True)
    c.add_argument("--new-id", required=True)
    c.add_argument("--prompt-file", required=True)
    c.add_argument("--head-ref", help="exact remote refs/heads/... override")
    c.add_argument("--write", action="store_true",
                   help="atomically append the validated unit to the plan")
    c.add_argument("--repoint-dependents", action="store_true",
                   help="repoint only never-allocated dependents to the successor")
    c.add_argument("--tickets", help="append the successor mapping to this tickets draft")
    c.set_defaults(fn=cmd_continue)

    r = sub.add_parser("run", help="dispatch what is ready, then exit")
    common(r)
    r.set_defaults(fn=cmd_run)

    a = sub.add_parser("advance", help="idempotent; for a schedule or cron")
    common(a)
    a.set_defaults(fn=cmd_advance)

    # Internal detached helper. It carries no independent authority and is
    # intentionally absent from the public CLI reference: `run`/`advance`
    # create it only after persisting the exact attempt and agent binding.
    w = sub.add_parser("watch-code-terminal", help=argparse.SUPPRESS)
    w.add_argument("plan")
    w.add_argument("--state-dir", required=True)
    w.add_argument("--root", required=True)
    w.add_argument("--unit", required=True)
    w.add_argument("--attempt", required=True)
    w.add_argument("--agent", required=True)
    w.set_defaults(fn=cmd_watch_code_terminal)

    # The watcher cannot write coordinator state. This separate invocation
    # uses the normal coordinator lock and verifies the triggering identity.
    tw = sub.add_parser("advance-code-terminal", help=argparse.SUPPRESS)
    tw.add_argument("plan")
    tw.add_argument("--state-dir", required=True)
    tw.add_argument("--root", required=True)
    tw.add_argument("--unit", required=True)
    tw.add_argument("--attempt", required=True)
    tw.add_argument("--agent", required=True)
    tw.set_defaults(fn=cmd_advance, terminal_watch=True, dry_run=False,
                    max_new_dispatches=0)

    for command in ("abandon", "supersede"):
        stop = sub.add_parser(command, help="record an approver's terminal disposition")
        stop.add_argument("plan")
        stop.add_argument("--state-dir", default=None)
        stop.add_argument("--unit", default=None)
        stop.add_argument("--approver", default=None)
        stop.add_argument("--reason", default=None)
        if command == "supersede":
            stop.add_argument("--by", default=None, help="DONE successor in this plan")
        stop.set_defaults(fn=cmd_stop_unit)

    pr = sub.add_parser("promote",
                        help="copy a DONE unit's outputs to its declared "
                             "shared path, on explicit approval")
    pr.add_argument("plan")
    pr.add_argument("--unit", required=True)
    pr.add_argument("--state-dir", default=None)
    pr.add_argument("--approve", action="store_true",
                    help="actually copy. Without it this is a dry run.")
    pr.add_argument("--accept-weak-evidence", action="store_true",
                    help="publish an output whose receipt holds only size and "
                         "mtime, which cannot establish unchanged content. "
                         "Needed for outputs too large to digest. Recorded in "
                         "the promotion record as weak.")
    pr.add_argument("--approver", default=None,
                    help="who accepted this result; recorded permanently")
    pr.set_defaults(fn=cmd_promote)

    sc = sub.add_parser("schema", help="every unit field, when it is "
                                      "required, and what it couples to")
    sc.add_argument("--json", action="store_true")
    sc.set_defaults(fn=cmd_schema)

    sc = sub.add_parser("scope-check", help="advisory local merge scope check")
    sc.add_argument("plan")
    sc.add_argument("--state-dir", required=True)
    sc.add_argument("--unit", required=True)
    sc.add_argument("--head", metavar="REV",
                    help="worker advisory head, resolved in the launch repository")
    sc.add_argument("--json", action="store_true")
    sc.set_defaults(fn=cmd_scope_check)

    v = sub.add_parser("verify", help="run an authorized pinned verifier")
    v.add_argument("--state-dir", default=None)
    v.add_argument("--unit", required=True)
    v.add_argument("--attempt", required=True,
                   help="the attempt directory whose produced commit is "
                        "being verified")
    v.add_argument("--claim", required=True,
                   help="what this verifier is asserting, e.g. tests-pass. "
                        "It must be a claim the policy allows it to make.")
    v.add_argument("--verifier", required=True,
                   help="the name the policy authorizes")
    v.add_argument("--path", required=True,
                   help="the file to run. Its bytes must hash to what the "
                        "policy recorded.")
    v.add_argument("--arg", action="append", default=[])
    v.add_argument("--timeout", type=int, default=900)
    v.add_argument(
        "--target-commit", default=None,
        help="required only for claim integration-tests: the exact target "
             "commit to merge the produced head into. Its object must "
             "already exist locally; verify never fetches it.")
    v.set_defaults(fn=cmd_verify)

    m = sub.add_parser("merge", help="record an observed merged PR for a "
                                     "code unit")
    m.add_argument("--state-dir", default=None)
    m.add_argument("--unit", required=True)
    m.add_argument("--pr", required=True, help="the PR's URL or number")
    m.add_argument("--head", required=True,
                   help="the PR head commit. Must be the commit this attempt "
                        "produced, or the receipt is refused.")
    m.add_argument("--target", required=True, help="the branch it merged into")
    m.add_argument(
        "--target-commit", default=None,
        help="the target branch commit immediately before this merge. "
             "Required for an integration-tests receipt to remain valid.")
    m.add_argument("--merged-as", required=True,
                   help="the resulting commit on the target")
    m.add_argument("--method", required=True, choices=SE.MERGE_METHODS)
    m.add_argument("--integration-status",
                   choices=("candidate-verified", "integration-unverified"),
                   help="connected operator's audit label; not verification authority")
    # REQUIRED, because _merge_shape_problem requires it: an optional flag
    # feeding a mandatory field is a command that can only fail, and only the
    # smoke test found it. Every unit test passed a repo.
    m.add_argument("--repo", required=True,
                   help="the repository the PR is in")
    m.set_defaults(fn=cmd_merge)

    sf = sub.add_parser("seq-floor", help="record an evidence-based tracker sequence floor")
    sf.add_argument("--floor", type=int, required=True)
    for option in ("--state-dir", "--approver", "--reason"):
        sf.add_argument(option, required=True)
    sf.set_defaults(fn=cmd_seq_floor)

    o = sub.add_parser("outbox", help="tracker intents, and what landed")
    o.add_argument("--state-dir", default=None)
    o.add_argument("--all", action="store_true",
                   help="include already-acknowledged intents")
    o.add_argument("--json", action="store_true")
    o.add_argument("--record-receipt", metavar="KEY",
                   help="record the compatible legacy attestation for this "
                        "intent. Receiver-confirmed observations go through "
                        "drain_contract.py with their complete bindings; a "
                        "false acknowledgment is worse than a missing one.")
    o.add_argument("--ref", help="the tracker's own reference for the "
                                 "operation that succeeded, e.g. ARC-171")
    o.add_argument("--op", help="optional: which operation was performed")
    o.add_argument("--source", choices=sorted(SO.RECEIPT_CONFIRMING_SOURCES),
                   help="reserved for compatibility; use drain_contract.py "
                        "with a complete observation file")
    o.add_argument("--matched", action="store_true",
                   help="reserved for compatibility; use drain_contract.py "
                        "with a complete observation file")
    o.set_defaults(fn=cmd_outbox)

    usage = sub.add_parser("usage", help="usage from preserved local rollouts")
    usage.add_argument("--state-dir", default=None)
    usage.add_argument("--unit")
    usage.add_argument("--since", help="inclusive finish date, YYYY-MM-DD")
    usage.add_argument("--tz", default="UTC")
    usage.add_argument("--json", action="store_true")
    usage.set_defaults(fn=cmd_usage)

    ag = sub.add_parser(
        "set-agent", help="switch the coding agent for new attempts; "
                          "plan.json is never edited")
    ag.add_argument("plan")
    ag.add_argument("--state-dir", default=None)
    ag.add_argument("--provider", default=None,
                    help="PROVIDER/MODEL, e.g. lor/<canonical_model_id> or a "
                         "Paseo provider/model; a direct CLI needs --backend")
    ag.add_argument("--backend", choices=LIO.BACKENDS, default=None)
    ag.add_argument("--model", default=None,
                    help="with --backend: the Lor canonical model id, the "
                         "Paseo PROVIDER/MODEL, the claude/codex model, or "
                         "pi's <provider>/<model>")
    ag.add_argument("--thinking", default=None,
                    help="effort id; 'none' sends no effort flag")
    ag.add_argument("--mode", default=None,
                    help="Paseo permission mode (required when switching "
                         "undispatched units without one to Paseo)")
    ag.add_argument("--unit", default=None,
                    help="override one code unit instead of the project")
    ag.add_argument("--clear", action="store_true",
                    help="remove the project (or --unit) override")
    ag.add_argument("--show", action="store_true",
                    help="read-only: print the effective agent of every "
                         "code unit and any in-flight attempt's backend")
    ag.add_argument("--json", action="store_true")
    ag.set_defaults(fn=cmd_set_agent)

    s = sub.add_parser("status", help="what every unit is doing")
    s.add_argument("--json", action="store_true",
                   help="machine-readable; exit 2 if any unit needs a person")
    s.add_argument("plan")
    s.add_argument("--state-dir", default=None)
    s.set_defaults(fn=cmd_status)

    args = ap.parse_args()
    _d5_bytecode_advisory(getattr(args, "state_dir", None), plan=getattr(args, "plan", None))
    _d5_dispatch(args)
    sys.exit(args.fn(args))


# Wildcard imports enumerate __all__, not names served only by __getattr__.
# Build this after every definition to retain the original public namespace.
__all__ = [name for name in globals() if not name.startswith("_")] + [
    "AGENTS_FILE", "DEFAULT_AGENT_PROVIDER", "DEFAULT_AGENT_THINKING",
    "THINKING_BY_MODEL", "PINNED_AGENT_PROVIDER", "PINNED_AGENT_THINKING",
    "PINNED_THINKING_BY_MODEL", "ORCHESTRATOR_DEFAULTS",
    "FORBID_WHEN_ORCHESTRATOR", "VALID_ORCHESTRATORS",
    "ACTIVE_ORCHESTRATOR", "effective_default_provider",
    "forbid_unit_provider_problem",
    "acquire_lease", "bump_lease_epoch", "renew_lease", "release_lease", "load_state",
    "save_state",
    "trusted_launch_facts",
    "trusted_launch_host_anchor",
    "LIVE_STATES", "OPERATOR_TERMINAL_STATES",
]


if __name__ == "__main__":
    main()
