"""Whole-module verifier fixtures; candidate code runs only in temporary trees."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
VERIFIER = ROOT / 'verifiers' / 'integration_tests.py'
spec = importlib.util.spec_from_file_location('parallel_verifier', VERIFIER)
P = importlib.util.module_from_spec(spec)
spec.loader.exec_module(P)
PASS = 'import unittest\nclass T(unittest.TestCase):\n    def test_ok(self): pass\n'


class TestParallelVerifier(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / 'tests').mkdir()

    def write(self, name, source):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)

    def run_verifier(self, *args):
        return subprocess.run([sys.executable, str(VERIFIER)] + list(args),
                              cwd=self.root, capture_output=True, text=True, timeout=30)

    def test_failure_kinds_and_candidate_cannot_steer(self):
        self.write('scripts/run_tests.py', "from pathlib import Path\nPath('steered').touch()\n")
        self.write('tests/test_a.py', PASS)
        # Exhaustive enumeration of four specified failure kinds.
        for body in (PASS.replace('pass', 'self.fail("intentional")'),
                     PASS.replace('pass', 'raise ValueError("intentional")'),
                     'import absent_r2_fixture_module\n',
                     PASS + '\ndef load_tests(loader, suite, pattern):\n'
                     '    if pattern is None: __import__("sys").exit(0)\n'
                     '    return suite\n'):
            with self.subTest(body=body):
                self.write('tests/test_b.py', body)
                result = self.run_verifier()
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertFalse((self.root / 'steered').exists())
                self.assertRegex(result.stderr, r'(?m)^Ran .+\nFAILED \(')

    def test_cli_cannot_report_success_without_executing_modules(self):
        self.write('tests/test_fail.py', PASS.replace('pass', 'self.fail("must execute")'))
        for option in ('--discover-only', '--help'):
            with self.subTest(option=option):
                result = self.run_verifier(option)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_discovery_exit_zero_before_manifest_is_failure(self):
        self.write('tests/test_a.py', 'raise SystemExit(0)\n')
        self.assertNotEqual(self.run_verifier().returncode, 0)

    def test_discovery_fidelity_and_empty_helper(self):
        self.write('tests/test_empty.py', '')
        self.write('tests/helper.py', PASS)
        self.write('tests/test_import.py', 'from helper import T\n')
        self.write('tests/pkg/__init__.py', '')
        self.write('tests/pkg/test_nested.py', PASS)
        self.write('tests/test_hook.py', PASS + '\ndef load_tests(loader, suite, pattern):\n'
                   '    return unittest.TestSuite([T("test_ok"), T("test_ok")])\n')
        self.write('tests/test_special.py', 'import test_empty, unittest\n'
                   'class T(unittest.TestCase):\n'
                   '    @unittest.skip("fixture")\n    def test_skip(self): pass\n'
                   '    @unittest.expectedFailure\n    def test_expected(self): self.fail()\n')
        selection = P.discover(self.root)
        self.assertEqual(selection, {
            'pkg.test_nested': ['pkg.test_nested.T.test_ok'],
            'test_hook': ['test_hook.T.test_ok', 'test_hook.T.test_ok'],
            'test_import': ['helper.T.test_ok'],
            'test_special': ['test_special.T.test_expected', 'test_special.T.test_skip']})
        serial = subprocess.run([sys.executable, '-B', '-m', 'unittest', 'discover', '-s',
                                 'tests', '-v'], cwd=self.root, capture_output=True, text=True)
        self.assertEqual(serial.returncode, 0, serial.stderr)
        self.assertIn('Ran 6 tests', serial.stderr)
        result = self.run_verifier()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Ran 6 tests', result.stderr)
        self.assertNotIn('test_empty:', result.stderr)

    def test_import_time_suite_loading_is_not_an_extra_discovered_module(self):
        self.write('tests/helper.py', PASS)
        # Exhaustive two cases: discard an internally loaded suite, or return
        # it from the module hook. Only discovery's returned suite is selected.
        for hook in (False, True):
            with self.subTest(hook=hook):
                source = ('import unittest, helper\n'
                          'cached = unittest.defaultTestLoader.loadTestsFromModule(helper)\n')
                source += ('def load_tests(loader, tests, pattern): return cached\n' if hook else PASS)
                self.write('tests/test_cached.py', source)
                selected = P.discover(self.root)
                self.assertEqual(selected, {'test_cached': [
                    'helper.T.test_ok' if hook else 'test_cached.T.test_ok']})
                result = self.run_verifier()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('Ran 1 test', result.stderr)

    def test_whole_module_can_select_more_tests(self):
        self.write('tests/test_hook.py', PASS + '\ndef load_tests(loader, suite, pattern):\n'
                   '    return unittest.TestSuite([T("test_ok")] * (1 if pattern else 2))\n')
        result = self.run_verifier()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Ran 2 tests', result.stderr)

    def test_smaller_module_summary_fails(self):
        self.write('tests/test_hook.py', PASS + '\ndef load_tests(loader, suite, pattern):\n'
                   '    return unittest.TestSuite([T("test_ok")] * (2 if pattern else 1))\n')
        result = self.run_verifier()
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertIn('Ran 1 test', result.stderr)

    def test_cli_import_path_matches_discovery(self):
        self.write('tests/test_path.py', 'import os, sys, unittest\n'
                   'assert sys.path[:2] == [os.path.join(os.getcwd(), "tests"), os.getcwd()]\n'
                   'assert os.environ["R2_ENV_WITNESS"] == "unchanged"\n' + PASS)
        with mock.patch.dict(os.environ, {'R2_ENV_WITNESS': 'unchanged'}):
            self.assertEqual(self.run_verifier().returncode, 0)

    def test_coverage_by_identity(self):
        self.write('tests/test_a.py', PASS)
        self.write('tests/test_b.py', PASS)
        selected = P.discover(self.root)
        with mock.patch.object(P, 'scheduled_modules', return_value=['test_a', 'test_a']), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertNotEqual(P.run_suite(self.root, selected), 0)

    def test_affinity_bounds_pool(self):
        self.write('tests/test_a.py', PASS)
        selected = P.discover(self.root)
        # Exhaustive finite product: two affinities and three requested caps.
        for cpus in (1, 8):
            for requested in (None, 2, 999):
                with self.subTest(cpus=cpus, requested=requested), \
                        mock.patch.object(P.os, 'sched_getaffinity', return_value=set(range(cpus)), create=True), \
                        mock.patch.object(P.os, 'cpu_count', return_value=208), \
                        mock.patch.object(P, 'ThreadPoolExecutor', wraps=P.ThreadPoolExecutor) as pool, \
                        contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(P.run_suite(self.root, selected, requested), 0)
                    self.assertEqual(pool.call_args.kwargs['max_workers'], min(cpus, requested or cpus))
        with mock.patch.object(P.os, 'sched_getaffinity', None, create=True):
            for count in (None, 0, 3):
                with mock.patch.object(P.os, 'cpu_count', return_value=count):
                    self.assertEqual(P.worker_count(), max(1, count or 1))

    def test_accepted_pollution_limit(self):
        self.write('tests/test_a.py', 'import os\nos.environ["R2_POLLUTION"]="yes"\n' + PASS)
        self.write('tests/test_b.py', PASS.replace('pass',
                   'self.assertNotIn("R2_POLLUTION", __import__("os").environ)'))
        serial = subprocess.run([sys.executable, '-B', '-m', 'unittest', 'discover', '-s', 'tests'],
                                cwd=self.root, capture_output=True, text=True)
        parallel = self.run_verifier()
        self.assertNotEqual(serial.returncode, 0, serial.stderr)
        self.assertEqual(parallel.returncode, 0, parallel.stderr)

    def test_real_verify_timeout_kills_module_child(self):
        sys.path.insert(0, str(ROOT / 'skills/hanig-swarm/scripts'))
        import verify as V
        pid_path = self.root / 'child.pid'
        self.write('tests/test_sleep.py', PASS.replace('pass',
                   '(__import__("pathlib").Path("child.pid").write_text(str(__import__("os").getpid())), '
                   '__import__("time").sleep(60))'))
        result, error = V.run_pinned(None, str(VERIFIER), V.digest_file(VERIFIER)[0],
                                    timeout=2, cwd=str(self.root), observe_completion=True,
                                    executables={'python': {'path': sys.executable},
                                                 'git': {'path': '/usr/bin/git'}})
        self.assertIsNone(error)
        self.assertIn('timed out', result['incomplete_reason'])
        self.assertTrue(pid_path.exists(), result)
        pid = int(pid_path.read_text())
        for _ in range(100):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.02)
        else:
            self.fail('module child survived verifier deadline: ' + str(pid))


if __name__ == '__main__':
    unittest.main()
