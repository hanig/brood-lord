"""Advance reconciliation phases: normalisation, cleanup, recovery, judgment, settlement.

Python 3.8+, standard library only; dependencies never point to the facade.
"""

import time
from pathlib import Path

import unit as U
import verify as V
import worktree as W
import swarm_dispatch as SD
import swarm_evidence as SE
import swarm_outbox as SO
import swarm_preflight as SP
import swarm_provider_stops as SPS
import swarm_state as SS
import swarm_workspaces as SW
from swarm_types import (
    DONE,
    INCOMPLETE,
    NAME,
    NEEDS_HUMAN,
    RUNNING,
)


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
            if not SD._bind(us["attempt_dir"], us["job_id"], state_dir):
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
        job, note = SW.reconcile_orphan(us["attempt_dir"], us.get("allocated_at"),
                                        kind=u.get("kind"))
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
            bind_error = SD._bind(us["attempt_dir"], job, state_dir)
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
                        bind_error = SD._bind(
                            us["attempt_dir"], recovered_job, state_dir)
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
