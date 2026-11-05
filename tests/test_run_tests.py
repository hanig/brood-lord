"""scripts/run_tests.py runs the same modules discovery does, isolates each
one, and fails the run when any module fails or runs nothing."""

import ast
import contextlib
import io
import importlib.util
import json
import os
import re
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("run_tests", ROOT / "scripts" / "run_tests.py")
RT = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(RT)


def write_tree(root, files):
    for name, body in files.items():
        path = Path(root) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(body))


PASSING = '''
    import unittest
    class T(unittest.TestCase):
        def test_ok(self):
            pass
        @unittest.skip("demo")
        def test_skipped(self):
            pass
    if __name__ == "__main__":
        unittest.main()
'''
FAILING = '''
    import unittest
    class T(unittest.TestCase):
        def test_bad(self):
            self.fail("boom")
'''
EMPTY = '''
    import unittest
'''
# Fails unless the runner gave it a fresh, private HOME/TMPDIR and no
# inherited variables.
ISOLATED = '''
    import os, tempfile, unittest
    class T(unittest.TestCase):
        def test_env(self):
            self.assertNotIn("RUN_TESTS_LEAK", os.environ)
            home = os.environ["HOME"]
            self.assertTrue(tempfile.gettempdir().startswith(os.path.dirname(home)))
            self.assertEqual(os.listdir(home), [])
            open(os.path.join(home, "marker"), "w").close()
'''


class TestModuleSet(unittest.TestCase):
    def test_matches_discovery_default_pattern(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_tree(tmp, {"test_a.py": "", "testb.py": "", "helper.py": "",
                             "test-c.py": "", "sub/test_d.py": ""})
            self.assertEqual(RT.discover_modules(tmp), ["test_a", "testb"])

    def test_tests_has_no_package_subdirectory_discovery_would_recurse_into(self):
        packages = [str(p.parent.relative_to(ROOT))
                    for p in (ROOT / "tests").glob("*/__init__.py")]
        self.assertEqual(packages, [], "discover would load test modules from "
                         "these packages, and run_tests.py would not")

    def test_discovery_load_agrees_on_a_sample_tree(self):
        names = set()
        loader = unittest.TestLoader()
        with tempfile.TemporaryDirectory() as tmp:
            write_tree(tmp, {"test_x.py": PASSING, "testy.py": PASSING,
                             "not_a_test.py": PASSING})
            sys.path.insert(0, tmp)
            try:
                suite = loader.discover(tmp, top_level_dir=tmp)
            finally:
                sys.path.remove(tmp)
                for name in ("test_x", "testy", "not_a_test"):
                    sys.modules.pop(name, None)

            def walk(s):
                for item in s:
                    if isinstance(item, unittest.TestSuite):
                        walk(item)
                    else:
                        names.add(type(item).__module__)
            walk(suite)
            self.assertEqual(sorted(names), RT.discover_modules(tmp))


class TestScheduling(unittest.TestCase):
    def test_slowest_first_and_unknown_modules_first(self):
        durations = {"test_fast": 1.0, "test_slow": 30.0, "test_mid": 5.0}
        self.assertEqual(
            RT.schedule(["test_fast", "test_new", "test_slow", "test_mid"], durations),
            ["test_new", "test_slow", "test_mid", "test_fast"])

    def test_bad_durations_file_means_no_durations(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "d.json"
            path.write_text("{not json")
            self.assertEqual(RT.load_durations(path), {})
            path.write_text(json.dumps({"test_a": 2, "test_b": "x"}))
            self.assertEqual(RT.load_durations(path), {"test_a": 2.0})


MANY = '''
    import unittest
    class T(unittest.TestCase):
        def test_a(self): __import__('time').sleep(0.6)
        def test_b(self): __import__('time').sleep(0.6)
        def test_c(self): __import__('time').sleep(0.1)
        def test_d(self): self.fail("chunk boom")
        def test_e(self): pass
'''


class TestSplit(unittest.TestCase):
    def test_chunks_cover_every_id_once(self):
        ids = ["t%d" % i for i in range(11)]
        parts = RT.chunk(ids, 4)
        self.assertEqual(len(parts), 4)
        self.assertEqual(sorted(sum(parts, [])), sorted(ids))
        self.assertEqual(RT.chunk(ids[:2], 6), [["t0"], ["t1"]])

    def test_split_modules_share_no_state_across_tests(self):
        for module in RT.SPLIT:
            path = ROOT / "tests" / (module + ".py")
            self.assertTrue(path.is_file(), module)
            tree = ast.parse(path.read_text())
            shared = sorted({n.name for n in ast.walk(tree)
                             if isinstance(n, ast.FunctionDef)
                             and n.name in ("setUpClass", "tearDownClass", "load_tests")})
            self.assertEqual(shared, [], "%s cannot be split by test id" % module)

    def test_unlistable_split_module_runs_whole_and_reports_its_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_tree(root / "tests", {"test_broken.py": "import no_such_module_x\n"})
            plan, removed = RT.plan_jobs(["test_broken"], {}, split={"test_broken": 3},
                                         root=root)
            self.assertEqual(removed, 0)
            self.assertEqual([(j[0], j[2]) for j in plan], [("test_broken", None)])
            result = RT.run_module("test_broken", root=root)
            self.assertFalse(result["ok"])
            self.assertIn("no_such_module_x", result["output"])


class TestFast(unittest.TestCase):
    def test_excluded_tests_are_left_out_and_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_tree(root / "tests", {"test_many.py": MANY, "test_pass.py": PASSING})
            slow = {"test_many.T.test_a", "test_many.T.test_b"}
            plan, removed = RT.plan_jobs(["test_many", "test_pass"], {}, split={},
                                         root=root, exclude=slow)
            self.assertEqual(removed, 2)
            by_label = {j[0]: j[2] for j in plan}
            self.assertEqual(by_label["test_pass"], None)
            self.assertEqual(sorted(by_label["test_many"]),
                             ["test_many.T.test_c", "test_many.T.test_d", "test_many.T.test_e"])
            every = {"test_many.T.test_" + c for c in "abcde"}
            plan, removed = RT.plan_jobs(["test_many"], {}, split={}, root=root,
                                         exclude=every)
            self.assertEqual((plan, removed), ([], 5))

    def test_only_selectable_slow_ids_are_recorded(self):
        timed = {"test_m.T.test_slow": 9.0, "test_m.T.test_quick": 0.2,
                 "tests.review_sandbox_worker.DelegatedReviewTests.runTest": 30.0}
        self.assertEqual(RT.slow_entries(timed, ["test_m"]), {"test_m.T.test_slow": 9.0})

    def test_parse_timings(self):
        out = ("Slowest test durations\n" + "-" * 70 + "\n"
               "52.507s    test_x (test_m.TestX.test_x)\n"
               "0.100s     test_y (test_m.TestX.test_y)\n\nRan 2 tests in 52.6s\n")
        self.assertEqual(RT.parse_timings(out),
                         {"test_m.TestX.test_x": 52.507, "test_m.TestX.test_y": 0.1})
        self.assertEqual(RT.parse_timings("Ran 2 tests in 0.1s\nOK\n"), {})


class TestParse(unittest.TestCase):
    def test_summaries(self):
        self.assertEqual(RT.parse_result("...\nRan 3 tests in 0.1s\n\nOK (skipped=2)\n"),
                         (3, 2, "OK (skipped=2)"))
        self.assertEqual(
            RT.parse_result("Ran 1 test in 0.0s\n\nFAILED (failures=1, errors=2)\n"),
            (1, 0, "FAILED (failures=1, errors=2)"))
        self.assertEqual(RT.parse_result("Traceback ...\nImportError: x\n"), (0, 0, ""))


class TestVerdict(unittest.TestCase):
    def test_zero_tests_with_exit_zero_is_a_failure(self):
        # Python 3.9 to 3.11 exit 0 for a module with no tests; 3.12 exits 5.
        self.assertFalse(RT.module_ok(0, 0))
        self.assertFalse(RT.module_ok(1, 4))
        self.assertFalse(RT.module_ok(None, 4))
        self.assertTrue(RT.module_ok(0, 4))


class TestRunModule(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        write_tree(self.root / "tests", {"test_pass.py": PASSING, "test_fail.py": FAILING,
                                         "test_empty.py": EMPTY, "test_iso.py": ISOLATED})

    def test_outcomes(self):
        ok = RT.run_module("test_pass", root=self.root)
        self.assertTrue(ok["ok"], ok["output"])
        self.assertEqual((ok["ran"], ok["skipped"]), (2, 1))
        bad = RT.run_module("test_fail", root=self.root)
        self.assertFalse(bad["ok"])
        self.assertIn("boom", bad["output"])
        empty = RT.run_module("test_empty", root=self.root)
        self.assertFalse(empty["ok"], "a module that runs no tests must fail")

    def test_each_run_gets_a_fresh_private_environment(self):
        os.environ["RUN_TESTS_LEAK"] = "1"
        self.addCleanup(os.environ.pop, "RUN_TESTS_LEAK", None)
        for _ in range(2):  # the marker from the first run must not survive
            result = RT.run_module("test_iso", root=self.root)
            self.assertTrue(result["ok"], result["output"])

    def test_timeout_fails_the_module(self):
        write_tree(self.root / "tests", {"test_slow.py": '''
            import time, unittest
            class T(unittest.TestCase):
                def test_sleep(self):
                    time.sleep(30)
        '''})
        result = RT.run_module("test_slow", timeout=1, root=self.root)
        self.assertFalse(result["ok"])
        self.assertIn("timed out", result["output"])


class TestMain(unittest.TestCase):
    def run_main(self, root, argv, split=None):
        saved = (RT.ROOT, RT.TESTS, RT.DURATIONS, RT.SPLIT, RT.SLOW, RT.SLOW_SECONDS)
        RT.SPLIT = split or {}
        RT.SLOW = root / "tests" / "slow_tests.json"
        RT.SLOW_SECONDS = 0.5
        RT.ROOT, RT.TESTS = root, root / "tests"
        RT.DURATIONS = RT.TESTS / "durations.json"
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                code = RT.main(argv)
        finally:
            RT.ROOT, RT.TESTS, RT.DURATIONS, RT.SPLIT, RT.SLOW, RT.SLOW_SECONDS = saved
        return code, out.getvalue()

    def test_exit_status_and_durations(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_tree(root / "tests", {"test_pass.py": PASSING, "test_fail.py": FAILING})
            code, out = self.run_main(root, ["-j", "2", "test_pass"])
            self.assertEqual(code, 0, out)
            code, out = self.run_main(root, ["-j", "2", "--update-durations"])
            self.assertEqual(code, 1, out)
            self.assertIn("FAILED (1 modules: test_fail)", out)
            self.assertIn("boom", out)
            recorded = json.loads((root / "tests" / "durations.json").read_text())
            self.assertEqual(sorted(recorded), ["test_fail", "test_pass"])

    def test_split_module_runs_every_test_and_fails_with_its_chunk(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_tree(root / "tests", {"test_many.py": MANY})
            code, out = self.run_main(root, ["-j", "2", "--update-durations"],
                                      split={"test_many": 2})
            self.assertEqual(code, 1, out)
            self.assertIn("test_many[1/2]", out)
            self.assertIn("test_many[2/2]", out)
            self.assertIn("Ran 5 tests in 1 modules (2 processes)", out)
            self.assertIn("FAILED (1 modules: test_many)", out)
            self.assertIn("chunk boom", out)
            recorded = json.loads((root / "tests" / "durations.json").read_text())
            self.assertEqual(list(recorded), ["test_many"])
            chunks = [float(m) for m in re.findall(r"\s([\d.]+)s  test_many\[", out)]
            self.assertEqual(len(chunks), 2, out)
            self.assertAlmostEqual(recorded["test_many"], sum(chunks), delta=0.15)
            code, out = self.run_main(root, ["--no-split"], split={"test_many": 2})
            self.assertIn("Ran 5 tests in 1 modules (1 processes)", out)

    def test_fast_runs_everything_but_the_recorded_slow_tests(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_tree(root / "tests", {"test_many.py": MANY})
            (root / "tests" / "slow_tests.json").write_text(
                json.dumps({"test_many.T.test_d": 9.0}))
            code, out = self.run_main(root, ["--fast"])
            self.assertEqual(code, 0, out)  # test_d, the failing one, was left out
            self.assertIn("Ran 4 tests", out)
            self.assertIn("[--fast: 1 slow tests not run]", out)
            with self.assertRaises(SystemExit):
                with contextlib.redirect_stderr(io.StringIO()):
                    self.run_main(root, ["--fast", "--update-durations"])

    @unittest.skipIf(sys.version_info < (3, 12), "unittest --durations needs 3.12")
    def test_update_durations_records_slow_tests(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_tree(root / "tests", {"test_many.py": MANY})
            (root / "tests" / "slow_tests.json").write_text(
                json.dumps({"test_many.T.test_gone": 9.0, "test_other.T.test_x": 7.0}))
            self.run_main(root, ["--update-durations"])
            slow = json.loads((root / "tests" / "slow_tests.json").read_text())
            self.assertEqual(sorted(slow), ["test_many.T.test_a", "test_many.T.test_b",
                                            "test_other.T.test_x"])

    def test_unknown_module_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_tree(root / "tests", {"test_pass.py": PASSING})
            with self.assertRaises(SystemExit):
                with contextlib.redirect_stderr(io.StringIO()):
                    self.run_main(root, ["test_nope"])


if __name__ == "__main__":
    unittest.main()
