"""L2 premise check: class-only strikes, consecutive rounds, L1 exclusion, opt-in gate.

Property bound: exhaustive itertools enumeration of 4 rounds x 0-2 findings per
round x defect_class in {None, "a", "b"} (13**4 = 28,561 histories), all on one
file, against an independent oracle. No random seed is used.
"""

from tests.watchdog_guard import guarded_main
import ast
import hashlib
import importlib.util
import io
import itertools
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills" / "hanig-review-gate" / "scripts"
SCRIPT, PREMISE = SCRIPTS / "review.py", SCRIPTS / "review_premise.py"
spec = importlib.util.spec_from_file_location("review_premise_facade", SCRIPT)
review = importlib.util.module_from_spec(spec)
spec.loader.exec_module(review)
P = review._premise
TREE, OLD_TREE, HEAD = "a" * 40, "b" * 40, "c" * 40


def finding(n, cls=None, file="code.py", summary="cache key omits the tenant scope"):
    out = {"finding_digest": "%064x" % n, "confirmed": True, "file": file, "line": n,
           "summary": summary}
    if cls is not None:
        out["defect_class"] = cls
    return out


def evidence(n, status, tree=TREE, at="2026-11-24T00:00:00Z"):
    return review._evidence_module().build_record(
        identity="d" * 64, reviewed_head=HEAD, finding_digest="%064x" % n, round_no=2,
        candidate_tree=tree, method="inspection", status=status, recorded_at=at)


def check(rounds, dispositions=(), ev=(), tree=TREE):
    return P.evaluate(rounds, dispositions, ev, tree)


class TestStrikeRules(unittest.TestCase):
    CASES = [
        # name, rounds, dispositions, evidence, triggered, status
        ("same class consecutive rounds", {1: [finding(1, "x")], 2: [finding(2, "x")]},
         (), (), True, "triggered"),
        ("same file different classes", {1: [finding(1, "x")], 2: [finding(2, "y")]},
         (), (), False, "not_triggered"),
        ("non-consecutive rounds", {1: [finding(1, "x")], 2: [finding(2, "y")],
                                    3: [finding(3, "x")]}, (), (), False, "not_triggered"),
        ("gap without a round two", {1: [finding(1, "x")], 3: [finding(3, "x")]},
         (), (), False, "not_triggered"),
        ("disposition supplies the class", {1: [finding(1)], 2: [finding(2, "x")]},
         ({"finding_digest": "%064x" % 1, "disposition": "reproduced", "defect_class": "x"},),
         (), True, "triggered"),
        ("not-reproduced disposition excluded", {1: [finding(1, "x")], 2: [finding(2, "x")]},
         ({"finding_digest": "%064x" % 2, "disposition": "not-reproduced"},),
         (), False, "not_triggered"),
        ("L1 not_reproduced on the current tree excluded",
         {1: [finding(1, "x")], 2: [finding(2, "x")]}, (), (evidence(2, "not_reproduced"),),
         False, "not_triggered"),
        ("stale L1 not_reproduced does not exclude",
         {1: [finding(1, "x")], 2: [finding(2, "x")]}, (),
         (evidence(2, "not_reproduced", tree=OLD_TREE),), True, "triggered"),
        ("latest current-tree L1 record wins",
         {1: [finding(1, "x")], 2: [finding(2, "x")]}, (),
         (evidence(2, "not_reproduced"), evidence(2, "reproduced", at="2026-11-24T01:00:00Z")),
         True, "triggered"),
        ("missing class is unavailable", {1: [finding(1)], 2: [finding(2)]},
         (), (), False, "unavailable"),
    ]

    def test_table(self):
        for name, rounds, dispositions, ev, triggered, status in self.CASES:
            with self.subTest(name):
                got = check(rounds, dispositions, ev)
                self.assertEqual((got["triggered"], got["status"]), (triggered, status))
                if triggered:
                    self.assertEqual((got["defect_classes"], got["rounds"]), (["x"], [1, 2]))

    def test_multiple_findings_in_one_round_are_one_strike(self):
        got = check({1: [finding(1, "x"), finding(2, "x", file="other.py"), finding(3, "x")],
                     2: [finding(4, "y")]})
        self.assertEqual(got["strikes"], {"1": ["x"], "2": ["y"]})
        self.assertFalse(got["triggered"])
        got = check({1: [finding(1, "x"), finding(2, "x")], 2: [finding(3, "x")]})
        self.assertEqual(got["triggers"], [{"defect_class": "x", "rounds": [1, 2]}])

    def test_strike_key_is_class_only_never_file_or_line(self):
        moved = check({1: [finding(1, "x", file="a.py")], 2: [finding(2, "x", file="b.py")]})
        same = check({1: [finding(1, "x")], 2: [finding(1, "y")]})
        self.assertEqual((moved["triggered"], same["triggered"]), (True, False))
        self.assertEqual(moved["strike_key"], "defect_class")

    def test_missing_class_weak_signal_is_advisory_only(self):
        got = check({1: [finding(1)], 2: [finding(2, summary="tenant scope omitted from cache key")]})
        self.assertEqual(got["status"], "unavailable")
        self.assertFalse(got["triggered"])
        weak = got["weak_signal"]
        self.assertEqual((weak["advisory_only"], weak["gating"], weak["detected"]),
                         (True, False, True))
        self.assertEqual(weak["matches"], [{"file": "code.py", "rounds": [1, 2]}])
        unrelated = check({1: [finding(1)], 2: [finding(2, summary="logs omit the exit status")]})
        self.assertFalse(unrelated["weak_signal"]["detected"])

    def test_inputs_are_not_modified(self):
        rounds = {1: [finding(1, "x")], 2: [finding(2, "x")]}
        before = json.dumps(rounds, sort_keys=True)
        check(rounds, ({"finding_digest": "%064x" % 1, "disposition": "deferred"},))
        self.assertEqual(json.dumps(rounds, sort_keys=True), before)

    def test_exhaustive_property_against_oracle(self):
        per_round = [()] + [(c,) for c in (None, "a", "b")] + list(
            itertools.product((None, "a", "b"), repeat=2))
        cases = 0
        for history in itertools.product(per_round, repeat=4):
            rounds, n = {}, 0
            for number, classes in enumerate(history, 1):
                rounds[number] = []
                for cls in classes:
                    n += 1
                    rounds[number].append(finding(n, cls))
            sets = [{c for c in classes if c} for classes in history]
            expected = sorted({c for i in range(3) for c in sets[i] & sets[i + 1]})
            got = check(rounds)
            self.assertEqual(got["defect_classes"], expected, history)
            unclassified = any(c is None for classes in history for c in classes)
            self.assertEqual(got["status"], "triggered" if expected else
                             "unavailable" if unclassified else "not_triggered")
            cases += 1
        self.assertEqual(cases, 13 ** 4)

    def test_defect_class_grammar_is_exact(self):
        for value in ("a", "stale-cache_2", "a" * 64):
            self.assertTrue(P.valid_defect_class(value), value)
        for value in ("", "A", " a", "a ", "1a", "a" * 65, "a.b", None, 1, ["a"]):
            self.assertFalse(P.valid_defect_class(value), value)
        self.assertEqual(check({1: [finding(1, "X")], 2: [finding(2, "X")]})["status"],
                         "unavailable")


class TestHistoryAndReplay(unittest.TestCase):
    def round_record(self, number, findings, identity="d" * 64, cycle=1, **extra):
        return dict({"type": "review_round", "kind": "implementation", "round": number,
                     "identity": identity, "cycle": cycle, "claim_digests": ["e" * 64],
                     "results": [{"name": "r", "findings": findings}]}, **extra)

    def test_fresh_cycle_and_other_identity_do_not_inherit_strikes(self):
        records = [self.round_record(1, [finding(1, "x")]),
                   self.round_record(2, [finding(2, "x")]),
                   self.round_record(3, [finding(3, "x")]),
                   self.round_record(1, [finding(4, "x")], cycle=2),
                   self.round_record(2, [finding(5, "x")], identity="9" * 64)]
        first = check(*P.journal_history(records, "d" * 64, 1)[:2])
        fresh = check(*P.journal_history(records, "d" * 64, 2)[:2])
        other = check(*P.journal_history(records, "9" * 64, 1)[:2])
        self.assertTrue(first["triggered"])
        self.assertEqual((fresh["triggered"], fresh["rounds_evaluated"]), (False, [1]))
        self.assertEqual((other["triggered"], other["rounds_evaluated"]), (False, [2]))

    def test_unconfirmed_findings_and_journaled_dispositions(self):
        quiet = dict(finding(2, "x"), confirmed=False)
        records = [self.round_record(1, [finding(1, "x")]), self.round_record(2, [quiet])]
        self.assertFalse(check(*P.journal_history(records, "d" * 64, 1)[:2])["triggered"])
        records = [self.round_record(1, [finding(1)]), self.round_record(
            2, [finding(2, "x")], dispositions=[{"finding_digest": "%064x" % 1,
                                                 "disposition": "reproduced",
                                                 "defect_class": "x"}])]
        self.assertTrue(check(*P.journal_history(records, "d" * 64, 1)[:2])["triggered"])

    def write_journal(self, root, records):
        for index, record in enumerate(records):
            event = root / ("%020d-1-%024x" % (index, index))
            event.mkdir(parents=True)
            (event / "record.jsonl").write_text(json.dumps(record) + "\n")
        (root / ("%020d-1-damaged" % len(records))).mkdir()
        (root / ("%020d-1-damaged" % len(records)) / "record.jsonl").write_text("{")

    def snapshot(self, root):
        return {str(p): (p.read_bytes(), p.stat().st_mtime_ns)
                for p in sorted(root.rglob("*")) if p.is_file()}

    def test_replay_is_read_only_and_prints_counts_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "review-rounds"
            legacy = {k: v for k, v in self.round_record(1, [finding(7)]).items()
                      if k not in ("identity", "cycle")}
            self.write_journal(root, [
                self.round_record(1, [finding(1, "x")]), self.round_record(2, [finding(2, "x")]),
                legacy, dict(legacy, round=2), dict(legacy, round=1),
                {"type": "review_round", "kind": "plan", "round": None}])
            before = self.snapshot(root)
            counts = P.replay(str(root))
            result = subprocess.run([sys.executable, str(PREMISE), "premise-replay",
                                     "--state-dir", str(root)], stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, universal_newlines=True, timeout=60)
            self.assertEqual(self.snapshot(root), before)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), counts)
        self.assertEqual(counts, {"identities_scanned": 2, "cycles_scanned": 3,
                                  "rounds_scanned": 5, "triggered_count": 1,
                                  "unavailable_count": 2, "weak_signal_count": 2,
                                  "records_scanned": 6, "unreadable_records": 1})

    def test_module_is_pure_stdlib_and_parses_as_python_38(self):
        tree = ast.parse(PREMISE.read_text(), feature_version=(3, 8))
        names = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        names |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        self.assertLessEqual(names, {"argparse", "hashlib", "importlib.util", "json", "os",
                                     "re", "stat", "sys", "pathlib"})
        for name in ("review_premise", "review_schema", "review_ledger", "review_cli", "review"):
            ast.parse((SCRIPTS / (name + ".py")).read_text(), feature_version=(3, 8))


class TestCliGate(unittest.TestCase):
    """Real parser, ledger, journal child and history read; offline reviewers."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        original = Path.cwd()
        os.chdir(str(self.repo))
        self.addCleanup(os.chdir, str(original))
        self.state = self.root / "state"
        env = patch.dict(os.environ, {"XDG_STATE_HOME": str(self.state)})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(review.JOURNAL_TEST_MARKER, None)
        for argv in (("init", "-q"), ("config", "user.name", "Premise Fixture"),
                     ("config", "user.email", "premise@example.invalid"),
                     ("config", "commit.gpgsign", "false"),
                     ("remote", "add", "origin", "https://example.invalid/repo.git")):
            self.git(*argv)
        Path("code.py").write_text("value = 1\n")
        self.git("add", ".")
        self.git("commit", "-qm", "base")
        self.git("update-ref", "refs/remotes/origin/main", self.git("rev-parse", "HEAD"))
        self.git("checkout", "-qb", "change")
        self.identity = review.change_identity()
        self.journal = self.state / review.JOURNAL_DIR / review.JOURNAL_NAME
        self.dispositions = self.root / "dispositions.json"
        self.dispositions.write_text("{}")

    def git(self, *args):
        return subprocess.check_output(["git", *args], stderr=subprocess.STDOUT).decode().strip()

    def history(self, classes):
        """Reserve rounds 1..len(classes) and journal one finding per round."""
        for number, cls in enumerate(classes, 1):
            review.reserve_round(self.identity, number, "standard")
            event = self.journal / ("%020d-1-%024x" % (number, number))
            event.mkdir(parents=True)
            record = {"type": "review_round", "kind": "implementation", "round": number,
                      "identity": self.identity, "cycle": 1,
                      "results": [{"name": "a", "findings": [finding(number, cls)]}]}
            (event / "record.jsonl").write_text(json.dumps(record) + "\n")

    def markers(self):
        return sorted((self.state / "hanig-review-gate/rounds").glob("*/*.json"))

    def invoke(self, number, *extra, findings=()):
        calls = []
        roster = [{"name": n, "model": n, "provider": "stub",
                   "profiles": ["standard"], "enabled": True} for n in ("a", "b")]

        def answer(reviewer, *_args):
            calls.append(reviewer["name"])
            return {"ok": True, "name": reviewer["name"], "findings": list(findings),
                    "verdict": "refuted" if findings else "upheld", "claims": [],
                    "elapsed_s": 0}

        argv = [str(SCRIPT), "--kind", "implementation", "--round", str(number),
                "--author", "codex/gpt-6-astra", "--file", "code.py", "--profile", "standard",
                "--dispositions", str(self.dispositions), "--claim", review.HONEST_RUN_CLAIM,
                "--json", *extra]
        out, err = io.StringIO(), io.StringIO()
        with ExitStack() as stack:
            for name, value in (("load_effective_reviewers", lambda: roster),
                                ("availability", lambda _r: None), ("run_one", answer)):
                stack.enter_context(patch.object(review, name, value))
            stack.enter_context(patch.object(sys, "argv", argv))
            stack.enter_context(redirect_stdout(out))
            stack.enter_context(redirect_stderr(err))
            with self.assertRaises(SystemExit) as stopped:
                guarded_main(review.main)
        report = json.loads(out.getvalue()) if stopped.exception.code != 4 else None
        return stopped.exception.code, report, err.getvalue(), calls

    def last_record(self):
        events = sorted(self.journal.iterdir())
        return json.loads((events[-1] / "record.jsonl").read_text())

    def test_default_exit_codes_unchanged_and_check_is_reported(self):
        self.history(["x", "x"])
        code, report, err, calls = self.invoke(3)
        self.assertEqual((code, report["state"], calls), (0, "REVIEW_PASS", ["a", "b"]))
        self.assertEqual((report["premise_check"]["triggered"],
                          report["premise_check"]["defect_classes"]), (True, ["x"]))
        self.assertIn("PREMISE CHECK (advisory)", err)
        record = self.last_record()
        self.assertEqual((record["identity"], record["cycle"], record["round"]),
                         (self.identity, 1, 3))
        self.assertTrue(record["premise_check"]["triggered"])

    def test_default_failing_review_still_exits_one(self):
        self.history(["x", "x"])
        bad = {"file": "code.py", "line": 1, "severity": "major", "confidence": "high",
               "summary": "s", "failure_scenario": "input dropped"}
        code, report, _err, _calls = self.invoke(3, findings=[bad])
        self.assertEqual((code, report["state"]), (1, "REVIEW_FAIL"))

    def test_require_ack_refuses_before_reservation_and_reviewers(self):
        self.history(["x", "x"])
        before = self.markers()
        code, _report, err, calls = self.invoke(3, "--require-premise-ack")
        self.assertEqual((code, calls, self.markers()), (4, [], before))
        self.assertIn("premise acknowledgment required", err)
        self.assertIn("rounds 1 and 2", err)

    def test_require_ack_accepts_and_records_memo(self):
        self.history(["x", "x"])
        memo = self.root / "memo.md"
        memo.write_bytes("Premise: the cache is keyed wrongly.\r\né\n".encode("utf-8"))
        code, report, _err, calls = self.invoke(3, "--require-premise-ack",
                                                "--premise-memo", str(memo))
        self.assertEqual((code, calls), (0, ["a", "b"]))
        ack = {"mode": "premise_memo", "path": str(memo),
               "sha256": hashlib.sha256(memo.read_bytes()).hexdigest()}
        self.assertEqual((report["premise_ack"], self.last_record()["premise_ack"]), (ack, ack))

    def test_require_ack_accepts_and_records_continue_patching(self):
        self.history(["x", "x"])
        code, report, _err, _calls = self.invoke(3, "--require-premise-ack",
                                                 "--continue-patching", "one more guard")
        self.assertEqual(code, 0)
        ack = {"mode": "continue_patching", "reason": "one more guard"}
        self.assertEqual((report["premise_ack"], self.last_record()["premise_ack"]), (ack, ack))

    def test_require_ack_passes_without_trigger_and_on_round_two(self):
        self.history(["x", "y"])
        self.assertEqual(self.invoke(3, "--require-premise-ack")[0], 0)

    def test_round_two_never_refuses(self):
        self.history(["x"])
        self.assertEqual(self.invoke(2, "--require-premise-ack")[0], 0)

    def test_disposition_class_drives_the_gate(self):
        """Round 1 carries its class; the round-3 dispositions file classifies round 2."""
        self.history(["x", None])
        entry = {"location": "code.py:2", "summary": "cache key omits the tenant scope",
                 "disposition": "reproduced", "reason": "reproduced in a test",
                 "defect_class": "x", "reproduction": {
                     "setup": "s", "command": "c", "expected": "e", "observed": "o",
                     "revision": "f" * 40}}
        digest = review.finding_digest(entry["location"], entry["summary"])
        second = self.journal / sorted(os.listdir(str(self.journal)))[1] / "record.jsonl"
        data = json.loads(second.read_text())
        data["results"][0]["findings"][0]["finding_digest"] = digest
        second.write_text(json.dumps(data) + "\n")
        self.dispositions.write_text(json.dumps({digest: entry}))
        self.assertEqual(self.invoke(3, "--require-premise-ack")[0], 4)
        self.dispositions.write_text(json.dumps({digest: dict(entry, defect_class="y")}))
        self.assertEqual(self.invoke(3, "--require-premise-ack")[0], 0)

    def test_invalid_acknowledgments_refuse_before_reviewers(self):
        self.history(["x", "x"])
        empty = self.root / "empty.md"
        empty.write_text("  \n")
        bad = self.root / "bad.md"
        bad.write_bytes(b"\xff\xfe")
        for extra in (("--premise-memo", str(empty)), ("--premise-memo", str(bad)),
                      ("--premise-memo", str(self.root / "missing.md")),
                      ("--continue-patching", "two\nlines"), ("--continue-patching", " "),
                      ("--premise-memo", str(bad), "--continue-patching", "x")):
            with self.subTest(extra=extra):
                code, _report, _err, calls = self.invoke(3, "--require-premise-ack", *extra)
                self.assertEqual((code, calls), (4, []))
        self.dispositions.write_text(json.dumps({review.finding_digest("a:1", "s"): {
            "location": "a:1", "summary": "s", "disposition": "deferred", "reason": "r",
            "defect_class": "Stale"}}))
        code, _report, err, calls = self.invoke(3)
        self.assertEqual((code, calls), (4, []))
        self.assertIn("defect_class must be a lowercase slug", err)


if __name__ == "__main__":
    unittest.main()
