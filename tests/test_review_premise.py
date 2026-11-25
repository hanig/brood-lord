"""L2 premise check (review_premise.py): strike rules, history and the CLI gate."""

from tests.watchdog_guard import guarded_main
import ast
import hashlib
import io
import json
import os
import sys
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from unittest.mock import patch
import tests.test_review_rounds as round_tests

review, SCRIPT = round_tests.review, round_tests.SCRIPT
P, TREE = review._premise, "a" * 40


def F(n, cls=None, file="code.py", summary="cache key omits tenant scope"):
    out = {"finding_digest": "%064x" % n, "confirmed": True, "file": file, "line": n,
           "summary": summary}
    return dict(out, defect_class=cls) if cls else out


def E(status, tree=TREE, at="2026-11-24T00:00:00Z"):
    return review._evidence_module().build_record(
        identity="d" * 64, reviewed_head="c" * 40, finding_digest="%064x" % 2, round_no=2,
        candidate_tree=tree, method="inspection", status=status, recorded_at=at)


def D(n, disposition, cls=None):
    return dict({"finding_digest": "%064x" % n, "disposition": disposition},
                **({"defect_class": cls} if cls else {}))


def R(number, findings, identity="d" * 64, cycle=1, **extra):
    return dict(type="review_round", kind="implementation", round=number, identity=identity,
                cycle=cycle, results=[{"name": "r", "findings": findings}], **extra)


X2 = {1: [F(1, "x")], 2: [F(2, "x")]}
CASES = [  # name, rounds, dispositions, evidence, expected status
    ("same class consecutive", X2, (), (), "triggered"),
    ("same file other classes", {1: [F(1, "x")], 2: [F(2, "y")]}, (), (), "not_triggered"),
    ("non-consecutive", {1: [F(1, "x")], 2: [F(2, "y")], 3: [F(3, "x")]}, (), (), "not_triggered"),
    ("gap", {1: [F(1, "x")], 3: [F(3, "x")]}, (), (), "not_triggered"),
    ("moved file", {1: [F(1, "x", "a.py")], 2: [F(2, "x", "b.py")]}, (), (), "triggered"),
    ("disposition class", {1: [F(1)], 2: [F(2, "x")]}, (D(1, "reproduced", "x"),), (), "triggered"),
    ("not-reproduced disposition", X2, (D(2, "not-reproduced"),), (), "not_triggered"),
    ("L1 not_reproduced on current tree", X2, (), (E("not_reproduced"),), "not_triggered"),
    ("stale L1 not_reproduced", X2, (), (E("not_reproduced", "b" * 40),), "triggered"),
    ("latest current-tree L1 wins", X2, (),
     (E("not_reproduced"), E("reproduced", at="2026-11-24T01:00:00Z")), "triggered"),
    ("missing class", {1: [F(1)], 2: [F(2)]}, (), (), "unavailable"),
    ("uppercase is no class", {1: [F(1, "X")], 2: [F(2, "X")]}, (), (), "unavailable"),
]


class TestStrikeRules(unittest.TestCase):
    def test_table(self):
        for name, rounds, dispositions, evidence, status in CASES:
            with self.subTest(name):
                got = P.evaluate(rounds, dispositions, evidence, TREE)
                self.assertEqual((got["status"], got["triggered"]), (status, status == "triggered"))
                if got["triggered"]:
                    self.assertEqual((got["defect_classes"], got["rounds"]), (["x"], [1, 2]))

    def test_one_strike_per_round_and_advisory_weak_signal(self):
        got = P.evaluate({1: [F(1, "x"), F(2, "x", "o.py"), F(3, "x")], 2: [F(4, "y")]})
        self.assertEqual((got["strikes"], got["triggered"]), ({"1": ["x"], "2": ["y"]}, False))
        weak = lambda text: P.evaluate({1: [F(1)], 2: [F(2, summary=text)]})["weak_signal"]
        self.assertEqual(weak("tenant scope omitted from cache"), {
            "advisory_only": True, "gating": False, "detected": True,
            "matches": [{"file": "code.py", "rounds": [1, 2]}]})
        self.assertFalse(weak("logs omit the exit status")["detected"])

    def test_history_cycles_identities_and_journaled_dispositions(self):
        records = [R(1, [F(1, "x")]), R(2, [F(2, "x")]), R(1, [F(4, "x")], cycle=2),
                   R(2, [F(5, "x")], identity="9" * 64), R(3, [dict(F(3, "x"), confirmed=False)])]
        check = lambda i, c: P.evaluate(*P.journal_history(records, i, c)[:2])["rounds"]
        self.assertEqual((check("d" * 64, 1), check("d" * 64, 2), check("9" * 64, 1)),
                         ([1, 2], [], []))
        records = [R(1, [F(1)]), R(2, [F(2, "x")], dispositions=[D(1, "reproduced", "x")])]
        self.assertEqual(check("d" * 64, 1), [1, 2])

    def test_module_is_pure_stdlib_and_parses_as_python_38(self):
        tree = ast.parse(SCRIPT.with_name("review_premise.py").read_text(), feature_version=(3, 8))
        names = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        self.assertLessEqual(names | {n.module for n in ast.walk(tree) if isinstance(
            n, ast.ImportFrom)}, {"hashlib", "importlib.util", "json", "os", "re", "stat", "pathlib"})


class TestCliGate(unittest.TestCase):
    """Real parser, ledger, journal child and history read; offline reviewers."""
    git = round_tests.TestRoundReservations.git

    def setUp(self):
        round_tests.TestRoundReservations.setUp(self)
        os.environ.pop(review.JOURNAL_TEST_MARKER, None)
        self.identity = review.change_identity()
        self.journal = self.state / review.JOURNAL_DIR / review.JOURNAL_NAME

    def history(self, *findings):
        for number, finding in enumerate(findings, 1):
            review.reserve_round(self.identity, number, "standard")
            (self.journal / ("%020d" % number)).mkdir(parents=True)
            (self.journal / ("%020d" % number) / "record.jsonl").write_text(
                json.dumps(R(number, [finding], identity=self.identity)) + "\n")

    def markers(self):
        return sorted((self.state / "hanig-review-gate/rounds").glob("*/*.json"))

    def invoke(self, number, *extra, findings=()):
        calls, out, err = [], io.StringIO(), io.StringIO()
        roster = [{"name": n, "model": n, "provider": "stub", "profiles": ["standard"]}
                  for n in "ab"]
        answer = lambda rev, *_a: calls.append(rev["name"]) or {
            "ok": True, "name": rev["name"], "findings": list(findings), "claims": [],
            "verdict": "refuted" if findings else "upheld", "elapsed_s": 0}
        argv = [str(SCRIPT), "--kind", "implementation", "--round", str(number), "--author",
                "codex/gpt-6-astra", "--file", "code.py", "--profile", "standard", "--json",
                "--dispositions", str(self.dispositions), "--claim", review.HONEST_RUN_CLAIM]
        with ExitStack() as stack:
            for context in (patch.multiple(review, load_effective_reviewers=lambda: roster,
                                           availability=lambda _r: None, run_one=answer),
                            patch.object(sys, "argv", argv + list(extra)),
                            redirect_stdout(out), redirect_stderr(err)):
                stack.enter_context(context)
            with self.assertRaises(SystemExit) as stopped:
                guarded_main(review.main)
        code = stopped.exception.code
        return code, (json.loads(out.getvalue()) if code != 4 else None), err.getvalue(), calls

    def last_record(self):
        return json.loads((sorted(self.journal.iterdir())[-1] / "record.jsonl").read_text())

    def test_default_exit_codes_unchanged_and_check_is_reported(self):
        self.history(F(1, "x"), F(2, "x"))
        code, report, err, calls = self.invoke(3)
        self.assertEqual((code, report["state"], calls), (0, "REVIEW_PASS", ["a", "b"]))
        self.assertEqual(report["premise_check"]["defect_classes"], ["x"])
        self.assertIn("PREMISE CHECK (advisory)", err)
        record = self.last_record()
        self.assertEqual((record["identity"], record["cycle"], record["round"]),
                         (self.identity, 1, 3))
        self.setUp()
        self.history(F(1, "x"), F(2, "x"))
        bad = {"file": "code.py", "line": 1, "severity": "major", "confidence": "high",
               "summary": "s", "failure_scenario": "input dropped"}
        self.assertEqual(self.invoke(3, findings=[bad])[0], 1)

    def test_require_ack_refuses_before_reservation_or_records_memo_or_reason(self):
        self.history(F(1, "x"), F(2, "x"))
        before = self.markers()
        code, _report, err, calls = self.invoke(3, "--require-premise-ack")
        self.assertEqual((code, calls, self.markers()), (4, [], before))
        self.assertIn("struck in consecutive rounds 1 and 2", err)
        memo = self.root / "memo.md"
        memo.write_bytes("Premise: the cache key is wrong.\r\né\n".encode("utf-8"))
        for flags, ack in ((("--premise-memo", str(memo)), {"mode": "premise_memo", "path": str(
                memo), "sha256": hashlib.sha256(memo.read_bytes()).hexdigest()}),
                (("--continue-patching", "one more guard"),
                 {"mode": "continue_patching", "reason": "one more guard"})):
            self.setUp()
            self.history(F(1, "x"), F(2, "x"))
            code, report, _err, calls = self.invoke(3, "--require-premise-ack", *flags)
            self.assertEqual((code, calls, report["premise_ack"], self.last_record()[
                "premise_ack"]), (0, ["a", "b"], ack, ack))

    def test_dispositions_file_classifies_the_prior_round_and_round_two_passes(self):
        digest = review.finding_digest("code.py:2", "s")
        entry = {"location": "code.py:2", "summary": "s", "disposition": "reproduced",
                 "reason": "r", "reproduction": dict(dict.fromkeys(
                     ("setup", "command", "expected", "observed"), "x"), revision="f" * 40)}
        self.history(F(1, "x"), dict(F(2), finding_digest=digest))
        for cls, code in (("y", 0), ("x", 4)):
            self.dispositions.write_text(json.dumps({digest: dict(entry, defect_class=cls)}))
            self.assertEqual(self.invoke(3, "--require-premise-ack")[0], code, cls)
        self.setUp()
        self.history(F(1, "x"))
        self.assertEqual(self.invoke(2, "--require-premise-ack")[0], 0)

    def test_invalid_acknowledgments_refuse_before_reviewers(self):
        self.history(F(1, "x"), F(2, "x"))
        (self.root / "blank.md").write_text("  \n")
        (self.root / "bad.md").write_bytes(b"\xff\xfe")
        for extra in (("--premise-memo", str(self.root / "blank.md")), ("--premise-memo", str(
                self.root / "bad.md")), ("--continue-patching", "a\nb"),
                ("--premise-memo", "m", "--continue-patching", "x")):
            self.assertEqual(self.invoke(3, "--require-premise-ack", *extra)[::3], (4, []), extra)
        self.dispositions.write_text(json.dumps({review.finding_digest("a:1", "s"): {
            "location": "a:1", "summary": "s", "disposition": "deferred", "reason": "r",
            "defect_class": "Stale"}}))
        code, _report, err, calls = self.invoke(3)
        self.assertEqual((code, calls, "lowercase slug" in err), (4, [], True))


if __name__ == "__main__":
    unittest.main()
