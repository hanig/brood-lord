"""CLI command handlers, profile/escalation policy, and _main() phases for review.py.

Python 3.8+, standard library only; never imports the review facade.
"""

import argparse


def cmd_ledger(args, facade):
    """Offline command outcomes are ledger status, never a review pass."""
    if not facade.valid_head(args.head):
        facade.config_error("--head must be a full lowercase commit SHA. Pass the "
                            "reviewed range head, not a moving ref or abbreviation.")
    if args.adjudicate is not None:
        if not facade.valid_digest(args.adjudicate):
            facade.config_error("--adjudicate must be a full lowercase SHA-256 digest. "
                                "Pass a validated finding digest from --open-findings.")
        if (not args.author or any(not facade.valid_author(a) for a in args.author)
                or not facade.valid_identity(args.accepted_by)):
            facade.config_error("Pass --author PROVIDER/MODEL and --accepted-by NAME "
                                "as ASCII tokens of at most 256 characters: letters, "
                                "digits, _ . : @ + - and nonempty / components; "
                                "each component starts with a letter or digit.")
        if facade.adjudicator_is_author(args.accepted_by, args.author):
            facade.config_error("the author cannot accept its own rebuttal. Pass the "
                                "independent owner or mandated orchestrator in --accepted-by.")
        if not args.reason or not args.reason.strip():
            facade.config_error("Pass a non-empty --reason for the external decision.")
    elif any(value is not None for value in
             (args.accepted_by, args.reason, args.decision, args.round)):
        facade.config_error("Drop adjudication fields when querying --open-findings.")
    try:
        records = facade.read_review_journal(facade.review_journal_path(args.file))
        findings, adjudicated, unattributable, unreadable = facade.journal_findings(
            records, args.head)
    except (OSError, ValueError, argparse.ArgumentTypeError) as exc:
        facade.config_error(facade.redact(
            f"cannot read adjudication history: {exc}. "
            "Pass the original state home after repairing its journal."))
    if args.open_findings:
        opened = [finding for key, finding in findings.items()
                  if key not in adjudicated]
        print(facade.json.dumps(facade.redact_ledger({
            "state": ("UNATTRIBUTABLE" if unattributable else
                      "OPEN_FINDINGS" if opened else "NO_OPEN_FINDINGS"),
            "reviewed_head": args.head, "open_findings": opened,
            "unattributable_records": unattributable,
            "unreadable_records": unreadable,
        }), indent=2))
        return 1 if opened or unattributable else 0
    selected_round = None if args.round == "plan" else args.round
    matches = [key for key in findings if key[1] == args.adjudicate
               and (args.round is None or key[0] == selected_round)]
    if not matches:
        facade.config_error("no confirmed finding matches this digest, head and round. "
                            "Pass a finding listed by --open-findings --head SHA.")
    if len(matches) != 1:
        selectors = sorted({"--round plan" if key[0] is None
                            else f"--round {key[0]}" for key in matches})
        facade.config_error("the finding occurs in multiple rounds. Available selectors: "
                            + ", ".join(selectors) + ". Pass one to identify the occurrence.")
    args.decision = args.decision or "overruled"
    journal = facade.record_adjudication(args, matches[0][0])
    print(facade.json.dumps(facade.redact_ledger({
        "state": ("ADJUDICATION_RECORDED" if journal["written"]
                  else "ADJUDICATION_UNCONFIRMED"),
        "reviewed_head": args.head, "round": matches[0][0],
        "finding_digest": args.adjudicate, "decision": args.decision,
        "journal": journal, "review_verdict_changed": False,
    }), indent=2))
    return 0


def cmd_record_evidence(args, facade):
    """Audit-only evidence bound to identity, head and tree; never a verdict change."""
    tree = facade.candidate_tree(args.head) if facade.valid_head(args.head) else None
    if tree is None or args.tree not in (None, tree):
        facade.config_error("Pass --head as the full checked commit; --tree, if given, "
                            "must be its exact tree.")
    try:
        record = facade._evidence_module().build_record(
            identity=facade.change_identity(
                "origin/main" if args.target is None else args.target,
                args.plan_name, args.unit),
            reviewed_head=args.head, finding_digest=args.finding, round_no=args.round,
            candidate_tree=tree, method=args.method, status=args.status,
            test_ref=None if args.test_ref is None else facade.redact(args.test_ref),
            summary=None if args.note is None else facade.redact(args.note))
    except (OSError, ValueError, RuntimeError) as exc:
        facade.config_error(facade.redact(
            f"evidence refused: {exc}. Pass valid fields and the review's "
            "--target/--plan-name/--unit."))
    journal = facade._ledger.record_evidence(record, facade)
    print(facade.json.dumps(facade.redact_ledger({
        "state": "EVIDENCE_RECORDED" if journal["written"] else "EVIDENCE_UNCONFIRMED",
        "reviewed_head": args.head, "finding_digest": args.finding,
        "candidate_tree": tree, "journal": journal, "review_verdict_changed": False,
    }), indent=2))
    return 0


def changed_paths(args, facade):
    """Collect paths for the same sources as gather; uncertainty costs a tier."""
    paths = set()
    revision = (["HEAD"] if args.diff else ["--cached"] if args.staged
                else [args.range] if args.range else None)
    if revision is not None:
        try:
            result = facade.subprocess.run(
                ["git", "diff", "--name-only", "-z", "--no-relative", "--no-renames",
                 "--no-ext-diff", "--no-textconv", *revision, "--"],
                stdout=facade.subprocess.PIPE, stderr=facade.subprocess.PIPE, timeout=30)
            if result.returncode:
                return None, "Git could not determine changed paths"
            raw = result.stdout.decode("utf-8")
            if raw and not raw.endswith("\0"):
                return None, "Git returned an incomplete changed-path set"
            paths.update(raw[:-1].split("\0") if raw else [])
        except (OSError, facade.subprocess.SubprocessError, UnicodeError, ValueError):
            return None, "Git changed-path lookup was unavailable"
    if args.file:
        top = facade.git_out("rev-parse", "--show-toplevel").rstrip("\n")
        root = facade.Path(top) if top else facade.Path.cwd()
        for value in args.file:
            path = facade.Path(value)
            if ".." in path.parts:
                return None, "a --file path contains unresolved parent components"
            absolute = path if path.is_absolute() else facade.Path.cwd() / path
            try:
                lexical = absolute.relative_to(root)
                target = absolute.resolve(strict=True).relative_to(root.resolve())
                paths.update((lexical.as_posix(), target.as_posix()))
            except (OSError, RuntimeError, ValueError):
                return None, "a --file path or target is outside the review root or unresolved"
    return paths, ""


def profile_for_paths(paths, unknown_reason=""):
    """Only a known, nonempty, wholly documentary path set earns fast."""
    if paths is None:
        return "standard", unknown_reason or "changed paths could not be determined"
    if not paths:
        return "standard", "no changed paths could be determined"
    for path in paths:
        parts = path.split("/")
        if (parts[-1] == "reviewers.json"
                or any(p in ("scripts", "lib", "bin", "tests") for p in parts[:-1])
                or not (path.endswith(".md")
                        or path.startswith(("docs/", "examples/")))):
            return "standard", "at least one changed path is not documentation"
    return "fast", f"all {len(paths)} changed paths are documentation"


def select_profile(args, facade):
    if args.profile is not None:
        return args.profile, ("plan review" if args.kind == "plan"
                              else "explicit --profile")
    if args.list:
        return facade.DEFAULT_PROFILE, "configured default for --list"
    return facade.profile_for_paths(*facade.changed_paths(args))


def escalation_tiers(args, facade):
    start = getattr(args, "selected_profile", "fast")
    ladder = facade.LADDER
    if start not in ladder:
        facade.config_error("--escalate requires an implementation tier: "
                            "Pass --profile fast, standard, or deep.")
    return ladder[ladder.index(start):]


def author_argument(value, facade):
    """Keep the complete model ID after the first provider separator."""
    if not facade.valid_author(value):
        raise argparse.ArgumentTypeError(
            "author must be PROVIDER/MODEL without whitespace or control "
            "characters, using ASCII token components (at most 256 characters); "
            "pass the provider and the model id the reviewer runs as, e.g. openai/MODEL")
    return value


def round_argument(value):
    """Keep an explicit plan selector distinct from an omitted selector."""
    if value == "plan":
        return value
    try:
        return int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("round must be an integer or plan")


def in_profile(rev, profile):
    """An undeclared or empty profiles list joins no profile."""
    return profile in (rev.get("profiles") or [])


def implementation_panel_policy(args, roster, selected, facade):
    """Validate the declared cycle without turning audit history into authority."""
    override = args.allow_single_reviewer
    if override is not None and (
            not override.strip() or override.splitlines() != [override]):
        facade.config_error("--allow-single-reviewer needs a non-empty one-line "
                            "reason. Pass the reason for the exceptional quorum.")
    if args.kind != "implementation":
        if override is not None or args.fresh_cycle_from is not None:
            facade.config_error("--allow-single-reviewer and --fresh-cycle-from are "
                                "implementation-only. Drop them for a plan review.")
        return {"single_reviewer_override": None, "fresh_cycle": None}
    if override is not None and args.quorum != 1:
        facade.config_error("--allow-single-reviewer is only for --quorum 1. Drop "
                            "the override when requesting a larger quorum.")

    fresh = None
    if args.fresh_cycle_from is not None:
        previous = {r["name"] for r in roster
                    if r.get("enabled", True)
                    and facade.in_profile(r, args.fresh_cycle_from)}
        if not previous:
            facade.config_error("the replaced profile has no enabled reviewers. "
                                "Pass --fresh-cycle-from with a populated profile.")
        floor = max(2, len(previous))
        selected_names = [r["name"] for r in selected if r.get("enabled", True)
                          and (not args.escalate
                               or any(facade.in_profile(r, tier)
                                      for tier in facade.escalation_tiers(args)))]
        panel = set(selected_names)
        if len(panel) != len(selected_names):
            facade.config_error("a fresh cycle cannot count duplicate reviewer "
                                "names as independent verdicts. Pass a routing "
                                "configuration with distinct enabled reviewer names.")
        if len(panel) < floor:
            facade.config_error(
                f"fresh cycle from {args.fresh_cycle_from} requires at least "
                f"{floor} reviewers; the selected panel has {len(panel)}. "
                "Drop --only or select a profile with enough reviewers; "
                "a single-reviewer override cannot lower this floor.")
        fresh = {"replaces_profile": args.fresh_cycle_from,
                 "profile_reviewers": sorted(previous),
                 "minimum_reviewers": floor,
                 "provenance": "caller-declared; current enabled profile"}

    if args.quorum == 1 and override is None:
        facade.config_error(
            "an implementation review needs at least two verdicts. Pass "
            "--quorum 2 or, for an explicit exception, "
            "--allow-single-reviewer REASON; the override will be recorded.")
    if fresh is not None:
        args.quorum = max(args.quorum, fresh["minimum_reviewers"])
    return {"single_reviewer_override": override, "fresh_cycle": fresh}


def panel_policy_text(policy, facade):
    """Provenance belongs on the verdict line, including non-pass outcomes."""
    parts = []
    fresh = policy["fresh_cycle"]
    if fresh is not None:
        parts.append(f"FRESH_CYCLE from {fresh['replaces_profile']}; "
                     f"minimum {fresh['minimum_reviewers']} reviewers; "
                     f"{fresh['provenance']}")
    if policy["single_reviewer_override"] is not None:
        parts.append("SINGLE_REVIEWER_OVERRIDE: "
                     + facade.redact(policy["single_reviewer_override"]))
    return "" if not parts else " — " + "; ".join(parts)


def validate_escalation_panel(all_reviewers, args, facade):
    """Refuse an empty ladder before main arms the provider watchdog."""
    tiers = facade.escalation_tiers(args)
    if not any(facade.in_profile(r, tier) for tier in tiers for r in all_reviewers):
        facade.config_error(
            "no reviewer is in any escalation tier "
            f"({', '.join(tiers)}). A reviewer with no 'profiles' key, or an "
            f"empty one, is in NO profile, so --escalate would run nobody "
            f"and report no findings, which is indistinguishable from a "
            f"clean review. Name one or more tiers in that reviewer's "
            f"'profiles' in reviewers.json, or pass --only to select "
            f"reviewers directly.")
    return tiers


def escalate(all_reviewers, prompt, args, truncated, label, body_len, facade):
    """Cheapest-first escalation stopping at the first failing tier with quorum."""
    seen, completed, failed, unavailable = set(), [], [], []
    tiers_run = []
    tiers = facade.validate_escalation_panel(all_reviewers, args)

    bad_so_far = False
    for tier in tiers:
        panel = [r for r in all_reviewers
                 if facade.in_profile(r, tier) and r["name"] not in seen]
        runnable, tier_unavail = [], []
        for rev in panel:
            why = facade.availability(rev)
            (tier_unavail if why else runnable).append(
                facade.unavailable_entry(rev, why) if why else rev)
        unavailable.extend(tier_unavail)
        for r in panel:
            seen.add(r["name"])
        if not runnable:
            continue
        tiers_run.append(tier)
        if not args.json:
            names = ", ".join(r["name"] for r in runnable)
            print(f"\ntier {tier}: {names}")
        with facade.concurrent.futures.ThreadPoolExecutor(
                max_workers=len(runnable)) as ex:
            res = list(ex.map(
                lambda r: facade.run_one(r, prompt, args.timeout, len(args.claim),
                                         args.claim),
                runnable))
        tier_completed = [r for r in res if r["ok"]]
        completed.extend(tier_completed)
        failed.extend(r for r in res if not r["ok"])
        if not args.json:
            for r in res:
                if r["ok"]:
                    print(f"  {r['name']:<18} {str(r['verdict']).upper():<9} "
                          f"{len(r['findings'])} finding(s)  {r['elapsed_s']}s  "
                          f"{seat_where(r)}")
                else:
                    print(f"  {r['name']:<18} ERROR     {seat_where(r)}  {r['error'][:70]}")
        bad_so_far = bad_so_far \
            or any(facade.is_confirmed(f) for r in tier_completed for f in r["findings"]) \
            or any(facade.norm(c.get("status")) == "refuted"
                   for r in tier_completed for c in r["claims"])
        bad = bad_so_far
        if bad and len(completed) >= args.quorum:
            if not args.json:
                print(f"  -> tier {tier} found problems with "
                      f"{len(completed)} verdict(s) (quorum {args.quorum}); "
                      f"stopping before the dearer tiers.")
            break
        if bad:
            if not args.json:
                print(f"  -> tier {tier} found problems but only "
                      f"{len(completed)} verdict(s) (quorum {args.quorum}); "
                      f"escalating to adjudicate rather than acting on one "
                      f"opinion.")
            continue
        if len(completed) < args.quorum:
            if not args.json:
                print(f"  -> only {len(completed)} verdict(s) so far "
                      f"(quorum {args.quorum}); escalating to recruit more.")
            continue
    return completed, failed, unavailable, tiers_run


def seat_where(entry):
    """provider/route[ job N] for human output; receipts carry the fields."""
    job = entry.get("slurm_job_id")
    return (f"[{entry.get('provider') or '?'}/{entry.get('route') or '?'}"
            + (f" job {job}" if job else "") + "]")


def decide_state(n_completed, n_failed, confirmed, refuted_claims,
                 rejecting, truncated, quorum, n_out_of_scope_critical=0,
                 n_incomplete=0):
    """Compute the gate's verdict state from completed and failed panels."""
    if n_completed == 0:
        return "REVIEW_INCOMPLETE" if n_incomplete else "REVIEW_UNAVAILABLE"
    if n_completed >= quorum:
        if confirmed:
            return "REVIEW_FAIL"
        if refuted_claims:
            return "REVIEW_CLAIMS_REFUTED"
    if n_incomplete:
        return "REVIEW_INCOMPLETE"
    if n_completed < quorum:
        return "REVIEW_PARTIAL"
    if n_failed:
        return "REVIEW_PARTIAL"
    if n_out_of_scope_critical:
        return "REVIEW_PARTIAL"
    if rejecting:
        return "REVIEW_PARTIAL"
    if truncated:
        return "REVIEW_PARTIAL"
    return "REVIEW_PASS"


def _slurm_job_in_error(error):
    """The job id in a transport error's `slurm job <digits>`, or None."""
    text = str(error or "")
    start = text.find("slurm job ")
    while start != -1:
        digits = ""
        for ch in text[start + len("slurm job "):]:
            if not ch.isdigit() or not ch.isascii():
                break
            digits += ch
        if digits and (start == 0 or not text[start - 1].isalnum()):
            return digits
        start = text.find("slurm job ", start + 1)
    return None


def plan_slurm_job_ids(answers):
    """Ordered unique Slurm job ids across one plan seat's turns.

    A turn's usage carries slurm_job_id when its CLI ran as a job; a turn that
    failed after submission names its job only in the transport error."""
    job_ids = []
    for answer in answers or ():
        if not isinstance(answer, dict):
            continue
        usage = answer.get("usage") if isinstance(answer.get("usage"), dict) else {}
        job_id = usage.get("slurm_job_id")
        if not (isinstance(job_id, str) and job_id.strip()):
            job_id = _slurm_job_in_error(answer.get("error"))
        if job_id and job_id.strip() not in job_ids:
            job_ids.append(job_id.strip())
    return job_ids


def cmd_plan_review(args, members, prompt, label, truncated, profile_reason,
                    excluded, facade):
    """Public plan-review entry."""
    plan = facade._load_plan_review()
    panel_error = facade._check_canonical_plan(members)
    if panel_error:
        facade.disarm_watchdog()
        print(f"error: {panel_error}", file=facade.sys.stderr)
        print("REVIEW_ERROR — configuration problem; no review took place.",
              file=facade.sys.stderr)
        return facade.STATES["REVIEW_ERROR"]
    unavailable = [facade.unavailable_entry(m, facade.availability(m))
                   for m in members if facade.availability(m)]
    if unavailable:
        state, exchange, completed = "REVIEW_UNAVAILABLE", {
            "members": {}, "turns": [], "joint_table": []}, []
    else:
        exchange, first, second = plan._run_plan_exchange(
            members, prompt, args.claim, args.timeout,
            validate=plan._plan_schema_error)
        state, exchange["joint_table"] = plan._joint_plan_verdict(
            members, first, second, args.claim, truncated)
        completed = [] if state == "REVIEW_UNAVAILABLE" else [
            {**second[m["name"]], "name": m["name"],
             "findings": [row["finding"] for row in exchange["joint_table"]
                          if row["author"] == m["name"]
                          and row["outcome"] not in ("refuted", "restated")]}
            for m in members]
    args.plan_exchange = exchange
    # A plan panel runs whole or not at all: one blocked seat leaves both unrun.
    rows = {"completed": [], "failed": [], "unavailable": []}
    for m in members:
        answers = exchange["members"].get(m["name"], {}).get("answers", [])
        status = ("unavailable" if unavailable else
                  "completed" if completed and not any(a.get("error") for a in answers)
                  else "failed")
        row = dict(facade.seat_placement(m), name=m["name"])
        job_ids = plan_slurm_job_ids(answers)
        if job_ids:
            row["slurm_job_id"] = job_ids[-1]
            if len(job_ids) > 1:
                row["slurm_job_ids"] = job_ids
        rows[status].append(row)
    args.seat_routes = facade.build_seat_routes(**rows)
    facade.disarm_watchdog()
    journal = facade.record_review_round(args, completed, state)
    report = {"state": state, "checked_at": facade.now(), "reviewed": label,
              "profile": "plan", "profile_reason": profile_reason, "escalated": False,
              "quorum": 2, "truncated": truncated, "excluded": excluded,
              "unavailable": unavailable, "results": completed,
              "panel_policy": args.panel_policy, "input_redactions": args.input_redactions,
              "plan_exchange": exchange, "seat_routes": args.seat_routes, "journal": journal}
    if args.json:
        print(facade.redact(facade.json.dumps(report, indent=2)))
    else:
        for name, member in exchange["members"].items():
            for answer in member["answers"]:
                print(facade.redact(f"\n{name} TURN {answer['turn']} usage={answer['usage']}"))
                raw = answer["raw"] or answer["error"] or "[no reply]"
                verdict = answer["verdict"]
                if isinstance(verdict, dict) and isinstance(verdict.get("findings"), list):
                    displayed = dict(
                        verdict,
                        findings=[facade.finding_for_text(f) if isinstance(f, dict) else f
                                  for f in verdict["findings"]])
                    if displayed != verdict:
                        raw = facade.json.dumps(displayed, indent=2)
                print(facade.redact(raw))
                if answer["error"]:
                    print(facade.redact(f"ERROR: {answer['error']}"))
        print("\nJOINT TABLE:")
        for row in exchange["joint_table"]:
            finding = facade.finding_for_text(row["finding"])
            print(facade.redact(facade.json.dumps(dict(row, finding=finding))))
            if (facade.is_confirmed(finding) and "counterexample" not in finding
                    and "non_executable_reason" not in finding):
                print("      no counterexample")
        for entry in unavailable:
            print(facade.redact(f"{entry['name']}: {entry['reason']} {seat_where(entry)}"))
        print(f"\n{state} — two seats, two turns; a pass is not proof.")
    return facade.PLAN_STATES[state]


def _dispatch_early_commands(args, facade):
    """Handle offline ledger commands and --size preflight before loading reviewers."""
    evidence = args.record_evidence
    if not evidence and any(getattr(args, name) is not None for name in (
            "finding", "method", "status", "tree", "test_ref", "note")):
        facade.config_error("Pass --record-evidence with evidence fields, or drop them.")
    if args.adjudicate is not None or args.open_findings or evidence:
        if (args.diff or args.staged or args.range or args.file or args.list or args.size or args.kind
                or args.plan or args.claim or args.escalate or args.only
                or args.profile or args.dispositions or args.fresh_cycle_from
                or args.allow_single_reviewer or args.context or args.threat_model
                or args.require_premise_ack or {args.premise_memo, args.continue_patching} != {None}
                or (args.accepted_by or args.reason or args.decision if evidence else
                    args.target is not None or args.plan_name is not None or args.unit is not None)):
            facade.config_error("Drop review options when using a ledger command; "
                                "adjudication never runs or replaces a review.")
        facade.sys.exit(cmd_record_evidence(args, facade) if evidence else facade.cmd_ledger(args))
    if args.round == "plan":
        facade.config_error("--round plan is only an adjudication selector. "
                            "Pass --kind plan without --round for a plan review.")
    if any(value is not None for value in
           (args.head, args.accepted_by, args.reason, args.decision)):
        facade.config_error("Pass --adjudicate or --open-findings with ledger fields.")
    if not (args.diff or args.staged or args.range or args.file):
        args.diff = True
    if args.size:
        if args.list:
            facade.config_error("Drop --list when using the offline --size preflight.")
        facade.sys.exit(facade.cmd_size(args))


def _validate_protocol_flags(args, facade):
    """Enforce plan vs implementation flags, rounds, claims, and dispositions."""
    if args.plan:
        if args.kind and args.kind != "plan":
            facade.config_error(f"--plan and --kind {args.kind} disagree. Pass one.")
        args.kind = "plan"
    if args.profile == "plan" and args.kind is None:
        args.kind = "plan"
    if not args.list and args.kind is None:
        facade.config_error(
            "--kind is required: pass `--kind plan` for acceptance criteria or "
            "a design (two contrasting models, never escalated), or "
            "`--kind implementation --round N` for code. Leaving it implicit "
            "is how a design proposal got reviewed by the four-model "
            "implementation panel.")
    if args.kind == "implementation" and args.round is None and not args.list:
        facade.config_error(
            f"--kind implementation requires --round N (1..{facade.MAX_ROUNDS}), so "
            f"the bound on rounds per change can be applied. Pass --round 1 if "
            f"this is the first round for this change.")
    if (args.kind == "implementation" and not args.list
            and not facade.has_honest_run_claim(args.claim)):
        facade.config_error(
            f"every implementation review must assert the required "
            f"counter-claim: {facade.HONEST_RUN_CLAIM!r} Pass it with --claim so the "
            f"gate checks that honest work still succeeds.")
    if (args.kind == "implementation" and args.round is not None
            and args.round >= 2 and not args.list and not args.dispositions):
        facade.config_error(
            "implementation round 2 and later requires --dispositions FILE "
            "mapping every prior confirmed finding to reproduced, "
            "not-reproduced, or deferred with a one-line reason. Pass the "
            "digest-keyed JSON file from the prior round.")
    if args.kind == "plan":
        if args.round is not None:
            facade.config_error("plan reviews take no --round; drop --round.")
        if args.escalate:
            facade.config_error("--plan and --escalate are contradictory: a plan "
                                "review is TWO contrasting models and stops there. "
                                "Drop --escalate, or drop --plan if this is an "
                                "implementation.")
        if args.profile and args.profile != "plan":
            facade.config_error(f"a plan review uses the 'plan' panel; remove "
                                f"--profile {args.profile}.")
        args.profile = "plan"
        if args.quorum > 2:
            facade.config_error(f"--plan runs two reviewers, so quorum {args.quorum} "
                                f"can never be met. Pass --quorum 2, or drop --plan.")
    if args.round is not None:
        if args.round < 1:
            facade.config_error(f"--round must be 1 or more, got {args.round}.")
        if args.round > facade.MAX_ROUNDS:
            facade.config_error(
                f"round {args.round} exceeds the bound of {facade.MAX_ROUNDS} for one "
                f"change. Past this, more rounds have not converged -- they "
                f"have been finding defects in the previous round's fixes. "
                f"Step back to root cause: state what keeps recurring, fix "
                f"THAT, declare new acceptance criteria, and start again at "
                f"--round 1 --fresh-cycle-from PROFILE naming the exhausted "
                f"cycle's last reserved profile.")


def _apply_author_exclusion(args, roster, reviewers, profile, profile_reason, facade):
    """Exclude declared authors, apply reserve substitution, and validate plan panel."""
    authors = facade.author_model_ids(args.author, roster)
    reviewers, excluded = facade.exclude_authors(reviewers, authors)
    if excluded and args.kind == "plan" and not (args.author or args.list):
        facade.config_error("Pass plan reviewers with a derivable model maker.")
    if excluded:
        required = max(args.quorum, 2 if args.kind == "plan" else 1)
        if args.fresh_cycle_from:
            required = max(required, 2, len({r["name"] for r in roster
                           if r.get("enabled", True)
                           and facade.in_profile(r, args.fresh_cycle_from)}))
        eligible = [r for r in reviewers if r.get("enabled", True)
                    and (not args.escalate or any(facade.in_profile(r, tier)
                                                  for tier in facade.escalation_tiers(args)))]
        plan_check = facade._check_canonical_plan if args.kind == "plan" else None
        attempted = False
        selection = None
        if not (args.list or args.only or args.escalate):
            panel, attempted = facade._panels.fill_profile(
                roster, profile, eligible, authors, required, excluded, plan_check)
            if panel is not None:
                eligible, added = panel
                reviewers = eligible
                selection = facade._panels.panel_selection(
                    profile, excluded, added, eligible)
        if not args.json:
            for removal in excluded:
                print(f"{removal['name']} excluded: {removal['reason']}")
        if selection:
            print("Panel substitution " + facade.redact(facade.json.dumps(selection)),
                  file=facade.sys.stderr if args.json else facade.sys.stdout, flush=True)
        remaining = len(eligible)
        if not args.list and not selection and (
                attempted or (excluded and remaining < required)):
            reason = (f"author exclusion leaves {remaining} eligible reviewers; "
                      f"required quorum is {required}. Select more independent "
                      "reviewers; author exclusion cannot lower quorum.")
            if attempted:
                reason += " Profile reserves cannot restore quorum and contrast."
            facade.disarm_watchdog()
            if args.json:
                print(facade.json.dumps({"state": "REVIEW_UNAVAILABLE",
                                         "profile": profile,
                                         "profile_reason": profile_reason,
                                         "checked_at": facade.now(), "reason": reason,
                                         "quorum": required, "excluded": excluded,
                                         "results": []}, indent=2))
            else:
                print("REVIEW_UNAVAILABLE — " + reason)
            facade.sys.exit(facade.STATES["REVIEW_UNAVAILABLE"])
    if args.kind == "plan" and not args.list:
        runnable = [r for r in reviewers if r.get("enabled", True)]
        panel_error = facade._load_plan_review()._plan_panel_error(runnable)
        if panel_error:
            facade.config_error(panel_error)
        if args.quorum != 2:
            facade.config_error(
                f"a plan review needs both verdicts: pass --quorum 2, not "
                f"{args.quorum}. Below that one model decides; above it, the "
                f"panel can never reach quorum.")
    return reviewers, excluded


def _sanitize_review_inputs(args, dispositions, facade):
    """Strip recognized prior-decision disclosures from context and dispositions."""
    args.input_redactions = []
    args.context = facade.strip_review_input(
        args.context, "--context", args.input_redactions)
    for index, entry in enumerate(dispositions):
        if entry["disposition"] in ("reproduced", "not-reproduced"):
            for field in ("location", "summary", "reason"):
                entry[field] = facade.strip_review_input(
                    entry[field],
                    f"--dispositions {args.dispositions} entry {index} {field}",
                    args.input_redactions)
            for field in ("setup", "command", "expected", "observed", "revision"):
                entry["reproduction"][field] = facade.strip_review_input(
                    entry["reproduction"][field],
                    f"--dispositions {args.dispositions} entry {index} reproduction.{field}",
                    args.input_redactions)


def _execute_implementation_panel(args, reviewers, prompt, body, label,
                                  truncated, profile, profile_reason, excluded,
                                  policy_text, facade):
    """Check availability, arm watchdog, and execute fixed or escalated panel."""
    runnable, unavailable = [], []
    for rev in reviewers:
        why = facade.availability(rev)
        (unavailable if why else runnable).append(
            facade.unavailable_entry(rev, why) if why else rev)

    if not runnable:
        facade.disarm_watchdog()
        args.seat_routes = facade.build_seat_routes(unavailable=unavailable)
        journal = facade.record_review_round(args, [], "REVIEW_UNAVAILABLE")
        facade.release_round(args, [], "REVIEW_UNAVAILABLE")
        report = {"state": "REVIEW_UNAVAILABLE", "checked_at": facade.now(),
                  "profile": profile, "profile_reason": profile_reason,
                  "reviewed": label, "unavailable": unavailable, "results": [],
                  "seat_routes": args.seat_routes,
                  "excluded": excluded,
                  "panel_policy": args.panel_policy,
                  "input_redactions": args.input_redactions,
                  "journal": journal}
        if args.json:
            print(facade.redact(facade.json.dumps(report, indent=2)))
        else:
            if args.input_redactions:
                print(facade.redact(facade.redaction_notice(args.input_redactions)))
            print("REVIEW_UNAVAILABLE" + policy_text
                  + " — no reviewer could run:")
            for u in unavailable:
                print(f"  - {u['name']} {seat_where(u)}: {u['reason']}")
            print("\nThis is NOT a pass. The change is unreviewed.")
        facade.sys.exit(facade.STATES["REVIEW_UNAVAILABLE"])

    if not args.json:
        print(f"reviewing {label} ({len(body)} chars"
              f"{', TRUNCATED' if truncated else ''}) "
              f"with {len(runnable)} reviewer(s) [profile: {profile}]...")

    tiers_run = [profile]
    if args.escalate:
        facade.validate_escalation_panel(reviewers, args)
    facade.arm_watchdog(args.watchdog if args.watchdog is not None
                        else max(1800, args.timeout * 3))
    if args.escalate:
        completed, failed, unavailable, tiers_run = facade.escalate(
            reviewers, prompt, args, truncated, label, len(body))
        if not completed and not failed:
            facade.disarm_watchdog()
            args.seat_routes = facade.build_seat_routes(unavailable=unavailable)
            journal = facade.record_review_round(args, [], "REVIEW_UNAVAILABLE")
            facade.release_round(args, [], "REVIEW_UNAVAILABLE")
            report = {"state": "REVIEW_UNAVAILABLE", "checked_at": facade.now(),
                      "profile": profile, "profile_reason": profile_reason,
                      "reviewed": label, "unavailable": unavailable, "results": [],
                      "seat_routes": args.seat_routes,
                      "excluded": excluded, "panel_policy": args.panel_policy,
                      "input_redactions": args.input_redactions, "journal": journal}
            if args.json:
                print(facade.redact(facade.json.dumps(report, indent=2)))
            elif args.input_redactions:
                print(facade.redact(facade.redaction_notice(args.input_redactions)))
            print("REVIEW_UNAVAILABLE" + policy_text
                  + " — no reviewer could run in any tier", file=facade.sys.stderr)
            facade.sys.exit(facade.STATES["REVIEW_UNAVAILABLE"])
    else:
        with facade.concurrent.futures.ThreadPoolExecutor(
                max_workers=len(runnable)) as ex:
            results = list(ex.map(
                lambda r: facade.run_one(r, prompt, args.timeout, len(args.claim),
                                         args.claim),
                runnable))
        completed = [r for r in results if r["ok"]]
        failed = [r for r in results if not r["ok"]]
    return completed, failed, unavailable, tiers_run


def _collect_and_emit_verdict(args, completed, failed, unavailable, tiers_run,
                              label, truncated, profile, profile_reason,
                              excluded, policy_text, tier_text, facade):
    """Aggregate findings/claims, record journal, emit JSON or prose, and exit."""
    confirmed, refuted_claims, rejecting = [], [], []
    out_of_scope = []
    for r in completed:
        if facade.norm(r.get("verdict")) == "refuted":
            rejecting.append(r["name"])
        for f in r["findings"]:
            if facade.is_confirmed(f):
                confirmed.append({**f, "reviewer": r["name"]})
            elif f.get("in_scope") is False:
                out_of_scope.append({**f, "reviewer": r["name"]})
        for c in r["claims"]:
            if facade.norm(c.get("status")) == "refuted":
                refuted_claims.append({**c, "reviewer": r["name"]})

    oos_critical = [f for f in out_of_scope
                    if facade.norm(f.get("severity")) == "critical"]
    incomplete = [r for r in failed if r.get("incomplete")]
    state = facade.decide_state(
        n_completed=len(completed), n_failed=len(failed),
        confirmed=confirmed, refuted_claims=refuted_claims,
        rejecting=rejecting, truncated=truncated,
        quorum=args.quorum,
        n_out_of_scope_critical=len(oos_critical),
        n_incomplete=len(incomplete))
    facade.disarm_watchdog()
    args.seat_routes = facade.build_seat_routes(completed, failed, unavailable)
    journal = facade.record_review_round(args, completed, state)
    facade.release_round(args, completed, state)

    report = {
        "state": state, "checked_at": facade.now(), "reviewed": label,
        "profile": profile, "escalated": bool(args.escalate),
        "profile_reason": profile_reason,
        "excluded": excluded,
        "tiers_run": tiers_run,
        "panel_policy": args.panel_policy,
        "input_redactions": args.input_redactions,
        "truncated": truncated, "quorum": args.quorum,
        "completed": len(completed), "unavailable": unavailable,
        "failed": [{"name": r["name"], "error": r["error"],
                    "incomplete": bool(r.get("incomplete")),
                    "provider": r.get("provider"), "model": r.get("model"),
                    "route": r.get("route")}
                   for r in failed],
        "seat_routes": args.seat_routes,
        "confirmed_findings": confirmed, "refuted_claims": refuted_claims,
        "out_of_scope_findings": out_of_scope,
        "rejecting_reviewers": rejecting,
        "journal": journal,
        "premise_check": facade._premise_module().final(args, completed, facade),
        "premise_ack": getattr(args, "premise_ack", None),
        "results": completed,
    }
    if state == "REVIEW_CLAIMS_REFUTED":
        report["next_action"] = (
            "No confirmed defect, but refuted claims are not a pass. "
            "Correct the claim (or the code), then re-run the gate. "
            "Do not argue a refuted claim into a pass.")

    if args.json:
        print(facade.redact(facade.json.dumps(report, indent=2)))
    else:
        print()
        if args.input_redactions:
            print(facade.redact(facade.redaction_notice(args.input_redactions)))
        if not args.escalate:
            for r in completed:
                tok = (f"{r.get('in_tokens') or '?'}in/"
                       f"{r.get('out_tokens') or '?'}out")
                print(f"  {r['name']:<18} {str(r['verdict']).upper():<9} "
                      f"{len(r['findings'])} finding(s)  {r['elapsed_s']}s  {tok}  "
                      f"{seat_where(r)}")
            for r in failed:
                print(f"  {r['name']:<18} ERROR     {seat_where(r)}  {r['error'][:80]}")
        else:
            spent = sum((r.get("out_tokens") or 0) for r in completed)
            print(f"  tiers run: {' -> '.join(tiers_run)}  "
                  f"({len(completed)} verdict(s), {spent} output tokens)")
        for u in unavailable:
            print(f"  {u['name']:<18} SKIPPED   {seat_where(u)}  {u['reason']}")

        if rejecting and not confirmed and not refuted_claims:
            print(f"\nREJECTED BY: {', '.join(rejecting)}")
            print("  (a top-level 'refuted' verdict, with no finding or claim "
                  "that met the confirmation filter — read their notes)")
            for r in completed:
                if r["name"] in rejecting and r.get("notes"):
                    print(f"  [{r['name']}] {facade.redact(str(r['notes']))[:300]}")
        if refuted_claims:
            print("\nREFUTED CLAIMS:")
            for c in refuted_claims:
                print(facade.redact(f"  [{c['reviewer']}] {c.get('claim','')}"))
                print(facade.redact(f"      {c.get('why','')}"))
        if confirmed:
            print("\nCONFIRMED FINDINGS:")
            for f in confirmed:
                loc = facade.redact(f"{f.get('file','?')}:{f.get('line','?')}")
                print(f"  [{f['reviewer']}] {f.get('severity','?').upper()} {loc}")
                print(facade.redact(f"      {f.get('summary','')}"))
                print(facade.redact(f"      -> {f.get('failure_scenario','')}"))
                has_example = not facade.counterexample_placeholder(f.get("counterexample"))
                has_reason = not facade.empty_evidence_value(f.get("non_executable_reason"))
                if has_example:
                    print("      counterexample:")
                    for field in ("setup", "input", "expected", "observed"):
                        print(facade.redact(f"        {field}: {f['counterexample'][field]}"))
                if has_reason:
                    print(facade.redact(f"      non_executable_reason: {f['non_executable_reason']}"))
                elif not has_example:
                    print("      no counterexample")

        if out_of_scope:
            print("\nOUT OF SCOPE (reported, not counted against the verdict "
                  "— the threat model excludes their preconditions):")
            for f in out_of_scope:
                loc = facade.redact(f"{f.get('file','?')}:{f.get('line','?')}")
                print(f"  [{f['reviewer']}] {f.get('severity','?').upper()} {loc}")
                print(facade.redact(f"      {f.get('summary','')}"))
                if f.get("preconditions"):
                    print(facade.redact(f"      requires: {f.get('preconditions')}"))

        print(f"\n{state}{policy_text} [{tier_text}]")
        if state == "REVIEW_CLAIMS_REFUTED":
            print("  " + report["next_action"])
        elif state == "REVIEW_PARTIAL":
            if rejecting:
                print(f"  Rejected by {', '.join(rejecting)} with no finding or "
                      f"claim that met the confirmation filter. Not a pass and "
                      f"not an actionable failure — read their notes above.")
            elif truncated:
                print(f"  Input was truncated at {facade.MAX_CHARS} chars — part of the "
                      f"change was never reviewed. Not a pass. Split it and "
                      f"re-run.")
            else:
                print(f"  Only {len(completed)} of {args.quorum} required "
                      f"reviewers completed. Not a pass — say so rather than "
                      f"implying review.")
        elif state == "REVIEW_UNAVAILABLE":
            print("  No reviewer completed. The change is unreviewed.")
        elif state == "REVIEW_INCOMPLETE":
            names = ", ".join(r["name"] for r in incomplete)
            print(f"  Required reviewer content was unusable ({names}). "
                  "This spent review budget but supplied no judgment; it is "
                  "not an implementation defect and not a pass.")
        elif state == "REVIEW_PASS":
            names = ", ".join(r["name"] for r in completed)
            print(f"  Reviewed by: {names}. Absence of a finding is not proof "
                  f"of correctness — it is {len(completed)} model(s) failing to "
                  f"refute it.")

    facade.sys.exit(facade.STATES[state])


def premise_preflight(args, dispositions, facade):
    """Validate premise acknowledgments; digest dispositions before sanitizing."""
    memo, reason, os = args.premise_memo, args.continue_patching, facade.os
    if (args.require_premise_ack or {memo, reason} != {None}) and args.kind != "implementation":
        facade.config_error("Drop premise flags outside --kind implementation.")
    args.premise_ack = args.premise_history = None
    if memo is not None:
        try:
            with os.fdopen(os.open(memo, os.O_RDONLY | os.O_NONBLOCK), "rb") as stream:
                regular = facade.stat.S_ISREG(os.fstat(stream.fileno()).st_mode)
                raw = stream.read(facade.MAX_FILE_READ_BYTES + 1) if regular else b""
            if len(raw) > facade.MAX_FILE_READ_BYTES or not raw.decode("utf-8").strip():
                raise ValueError("blank, oversized or not a regular file")
        except (OSError, ValueError) as exc:
            facade.config_error("--premise-memo must be a non-empty UTF-8 file: {}".format(exc))
        args.premise_ack = {"mode": "premise_memo", "path": os.path.abspath(memo),
                            "sha256": facade.hashlib.sha256(raw).hexdigest()}
    elif reason is not None:
        if not reason.strip() or reason.splitlines() != [reason]:
            facade.config_error("--continue-patching must be a non-empty one-line reason.")
        args.premise_ack = {"mode": "continue_patching", "reason": reason}
    args.premise_dispositions = [dict(
        finding_digest=facade.finding_digest(e["location"], e["summary"]),
        disposition=e["disposition"], **{k: e[k] for k in ("defect_class",) if k in e})
        for e in dispositions]


def premise_journal_fields(args):
    owned = (getattr(args, "round_reservation", None) or (None, {}))[1]
    return dict({k: owned[k] for k in ("identity", "cycle") if isinstance(owned, dict) and k in owned},
                dispositions=getattr(args, "premise_dispositions", None) or [],
                premise_ack=getattr(args, "premise_ack", None))
