#!/usr/bin/env python3
"""Pre-registered hill-climbing ledger with a paired A/B noise screen.

State lives outside every Git worktree.  ``decision.tsv`` is append-only and
SHA-256 chained; the chain plus the recorded-prefix witness in ``state.json``
is non-cryptographic tamper-evidence: it detects accidental edits, not a
same-UID writer who recomputes both.  Arm A is the baseline, arm B the
candidate.  Python 3.9+, standard library only.
"""

import argparse
import fcntl
import hashlib
import json
import math
import os
import statistics
import sys
from contextlib import contextmanager
from fractions import Fraction

SCHEMA = "hanig_hillclimb_decision.v1"
HEADER = ('# schema="%s"\tchain=sha256 accidental-edit evidence, not '
          'cryptographic tamper-proofing\nseq\tkind\trecord\tchain\n' % SCHEMA)
BENCHMARK_QUESTIONS = (
    "Why not double?", "Was it tuned?", "Did it break limits?", "Did it error?",
    "Does it reproduce?", "Does it matter?", "Did it even happen?",
)
MIN_RUNS = 5
ALPHA = Fraction(1, 20)
# Declared bound: exact rank-sum enumeration runs for n_A + n_B <= 60; larger
# screens are inconclusive rather than approximated.
EXACT_MAX_N = 60
OPS = {"<=": lambda v, t: v <= t, ">=": lambda v, t: v >= t,
       "<": lambda v, t: v < t, ">": lambda v, t: v > t}


class Refusal(Exception):
    pass


def _path(state, name):
    return os.path.join(state, name)


def worktree_of(path):
    """Return the nearest ancestor holding ``.git`` (lexical or resolved)."""
    for start in (os.path.abspath(path), os.path.realpath(path)):
        current = start
        while True:
            if os.path.lexists(os.path.join(current, ".git")):
                return current
            parent = os.path.dirname(current)
            if parent == current:
                break
            current = parent
    return None


def check_state_dir(state):
    hit = worktree_of(state)
    if hit:
        raise Refusal("state dir %s is inside the Git worktree %s; use a path "
                      "outside every worktree" % (state, hit))


def digest_file(path):
    sha = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            sha.update(block)
    return sha.hexdigest()


def read_json(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path, data):
    temp = path + ".tmp"
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=1, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


@contextmanager
def locked(state):
    check_state_dir(state)
    if not os.path.isfile(_path(state, "harness.lock.json")):
        raise Refusal("not an initialized hillclimb state dir: %s" % state)
    fd = os.open(_path(state, ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield read_json(_path(state, "harness.lock.json"))
    finally:
        os.close(fd)


def harness_drift(lock):
    problems = []
    for path, frozen in sorted(lock["files"].items()):
        try:
            current = digest_file(path)
        except OSError as exc:
            problems.append("%s: unreadable or deleted (%s)" % (path, exc.strerror or exc))
            continue
        if current != frozen:
            problems.append("%s: sha256 %s differs from frozen %s" % (path, current, frozen))
    return problems


def require_frozen(lock):
    problems = harness_drift(lock)
    if problems:
        raise Refusal("harness drifted since init:\n  " + "\n  ".join(problems))


def read_log(state):
    """Return (problems, last_chain, rows) after checking header, chain and prefix."""
    with open(_path(state, "decision.tsv"), "rb") as handle:
        data = handle.read()
    head = HEADER.encode("utf-8")
    if not data.startswith(head):
        return ["decision.tsv header magic is missing or edited"], None, []
    problems = []
    witness = read_json(_path(state, "state.json"))
    size = witness["log_bytes"]
    if len(data) < size or hashlib.sha256(data[:size]).hexdigest() != witness["log_digest"]:
        problems.append("decision.tsv recorded prefix (%d bytes) was edited or truncated" % size)
    chain, rows = hashlib.sha256(head).hexdigest(), []
    lines = data[len(head):].decode("utf-8", "replace").split("\n")
    if lines[-1]:
        problems.append("decision.tsv ends mid-row")
    for seq, line in enumerate(lines[:-1], 1):
        parts = line.split("\t")
        if len(parts) != 4 or parts[0] != str(seq):
            problems.append("row %d is malformed" % seq)
            break
        expected = hashlib.sha256((chain + "\t" + "\t".join(parts[:3])).encode("utf-8")).hexdigest()
        if parts[3] != expected:
            problems.append("row %d chain mismatch (edited row)" % seq)
            break
        chain = parts[3]
        rows.append((parts[1], json.loads(parts[2])))
    return problems, chain, rows


def append_row(state, kind, record):
    problems, chain, rows = read_log(state)
    if problems:
        raise Refusal("decision.tsv failed verification:\n  " + "\n  ".join(problems))
    body = "%d\t%s\t%s" % (len(rows) + 1, kind, json.dumps(record, sort_keys=True))
    line = (body + "\t" + hashlib.sha256((chain + "\t" + body).encode("utf-8")).hexdigest() + "\n").encode("utf-8")
    fd = os.open(_path(state, "decision.tsv"), os.O_WRONLY | os.O_APPEND)
    try:
        while line:
            line = line[os.write(fd, line):]
        os.fsync(fd)
    finally:
        os.close(fd)
    with open(_path(state, "decision.tsv"), "rb") as handle:
        data = handle.read()
    write_json(_path(state, "state.json"),
               {"log_bytes": len(data), "log_digest": hashlib.sha256(data).hexdigest()})


def open_hypothesis(rows):
    current = None
    for kind, record in rows:
        current = record if kind == "hypothesis" else None
    return current


def rank_sum_p(a, b):
    """Exact two-sided permutation p of the Mann-Whitney U / rank-sum statistic.

    Ties take midranks (doubled to stay integral); the null distribution is
    enumerated over every split of the pooled ranks by dynamic programming.
    """
    pooled, rank, i = sorted(a + b), {}, 0
    while i < len(pooled):
        j = i
        while j < len(pooled) and pooled[j] == pooled[i]:
            j += 1
        rank[pooled[i]] = i + 1 + j  # 2 * midrank of positions i+1..j
        i = j
    n, k = len(pooled), len(a)
    centre, observed = k * (n + 1), sum(rank[v] for v in a)
    counts = [dict() for _ in range(k + 1)]
    counts[0][0] = 1
    for r in (rank[v] for v in pooled):
        for m in range(k, 0, -1):
            target = counts[m]
            for total, ways in counts[m - 1].items():
                target[total + r] = target.get(total + r, 0) + ways
    extreme = abs(observed - centre)
    hits = sum(w for s, w in counts[k].items() if abs(s - centre) >= extreme)
    return Fraction(hits, math.comb(n, k))


def screen(runs, direction, min_effect, guardrails, observed, errors):
    """Return (verdict, reasons, stats) for the paired A/B noise screen."""
    arms = [run["arm"] for run in runs]
    a = [run["value"] for run in runs if run["arm"] == "A"]
    b = [run["value"] for run in runs if run["arm"] == "B"]
    stats, reasons = {"n_A": len(a), "n_B": len(b)}, []
    if len(a) < MIN_RUNS or len(b) < MIN_RUNS:
        reasons.append("needs >= %d runs per arm" % MIN_RUNS)
    if any(x == y for x, y in zip(arms, arms[1:])):
        reasons.append("runs do not strictly alternate A/B")
    if len(runs) > EXACT_MAX_N:
        reasons.append("n_A + n_B exceeds the exact rank-sum bound %d" % EXACT_MAX_N)
    if errors is None:
        reasons.append("checklist lacks an integer 'errors' count")
    missing = sorted(g["name"] for g in guardrails if g["name"] not in observed)
    if missing:
        reasons.append("missing guardrail observations: " + ", ".join(missing))
    if reasons:
        return "inconclusive", reasons, stats
    median_a, median_b = statistics.median(a), statistics.median(b)
    improvement = median_b - median_a if direction == "higher" else median_a - median_b
    spread, p = max(a) - min(a), rank_sum_p(a, b)
    stats.update(median_A=median_a, median_B=median_b, improvement=improvement,
                 baseline_range=spread, rank_sum_p=float(p), errors=errors)
    if not improvement >= min_effect:
        reasons.append("improvement %g below min_effect %g" % (improvement, min_effect))
    if not improvement > spread:
        reasons.append("improvement %g not above baseline range %g" % (improvement, spread))
    if not p < ALPHA:
        reasons.append("rank-sum p %.4g not below 0.05" % p)
    for g in guardrails:
        if not OPS[g["op"]](observed[g["name"]], g["threshold"]):
            reasons.append("guardrail regression: %s=%g violates %s %g" % (
                g["name"], observed[g["name"]], g["op"], g["threshold"]))
    if errors:
        reasons.append("%d errors reported" % errors)
    return ("revert" if reasons else "keep"), reasons, stats


def _number(value, what):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise Refusal("%s must be a finite number" % what)
    return float(value)


def _count(value, what):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise Refusal("%s must be a non-negative integer" % what)
    return value


def parse_guardrail(text):
    name, op, threshold = (text.rsplit(":", 2) + ["", ""])[:3]
    if not name or op not in OPS:
        raise Refusal("guardrail %r must be NAME:op:threshold with op in %s" % (text, " ".join(OPS)))
    try:
        return {"name": name, "op": op, "threshold": _number(float(threshold), "guardrail threshold")}
    except ValueError:
        raise Refusal("guardrail %r has a non-numeric threshold" % text)


def load_inputs(args):
    try:
        runs, checklist = read_json(args.runs_json), read_json(args.checklist_json)
        observed = read_json(args.guardrails_json) if args.guardrails_json else {}
    except (OSError, ValueError) as exc:
        raise Refusal("cannot read input JSON: %s" % exc)
    if not isinstance(checklist, dict):
        raise Refusal("checklist must be a JSON object")
    unanswered = [q for q in BENCHMARK_QUESTIONS
                  if not isinstance(checklist.get(q), str) or not checklist[q].strip()]
    if unanswered:
        raise Refusal("checklist lacks non-empty answers to: " + "; ".join(unanswered))
    if not isinstance(runs, list) or not all(isinstance(r, dict) and r.get("arm") in ("A", "B") for r in runs):
        raise Refusal('runs must be a JSON list of {"arm": "A"|"B", "value": number}')
    runs = [{"arm": r["arm"], "value": _number(r.get("value"), "run value"),
             "errors": _count(r.get("errors", 0), "run errors")} for r in runs]
    if not isinstance(observed, dict):
        raise Refusal("guardrails JSON must be an object of NAME: number")
    observed = {k: _number(v, "guardrail %s" % k) for k, v in observed.items()}
    errors = checklist.get("errors")
    if errors is not None:
        errors = _count(errors, "checklist errors") + sum(r["errors"] for r in runs)
    return runs, checklist, observed, errors


def cmd_init(args):
    state = args.state_dir
    check_state_dir(state)
    if os.path.lexists(_path(state, "decision.tsv")) or os.path.lexists(_path(state, "harness.lock.json")):
        raise Refusal("state dir is already initialized: %s" % state)
    if not math.isfinite(args.min_effect) or args.min_effect < 0:
        raise Refusal("--min-effect must be a finite number >= 0")
    try:
        files = {os.path.abspath(f): digest_file(os.path.abspath(f)) for f in args.harness}
    except OSError as exc:
        raise Refusal("cannot hash harness file: %s" % exc)
    lock = {"schema": SCHEMA, "files": files, "metric": args.metric, "direction": args.direction,
            "min_effect": args.min_effect, "guardrails": [parse_guardrail(g) for g in args.guardrail]}
    os.makedirs(state, mode=0o700, exist_ok=True)
    fd = os.open(_path(state, "decision.tsv"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(HEADER.encode("utf-8"))
    head = HEADER.encode("utf-8")
    write_json(_path(state, "state.json"),
               {"log_bytes": len(head), "log_digest": hashlib.sha256(head).hexdigest()})
    write_json(_path(state, "harness.lock.json"), lock)
    print("initialized %s: %d harness files frozen" % (state, len(files)))


def cmd_hypothesize(args):
    with locked(args.state_dir) as lock:
        require_frozen(lock)
        rows = read_log(args.state_dir)[2]
        current = open_hypothesis(rows)
        if current:
            raise Refusal("hypothesis %s is still open; record it first" % current["id"])
        if any(record.get("id") == args.id for _kind, record in rows):
            raise Refusal("id %s was already used" % args.id)
        effect = lock["min_effect"] if args.min_effect is None else args.min_effect
        if not math.isfinite(effect) or effect < 0:
            raise Refusal("--min-effect must be a finite number >= 0")
        append_row(args.state_dir, "hypothesis", {"id": args.id, "hypothesis": args.hypothesis,
                                                  "expected_limiter": args.expected_limiter,
                                                  "min_effect": effect})
    print("pre-registered %s (min_effect %g)" % (args.id, effect))


def cmd_record(args):
    with locked(args.state_dir) as lock:
        require_frozen(lock)
        current = open_hypothesis(read_log(args.state_dir)[2])
        if current is None or current["id"] != args.id:
            raise Refusal("%s is not the open hypothesis (open: %s)" % (
                args.id, current["id"] if current else "none"))
        runs, checklist, observed, errors = load_inputs(args)
        verdict, reasons, stats = screen(runs, lock["direction"], current["min_effect"],
                                         lock["guardrails"], observed, errors)
        append_row(args.state_dir, "decision", {"id": args.id, "verdict": verdict, "reasons": reasons,
                                                "stats": stats, "runs": runs, "checklist": checklist,
                                                "guardrails": observed})
    print("paired A/B noise screen for %s: %s" % (args.id, verdict.upper()))
    for reason in reasons:
        print("  - " + reason)
    print("  " + json.dumps(stats, sort_keys=True))


def cmd_verify_harness(args):
    with locked(args.state_dir) as lock:
        problems = harness_drift(lock)
    for problem in problems:
        print("DRIFT " + problem)
    if not problems:
        print("harness frozen: %d files match" % len(lock["files"]))
    return 1 if problems else 0


def cmd_verify_log(args):
    with locked(args.state_dir):
        problems, _chain, rows = read_log(args.state_dir)
    for problem in problems:
        print("LOG " + problem)
    if not problems:
        print("decision.tsv: %d rows, chain and recorded prefix intact" % len(rows))
    return 1 if problems else 0


def cmd_summary(args):
    with locked(args.state_dir) as lock:
        problems, _chain, rows = read_log(args.state_dir)
    decisions = [r for kind, r in rows if kind == "decision"]
    current = open_hypothesis(rows)
    report = {"metric": lock["metric"], "direction": lock["direction"], "log_problems": problems,
              "open": current["id"] if current else None,
              "verdicts": {v: sum(d["verdict"] == v for d in decisions) for v in ("keep", "revert", "inconclusive")},
              "decisions": [{"id": d["id"], "verdict": d["verdict"]} for d in decisions]}
    if args.json:
        print(json.dumps(report, indent=1, sort_keys=True))
    else:
        print("%s (%s is better): %s; open: %s" % (report["metric"], report["direction"],
              ", ".join("%s %d" % kv for kv in sorted(report["verdicts"].items())), report["open"]))
        for d in report["decisions"]:
            print("  %s %s" % (d["id"], d["verdict"]))
    return 1 if problems else 0


def build_parser():
    parser = argparse.ArgumentParser(description=(
        "Pre-registered hill climbing: freeze a harness, pre-register one hypothesis, "
        "then keep/revert/inconclusive by a paired A/B noise screen."))
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name, func, text):
        command = sub.add_parser(name, help=text)
        command.set_defaults(func=func)
        return command

    p = add("init", cmd_init, "freeze harness digests and start decision.tsv")
    p.add_argument("--harness", nargs="+", required=True)
    p.add_argument("--metric", required=True)
    p.add_argument("--direction", choices=("higher", "lower"), required=True)
    p.add_argument("--min-effect", type=float, required=True)
    p.add_argument("--guardrail", action="append", default=[], help="NAME:op:threshold, op in <= >= < >")
    p = add("hypothesize", cmd_hypothesize, "pre-register the single open hypothesis")
    p.add_argument("--id", required=True)
    p.add_argument("--hypothesis", required=True)
    p.add_argument("--expected-limiter", required=True)
    p.add_argument("--min-effect", type=float)
    p = add("record", cmd_record, "screen alternating A/B runs for the open hypothesis")
    p.add_argument("--id", required=True)
    p.add_argument("--runs-json", required=True)
    p.add_argument("--checklist-json", required=True)
    p.add_argument("--guardrails-json")
    add("verify-harness-frozen", cmd_verify_harness, "re-hash every harness file")
    add("verify-log", cmd_verify_log, "check decision.tsv header, chain and prefix")
    p = add("summary", cmd_summary, "count verdicts")
    p.add_argument("--json", action="store_true")
    for name in sub.choices:
        sub.choices[name].add_argument("--state-dir", required=True)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.func(args) or 0
    except Refusal as exc:
        print("refused: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
