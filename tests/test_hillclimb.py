"""Seeded property check: random.Random(20261124), 300 cases, n 1..6."""
import contextlib
import importlib.util
import io
import itertools
import json
import random
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1] / "skills" / "hanig-hillclimb"
spec = importlib.util.spec_from_file_location("hillclimb", SKILL / "scripts" / "hillclimb.py")
hc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hc)
QS = ("Why not double?", "Was it tuned?", "Did it break limits?", "Did it error?",
      "Does it reproduce?", "Does it matter?", "Did it even happen?")
A, B = [10, 10.4, 10.1, 10.3, 10.2], [20, 21, 20.5, 22, 20.7]
WIDE = ([10, 10.5, 11, 11.5, 12], [1, 14, 2, 14.5, 15])

def alt(a, b, arms="AB"):
    return [{"arm": arm, "value": v} for pair in zip(a, b) for arm, v in zip(arms, pair)]

def ok(errors=0, **kw):
    return dict({q: "e" for q in QS}, errors=errors, **kw)

class TestHillclimb(unittest.TestCase):
    CASES = (
        ("keep", alt(A, B), "keep", "", {}),
        ("B first", alt(B, A, "BA"), "keep", "", {}),
        ("1e308", alt([1e308] * 6, [1.1e308] * 6), "keep", "", {}),
        ("n=4", alt(A[:4], B[:4]), "inconclusive", "runs per arm", {}),
        ("non-alternating", alt(A, B)[::2] + alt(A, B)[1::2], "inconclusive", "alternate", {}),
        ("min_effect", alt(A, B), "revert", "below min_effect", {"effect": "50"}),
        ("median <= range", alt([0, 1, 2, 3, 10], [11, 11.5, 12, 12.2, 12.4]), "revert", "range", {}),
        ("p >= 0.05", alt(*WIDE), "revert", "rank-sum p", {}),
        ("guardrail", alt(A, B), "revert", "guardrail regression", {"guard": {"p99": 51}}),
        ("no guard", alt(A, B), "inconclusive", "missing guardrail", {"guard": {}}),
        ("check errors", alt(A, B), "revert", "errors reported", {"check": ok(1)}),
        ("run errors", [dict(r, errors=int(i == 3)) for i, r in enumerate(alt(A, B))], "revert", "errors", {}),
        ("no error count", alt(A, B), "inconclusive", "'errors'", {"check": ok(None)}),
        ("n > 60", alt([10 + i / 99 for i in range(31)], [20 + i / 99 for i in range(31)]),
         "inconclusive", "exact", {}),
    )

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.tmp, self.out = Path(temp.name), []
        if hc.worktree_of(temp.name):
            self.skipTest("temporary directory is inside a Git worktree")
        self.state, self.harness = str(self.tmp / "s"), self.tmp / "bench.py"
        self.harness.write_text("v1")

    def cli(self, *argv):
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                return hc.main(list(argv) + ["--state-dir", self.state])
        finally:
            self.out.append(buf.getvalue())

    def init(self):
        return self.cli("init", "--harness", str(self.harness), "--metric", "qps", "--direction", "higher",
                        "--min-effect", "1", "--guardrail", "p99:<=:50")

    def hyp(self, ident="h1", *extra):
        return self.cli("hypothesize", "--id", ident, "--hypothesis", "x", "--expected-limiter", "y", *extra)

    def record(self, runs, check=None, guard=None, ident="h1"):
        args = ["record", "--id", ident]
        for flag, data in (("--runs-json", runs), ("--checklist-json", ok() if check is None else check),
                           ("--guardrails-json", {"p99": 40} if guard is None else guard)):
            (self.tmp / flag).write_text(json.dumps(data))
            args += [flag, str(self.tmp / flag)]
        return self.cli(*args)

    def test_one_failing_fixture_per_keep_condition(self):
        for name, runs, verdict, reason, opt in self.CASES:
            with self.subTest(case=name):
                self.setUp()
                self.assertEqual((self.init(), self.hyp("h1", *(["--min-effect", opt["effect"]] if opt.get("effect") else []))), (0, 0))
                self.assertEqual(self.record(runs, opt.get("check"), opt.get("guard")), 0)
                decision = hc.read_log(self.state)[2][-1][1]
                self.assertEqual((decision["verdict"], len(decision["reasons"])), (verdict, int(bool(reason))))
                self.assertIn(reason, "".join(decision["reasons"]))
                self.assertIn("noise screen for h1: " + verdict.upper(), self.out[-1])

    def test_rank_sum_matches_brute_force_with_ties(self):
        rng = random.Random(20261124)
        for _ in range(300):
            a, b = ([rng.randint(0, 4) for _ in range(rng.randint(1, 6))] for _ in "ab")
            rank = [2 * sum(x < v for x in a + b) + 1 + (a + b).count(v) for v in a + b]
            centre, obs = len(a) * (len(rank) + 1), sum(rank[:len(a)])
            splits = list(itertools.combinations(rank, len(a)))
            brute = Fraction(sum(abs(sum(s) - centre) >= abs(obs - centre) for s in splits), len(splits))
            self.assertEqual(hc.rank_sum_p(a, b), brute, (a, b))
        self.assertEqual((hc.rank_sum_p(A, B), hc.rank_sum_p([1] * 5, [1] * 5)), (Fraction(2, 252), 1))

    def test_preregistration_drift_and_checklist(self):
        self.assertEqual((self.init(), self.record(alt(A, B))), (0, 2))
        self.harness.write_text("v2")
        self.assertEqual((self.hyp(), "harness drifted" in self.out[-1]), (2, True))
        self.harness.write_text("v1")
        self.assertEqual((self.hyp("h1"), self.hyp("h2"), "open hypothesis" in self.out[-1]), (0, 2, True))
        self.assertEqual(self.record(alt(A, B), ident="h2"), 2)
        for q in QS:
            self.assertEqual((self.record(alt(A, B), ok(**{q: " "})), q in self.out[-1]), (2, True))
        self.assertEqual((self.record(alt(A, B), {q: "y" for q in QS[1:]}), self.record([{"arm": "AB", "value": 1}]),
                          self.record([{"arm": "A", "value": 10 ** 400}])), (2, 2, 2))
        self.harness.unlink()
        self.assertEqual((self.record(alt(A, B)), "unreadable" in self.out[-1], self.cli("verify-harness-frozen")), (2, True, 1))
        self.harness.write_text("v1")
        self.assertEqual((self.record(alt(A, B)), self.hyp("h1")), (0, 2))
        self.assertEqual([kind for kind, _ in hc.read_log(self.state)[2]], ["hypothesis", "decision"])
        self.assertEqual(hc.BENCHMARK_QUESTIONS, QS)
        for i, q in enumerate(QS, 1):
            self.assertIn("%d. **%s**" % (i, q), (SKILL / "SKILL.md").read_text())

    def test_state_inside_a_worktree_is_refused(self):
        (self.tmp / "repo" / ".git").mkdir(parents=True)
        (self.tmp / "repo" / "a").mkdir()
        (self.tmp / "link").symlink_to(self.tmp / "repo" / "a")
        for self.state in (str(self.tmp / "repo" / "a" / "s"), str(self.tmp / "link")):
            self.assertEqual((self.init(), "inside the Git worktree" in self.out[-1]), (2, True))
        self.assertEqual(list((self.tmp / "repo" / "a").iterdir()), [])

    def test_log_prefix_stability_edits_and_wording(self):
        self.init()
        path = Path(self.state) / "decision.tsv"
        snaps = [path.read_bytes()]
        self.assertTrue(snaps[0].startswith(b'# schema="hanig_hillclimb_decision.v1"') and b"not cryptographic" in snaps[0])
        for ident, runs in (("h1", alt(A, B)), ("h2", alt(*WIDE)), ("h3", alt(A[:4], B[:4]))):
            snaps += [self.hyp(ident) or path.read_bytes(), self.record(runs, ident=ident) or path.read_bytes()]
        self.assertTrue(all(b.startswith(a) and b != a for a, b in zip(snaps, snaps[1:])))
        self.assertEqual((self.cli("verify-log"), self.cli("summary", "--json")), (0, 0))
        self.assertEqual(json.loads(self.out[-1])["verdicts"], {"keep": 1, "revert": 1, "inconclusive": 1})
        for edited in (snaps[-1].replace(b'"h1"', b'"h9"', 1), snaps[-2], snaps[-1].replace(b"v1", b"v2", 1)):
            path.write_bytes(edited)
            self.assertEqual([self.cli("verify-log"), self.cli("summary"), self.hyp("h4")], [1, 1, 2])
        (Path(self.state) / "state.json").write_text("{")
        self.assertEqual(self.cli("verify-log"), 2)
        for sub in ([], ["record"]):
            self.assertRaises(SystemExit, self.cli, *sub, "--help")
        self.assertIn("noise screen", self.out[-2] + self.out[-1])
        corpus = "".join(self.out) + snaps[-1].decode() + (SKILL / "SKILL.md").read_text()
        self.assertNotIn("significant", corpus.lower())

if __name__ == "__main__":
    unittest.main()
