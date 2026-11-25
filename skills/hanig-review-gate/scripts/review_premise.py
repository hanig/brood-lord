"""L2 premise check: one defect_class striking in rounds N-1 and N of a cycle.

Python 3.8+, stdlib only, no process or network import. Advisory; only the
opt-in --require-premise-ack refuses.
"""

import hashlib
import importlib.util
import json
import os
import re
import stat
from pathlib import Path

_MARKER = re.compile(r"cycle-([1-9][0-9]*)-round-[1-9][0-9]*\.json")
_mods = {}


def _sibling(name):
    if name not in _mods:
        spec = importlib.util.spec_from_file_location(
            "_premise_" + name, Path(__file__).resolve().parent / (name + ".py"))
        _mods[name] = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_mods[name])
    return _mods[name]


def valid_defect_class(value):
    return _sibling("review_schema").valid_defect_class(value)


def _round(r):
    return type(r.get("round")) is int and r["round"] >= 1


def l1_contested(evidence, tree):
    """Digests whose latest evidence on exactly tree is not_reproduced."""
    latest = {}
    for index, r in enumerate(evidence):
        key, digest = (str(r.get("recorded_at")), index), r.get("finding_digest")
        if tree is not None and r.get("candidate_tree") == tree and (
                digest not in latest or key >= latest[digest][0]):
            latest[digest] = (key, r.get("status"))
    return {d for d, (_k, status) in latest.items() if status == "not_reproduced"}


def _weak_signal(active):
    words = lambda f: set(re.findall(r"[a-z0-9_]{4,}", str(f.get("summary")).lower()))
    matches = []
    for n in sorted(active):
        for a, b in ((a, b) for a in active[n] for b in active.get(n + 1, ())):
            hit, path = {"file": a.get("file"), "rounds": [n, n + 1]}, a.get("file")
            if (path not in (None, "", "?") and path == b.get("file") and hit not in matches
                    and 4 * len(words(a) & words(b)) >= max(8, len(words(a) | words(b)))):
                matches.append(hit)
    return {"advisory_only": True, "gating": False, "detected": bool(matches), "matches": matches}


def evaluate(rounds, dispositions=(), evidence=(), tree=None):
    """rounds {n: [confirmed finding]}; later dispositions win; evidence is valid
    and identity-bound. Inputs are not modified."""
    latest = {e.get("finding_digest"): e for e in dispositions}
    contested = l1_contested(evidence, tree)
    strikes, active, unclassified = {}, {}, 0
    for n in sorted(rounds):
        strikes[n], active[n] = set(), []
        for f in rounds[n]:
            entry = latest.get(f.get("finding_digest"), {})
            if entry.get("disposition") == "not-reproduced" or f.get("finding_digest") in contested:
                continue
            active[n].append(f)
            cls = next((c for c in (entry.get("defect_class"), f.get("defect_class"))
                        if valid_defect_class(c)), None)
            strikes[n] |= {cls} - {None}
            unclassified += cls is None
    triggers = [{"defect_class": c, "rounds": [n, n + 1]} for n in sorted(strikes)
                if n + 1 in strikes for c in sorted(strikes[n] & strikes[n + 1])]
    return {"status": "triggered" if triggers else "unavailable" if unclassified
            else "not_triggered", "triggered": bool(triggers),
            "defect_classes": sorted({t["defect_class"] for t in triggers}),
            "rounds": sorted({n for t in triggers for n in t["rounds"]}), "triggers": triggers,
            "strikes": {str(n): sorted(s) for n, s in strikes.items()},
            "weak_signal": _weak_signal(active)}


def journal_history(records, identity, cycle):
    """One identity's cycle: (confirmed findings by round, dispositions in round
    order, valid evidence)."""
    rounds, rows, problems = {}, [], _sibling("review_evidence").record_problems
    for r in records:
        if (r.get("type"), r.get("kind"), r.get("identity"), type(r.get("cycle")), r.get(
                "cycle")) != ("review_round", "implementation", identity, int, cycle) or not _round(r):
            continue
        found = rounds.setdefault(r["round"], [])
        for result in (x for x in r.get("results") or [] if isinstance(x, dict)):
            found += [f for f in result.get("findings") or []
                      if isinstance(f, dict) and f.get("confirmed") is True]
        rows += [(r["round"], e) for e in r.get("dispositions") or [] if isinstance(e, dict)]
    return rounds, [e for _n, e in sorted(rows, key=lambda row: row[0])], [
        r for r in records if r.get("type") == "evidence" and r.get("identity") == identity
        and not problems(r)]


def _read(path, cap):
    with os.fdopen(os.open(str(path), os.O_RDONLY | os.O_NONBLOCK), "rb") as stream:
        raw = stream.read(cap + 1) if stat.S_ISREG(os.fstat(stream.fileno()).st_mode) else b""
    if not 0 < len(raw) <= cap:
        raise ValueError("not a non-empty regular file within {} bytes".format(cap))
    return raw


def read_journal(directory, identity):
    """Records naming identity; read-only. Undecodable records are skipped."""
    records = []
    for name in sorted(os.listdir(directory)) if os.path.isdir(directory) else []:
        path = os.path.join(directory, name, "record.jsonl")
        try:
            text = _read(path, 16 << 20).decode("utf-8") if os.path.lexists(path) else ""
            record = json.loads(text) if json.dumps(identity) in text else None
        except (OSError, ValueError, RecursionError):
            continue
        if isinstance(record, dict) and record.get("identity") == identity:
            records.append(record)
    return records


def preflight(args, dispositions, facade):
    """Validate acknowledgments; digest dispositions before sanitizing."""
    memo, reason = args.premise_memo, args.continue_patching
    if (args.require_premise_ack or memo is not None or reason is not None) \
            and args.kind != "implementation":
        facade.config_error("premise flags apply to --kind implementation only.")
    args.premise_ack = args.premise_history = None
    if memo is not None:
        try:
            raw = _read(memo, 1 << 20)
            if not raw.decode("utf-8").strip():
                raise ValueError("blank")
        except (OSError, ValueError) as exc:
            facade.config_error("--premise-memo needs a non-empty UTF-8 file: {}".format(exc))
        args.premise_ack = {"mode": "premise_memo", "path": os.path.abspath(memo),
                            "sha256": hashlib.sha256(raw).hexdigest()}
    elif reason is not None:
        if not reason.strip() or reason.splitlines() != [reason]:
            facade.config_error("--continue-patching needs a non-empty one-line reason.")
        args.premise_ack = {"mode": "continue_patching", "reason": reason}
    args.premise_dispositions = [dict(
        finding_digest=facade.finding_digest(e["location"], e["summary"]),
        disposition=e["disposition"], **{k: e[k] for k in ("defect_class",) if k in e})
        for e in dispositions]


def _load_history(args, facade, identity, cycle):
    """Store (identity, cycle, records, tree); return an error name or None."""
    try:
        records = json.loads(facade._run_journal_append(
            args.file, {"read": "premise", "identity": identity}))
        if not all(isinstance(r, dict) for r in records):
            raise ValueError("non-object record")
        head = getattr(args, "reviewed_head", None)
        tree = facade.candidate_tree(head) if facade.valid_head(head) else None
        args.premise_history = (identity, cycle, list(records), tree)
    except Exception as exc:  # advisory input; SystemExit still propagates
        return type(exc).__name__


def gate(args, facade):
    """Opt-in, before reservation and reviewers; otherwise reads nothing."""
    if args.round < 2 or not args.require_premise_ack or args.premise_ack is not None:
        return
    try:
        identity = facade.change_identity(
            "origin/main" if args.target is None else args.target, args.plan_name, args.unit)
        ledger = str(facade.round_ledger_root() / identity)
        names = os.listdir(ledger) if os.path.isdir(ledger) else []
        error = _load_history(args, facade, identity, max(
            [int(m.group(1)) for m in map(_MARKER.fullmatch, names) if m], default=1))
        wanted = [args.round - 2, args.round - 1]
        hits = [] if error else [t["defect_class"] for t in _prior(args)["triggers"]
                                 if t["rounds"] == wanted]
    except Exception as exc:
        error = type(exc).__name__
    if error:
        facade.config_error("cannot read premise history ({}); pass --premise-memo FILE "
                            "or --continue-patching REASON.".format(error))
    if hits:
        facade.config_error(
            "premise acknowledgment required: defect class(es) {} struck in consecutive "
            "rounds {} and {}; pass --premise-memo FILE or --continue-patching "
            "REASON.".format(", ".join(hits), *wanted))


def _prior(args, current=None):
    rounds, dispositions, evidence, tree = {}, [], [], None
    if args.premise_history is not None:
        identity, cycle, records, tree = args.premise_history
        rounds, dispositions, evidence = journal_history(records, identity, cycle)
        rounds = {n: f for n, f in rounds.items() if n < args.round}
    if current is not None:
        rounds[args.round] = current
    return evaluate(rounds, dispositions + args.premise_dispositions, evidence, tree)


def final(args, completed, facade):
    """Advisory premise_check for the report; never raises or gates."""
    try:
        owned = (getattr(args, "round_reservation", None) or (None, None))[1]
        if args.round >= 2 and args.premise_history is None and isinstance(owned, dict) \
                and type(owned.get("cycle")) is int:
            _load_history(args, facade, owned["identity"], owned["cycle"])
        check = _prior(args, [dict(f, finding_digest=facade.finding_digest(
            "{}:{}".format(f.get("file", "?"), f.get("line", "?")), f.get("summary", "")))
            for r in completed for f in r.get("findings") or [] if facade.is_confirmed(f)])
        if args.round >= 2 and args.premise_history is None and not check["triggered"]:
            check["status"], check["reason"] = "unavailable", "premise history unreadable"
    except Exception as exc:
        check = {"status": "unavailable", "triggered": False, "reason": type(exc).__name__,
                 "weak_signal": _weak_signal({})}
    args.premise_check = check
    if check["triggered"]:
        print(facade.redact("PREMISE CHECK (advisory): defect class(es) {} recurred in "
                            "consecutive rounds {}. Attack the premise before patching "
                            "again.".format(", ".join(check["defect_classes"]), check["rounds"])),
              file=facade.sys.stderr)
    return check


def journal_fields(args):
    """Premise fields for an implementation review_round record."""
    owned = (getattr(args, "round_reservation", None) or (None, {}))[1]
    fields = {k: owned[k] for k in ("identity", "cycle") if isinstance(owned, dict) and k in owned}
    return dict(fields, dispositions=getattr(args, "premise_dispositions", None) or [],
                premise_ack=getattr(args, "premise_ack", None))
