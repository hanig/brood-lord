"""L2 "attack the premise" check: a two-strike consecutive-round defect-class trigger.

Python 3.8+, standard library only, no process or network import. The strike
key is the author-declared defect_class alone, never file or line. A class
strikes once per round however many findings carry it, and fires only when it
strikes in rounds N-1 and N of one review cycle. Findings contested at L1
(a not-reproduced disposition, or not_reproduced evidence on the tree under
review) never strike. The check is advisory: it changes no verdict, severity
or default exit code. Only review.py's opt-in --require-premise-ack refuses.

    review_premise.py premise-replay --state-dir JOURNAL_DIR

scans a review-rounds journal read-only and prints aggregate counts only.
"""

import argparse
import hashlib
import importlib.util
import json
import os
import re
import stat
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
RECORD_MAX_BYTES = 16 * 1024 * 1024
MEMO_MAX_BYTES = 1024 * 1024
_WORD = re.compile(r"[a-z0-9_]+")
_STOPWORDS = frozenset("""
that this then than there their they when where which while with without from
into onto over under only ever never always cannot must should would could
does done have been being were also just even more most less other another
using used uses make makes made returns return value values case cases
""".split())
_MARKER = re.compile(r"cycle-([1-9][0-9]*)-round-([1-9][0-9]*)\.json")
_siblings = {}


def _sibling(filename):
    if filename not in _siblings:
        spec = importlib.util.spec_from_file_location(
            "_hanig_review_premise_" + filename[:-3], HERE / filename)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _siblings[filename] = module
    return _siblings[filename]


def valid_defect_class(value):
    """review_schema owns the token grammar; nothing is case-folded or trimmed."""
    return _sibling("review_schema.py").valid_defect_class(value)


def _round_no(value):
    return type(value) is int and value >= 1


def _tokens(text):
    return {w for w in _WORD.findall(str(text or "").lower())
            if len(w) > 3 and w not in _STOPWORDS}


def l1_contested(evidence, current_tree):
    """Digests whose latest evidence on exactly current_tree is not_reproduced."""
    latest = {}
    if current_tree is None:
        return set()
    for index, record in enumerate(evidence):
        if record.get("candidate_tree") != current_tree:
            continue
        key = (str(record.get("recorded_at", "")), index)
        digest = record.get("finding_digest")
        if digest not in latest or key >= latest[digest][0]:
            latest[digest] = (key, record.get("status"))
    return {digest for digest, (_key, status) in latest.items()
            if status == "not_reproduced"}


def _weak_signal(active):
    matches = []
    for first in sorted(active):
        if first + 1 not in active:
            continue
        for a in active[first]:
            for b in active[first + 1]:
                path = a.get("file")
                if not isinstance(path, str) or path in ("", "?") or path != b.get("file"):
                    continue
                left, right = _tokens(a.get("summary")), _tokens(b.get("summary"))
                shared = left & right
                if len(shared) >= 2 and len(shared) * 4 >= len(left | right):
                    match = {"file": path, "rounds": [first, first + 1]}
                    if match not in matches:
                        matches.append(match)
    return {"advisory_only": True, "gating": False, "detected": bool(matches),
            "basis": "same file and summary token overlap in consecutive rounds",
            "matches": matches}


def evaluate(rounds, dispositions=(), evidence=(), current_tree=None):
    """Premise status for one cycle.

    rounds maps round number -> confirmed findings (finding_digest, file,
    summary, optional defect_class). dispositions are in chronological order;
    a later entry for a digest wins. evidence must already be validated and
    bound to this identity. Inputs are not modified.
    """
    latest = {}
    for entry in dispositions:
        if isinstance(entry, dict) and isinstance(entry.get("finding_digest"), str):
            latest[entry["finding_digest"]] = entry
    contested = l1_contested(evidence, current_tree)
    strikes, active = {}, {}
    unclassified = excluded = 0
    for number in sorted(r for r in rounds if _round_no(r)):
        active[number], strikes[number] = [], set()
        for finding in rounds[number]:
            if not isinstance(finding, dict):
                continue
            digest = finding.get("finding_digest")
            entry = latest.get(digest, {})
            if entry.get("disposition") == "not-reproduced" or digest in contested:
                excluded += 1
                continue
            active[number].append(finding)
            declared = entry.get("defect_class")
            if not valid_defect_class(declared):
                declared = finding.get("defect_class")
            if valid_defect_class(declared):
                strikes[number].add(declared)
            else:
                unclassified += 1
    triggers = [{"defect_class": name, "rounds": [number, number + 1]}
                for number in sorted(strikes) if number + 1 in strikes
                for name in sorted(strikes[number] & strikes[number + 1])]
    status = ("triggered" if triggers else "unavailable" if unclassified
              else "not_triggered")
    return {"status": status, "triggered": bool(triggers),
            "defect_classes": sorted({t["defect_class"] for t in triggers}),
            "rounds": sorted({n for t in triggers for n in t["rounds"]}),
            "triggers": triggers, "rounds_evaluated": sorted(strikes),
            "strikes": {str(n): sorted(strikes[n]) for n in sorted(strikes)},
            "unclassified_findings": unclassified, "excluded_findings": excluded,
            "strike_key": "defect_class", "weak_signal": _weak_signal(active)}


def cycle_history(round_records):
    """Confirmed findings by round and dispositions in round order."""
    rounds, rows = {}, []
    for record in round_records:
        number = record.get("round")
        if not _round_no(number):
            continue
        found = rounds.setdefault(number, [])
        for result in record.get("results") or []:
            for finding in (result.get("findings") or [] if isinstance(result, dict) else []):
                if isinstance(finding, dict) and finding.get("confirmed") is True:
                    found.append(finding)
        for entry in record.get("dispositions") or []:
            if isinstance(entry, dict):
                rows.append((number, entry))
    rows.sort(key=lambda row: row[0])
    return rounds, [entry for _number, entry in rows]


def _is_round(record):
    return (record.get("type") == "review_round" and record.get("kind") == "implementation"
            and _round_no(record.get("round")))


def journal_history(records, identity, cycle):
    """One identity's cycle: (rounds, dispositions, valid evidence)."""
    selected = [r for r in records if isinstance(r, dict) and r.get("identity") == identity
                and _is_round(r) and type(r.get("cycle")) is int and r["cycle"] == cycle]
    problems = _sibling("review_evidence.py").record_problems
    evidence = [r for r in records if isinstance(r, dict) and r.get("type") == "evidence"
                and r.get("identity") == identity and not problems(r)]
    rounds, dispositions = cycle_history(selected)
    return rounds, dispositions, evidence


def read_journal(directory, identity=None):
    """(records, unreadable) from canonical event files; never writes.

    With identity, records whose bytes cannot name it are skipped before
    decoding; the decoded field is still compared exactly afterwards.
    """
    records, unreadable = [], 0
    try:
        names = sorted(os.listdir(directory))
    except FileNotFoundError:
        return [], 0
    needle = None if identity is None else json.dumps(identity)
    for name in names:
        path = os.path.join(directory, name, "record.jsonl")
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0))
        except FileNotFoundError:
            continue
        except OSError:
            unreadable += 1
            continue
        try:
            with os.fdopen(fd, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise ValueError("not a regular file")
                raw = stream.read(RECORD_MAX_BYTES + 1)
            if len(raw) > RECORD_MAX_BYTES:
                raise ValueError("oversized record")
            text = raw.decode("utf-8")
            if needle is not None and needle not in text:
                continue
            record = json.loads(text)
            if not isinstance(record, dict):
                raise ValueError("not an object")
        except (OSError, ValueError, RecursionError):
            unreadable += 1
            continue
        if identity is None or record.get("identity") == identity:
            records.append(record)
    return records, unreadable


def ledger_cycle(directory):
    """Highest reserved cycle for an identity (1 when none); read-only peek."""
    try:
        names = os.listdir(directory)
    except FileNotFoundError:
        return 1
    cycles = [int(m.group(1)) for m in map(_MARKER.fullmatch, names) if m]
    return max(cycles, default=1)


def replay(directory):
    """Aggregate counts over a journal; legacy records group by claim digests."""
    records, unreadable = read_journal(directory)
    cycles, evidence = {}, {}
    last_round = {}
    for record in records:
        if record.get("type") == "evidence" and isinstance(record.get("identity"), str):
            evidence.setdefault(record["identity"], []).append(record)
            continue
        if not _is_round(record):
            continue
        identity = record.get("identity")
        if (isinstance(identity, str) and re.fullmatch(r"[0-9a-f]{64}", identity)
                and type(record.get("cycle")) is int):
            group, cycle = ("identity", identity), record["cycle"]
        else:
            digests = record.get("claim_digests")
            group = ("legacy", tuple(sorted(d for d in digests if isinstance(d, str)))
                     if isinstance(digests, list) else ())
            previous = last_round.get(group)
            cycle = previous[0] + (record["round"] < previous[1]) if previous else 1
            last_round[group] = (cycle, record["round"])
        cycles.setdefault((group, cycle), []).append(record)
    counts = {"identities_scanned": len({group for group, _c in cycles}),
              "cycles_scanned": len(cycles), "rounds_scanned": 0, "triggered_count": 0,
              "unavailable_count": 0, "weak_signal_count": 0,
              "records_scanned": len(records), "unreadable_records": unreadable}
    problems = _sibling("review_evidence.py").record_problems
    for (group, _cycle), selected in cycles.items():
        rounds, dispositions = cycle_history(selected)
        bound = [r for r in evidence.get(group[1], []) if not problems(r)] \
            if group[0] == "identity" else []
        result = evaluate(rounds, dispositions, bound, current_tree=None)
        counts["rounds_scanned"] += len(rounds)
        counts["triggered_count"] += result["triggered"]
        counts["unavailable_count"] += result["status"] == "unavailable"
        counts["weak_signal_count"] += result["weak_signal"]["detected"]
    return counts


# --- review.py integration (facade-injected; no process import here) ---------

def disposition_rows(dispositions, finding_digest):
    """Digest, disposition and class only; computed before input sanitizing."""
    rows = []
    for entry in dispositions:
        row = {"finding_digest": finding_digest(entry["location"], entry["summary"]),
               "disposition": entry["disposition"]}
        if "defect_class" in entry:
            row["defect_class"] = entry["defect_class"]
        rows.append(row)
    return rows


def read_memo(path):
    """(sha256, error) for an existing non-empty UTF-8 regular file."""
    try:
        fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    except OSError as exc:
        return None, "unreadable: {}".format(type(exc).__name__)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            return None, "not a regular file"
        raw = stream.read(MEMO_MAX_BYTES + 1)
    if len(raw) > MEMO_MAX_BYTES:
        return None, "above the {}-byte limit".format(MEMO_MAX_BYTES)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None, "not valid UTF-8"
    if not text.strip():
        return None, "empty"
    return hashlib.sha256(raw).hexdigest(), None


def preflight(args, dispositions, facade):
    """Validate acknowledgment flags and summarize dispositions, before any I/O."""
    memo, reason = args.premise_memo, args.continue_patching
    if (args.require_premise_ack or memo is not None or reason is not None) \
            and args.kind != "implementation":
        facade.config_error("--require-premise-ack, --premise-memo and "
                            "--continue-patching apply to implementation reviews. "
                            "Pass --kind implementation --round N, or drop them.")
    args.premise_ack = None
    if memo is not None:
        digest, error = read_memo(memo)
        if error:
            facade.config_error("--premise-memo {}: {}. Pass an existing non-empty "
                                "UTF-8 premise memo file.".format(memo, error))
        args.premise_ack = {"mode": "premise_memo", "path": os.path.abspath(memo),
                            "sha256": digest}
    elif reason is not None:
        if not reason.strip() or reason.splitlines() != [reason]:
            facade.config_error("--continue-patching needs a non-empty one-line "
                                "reason. Pass it without a line break.")
        args.premise_ack = {"mode": "continue_patching", "reason": reason}
    args.premise_dispositions = disposition_rows(dispositions, facade.finding_digest)
    args.premise_history = None


def _load_history(args, facade, identity, cycle):
    """Store (identity, cycle, records, tree); return an error name or None."""
    try:
        records = json.loads(facade._run_journal_append(
            args.file, {"read": "premise", "identity": identity}))
        if not isinstance(records, list):
            raise ValueError("premise history is not a list")
        head = getattr(args, "reviewed_head", None)
        tree = facade.candidate_tree(head) if facade.valid_head(head) else None
        args.premise_history = (identity, cycle, records, tree)
        return None
    except Exception as exc:  # advisory input; SystemExit still propagates
        return type(exc).__name__


def gate(args, facade):
    """Opt-in only: before reservation and reviewers, refuse an unacknowledged
    premise trigger. Without --require-premise-ack nothing is read here, so a
    reservation refusal still precedes every journal access."""
    if args.round < 2 or not args.require_premise_ack or args.premise_ack is not None:
        return
    try:
        identity = facade.change_identity(
            "origin/main" if args.target is None else args.target, args.plan_name, args.unit)
        cycle = ledger_cycle(str(facade.round_ledger_root() / identity))
        error = _load_history(args, facade, identity, cycle)
    except Exception as exc:
        error = type(exc).__name__
    if error:
        facade.config_error("--require-premise-ack cannot read this cycle's premise "
                            "history ({}). Pass --premise-memo FILE or "
                            "--continue-patching REASON, or drop "
                            "--require-premise-ack.".format(error))
    prior = _prior(args)
    wanted = [args.round - 2, args.round - 1]
    hits = sorted(t["defect_class"] for t in prior["triggers"] if t["rounds"] == wanted)
    if hits:
        facade.config_error(
            "premise acknowledgment required: defect class(es) {} struck in "
            "consecutive rounds {} and {}. Attack the premise before patching "
            "again: pass --premise-memo FILE (a non-empty premise memo; only its "
            "SHA-256 and path are recorded, not its soundness) or "
            "--continue-patching REASON.".format(", ".join(hits), *wanted))


def _prior(args, current=None):
    rounds, dispositions, evidence, tree = {}, [], [], None
    history = getattr(args, "premise_history", None)
    if history is not None:
        identity, cycle, records, tree = history
        rounds, dispositions, evidence = journal_history(records, identity, cycle)
        rounds = {n: f for n, f in rounds.items() if n < args.round}
    if current is not None:
        rounds[args.round] = current
    dispositions += list(getattr(args, "premise_dispositions", None) or [])
    return evaluate(rounds, dispositions, evidence, tree)


def final(args, completed, facade):
    """Advisory premise_check for the report; never raises, never gates."""
    try:
        reservation = getattr(args, "round_reservation", None)
        owned = reservation[1] if isinstance(reservation, tuple) else None
        if (args.round >= 2 and getattr(args, "premise_history", None) is None
                and isinstance(owned, dict) and type(owned.get("cycle")) is int):
            _load_history(args, facade, owned["identity"], owned["cycle"])
        current = []
        for result in completed:
            for f in result.get("findings") or []:
                if facade.is_confirmed(f):
                    location = "{}:{}".format(f.get("file", "?"), f.get("line", "?"))
                    current.append(dict(f, finding_digest=facade.finding_digest(
                        location, f.get("summary", ""))))
        check = _prior(args, current)
        if (args.round >= 2 and getattr(args, "premise_history", None) is None
                and not check["triggered"]):
            check = dict(check, status="unavailable", reason="premise history unreadable")
    except Exception as exc:
        check = {"status": "unavailable", "triggered": False, "defect_classes": [],
                 "rounds": [], "reason": "premise check failed: " + type(exc).__name__,
                 "weak_signal": {"advisory_only": True, "gating": False,
                                 "detected": False, "matches": []}}
    args.premise_check = check
    if check["triggered"]:
        print(facade.redact("PREMISE CHECK (advisory): defect class(es) {} recurred in "
                            "consecutive rounds {}. Attack the premise before patching "
                            "again.".format(", ".join(check["defect_classes"]),
                                            ", ".join(map(str, check["rounds"])))),
              file=facade.sys.stderr)
    return check


def journal_fields(args):
    """Premise fields for an implementation review_round record."""
    if getattr(args, "kind", None) != "implementation":
        return {}
    fields = {}
    reservation = getattr(args, "round_reservation", None)
    owned = reservation[1] if isinstance(reservation, tuple) and len(reservation) == 2 else {}
    if isinstance(owned, dict):
        for key in ("identity", "cycle"):
            if key in owned:
                fields[key] = owned[key]
    for key, name in (("dispositions", "premise_dispositions"), ("premise_ack", "premise_ack"),
                      ("premise_check", "premise_check")):
        value = getattr(args, name, None)
        if value is not None:
            fields[key] = value
    return fields


def main(argv=None):
    parser = argparse.ArgumentParser(prog="review_premise.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command")
    command = commands.add_parser("premise-replay", help="read-only aggregate counts")
    command.add_argument("--state-dir", required=True, metavar="DIR",
                         help="review-rounds journal directory to scan")
    args = parser.parse_args(argv)
    if args.command != "premise-replay":
        parser.error("pass premise-replay --state-dir DIR")
    if not os.path.isdir(args.state_dir):
        parser.error("--state-dir {} is not a directory".format(args.state_dir))
    print(json.dumps(replay(args.state_dir), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
