"""Advance phases: normalisation, recovery, judgment, settlement, retry, dispatch, outbox.

Python 3.8+, standard library only; dependencies never point to the facade.
"""

import sys
import time
from pathlib import Path

import lor_io as LIO
import unit as U
import verify as V
import worktree as W
import swarm_dispatch as SD
import swarm_evidence as SE
import swarm_outbox as SO
import swarm_preflight as SP
import swarm_provider_resume as SPRS
import swarm_provider_stops as SPS
import swarm_state as SS
import swarm_workspaces as SW
from swarm_types import (
    DONE,
    FAILED,
    INCOMPLETE,
    NAME,
    NEEDS_HUMAN,
    OutboxError,
    RUNNING,
)



def _bind_attempt(state, uid, unit_dir, job_id, state_dir):
    """Bind via unit.py with the attempt's FROZEN backend.

    Paseo (and legacy) attempts take the unchanged SD._bind path. A Lor
    attempt also records its backend and parent session in unit.json so the
    checker observes it through Lor, never `paseo inspect`.
    """
    us = ((state.get("units") or {}).get(uid) or {})
    if SS.attempt_backend(us, unit_dir) != LIO.LOR_BACKEND:
        return SD._bind(unit_dir, job_id, state_dir)
    parent = SS.attempt_parent_session(us, unit_dir)
    rc, out, err = U.run(
        [sys.executable, str(Path(__file__).resolve().parent / "unit.py"),
         "bind", str(unit_dir), "--state-dir", str(state_dir),
         "--job-id", str(job_id), "--backend", LIO.LOR_BACKEND,
         "--parent-session-id", str(parent)], timeout=120)
    return None if rc == 0 else f"bind failed: {(err or out).strip()[:200]}"

def _check_dry_run_contamination(units, state, report):
    """Refuse a dry run when the state directory already holds live real attempts."""
    real = sorted(uid for uid in units
                  if (SS._unit_state(state, uid).get("attempt_dir")
                      and not str(
                          SS._unit_state(state, uid).get("job_id") or "")
                      .startswith("dry-")
                      and SS._occupies_live_resources(
                          units[uid], SS._unit_state(state, uid))))
    if real:
        return (report + [
            f"REFUSING to dry-run against a state directory that "
            f"holds REAL attempts ({', '.join(real)}).",
            f"A dry run records placeholder job ids, and a unit that "
            f"has one can never be judged or re-dispatched, so this "
            f"would wedge work that is genuinely running.",
            f"Use a throwaway state directory instead:",
            f"    swarm.py run <plan> --dry-run --state-dir "
            f"$(mktemp -d)/state --root $(mktemp -d)/runs"], 0, None)
    return None


def _normalise_persisted_state(units, state, state_dir, dry_run,
                               clock_observed_at, report):
    """Persist missing state clocks and downgrade unmerged non-receipt DONE units."""
    if not dry_run:
        for uid in units:
            SS._normalise_state_clock(
                SS._unit_state(state, uid), clock_observed_at)
    for uid, u in sorted(units.items()):
        us = SS._unit_state(state, uid)
        if (us.get("state") == "DONE"
                and not us.get("merged_as")
                and SO.closing_evidence_for(u.get("kind")) != "predicate_receipt"):
            SS._set_unit_state(us, "READY_FOR_PR")
            report.append(
                f"{uid}: recorded DONE, but a {u.get('kind')} unit is closed "
                f"by a merged pull request, not by its own receipt. Corrected "
                f"to READY_FOR_PR; anything depending on it waits.")
            SS.save_state(state_dir, state)


def _cleanup_worktrees(units, state, state_dir, dry_run,
                       worktree_cleanup_states, report):
    """Archive terminal/pending worktrees and retry pending codex-home removals."""
    for uid, u in sorted(units.items()):
        us = SS._unit_state(state, uid)
        if us.get("state") in SS.OPERATOR_TERMINAL_STATES:
            continue
        current_attempt = (Path(us["attempt_dir"]).name
                           if us.get("attempt_dir") else None)
        current_meta = ((us.get("attempt_workspaces") or {}).get(
            current_attempt) if current_attempt else None)
        if (us.get("state") in worktree_cleanup_states
                or (isinstance(current_meta, dict)
                    and current_meta.get("cleanup_pending"))):
            if not dry_run:
                SW._archive_code_worktree(
                    state, u, us.get("attempt_dir"), report, state_dir)
            else:
                SW._report_would_archive_code_worktree(
                    state, u, us.get("attempt_dir"), report)
        for attempt, meta in (us.get("attempt_workspaces") or {}).items():
            if (attempt != current_attempt and isinstance(meta, dict)
                    and meta.get("cleanup_pending")):
                if not dry_run:
                    SW._archive_code_worktree(
                        state, u, attempt, report, state_dir)
                else:
                    SW._report_would_archive_code_worktree(
                        state, u, attempt, report)
        for attempt, meta in (us.get("attempt_codex_homes") or {}).items():
            if (attempt != current_attempt and isinstance(meta, dict)
                    and meta.get("cleanup_pending") and not dry_run):
                SW._cleanup_codex_home(state, u, attempt, report, state_dir)
    SW._report_retained_worktrees(state, report, dry_run=dry_run)
    if not dry_run:
        SS.save_state(state_dir, state)


def _evaluate_deadlines(units, state, state_dir, dry_run,
                        deadline_observed_at, report):
    """Evaluate host-local per-unit deadlines and clear resolved breaches."""
    for uid, u in sorted(units.items()):
        us = SS._unit_state(state, uid)
        if us.get("state") in SS.OPERATOR_TERMINAL_STATES:
            continue
        host_local, host_problem = SS._deadline_host_evaluation(state, uid, us)
        if (u.get("deadline_s") is not None and us.get("attempt_dir")
                and host_local is not True):
            report.append(
                f"{uid}: DEADLINE_NOT_EVALUABLE_HERE -- {host_problem}")
            continue
        if (not dry_run
                and not SS._deadline_exceeded(
                    u, us, state, uid, deadline_observed_at)):
            SS._clear_deadline_breach(us)
    if not dry_run:
        SS.save_state(state_dir, state)


def _recover_unbound_attempts(units, state, state_dir, dry_run,
                              dispatch_targets, plan_path, report, submit_fn):
    """Recover missing code launch snapshots, pending binds, and orphan attempts."""
    for uid, u in sorted(units.items()):
        us = SS._unit_state(state, uid)
        if (us.get("state") in SS.OPERATOR_TERMINAL_STATES
                or SPS.retained(us, us.get("attempt_dir"))):
            continue
        if dry_run or not us["attempt_dir"]:
            continue
        if (u.get("kind") == "code" and us.get("job_id")
                and not SS.trusted_launch_facts(
                    state, uid, us["attempt_dir"])):
            host_problem = W.launch_host_problem(
                SS.trusted_launch_host_anchor(
                    state, uid, us["attempt_dir"]))
            if host_problem:
                us["launch_recovery_pending"] = True
                us["host_judgment"] = "UNJUDGEABLE_HERE"
                us.pop("launch_recovery_problem", None)
                report.append(f"{uid}: UNJUDGEABLE_HERE -- {host_problem}. "
                              f"The agent is retained for a coordinator on "
                              f"its launch host.")
                SS.save_state(state_dir, state)
                continue
            recovery_error = SW._recover_code_launch(
                state, u, us["attempt_dir"], us["job_id"])
            if recovery_error:
                us["launch_recovery_problem"] = recovery_error
                SS._set_unit_state(us, "NEEDS_HUMAN")
                if SS._deadline_exceeded(u, us, state, uid):
                    first_breach = SS._mark_deadline_exceeded(u, us)
                    if first_breach:
                        report.append(
                            f"{uid}: NEEDS_HUMAN -- deadline_exceeded during "
                            f"attempt recovery. The attempt remains bound; "
                            f"no cancellation, claim release, or retry was "
                            f"requested.")
                report.append(f"{uid}: NEEDS_HUMAN -- {recovery_error}. The "
                              f"agent remains bound and no launch facts were "
                              f"admitted; repair or abandon this exact "
                              f"attempt before it can be judged.")
                SS.save_state(state_dir, state)
                continue
            us.pop("launch_recovery_problem", None)
            SS._set_unit_state(us, "SUBMITTED")
            report.append(f"{uid}: recovered the agent's attempt worktree and "
                          f"completed its trusted launch snapshot")
            SS.save_state(state_dir, state)
        if us.get("bind_pending") and us.get("job_id"):
            if not _bind_attempt(state, uid, us["attempt_dir"], us["job_id"],
                                 state_dir):
                us.pop("bind_pending", None)
                SS._set_unit_state(us, "SUBMITTED")
                report.append(f"{uid}: binding to job {us['job_id']} succeeded "
                              f"on retry; the unit can now be judged.")
                SS.save_state(state_dir, state)
            continue
        if str(us.get("job_id") or "").startswith("dry-"):
            report.append(f"{uid}: clearing a dry-run placeholder "
                          f"({us['job_id']}); nothing was ever submitted for "
                          f"it, so the unit will dispatch normally.")
            us["job_id"] = None
            us["attempt_dir"] = None
            SS._set_unit_state(us, None)
            SS.save_state(state_dir, state)
            continue
        if us.get("job_id"):
            continue
        job, note = SW.reconcile_orphan(
            us["attempt_dir"], us.get("allocated_at"), kind=u.get("kind"),
            intent=SS.attempt_launch_intent(us, us["attempt_dir"]))
        if job:
            if u.get("kind") == "code":
                host_problem = W.launch_host_problem(
                    SS.trusted_launch_host_anchor(
                        state, uid, us["attempt_dir"]))
                if host_problem:
                    us["job_id"] = job
                    us["launch_recovery_pending"] = True
                    us["host_judgment"] = "UNJUDGEABLE_HERE"
                    us.pop("launch_recovery_problem", None)
                    report.append(f"{uid}: {note}; UNJUDGEABLE_HERE -- "
                                  f"{host_problem}. The agent is retained for "
                                  f"a coordinator on its launch host.")
                    SS.save_state(state_dir, state)
                    continue
                recovery_error = SW._recover_code_launch(
                    state, u, us["attempt_dir"], job)
                if recovery_error:
                    us["job_id"] = job
                    us["launch_recovery_pending"] = True
                    us["launch_recovery_problem"] = recovery_error
                    SS._set_unit_state(us, "NEEDS_HUMAN")
                    if SS._deadline_exceeded(u, us, state, uid):
                        first_breach = SS._mark_deadline_exceeded(u, us)
                        if first_breach:
                            report.append(
                                f"{uid}: NEEDS_HUMAN -- deadline_exceeded "
                                f"during attempt recovery. The attempt "
                                f"remains bound; no cancellation, claim "
                                f"release, or retry was requested.")
                    report.append(f"{uid}: {note}; NEEDS_HUMAN -- "
                                  f"{recovery_error}. The agent is retained, "
                                  f"but no launch facts were admitted.")
                    SS.save_state(state_dir, state)
                    continue
                us.pop("launch_recovery_pending", None)
                us.pop("launch_recovery_problem", None)
            us["job_id"] = job
            us["bind_pending"] = True
            SS._set_unit_state(us, "SUBMITTED")
            SS.save_state(state_dir, state)
            bind_error = _bind_attempt(state, uid, us["attempt_dir"], job,
                                       state_dir)
            if bind_error:
                report.append(f"{uid}: recovered {job} but {bind_error}; "
                              f"binding will retry next advance")
            else:
                us.pop("bind_pending", None)
            report.append(f"{uid}: {note}")
            SS.save_state(state_dir, state)
        elif SS._deadline_exceeded(u, us, state, uid):
            SS._mark_deadline_exceeded(u, us)
            report.append(
                f"{uid}: NEEDS_HUMAN -- deadline_exceeded while recovering "
                f"the unbound attempt. It remains allocated; no submission, "
                f"release, or retry was performed.")
            SS.save_state(state_dir, state)
        elif note == "UNKNOWN":
            report.append(f"{uid}: allocated at {us['attempt_dir']} with no "
                          f"binding, and the scheduler could not be asked "
                          f"(the query itself failed). NOT releasing the "
                          f"attempt: a failed query is not evidence that "
                          f"nothing is running. Retrying next advance.")
        elif us["state"] == "ALLOCATED":
            if u.get("kind") == "code":
                attempt = Path(us["attempt_dir"]).name
                intent = (us.get("attempt_launch_intents") or {}).get(attempt)
                if isinstance(intent, dict):
                    branch_rc, _out, _err = SP._git(
                        intent.get("repo"), "show-ref", "--verify", "--quiet",
                        f"refs/heads/{intent.get('branch')}")
                    if branch_rc == 0:
                        target, problem = SP._dispatch_target_for_advance(
                            u, dispatch_targets)
                        if not problem:
                            source, problem = SP._dispatch_source_identity(
                                u, target)
                        if not problem:
                            recovered_job, problem = submit_fn(
                                u, us["attempt_dir"], False, state, state_dir,
                                dispatch_source=source, plan_path=plan_path)
                        if problem:
                            SS._set_unit_state(us, "NEEDS_HUMAN")
                            us["launch_recovery_problem"] = problem
                            report.append(f"{uid}: NEEDS_HUMAN -- {problem}")
                            SS.save_state(state_dir, state)
                            continue
                        us["job_id"] = str(recovered_job)
                        SS._set_unit_state(us, "SUBMITTED")
                        us["bind_pending"] = True
                        SS.save_state(state_dir, state)
                        bind_error = _bind_attempt(
                            state, uid, us["attempt_dir"], recovered_job,
                            state_dir)
                        if bind_error:
                            report.append(
                                f"{uid}: recovered agent {recovered_job} but "
                                f"{bind_error}; binding will retry next advance")
                        else:
                            us.pop("bind_pending", None)
                        us.pop("launch_recovery_problem", None)
                        report.append(
                            f"{uid}: recovered exact-base worktree and "
                            f"created missing agent {recovered_job}")
                        SS.save_state(state_dir, state)
                        continue
            report.append(f"{uid}: allocated at {us['attempt_dir']} but never "
                          f"reached the scheduler (absent from both squeue and "
                          f"sacct). Releasing the attempt; it will re-dispatch "
                          f"into a new directory. The stale one is left on disk "
                          f"rather than deleted.")
            us["attempt_dir"] = None
            SS.save_state(state_dir, state)


def _check_unit_attempt(uid, u, us, state, state_dir, attempt, launch_facts,
                        accounting_job_id, report):
    """Run unit.py check (unless code head is already pinned) and bind evidence."""
    pinned_before = SE.trusted_produced_head(state, uid, attempt)
    ran_check = not (u.get("kind") == "code" and pinned_before)
    protocol_problem = None
    if ran_check:
        if u.get("kind") == "slurm":
            job_id = us.get("job_id")
            if job_id != accounting_job_id:
                U.SACCT_UNAVAILABLE = False
                accounting_job_id = job_id
        isolation_facts = SE.trusted_isolation_facts(state, u, attempt)
        check_args = (
            attempt, launch_facts,
            SE.raw_artifact_basis(state, uid, attempt))
        check_result = (SE._check(
            *check_args, isolation_facts, isolation_required=True,
            state_dir=state_dir)
            if u.get("isolation") is not None
            else SE._check(*check_args, state_dir=state_dir))
        if len(check_result) == 4:
            rc, stdout, stderr, result_channel = check_result
        elif len(check_result) == 3:
            rc, stdout, stderr = check_result
            result_channel = ""
        else:
            rc, stdout = check_result
            stderr = ""
            result_channel = ""
        check_report, protocol_problem = SE._reported_check_result(
            result_channel)
        if u.get("isolation") is not None and not isolation_facts:
            protocol_problem = (
                "the unit declares OS-backed isolation, but coordinator "
                "state has no matching applied wrapper facts for this "
                "attempt. Refusing rather than degrading to the trusted-"
                "writer basis")
        if rc == DONE and not check_report and not protocol_problem:
            protocol_problem = (
                "a successful checker emitted no SWARM_CHECK_RESULT "
                "on stdout, so its receipt cannot be attributed")
        if protocol_problem:
            check_report = None
            report.append(f"{uid}: CHECK RESULT REFUSED -- "
                          f"{protocol_problem}")
        digest = ((check_report or {}).get("receipt_sha256"))
        SE._record_receipt_provenance(state, uid, attempt, digest)

        if check_report and u.get("kind") == "code" and rc == DONE:
            produced = check_report.get("produced_head")
            basis_problem = W.validate_pinned_head(
                U.run, launch_facts, produced)
            if basis_problem:
                protocol_problem = basis_problem
                report.append(f"{uid}: CHECK RESULT REFUSED -- "
                              f"{basis_problem}")
            else:
                us.setdefault("attempt_produced_heads", {})[
                    Path(attempt).name] = produced
    else:
        rc, stdout, stderr = DONE, "", ""
    checked_state = ("FAILED_EVIDENCE" if protocol_problem
                     else NAME.get(rc, f"rc={rc}"))
    return rc, stdout, stderr, protocol_problem, checked_state, accounting_job_id


def _apply_convergence_gate(uid, u, us, attempt, converge_fn, report):
    """Gate DONE behind a declared convergence criterion."""
    if us["state"] == "DONE" and u.get("converge"):
        verdict, why = converge_fn(u, attempt)
        us["converge_verdict"] = verdict
        us["converge_reasons"] = list(why)
        if verdict != "CONVERGED":
            SS._set_unit_state(us, "NEEDS_HUMAN")
        report.append(f"{uid}: convergence {verdict} -- " +
                      " ".join(str(w) for w in why))


def _admit_code_closure(uid, u, us, state, state_dir, report):
    """Admit merge and declared verification receipts for non-receipt-closed units."""
    if (us["state"] in ("DONE", "READY_FOR_PR")
            and not us.get("merged_as")
            and SO.closing_evidence_for(u.get("kind")) != "predicate_receipt"):
        attempt = us.get("attempt_dir") or ""
        produced = SE.trusted_produced_head(state, uid, attempt)
        launch_facts = SS.trusted_launch_facts(state, uid, attempt)
        immutable_problem = W.validate_pinned_head(
            U.run, launch_facts, produced) if produced else (
                "coordinator state records no produced commit for this "
                "attempt. Re-run the attempt; do not recover a basis from "
                "its receipt or current branch")
        merge_args = {
            "expect_repo": (launch_facts or {}).get("repository_remote")}
        if V.INTEGRATION_CLAIM in (u.get("requires_verification") or []):
            merge_args["repo"] = (launch_facts or {}).get("repo")
            merge_args["require_target_binding"] = True
            merge_args["expect_target"] = u.get("target_branch")
        merge_receipt, merge_refusal = SE.admit_merge(
            state_dir, uid, produced, **merge_args)
        vrefusal = None
        required = u.get("requires_verification") or []
        policy_digest, perr, _pol = None, None, None
        if required:
            base = SE.trusted_base(state, uid, attempt)
            if immutable_problem:
                perr = immutable_problem
            elif launch_facts and launch_facts.get("repo"):
                _pol, policy_digest, perr = V.read_policy(
                    U.run, launch_facts["repo"], base)
            else:
                perr = "this attempt anchored no repository"
        for claim in required:
            if perr:
                vrefusal = (f"{uid} requires verification of {claim!r}, "
                            f"and the authorizing policy cannot be read: "
                            f"{perr}")
                break
            _vr, vrefusal = SE.admit_verification(
                state_dir, uid, claim, produced, policy_digest,
                policy=_pol, repo=(launch_facts or {}).get("repo"),
                base_commit=base,
                target_commit=((merge_receipt or {}).get("target_commit")
                               if claim == V.INTEGRATION_CLAIM else None))
            if vrefusal:
                break

        if immutable_problem and not vrefusal:
            vrefusal = immutable_problem
        receipt, refusal = ((None, vrefusal) if vrefusal else
                            (merge_receipt, merge_refusal))
        if receipt:
            SS._set_unit_state(us, "DONE")
            us["merged_as"] = receipt.get("merged_as")
            us["merge_pr"] = receipt.get("pr")
            us["merge_receipt"] = dict(receipt)
            report.append(
                f"{uid}: DONE on a merged PR ({receipt.get('pr')}, "
                f"{receipt.get('method')} as "
                f"{str(receipt.get('merged_as'))[:12]}). The merge itself "
                f"is attested, not verified; the head it pins was "
                f"produced by this attempt.")
        else:
            SS._set_unit_state(us, "READY_FOR_PR")
            us["merge_refusal"] = refusal
            report.append(f"{uid}: READY_FOR_PR. {refusal}")


def _settle_incomplete_attempt(uid, u, us, state, state_dir, rc,
                               protocol_problem, settle_s, continue_fn, report):
    """Manage continuation liveness, accounting outage, and INCOMPLETE settle window.

    Returns True when a continuation was sent and the caller should skip further
    handling for this unit in the current pass.
    """
    if u.get("kind") == "code" and rc == RUNNING:
        for entry in us.get("continuations", []):
            if (isinstance(entry, dict)
                    and entry.get("agent_id") == str(us.get("job_id"))):
                entry["active_since_send"] = True
    if rc == NEEDS_HUMAN:
        us.pop("incomplete_since", None)
    accounting_unavailable = (u.get("kind") == "slurm"
                              and rc == INCOMPLETE
                              and not protocol_problem
                              and U.SACCT_UNAVAILABLE)
    if accounting_unavailable:
        since = us.setdefault("accounting_unavailable_since", time.time())
        us.pop("incomplete_since", None)
        observed = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(since))
        report.append(f"{uid}: accounting unavailable since {observed}; "
                      f"kept nonterminal pending accounting recovery")
    else:
        us.pop("accounting_unavailable_since", None)
    if rc == INCOMPLETE:
        if not accounting_unavailable:
            for entry in us.get("continuations", []):
                if (isinstance(entry, dict)
                        and entry.get("agent_id") == str(us.get("job_id"))
                        and entry.get("status") == "pending"
                        and entry.get("sent") is None
                        and not entry.get("settle_recovered")):
                    us["incomplete_since"] = time.time()
                    entry["settle_recovered"] = True
            first = us.get("incomplete_since")
            if first is None:
                us["incomplete_since"] = time.time()
            elif time.time() - float(first) > settle_s:
                waited = int(time.time() - float(first))
                reason = SE._receipt_reason(state, uid, us["attempt_dir"])
                if (reason == U.REASON_NO_OUTPUTS
                        and continue_fn(state_dir, uid, u, us, report,
                                        state)):
                    SS.save_state(state_dir, state)
                    return True
                if reason == U.REASON_NO_OUTPUTS:
                    used = len(us.get("continuations") or [])
                    if used:
                        report.append(
                            f"{uid}: {used} continuation(s) sent and it still "
                            f"produced nothing. The bound is the point: this "
                            f"fails for missing production evidence rather "
                            f"than being prodded again.")
                    SS._set_unit_state(us, "FAILED")
                    report.append(
                        f"{uid}: the job finished cleanly and its declared "
                        f"outputs never appeared, {waited}s on. This is a "
                        f"FAILED unit, not missing evidence: the scheduler "
                        f"will tell you it succeeded. Read the job's own log "
                        f"in {us['attempt_dir']}.")
                elif reason == U.REASON_NO_PRODUCED_CHANGE:
                    SS._set_unit_state(us, "FAILED")
                    receipt, why = SE.attested_receipt(
                        state, uid, us["attempt_dir"])
                    details = " ".join(
                        W.render_for_record(note, 2048)
                        for note in ((receipt or {}).get("notes") or [])
                        if not str(note).startswith("REASON="))
                    report.append(
                        f"{uid}: declared outputs are present, but no produced "
                        f"repository change was established, {waited}s on. "
                        f"This is a FAILED unit. "
                        f"{details or why or 'See the check receipt.'}")
                else:
                    SS._set_unit_state(us, "FAILED_EVIDENCE")
                    report.append(
                        f"{uid}: no verdict {waited}s after the first "
                        f"INCOMPLETE, past the {settle_s}s accounting settle "
                        f"window. Treating as terminal: the evidence never "
                        f"arrived. Check `sacct -j {us.get('job_id')}` by "
                        f"hand.")
    else:
        us.pop("incomplete_since", None)
    return False


def _handle_post_check_deadline_and_resume(
        uid, u, us, state, state_dir, rc, protocol_problem, checked_state,
        previous, dry_run, worktree_cleanup_states, report):
    """Apply provider resume, post-check deadline escalation, and checked state.

    Returns True when unit handling should stop for this pass (resumed or
    deadline-escalated).
    """
    if (not dry_run and u.get("kind") == "code"
            and (u.get("provider_resume") or {}).get("max", 0)
            and previous not in worktree_cleanup_states
            and not protocol_problem
            and rc == FAILED
            and SPRS.maybe_resume(state_dir, uid, u, us, report, state)):
        return True

    terminal = {"DONE", "FAILED", "FAILED_EVIDENCE", "READY_FOR_PR",
                "PREFLIGHT_REFUSED", "HELD"}
    if (checked_state not in terminal
            and SS._deadline_exceeded(u, us, state, uid)):
        if dry_run:
            report.append(
                f"{uid}: DRY RUN -- deadline_exceeded after check; would "
                f"become NEEDS_HUMAN without cancellation, claim "
                f"release, or retry")
            return True
        first_breach = SS._mark_deadline_exceeded(u, us)
        if first_breach:
            report.append(
                f"{uid}: NEEDS_HUMAN -- deadline_exceeded while the "
                f"attempt was being checked. It remains bound and its "
                f"output claim is retained; no cancellation or retry "
                f"was requested.")
        SS.save_state(state_dir, state)
        return True
    SS._set_unit_state(us, checked_state)
    if checked_state in terminal:
        SS._clear_deadline_breach(us)
    return False


def _apply_retry_and_teardown(
        uid, u, us, state, state_dir, attempt, rc, previous,
        previous_state_changed_at, previous_state_changed_at_basis, dry_run,
        retryable, default_max_attempts, worktree_cleanup_states, watch_fn,
        report):
    """Apply preemption retry policy, preserve unchanged state clock, and teardown."""
    if rc in retryable:
        policy = u.get("max_attempts", default_max_attempts)
        real_attempts = [a for a in us["attempts"]
                         if not str(a).startswith(SP.DRY_PREFIX)]
        if len(real_attempts) < policy:
            us["attempt_dir"] = None
            report.append(f"{uid}: preempted, will re-attempt "
                          f"({len(real_attempts)}/{policy})")
        else:
            SS._set_unit_state(us, "FAILED")
            if policy == 1 and "max_attempts" not in u:
                report.append(
                    f"{uid}: preempted once and not retried, because "
                    f"max_attempts defaults to 1. A retry starts in a "
                    f"FRESH EMPTY directory and redoes the whole unit, so "
                    f"repetition is opt-in: set max_attempts with a "
                    f"'retry' contract stating what one interruption "
                    f"costs, or split the unit smaller.")
            elif policy == 1:
                report.append(
                    f"{uid}: preempted, and the plan allows one attempt, "
                    f"so it is not retried. Raise max_attempts with a "
                    f"'retry' contract, or split the unit smaller.")
            else:
                report.append(f"{uid}: preempted {policy} times, giving up")
    if us.get("state") == previous:
        us["state_changed_at"] = previous_state_changed_at
        if previous_state_changed_at_basis is None:
            us.pop("state_changed_at_basis", None)
        else:
            us["state_changed_at_basis"] = previous_state_changed_at_basis
    if us.get("state") in worktree_cleanup_states:
        if not dry_run:
            if u.get("kind") == "code":
                watch = watch_fn(
                    state, uid, Path(attempt).name, us.get("job_id"))
                if watch is not None and not (
                        watch.get("status") == "checked"
                        and watch.get("checked_at")):
                    watch["status"] = "checked"
                    watch["checked_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            SS.save_state(state_dir, state)
            SW._archive_code_worktree(
                state, u, attempt, report, state_dir)
        else:
            SW._report_would_archive_code_worktree(
                state, u, attempt, report)


def _lor_claim_liveness(us):
    """Claim liveness for a Lor attempt from Lor, never from Paseo's registry."""
    agent = us.get("job_id")
    if not agent:
        return SP.CLAIM_UNKNOWN, "the lor attempt records no session id"
    obs = LIO.poll_session(
        str(agent),
        parent_session_id=SS.attempt_parent_session(us, us.get("attempt_dir")))
    if obs["state"] == "running":
        return SP.CLAIM_LIVE, f"lor session {agent} is still running"
    if obs["state"] in ("idle", "failed", "cancelled"):
        return SP.CLAIM_FREE, f"lor session {agent} is {obs['state']}"
    return SP.CLAIM_UNKNOWN, f"lor session {agent}: {obs['detail']}"


def _release_completed_output_claims(units, state, state_dir, root, dry_run,
                                     report):
    """Release output claims for units no longer occupying live resources."""
    if not dry_run:
        for uid, u in sorted(units.items()):
            us = SS._unit_state(state, uid)
            if (us.get("state") in SS.OPERATOR_TERMINAL_STATES
                    and us.get("attempt_dir")
                    and SS._occupies_live_resources(u, us)
                    and (u.get("kind") != "code" or W.attempt_belongs_to_host(
                        SS.trusted_launch_host_anchor(
                            state, uid, us["attempt_dir"])) is True)):
                if (u.get("kind") == "code" and SS.attempt_backend(
                        us, us["attempt_dir"]) == LIO.LOR_BACKEND):
                    live, why = _lor_claim_liveness(us)
                else:
                    live, why = SP._claim_liveness({
                        "attempt": Path(us["attempt_dir"]).name,
                        "kind": u.get("kind"), "job": us.get("job_id")})
                if live == SP.CLAIM_FREE:
                    us["terminal_resource_release"] = {
                        "attempt_dir": us["attempt_dir"],
                        "job_id": us.get("job_id"),
                        "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                        "basis": why}
                    SS.save_state(state_dir, state)
                    report.append(
                        f"{uid}: stopped attempt released resources -- {why}")
            if not SS._occupies_live_resources(u, us):
                SP._release_output_claims(u, root, state_dir)


def _dispatch_ready_units(plan, units, state, state_dir, root, dry_run,
                          max_new, plan_path, dispatch_targets, halted,
                          report, submit_fn, agent_preflight=None):
    """Hold failed-upstream units and dispatch every ready unit within caps."""
    budget = (plan.get("budget") or {}).get("gpu_hours")
    spent = sum(SS._unit_state(state, uid)["gpu_hours"] for uid in units)
    dispatched = 0
    dispatch_refusal = None

    for uid, u in sorted(units.items()):
        us = SS._unit_state(state, uid)
        if us["attempt_dir"] or us["state"] in ("DONE", "FAILED",
                                                "FAILED_EVIDENCE", "ABANDONED",
                                                "SUPERSEDED"):
            continue
        if uid == plan.get("canary") and us["state"] == "HELD":
            continue
        needs = u.get("needs") or []
        failed_upstream = [d for d in needs
                           if SS._unit_state(state, d)["state"] in
                           ("FAILED", "HELD", "FAILED_EVIDENCE",
                            "ABANDONED", "SUPERSEDED")]
        if failed_upstream:
            SS._set_unit_state(us, "HELD")
            report.append(f"{uid}: held, upstream "
                          f"{', '.join(failed_upstream)} will not complete")
            SS.save_state(state_dir, state)
            continue
        unmet = [d for d in needs
                 if SS._unit_state(state, d)["state"] != "DONE"]
        if unmet:
            if plan.get("canary") in unmet:
                report.append(f"{uid}: waiting on canary {plan['canary']}")
            continue
        want = float(u.get("gpu_hours") or 0)
        if budget is not None and spent + want > float(budget):
            halted = (f"budget: {spent + want:.1f} of {budget} GPU-hours would "
                      f"be committed")
            report.append(f"{uid}: SKIPPED -- {halted}")
            continue
        if max_new is not None and dispatched >= max_new:
            report.append(f"{uid}: skipped, --max-new-dispatches reached")
            continue

        live = [x for x in units
                if SS._occupies_live_resources(
                    units[x], SS._unit_state(state, x))]
        cap_all = (plan.get("limits") or {}).get("max_running")
        if cap_all is not None and len(live) >= cap_all:
            report.append(f"{uid}: waiting, {len(live)} of {cap_all} slots in "
                          f"use")
            continue
        pool = u.get("pool")
        if pool:
            caps = ((plan.get("limits") or {}).get("pools") or {})
            in_pool = [x for x in live if units[x].get("pool") == pool]
            if len(in_pool) >= caps.get(pool, 10**9):
                report.append(f"{uid}: waiting, pool {pool!r} full "
                              f"({len(in_pool)} of {caps[pool]})")
                continue

        dispatch_source = None
        if u.get("kind") == "code" and agent_preflight is not None:
            # Before allocation: an agent that cannot be started from here
            # (e.g. a Lor model with no LOR_SESSION_ID) is refused with no
            # attempt directory, worktree, claim or agent created.
            problem = agent_preflight(u)
            if problem:
                report.append(f"{uid}: REFUSING dispatch -- {problem}")
                dispatch_refusal = str(problem)
                continue
        if u.get("kind") == "code":
            target, problem = SP._dispatch_target_for_advance(
                u, dispatch_targets)
            if not problem:
                dispatch_source, problem = SP._dispatch_source_identity(
                    u, target)
            if problem:
                report.append(f"{uid}: REFUSING dispatch -- {problem}")
                dispatch_refusal = str(problem)
                continue

        unit_dir, err = SD._allocate(plan, u, root, state_dir)
        if err:
            SS._set_unit_state(us, "FAILED")
            report.append(f"{uid}: {err}")
            SS.save_state(state_dir, state)
            continue
        us["attempt_dir"] = unit_dir
        us["attempts"].append(f"{SP.DRY_PREFIX}{unit_dir}" if dry_run
                              else unit_dir)
        allocated_at = time.time()
        us["allocated_at"] = allocated_at
        SS._set_unit_state(us, "ALLOCATED", allocated_at)
        us["gpu_hours"] = float(us.get("gpu_hours") or 0) + want
        spent += want
        SE._capture_artifact_basis(state, uid, unit_dir, u,
                                   enable_slurm_exit=u.get("kind") == "slurm")
        SS.save_state(state_dir, state)

        claim_refusal, held_claims = (None, [])
        if not dry_run:
            claim_refusal, held_claims = SP._take_output_claims(
                u, root, state_dir, Path(unit_dir).name)
        submit_args = ({"dispatch_source": dispatch_source}
                       if dispatch_source is not None else {})
        if plan_path is not None:
            submit_args["plan_path"] = plan_path
        job_id, err = ((None, claim_refusal) if claim_refusal
                       else submit_fn(u, unit_dir, dry_run, state, state_dir,
                                      **submit_args))
        if err:
            if held_claims:
                SP._release_output_claims(u, root, state_dir)
            if isinstance(err, SP.PreflightRefusal):
                launch, _launch_err = W.read_launch_record(unit_dir)
                on_disk = ((launch or {}).get("preflight") or {})
                receipt = str(W.launch_record_path(unit_dir))
                us.setdefault("preflight_refusals", []).append({
                    "attempt_dir": unit_dir,
                    "receipt": (receipt if on_disk.get("status") == "refused"
                                else None),
                    "workspace": err.workspace,
                    "dirty_path_count": err.dirty_count,
                    "reason": err.reason,
                    "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                })
                if us.get("attempts"):
                    last = str(us["attempts"][-1])
                    if last in (str(unit_dir), f"{SP.DRY_PREFIX}{unit_dir}"):
                        us["attempts"].pop()
                us["attempt_dir"] = None
                SS._set_unit_state(us, "PREFLIGHT_REFUSED")
                us.pop("allocated_at", None)
                us["gpu_hours"] = max(
                    0.0, float(us.get("gpu_hours") or 0) - want)
                spent -= want
                cited = us["preflight_refusals"][-1]["receipt"]
                report.append(f"{uid}: {err}" +
                              (f"\n  receipt: {cited}" if cited else ""))
            else:
                SS._set_unit_state(us, "FAILED")
                report.append(f"{uid}: {err}")
            SS.save_state(state_dir, state)
            continue
        needs_bind = bool(
            job_id and not str(job_id).startswith(("dry-", "engine-")))
        us["job_id"] = str(job_id)
        SS._set_unit_state(us, "SUBMITTED")
        if needs_bind:
            us["bind_pending"] = True
        SS.save_state(state_dir, state)
        if held_claims:
            SP._note_claimed_attempt(u, root, state_dir, Path(unit_dir).name,
                                     job_id)
        berr = (_bind_attempt(state, uid, unit_dir, job_id, state_dir)
                if needs_bind else None)
        if berr:
            us["bind_pending"] = True
            report.append(f"{uid}: submitted {job_id} but {berr}. The job is "
                          f"running; the binding will be retried next advance.")
        else:
            us.pop("bind_pending", None)
        dispatched += 1
        report.append(f"{uid}: submitted {job_id} -> {unit_dir}")
        SS.save_state(state_dir, state)

    state["halted"] = halted
    SS.save_state(state_dir, state)
    return dispatched, halted, dispatch_refusal


def _emit_unit_final_intent(state_dir, project, uid, us, units, state,
                            dry_run, existing_outbox_keys, report,
                            emit_intent_fn):
    """Assemble attested closing evidence and emit or preview one tracker intent."""
    now = us.get("state")
    if not now:
        return
    evidence = None
    kind = (units.get(uid) or {}).get("kind")
    if now == "SUPERSEDED":
        evidence = SO.superseded_evidence(us, state_dir)
    if now == "DONE" and us.get("attempt_dir"):
        if SO.closing_evidence_for(kind) == "merged_pr":
            rp = us.get("merge_receipt")
            if not SO._has_bound_merge_evidence(
                    uid, us, {"receipt": rp} if rp else None) and not dry_run:
                produced = SE.trusted_produced_head(
                    state, uid, us["attempt_dir"])
                facts = SS.trusted_launch_facts(
                    state, uid, us["attempt_dir"])
                try:
                    recover_args = {
                        "expect_repo": (facts or {}).get("repository_remote")}
                    if V.INTEGRATION_CLAIM in (
                            (units.get(uid) or {}).get(
                                "requires_verification") or []):
                        recover_args["repo"] = (facts or {}).get("repo")
                        recover_args["require_target_binding"] = True
                        recover_args["expect_target"] = (
                            (units.get(uid) or {}).get("target_branch"))
                    recovered, _why = SE.admit_merge(
                        state_dir, uid, produced, **recover_args)
                except OutboxError:
                    recovered = None
                if (recovered
                        and recovered.get("merged_as") == us.get("merged_as")
                        and recovered.get("pr") == us.get("merge_pr")):
                    us["merge_receipt"] = dict(recovered)
                    SS.save_state(state_dir, state)
                    rp = recovered
        else:
            rp, _why = SE.attested_receipt(state, uid, us["attempt_dir"])
        evidence = {"receipt": rp} if rp else None
    if dry_run:
        action = SO.TRACKER_EVENTS.get(now)
        if action:
            verb = action[0]
            if (verb == "close" and now != "SUPERSEDED"
                    and SO.closing_evidence_for(kind) == "merged_pr"
                    and not SO._has_bound_merge_evidence(uid, us, evidence)):
                verb = "open_pr"
            key = SO._intent_key(
                project, uid, now, us, verb, kind, evidence)
        if action and key not in existing_outbox_keys:
            if verb == "close" and not evidence:
                report.append(
                    f"{uid}: DRY RUN -- would retry outbox emission for "
                    f"{now}, but no attested closing receipt is readable")
            else:
                report.append(
                    f"{uid}: DRY RUN -- would re-emit tracker {verb} "
                    f"intent from durable state {now}")
        return
    emit_intent_fn(state_dir, project, uid, us, units[uid], evidence)
