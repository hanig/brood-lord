import contextlib
import importlib.util
import io
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
HOOK = '\ndef load_tests(loader, suite, pattern):\n'


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

    def serial(self, *args):
        return subprocess.run([sys.executable, '-B', '-m', 'unittest', 'discover', '-s', 'tests'] + list(args),
                              cwd=self.root, capture_output=True, text=True)

    def passing_run(self):
        result = self.run_verifier()
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def hook(self, count):
        self.write('tests/test_hook.py', PASS + HOOK +
                   '    return unittest.TestSuite([T("test_ok")] * (' + count + '))\n')

    def test_failure_kinds_and_candidate_cannot_steer(self):
        self.write('scripts/run_tests.py', "from pathlib import Path\nPath('steered').touch()\n")
        self.write('tests/test_a.py', PASS)
        # Exhaustive failure kinds (4) and reserved ids (2).
        for body in (PASS.replace('pass', 'self.fail("intentional")'),
                     PASS.replace('pass', 'raise ValueError("intentional")'),
                     'import absent_r2_fixture_module\n',
                     PASS + HOOK +
                     '    if pattern is None: __import__("sys").exit(0)\n'
                     '    return suite\n',
                     PASS + 'T.id = lambda self: \"unittest.loader.ModuleImportFailure.broken\"\n',
                     PASS + 'T.id = lambda self: \"unittest.loader._FailedTest.broken\"\n'):
            with self.subTest(body=body):
                self.write('tests/test_b.py', body)
                result = self.run_verifier()
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertFalse((self.root / 'steered').exists())
                self.assertRegex(result.stderr, r'(?m)^Ran .+\nFAILED \(')

    def test_cli_requires_execution(self):
        self.write('tests/test_fail.py', PASS.replace('pass', 'self.fail("must execute")'))
        for option in ('--discover-only', '--help'):
            with self.subTest(option=option):
                result = self.run_verifier(option)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_discovery_requires_manifest(self):
        self.write('tests/test_a.py', 'raise SystemExit(0)\n')
        self.assertNotEqual(self.run_verifier().returncode, 0)

    def test_discovery_fidelity_and_empty_helper(self):
        self.write('tests/test_empty.py', '')
        self.write('tests/helper.py', PASS)
        self.write('tests/test_import.py', 'from helper import T\n')
        self.write('tests/pkg/__init__.py', '')
        self.write('tests/pkg/test_nested.py', PASS)
        self.hook('2')
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
        serial = self.serial('-v')
        self.assertEqual(serial.returncode, 0, serial.stderr)
        self.assertIn('Ran 6 tests', serial.stderr)
        result = self.passing_run()
        self.assertIn('Ran 6 tests', result.stderr)
        self.assertNotIn('test_empty:', result.stderr)

    def test_cached_suite_ownership(self):
        self.write('tests/helper.py', PASS)
        # Exhaustive cache hook: off/on.
        for hook in (False, True):
            with self.subTest(hook=hook):
                source = ('import unittest, helper\n'
                          'cached = unittest.defaultTestLoader.loadTestsFromModule(helper)\n')
                source += ('def load_tests(loader, tests, pattern): return cached\n' if hook else PASS)
                self.write('tests/test_cached.py', source)
                selected = P.discover(self.root)
                self.assertEqual(selected, {'test_cached': [
                    'helper.T.test_ok' if hook else 'test_cached.T.test_ok']})
                result = self.passing_run()
                self.assertIn('Ran 1 test', result.stderr)

    def test_package_hook_owns_nested_discovery_suite(self):
        self.write('tests/pkg/__init__.py',
                   'from pathlib import Path\n'
                   'def load_tests(loader, suite, pattern):\n'
                   '    here = Path(__file__).parent\n'
                   '    return loader.discover(str(here), pattern=pattern or "test*.py",\n'
                   '                           top_level_dir=str(here.parent))\n')
        self.write('tests/pkg/test_child.py', PASS)
        self.assertEqual(P.discover(self.root), {'pkg': ['pkg.test_child.T.test_ok']})
        result = self.passing_run()
        self.assertIn('Ran 1 test', result.stderr)

    def test_literal_module_names_and_import_skips(self):
        # Exhaustive CLI forms: package, dash, import skip.
        for name, source in (
                ('discover', PASS + '\ndef load_tests(loader, tests, pattern): return tests\n'),
                ('-package', PASS),
                ('test_skip', 'import unittest\nraise unittest.SkipTest("fixture")\n')):
            with self.subTest(name=name):
                self.write('tests/' + (name + '.py' if name == 'test_skip' else name + '/__init__.py'), source)
                serial = self.serial()
                self.assertEqual(serial.returncode, 0, serial.stderr)
                self.assertIn(name, P.discover(self.root))
                self.passing_run()

    def test_valid_failure_marker_names(self):
        for name in ('TestModuleImportFailure', 'Test_FailedTest'):
            with self.subTest(name=name):
                self.write('tests/test_names.py', PASS.replace('class T', 'class ' + name))
                self.passing_run()

    def test_review_counterexamples(self):
        # Exhaustive five distinct round-1 counterexamples.
        cases = (
            ('base', PASS + HOOK +
             '    return unittest.BaseTestSuite([T("test_ok")])\n', 0),
            ('import_name', PASS.replace('class T', 'class ModuleImportFailure'), 0),
            ('failed_name', PASS.replace('class T', 'class _FailedTest'), 0),
            ('nested', PASS.replace('pass', 'self.fail("selected")') +
             'def load_tests(loader, suite, pattern):\n'
             '    if pattern is None:\n'
             '        unittest.TextTestRunner().run(unittest.TestSuite([unittest.FunctionTestCase(lambda: None)]))\n'
             '        raise SystemExit(0)\n'
             '    return suite\n', 1),
            ('shutdown', 'import atexit, sys\n'
             'atexit.register(lambda: print("Ran 0 tests in 0.000s", file=sys.stderr))\n' + PASS, 0))
        for name, source, expected in cases:
            with self.subTest(name=name):
                self.write('tests/test_case.py', source)
                serial = self.serial()
                self.assertEqual(serial.returncode, expected, serial.stderr)
                result = self.run_verifier()
                self.assertEqual(result.returncode, expected, result.stderr)

    def test_whole_module_can_select_more_tests(self):
        self.hook('1 if pattern else 2')
        result = self.passing_run()
        self.assertIn('Ran 2 tests', result.stderr)

    def test_bootstrap_dependencies_and_program_ownership(self):
        local_json = 'import json\n' + PASS.replace('pass', 'self.assertEqual(json.VALUE, 1)')
        nested = "unittest.main(module=helper, argv=['nested'], exit=False)"
        # Exhaustive three serialization and two nested-program cases.
        cases = (
            ('root_manifest', 'json.py', "def dump(obj, stream): stream.write('{}')\n",
             PASS.replace('pass', 'self.fail("selected")'), 1),
            ('root_helper', 'json.py', 'VALUE = 1\n', local_json, 0),
            ('tests_helper', 'tests/json.py', 'VALUE = 1\n', local_json, 0),
            ('nested_exit', 'tests/helper.py', PASS,
             'import helper\n' + PASS.replace('pass', 'self.fail("selected")') + HOOK +
             '    if pattern is None:\n        ' + nested + '\n        raise SystemExit(0)\n'
             '    return suite\n', 1),
            ('nested_discovery', 'tests/helper.py', PASS,
             'import helper\n' + PASS + HOOK + '    ' + nested + '\n    return suite\n', 0))
        for name, path, helper, source, expected in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp, \
                    mock.patch.object(self, 'root', Path(tmp)), mock.patch.dict(os.environ):
                os.environ.pop('PYTHONPATH', None)
                self.write(path, helper)
                self.write('tests/test_case.py', source)
                serial = self.serial()
                self.assertEqual(serial.returncode, expected, serial.stderr)
                result = self.run_verifier()
                self.assertEqual(result.returncode, expected, result.stderr)

    def test_smaller_module_summary_fails(self):
        self.hook('2 if pattern else 1')
        result = self.run_verifier()
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertIn('Ran 1 test', result.stderr)

    def test_fixture_skips_account_for_selected_tests(self):
        skipped = ('import unittest\nclass T(unittest.TestCase):\n'
                   '    @classmethod\n    def setUpClass(cls): raise unittest.SkipTest("fixture")\n'
                   '    def test_one(self): self.fail("skipped")\n'
                   '    def test_two(self): self.fail("skipped")\n')
        # Exhaustive five skip forms and a custom test recording its own result.
        cases = (
            ('class', skipped, 0),
            ('module', 'def setUpModule(): raise unittest.SkipTest("fixture")\n' + PASS, 0),
            ('mixed', skipped + PASS.replace('class T', 'class Other'), 1),
            ('decorator', PASS.replace('class T', '@unittest.skip("fixture")\nclass T'), 1),
            ('nested_class', skipped + 'T.__qualname__ = "Outer.T"\n', 0),
            ('custom_result', PASS +
             '    def run(self, result):\n        result.testsRun += 1\n        result.addSuccess(self)\n', 1))
        for name, source, count in cases:
            with self.subTest(name=name):
                self.write('tests/test_case.py', source)
                serial = self.serial()
                self.assertEqual(serial.returncode, 0, serial.stderr)
                result = self.passing_run()
                self.assertIn('Ran {} test'.format(count), result.stderr)

    def test_accounting_uses_ids_and_fixture_scope(self):
        skipped = ('class T(unittest.TestCase):\n'
                   '    @classmethod\n    def setUpClass(cls): raise unittest.SkipTest("fixture")\n'
                   '    def test_one(self): pass\n')
        # Exhaustive omissions: replacement id, neighboring class, neighboring module.
        cases = (
            PASS + HOOK + '    T.test_other = T.test_ok\n'
            '    return unittest.TestSuite([T("test_ok" if pattern else "test_other")])\n',
            'import unittest\n' + skipped + PASS.replace('class T', 'class TExtra') + HOOK +
            '    return suite if pattern else loader.loadTestsFromTestCase(T)\n',
            'import unittest, helper, helper_extra\n' + HOOK +
            '    return unittest.TestSuite([loader.loadTestsFromModule(helper)] +\n'
            '        ([loader.loadTestsFromModule(helper_extra)] if pattern else []))\n')
        self.write('tests/helper.py', 'def setUpModule(): raise unittest.SkipTest("fixture")\n' + PASS)
        self.write('tests/helper_extra.py', PASS)
        for source in cases:
            with self.subTest(source=source):
                self.write('tests/test_case.py', source)
                result = self.run_verifier()
                self.assertNotEqual(result.returncode, 0, result.stderr)
        # A skip of a package module must not cover its omitted child module.
        (self.root / 'tests/helper.py').unlink()
        self.write('tests/helper/__init__.py',
                   'def setUpModule(): raise unittest.SkipTest("fixture")\n' + PASS)
        self.write('tests/helper/child.py', PASS)
        self.write('tests/test_case.py', cases[-1].replace('helper_extra', 'helper.child'))
        result = self.run_verifier()
        self.assertNotEqual(result.returncode, 0, result.stderr)

    def test_subtest_dispositions_and_fixture_errors(self):
        # Exhaustive subtest outcomes: pass, skip, failure, error; both fixture errors.
        for action, expected in (('pass', 0), ('self.skipTest("fixture")', 0),
                                 ('self.fail("fixture")', 1), ('raise ValueError("fixture")', 1)):
            with self.subTest(action=action):
                self.write('tests/test_case.py', PASS.replace('pass',
                           '\n        for value in range(2):\n'
                           '            with self.subTest(value=value): ' + action))
                result = self.run_verifier()
                self.assertEqual(result.returncode, expected, result.stderr)
        for fixture in ('setUpModule', 'setUpClass'):
            with self.subTest(fixture=fixture):
                source = ('def setUpModule(): raise ValueError("fixture")\n' + PASS
                          if fixture == 'setUpModule' else PASS +
                          '    @classmethod\n    def setUpClass(cls): raise ValueError("fixture")\n')
                self.write('tests/test_case.py', source)
                result = self.run_verifier()
                self.assertNotEqual(result.returncode, 0, result.stderr)

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
        serial = self.serial()
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
