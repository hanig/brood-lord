#!/usr/bin/env python3
"""Pre-registered hill climbing by a paired A/B noise screen; A is the baseline.

The decision.tsv SHA-256 chain detects accidental edits, not a same-UID writer.
"""
import argparse
import fcntl
import hashlib
import json
import math
import operator
import os
import statistics
import sys
from fractions import Fraction

SCHEMA = "hanig_hillclimb_decision.v1"
HEADER = ('# schema="%s"\tchain=sha256 accidental-edit evidence, not cryptographic\n'
          'seq\tkind\trecord\tchain\n' % SCHEMA).encode()
BENCHMARK_QUESTIONS = ("Why not double?", "Was it tuned?", "Did it break limits?", "Did it error?",
                       "Does it reproduce?", "Does it matter?", "Did it even happen?")
MIN_RUNS, ALPHA, EXACT_MAX_N = 5, Fraction(1, 20), 60  # exact rank-sum bound: n_A + n_B <= 60
OPS = {"<=": operator.le, ">=": operator.ge, "<": operator.lt, ">": operator.gt}
J = os.path.join

class Refusal(Exception):
    pass

def sha(data):
    return hashlib.sha256(data).hexdigest()

def digest(path):
    with open(path, "rb") as handle:
        return sha(handle.read())

def worktree_of(path):
    for cur in (os.path.abspath(path), os.path.realpath(path)):
        while not os.path.lexists(J(cur, ".git")):
            if os.path.dirname(cur) == cur:
                break
            cur = os.path.dirname(cur)
        else:
            return cur

def load(path):
    with open(path, "rb") as handle:
        return json.loads(handle.read())

def save(path, data):
    with open(path + ".tmp", "w") as handle:
        json.dump(data, handle)
    os.replace(path + ".tmp", path)

def drift(lock):
    out = []
    for path, frozen in sorted(lock["files"].items()):
        try:
            out += [path + " changed"] * (digest(path) != frozen)
        except OSError as exc:
            out.append("%s unreadable (%s)" % (path, exc))
    return out

def read_log(state):
    with open(J(state, "decision.tsv"), "rb") as handle:
        data = handle.read()
    size, want = load(J(state, "state.json"))
    bad, chain, rows = ["prefix edited or truncated"] * (sha(data[:size]) != want), sha(HEADER), []
    if not data.startswith(HEADER):
        return bad + ["header magic edited"], None, []
    lines = data[len(HEADER):].decode("utf-8", "replace").split("\n")
    for seq, line in enumerate(lines[:-1], 1):
        part = line.split("\t")
        if len(part) != 4 or part[0] != str(seq) or part[3] != sha((chain + "\t" + "\t".join(part[:3])).encode()):
            return bad + ["row %d edited" % seq], None, rows
        chain = part[3]
        rows.append((part[1], json.loads(part[2])))
    return bad + ["ends mid-row"] * bool(lines[-1]), chain, rows

def append_row(state, kind, record):
    bad, chain, rows = read_log(state)
    if bad:
        raise Refusal("log failed verification: " + "; ".join(bad))
    body = "%d\t%s\t%s" % (len(rows) + 1, kind, json.dumps(record, sort_keys=True))
    with open(J(state, "decision.tsv"), "ab") as handle:  # O_APPEND
        handle.write(("%s\t%s\n" % (body, sha((chain + "\t" + body).encode()))).encode())
    save(J(state, "state.json"), [os.path.getsize(J(state, "decision.tsv")), digest(J(state, "decision.tsv"))])

def open_hypothesis(rows):
    current = None
    for kind, record in rows:
        current = record if kind == "hypothesis" else None
    return current

def rank_sum_p(a, b):
    """Exact two-sided permutation p of the Mann-Whitney U statistic; ties get midranks."""
    pooled, rank, i = sorted(a + b), {}, 0
    while i < len(pooled):
        j = i
        while j < len(pooled) and pooled[j] == pooled[i]:
            j += 1
        rank[pooled[i]] = i + 1 + j  # doubled midrank of positions i+1..j
        i = j
    n, k = len(pooled), len(a)
    centre, observed = k * (n + 1), sum(rank[v] for v in a)
    counts = [{} for _ in range(k + 1)]  # counts[m][s]: size-m subsets with doubled rank sum s
    counts[0][0] = 1
    for r in (rank[v] for v in pooled):
        for m in range(k, 0, -1):
            for s, ways in counts[m - 1].items():
                counts[m][s + r] = counts[m].get(s + r, 0) + ways
    hits = sum(w for s, w in counts[k].items() if abs(s - centre) >= abs(observed - centre))
    return Fraction(hits, math.comb(n, k))

def screen(runs, direction, min_effect, guardrails, seen, errors):
    arms = [r["arm"] for r in runs]
    a, b = ([r["value"] for r in runs if r["arm"] == x] for x in "AB")
    why = (["needs >= %d runs per arm" % MIN_RUNS] * (min(len(a), len(b)) < MIN_RUNS)
           + ["runs do not strictly alternate A/B"] * any(x == y for x, y in zip(arms, arms[1:]))
           + ["over the exact rank-sum bound of %d runs" % EXACT_MAX_N] * (len(runs) > EXACT_MAX_N)
           + ["checklist lacks an integer 'errors' count"] * (errors is None)
           + ["missing guardrail " + g["name"] for g in guardrails if g["name"] not in seen])
    if why:
        return "inconclusive", why, {}
    gain = (statistics.median(b) - statistics.median(a)) * (1 if direction == "higher" else -1)
    spread, p = max(a) - min(a), rank_sum_p(a, b)
    why = (["improvement %g below min_effect %g" % (gain, min_effect)] * (not gain >= min_effect)
           + ["improvement %g not above baseline range %g" % (gain, spread)] * (not gain > spread)
           + ["rank-sum p %.4g not below 0.05" % p] * (not p < ALPHA)
           + ["guardrail regression: " + g["name"] for g in guardrails if not OPS[g["op"]](seen[g["name"]], g["threshold"])]
           + ["%d errors reported" % errors] * bool(errors))
    return "revert" if why else "keep", why, {"improvement": gain, "baseline_range": spread, "rank_sum_p": float(p)}

def num(value, low=-math.inf, kinds=(int, float)):
    if type(value) not in kinds or not math.isfinite(value) or value < low:
        raise ValueError(value)
    return value

def effect(text):
    return num(float(text), 0)

def guardrail(text):
    name, op, limit = (text.rsplit(":", 2) + ["", ""])[:3]
    if name and op in OPS:
        return {"name": name, "op": op, "threshold": num(float(limit))}
    raise ValueError(text)

def cmd_init(args, state, _lock):
    files = {path: digest(path) for path in map(os.path.abspath, args.harness)}
    with open(J(state, "decision.tsv"), "xb") as handle:
        handle.write(HEADER)
    save(J(state, "state.json"), [len(HEADER), sha(HEADER)])
    save(J(state, "harness.lock.json"), {"schema": SCHEMA, "files": files, "metric": args.metric, "direction":
                                         args.direction, "min_effect": args.min_effect, "guardrails": args.guardrail})

def cmd_hypothesize(args, state, lock):
    rows = read_log(state)[2]
    if open_hypothesis(rows) or args.id in [r["id"] for _kind, r in rows]:
        raise Refusal("an open hypothesis or reused id %s" % args.id)
    append_row(state, "hypothesis", {"id": args.id, "hypothesis": args.hypothesis, "expected_limiter":
                                     args.expected_limiter, "min_effect": lock["min_effect"] if args.min_effect is None else args.min_effect})

def cmd_record(args, state, lock):
    """run the paired A/B noise screen for the open hypothesis"""
    current = open_hypothesis(read_log(state)[2]) or {}
    if current.get("id") != args.id:
        raise Refusal("%s is not the open hypothesis (open: %s)" % (args.id, current.get("id")))
    try:
        check, seen = load(args.checklist_json), load(args.guardrails_json) if args.guardrails_json else {}
        missing = [q for q in BENCHMARK_QUESTIONS if not str(check.get(q) or "").strip()]
        runs = [{"arm": {"A": "A", "B": "B"}[r["arm"]], "value": num(r["value"]),
                 "errors": num(r.get("errors", 0), 0, (int,))} for r in load(args.runs_json)]
        seen = {k: num(v) for k, v in seen.items()}
        errors = None if check.get("errors") is None else num(check["errors"], 0, (int,)) + sum(r["errors"] for r in runs)
    except (ValueError, AttributeError, TypeError, KeyError) as exc:
        raise Refusal("malformed JSON input: %r" % exc)
    if missing:
        raise Refusal("checklist lacks answers to: " + "; ".join(missing))
    verdict, why, stats = screen(runs, lock["direction"], current["min_effect"], lock["guardrails"], seen, errors)
    append_row(state, "decision", {"id": args.id, "verdict": verdict, "reasons": why, "stats": stats,
                                   "runs": runs, "checklist": check, "guardrails": seen})
    print("\n  ".join(["paired A/B noise screen for %s: %s" % (args.id, verdict.upper())] + why))

def cmd_check(args, state, lock):
    bad, _chain, rows = read_log(state)
    bad = drift(lock) if args.command == "verify-harness-frozen" else bad
    done = [[r["id"], r["verdict"]] for kind, r in rows if kind == "decision"]
    report = {"problems": bad, "decisions": done, "open": (open_hypothesis(rows) or {}).get("id"),
              "verdicts": {v: [d[1] for d in done].count(v) for v in ("keep", "revert", "inconclusive")}}
    print(json.dumps(report, indent=1 if args.json else None) if args.command == "summary"
          else "\n".join("PROBLEM " + p for p in bad) or args.command + ": ok")
    return 1 if bad else 0

def main(argv=None):
    parser = argparse.ArgumentParser(description="pre-registered hill climbing by a paired A/B noise screen")
    sub, req = parser.add_subparsers(dest="command", required=True), {"required": True}
    for func, name, *options in (
            (cmd_init, "init", ("--harness", dict(req, nargs="+")), ("--metric", req), ("--min-effect", dict(req, type=effect)),
             ("--direction", dict(req, choices=("higher", "lower"))),
             ("--guardrail", dict(type=guardrail, action="append", default=[], help="NAME:op:threshold"))),
            (cmd_hypothesize, "hypothesize", ("--id", req), ("--hypothesis", req), ("--expected-limiter", req),
             ("--min-effect", dict(type=effect))),
            (cmd_record, "record", ("--id", req), ("--runs-json", req), ("--checklist-json", req), ("--guardrails-json", {})),
            (cmd_check, "verify-harness-frozen"), (cmd_check, "verify-log"),
            (cmd_check, "summary", ("--json", dict(action="store_true")))):
        command = sub.add_parser(name, help=func.__doc__)
        command.set_defaults(func=func)
        for flag, kwargs in [("--state-dir", req)] + options:
            command.add_argument(flag, **kwargs)
    args = parser.parse_args(argv)
    state, init = args.state_dir, args.func is cmd_init
    try:
        if worktree_of(state):
            raise Refusal("state dir is inside the Git worktree " + worktree_of(state))
        if os.path.lexists(J(state, "harness.lock.json")) == init:
            raise Refusal("state dir is %s initialized" % ("already" if init else "not"))
        os.makedirs(state, 0o700, exist_ok=True)
        with open(J(state, ".lock"), "a") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            lock = None if init else load(J(state, "harness.lock.json"))
            bad = drift(lock) if args.func in (cmd_hypothesize, cmd_record) else []
            if bad:
                raise Refusal("harness drifted: " + "; ".join(bad))
            return args.func(args, state, lock) or 0
    except (Refusal, OSError) as exc:
        print("refused: %s" % exc, file=sys.stderr)
        return 2

if __name__ == "__main__":
    sys.exit(main())
