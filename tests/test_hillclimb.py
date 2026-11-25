"""Contract tests for skills/hanig-hillclimb/scripts/hillclimb.py.

Property check: rank_sum_p is compared with brute-force itertools enumeration
on random.Random(SEED=20261124) fixtures, 300 cases, n_A and n_B in 1..6.
"""

import contextlib
import importlib.util
import io
import itertools
import json
import os
import random
import subprocess
import sys
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "hanig-hillclimb"
SCRIPT = SKILL / "scripts" / "hillclimb.py"
spec = importlib.util.spec_from_file_location("hillclimb", SCRIPT)
hc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hc)

SEED = 20261124
QUESTIONS = ("Why not double?", "Was it tuned?", "Did it break limits?", "Did it error?",
             "Does it reproduce?", "Does it matter?", "Did it even happen?")
A5, B5 = [10, 10.4, 10.1, 10.3, 10.2], [20, 21, 20.5, 22, 20.7]


def alternate(a, b, first="A"):
    pairs = zip(a, b) if first == "A" else zip(b, a)
    order = ("A", "B") if first == "A" else ("B", "A")
    return [{"arm": arm, "value": v} for pair in pairs for arm, v in zip(order, pair)]


def checklist(errors=0, drop=None):
    data = {q: "answered from run evidence" for q in QUESTIONS if q != drop}
    if errors is not None:
        data["errors"] = errors
    return data


class Base(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.tmp = Path(temp.name)
        if hc.worktree_of(str(self.tmp)):
            self.skipTest("temporary directory sits inside a Git worktree")
        self.state = str(self.tmp / "state")
        self.harness = self.tmp / "bench.py"
        self.harness.write_text("print('bench')\n")
        self.outputs = []

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = hc.main(list(argv))
        self.outputs.append(out.getvalue() + err.getvalue())
        return rc, out.getvalue(), err.getvalue()

    def init(self, *extra):
        rc = self.cli("init", "--state-dir", self.state, "--harness", str(self.harness),
                      "--metric", "qps", "--direction", "higher", "--min-effect", "1",
                      "--guardrail", "p99_ms:<=:50", *extra)[0]
        self.assertEqual(rc, 0)

    def hypothesize(self, ident="h1", *extra):
        return self.cli("hypothesize", "--state-dir", self.state, "--id", ident,
                        "--hypothesis", "batch writes", "--expected-limiter", "fsync", *extra)

    def record(self, runs, check=None, guard=None, ident="h1"):
        files = {}
        for name, data in (("runs", runs), ("check", checklist() if check is None else check),
                           ("guard", {"p99_ms": 40} if guard is None else guard)):
            files[name] = self.tmp / (name + ".json")
            files[name].write_text(json.dumps(data))
        return self.cli("record", "--state-dir", self.state, "--id", ident,
                        "--runs-json", str(files["runs"]), "--checklist-json", str(files["check"]),
                        "--guardrails-json", str(files["guard"]))

    def log(self):
        return (Path(self.state) / "decision.tsv").read_bytes()

    def rows(self):
        return hc.read_log(self.state)[2]


class TestKeepGate(Base):
    CASES = (
        ("keep", alternate(A5, B5), {}, "keep", None),
        ("keep B first", alternate(A5, B5, "B"), {}, "keep", None),
        ("n=4 per arm", alternate(A5[:4], B5[:4]), {}, "inconclusive", "runs per arm"),
        ("non-alternating", [{"arm": "A", "value": v} for v in A5] + [{"arm": "B", "value": v} for v in B5],
         {}, "inconclusive", "alternate"),
        ("below min_effect", alternate(A5, B5), {"effect": "50"}, "revert", "below min_effect"),
        ("median <= baseline range", alternate([0, 1, 2, 3, 10], [11, 11.5, 12, 12.2, 12.4]), {},
         "revert", "baseline range"),
        ("rank-sum p >= 0.05", alternate([10, 10.5, 11, 11.5, 12], [1, 14, 2, 14.5, 15]), {}, "revert", "rank-sum p"),
        ("guardrail regression", alternate(A5, B5), {"guard": {"p99_ms": 51}}, "revert", "guardrail regression"),
        ("missing guardrail", alternate(A5, B5), {"guard": {}}, "inconclusive", "missing guardrail"),
        ("checklist errors", alternate(A5, B5), {"check": checklist(errors=1)}, "revert", "errors reported"),
        ("run errors", [dict(r, errors=1) if i == 3 else r for i, r in enumerate(alternate(A5, B5))],
         {}, "revert", "errors reported"),
        ("no error count", alternate(A5, B5), {"check": checklist(errors=None)}, "inconclusive", "'errors'"),
        ("over exact bound", alternate([10.0 + i / 100 for i in range(31)], [20.0 + i / 100 for i in range(31)]),
         {}, "inconclusive", "exact rank-sum bound"),
    )

    def test_each_keep_condition_has_a_failing_fixture(self):
        for name, runs, opts, verdict, reason in self.CASES:
            with self.subTest(case=name):
                self.setUp()
                self.init()
                extra = ("--min-effect", opts["effect"]) if "effect" in opts else ()
                self.assertEqual(self.hypothesize("h1", *extra)[0], 0)
                rc, out, _ = self.record(runs, opts.get("check"), opts.get("guard"))
                self.assertEqual(rc, 0, out)
                decision = self.rows()[-1][1]
                self.assertEqual(decision["verdict"], verdict, decision["reasons"])
                self.assertIn("paired A/B noise screen for h1: " + verdict.upper(), out)
                if reason:
                    self.assertEqual(len(decision["reasons"]), 1, decision["reasons"])
                    self.assertIn(reason, decision["reasons"][0])
                else:
                    self.assertEqual(decision["reasons"], [])
                self.assertIsNone(hc.open_hypothesis(self.rows()))


class TestRankSum(unittest.TestCase):
    def brute(self, a, b):
        pooled = sorted(a + b)
        rank = {v: sum(1 for x in pooled if x < v) * 2 + 1 + pooled.count(v) for v in pooled}
        ranks, k = [rank[v] for v in a + b], len(a)
        centre, observed = k * (len(ranks) + 1), sum(rank[v] for v in a)
        splits = list(itertools.combinations(range(len(ranks)), k))
        hits = sum(abs(sum(ranks[i] for i in s) - centre) >= abs(observed - centre) for s in splits)
        return Fraction(hits, len(splits))

    def test_matches_brute_force_enumeration_with_ties(self):
        rng = random.Random(SEED)
        for _ in range(300):
            a = [rng.randint(0, 4) for _ in range(rng.randint(1, 6))]
            b = [rng.randint(0, 4) for _ in range(rng.randint(1, 6))]
            self.assertEqual(hc.rank_sum_p(a, b), self.brute(a, b), (a, b))

    def test_known_values(self):
        self.assertEqual(hc.rank_sum_p(A5, B5), Fraction(2, 252))
        self.assertEqual(hc.rank_sum_p([1.0] * 5, [1.0] * 5), 1)


class TestPreRegistration(Base):
    def test_hypothesize_and_record_refuse_harness_drift(self):
        self.init()
        self.harness.write_text("print('tuned')\n")
        rc, _, err = self.hypothesize()
        self.assertEqual((rc, "harness drifted" in err), (2, True))
        self.harness.write_text("print('bench')\n")
        self.assertEqual(self.hypothesize()[0], 0)
        self.harness.unlink()
        rc, _, err = self.record(alternate(A5, B5))
        self.assertEqual((rc, "unreadable or deleted" in err), (2, True))
        self.assertEqual(self.cli("verify-harness-frozen", "--state-dir", self.state)[0], 1)
        self.assertEqual(len(self.rows()), 1)

    def test_single_open_hypothesis_and_matching_id(self):
        self.init()
        rc, _, err = self.record(alternate(A5, B5))
        self.assertEqual((rc, "open: none" in err), (2, True))
        self.assertEqual(self.hypothesize("h1")[0], 0)
        rc, _, err = self.hypothesize("h2")
        self.assertEqual((rc, "h1 is still open" in err), (2, True))
        self.assertEqual(self.record(alternate(A5, B5), ident="h2")[0], 2)
        self.assertEqual(self.record(alternate(A5, B5))[0], 0)
        self.assertEqual(self.hypothesize("h1")[0], 2)
        self.assertEqual([kind for kind, _ in self.rows()], ["hypothesis", "decision"])

    def test_every_question_needs_a_non_empty_answer(self):
        self.init()
        self.hypothesize()
        for question in QUESTIONS:
            with self.subTest(question=question):
                rc, _, err = self.record(alternate(A5, B5), checklist(drop=question))
                self.assertEqual((rc, question in err), (2, True))
                blank = dict(checklist(), **{question: "  "})
                self.assertEqual(self.record(alternate(A5, B5), blank)[0], 2)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.record(alternate(A5, B5))[0], 0)

    def test_questions_are_pinned_in_script_and_skill(self):
        self.assertEqual(hc.BENCHMARK_QUESTIONS, QUESTIONS)
        text = (SKILL / "SKILL.md").read_text()
        for number, question in enumerate(QUESTIONS, 1):
            self.assertIn("%d. **%s**" % (number, question), text)


class TestStateAndLog(Base):
    def test_state_dir_inside_a_worktree_is_refused(self):
        repo = self.tmp / "repo"
        (repo / ".git").mkdir(parents=True)
        link = self.tmp / "link"
        link.symlink_to(repo, target_is_directory=True)
        for target in (repo / "deep" / "state", link / "state"):
            self.state = str(target)
            rc, _, err = self.cli("init", "--state-dir", self.state, "--harness", str(self.harness),
                                  "--metric", "m", "--direction", "lower", "--min-effect", "0")
            self.assertEqual((rc, "inside the Git worktree" in err), (2, True))
            self.assertFalse(os.path.lexists(str(repo / "deep")) or os.path.lexists(str(repo / "state")))

    def test_log_is_byte_prefix_stable_and_detects_edits(self):
        self.init()
        snapshots = [self.log()]
        self.assertTrue(snapshots[0].startswith(b'# schema="hanig_hillclimb_decision.v1"'))
        self.assertIn(b"not cryptographic", snapshots[0])
        for ident in ("h1", "h2"):
            self.hypothesize(ident)
            snapshots.append(self.log())
            self.record(alternate(A5, B5), ident=ident)
            snapshots.append(self.log())
        for before, after in zip(snapshots, snapshots[1:]):
            self.assertTrue(after.startswith(before) and len(after) > len(before))
        self.assertEqual(self.cli("verify-log", "--state-dir", self.state)[0], 0)
        path = Path(self.state) / "decision.tsv"
        good = self.log()
        for name, edited in (("row edit", good.replace(b'"h1"', b'"h9"', 1)),
                             ("truncation", snapshots[-2]),
                             ("header edit", good.replace(b"v1", b"v2", 1))):
            with self.subTest(edit=name):
                path.write_bytes(edited)
                self.assertEqual(self.cli("verify-log", "--state-dir", self.state)[0], 1)
                self.assertEqual(self.hypothesize("h3")[0], 2)
                self.assertEqual(self.cli("summary", "--state-dir", self.state)[0], 1)
        path.write_bytes(good)
        rc, out, _ = self.cli("summary", "--state-dir", self.state, "--json")
        self.assertEqual(json.loads(out)["verdicts"], {"keep": 2, "revert": 0, "inconclusive": 0})

    def test_outputs_never_say_significant(self):
        self.init()
        self.hypothesize()
        self.record(alternate(A5, B5, "B"))
        self.hypothesize("h2")
        self.record(alternate([10, 10.5, 11, 11.5, 12], [1, 14, 2, 14.5, 15]), ident="h2")
        self.hypothesize("h3")
        self.record(alternate(A5[:4], B5[:4]), ident="h3")
        self.record(alternate(A5, B5), ident="h3")
        for command in (("summary",), ("summary", "--json"), ("verify-log",), ("verify-harness-frozen",)):
            self.cli(*command, "--state-dir", self.state)
        helps = [subprocess.run([sys.executable, str(SCRIPT)] + ([sub] if sub else []) + ["--help"],
                                capture_output=True, text=True, timeout=60).stdout
                 for sub in ("", "init", "hypothesize", "record", "summary")]
        self.assertIn("noise screen", helps[0])
        corpus = "\n".join(self.outputs + helps) + self.log().decode() + (SKILL / "SKILL.md").read_text()
        self.assertNotIn("significant", corpus.lower())


if __name__ == "__main__":
    unittest.main()
