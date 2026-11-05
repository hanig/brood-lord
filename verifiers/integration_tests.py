#!/usr/bin/env python3
"""Run whole discovered modules in parallel from pinned bytes.

Accepted limit (2026-11-05): per-module isolation loses cross-module pollution
detection; required CI retains shared-process discovery on the merge ref.
Children share checkout/environment and the coordinator's process group and
overall deadline. Candidate tests are not an immutable corpus or OS boundary.
"""
import argparse
import ast
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time


BOOTSTRAP = r'''
import os, runpy, sys, unittest
mode, destination = sys.argv[1:3]
sys.path[0] = os.getcwd()
owner = None
original_init = unittest.TestProgram.__init__
original_run = unittest.TestProgram.runTests
def initialize(program, *args, **kwargs):
    global owner
    if owner is None:
        owner = program
    original_init(program, *args, **kwargs)
unittest.TestProgram.__init__ = initialize
if mode == 'discover':
    loader = unittest.defaultTestLoader
    original_path = loader._find_test_path
    original_import = loader._get_module_from_name
    groups = {}
    scopes = {}
    frames = []
    def ids(suite):
        if not unittest.suite._isnotsuite(suite):
            return [name for child in suite for name in ids(child)]
        name = suite.id()
        if isinstance(suite, unittest.loader._FailedTest) or name.startswith(
                ('unittest.loader._FailedTest.', 'unittest.loader.ModuleImportFailure.')):
            raise ValueError('discovery failure: ' + name)
        scopes[name] = (suite.__class__.__module__, unittest.util.strclass(suite.__class__))
        return [name]
    def imported(name):
        # Attribute the returned suite to its first discovery import.
        if frames and frames[-1] is None:
            frames[-1] = name
        return original_import(name)
    def capture_path(*args, **kwargs):
        frames.append(None)
        try:
            result = original_path(*args, **kwargs)
            if len(frames) == 1 and result[0] is not None:
                selected = ids(result[0])
                if selected:
                    if frames[-1] is None:
                        raise ValueError('selected suite has no discovery module')
                    groups.setdefault(frames[-1], []).extend(selected)
            return result
        finally:
            frames.pop()
    loader._get_module_from_name = imported
    loader._find_test_path = capture_path
    def enumerate_only(program):
        if program is not owner:
            return original_run(program)
        selected = ids(program.test)
        if loader.errors:
            raise ValueError('\n'.join(loader.errors))
        from collections import Counter
        if Counter(selected) != Counter(name for names in groups.values() for name in names):
            raise ValueError('discovery module accounting differs from selected suite')
        with open(destination, 'w') as stream:
            stream.write(ascii((groups, scopes)))
    unittest.TestProgram.runTests = enumerate_only
    sys.argv = ['unittest', 'discover', '-s', 'tests']
else:
    report = sys.argv[3]
    class ReportResult(unittest.TextTestResult):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.recorded, self.fixture_skips, self.active = [], [], {}
            def wrap(method):
                def record(test, *args, **kwargs):
                    if (method.__name__ == 'addSkip'
                            and isinstance(test, unittest.suite._ErrorHolder)):
                        self.fixture_skips.append(test.id())
                    subject = test.test_case if isinstance(test, unittest.case._SubTest) else test
                    if id(subject) in self.active:
                        self.active[id(subject)][1] = True
                    elif not isinstance(test, unittest.suite._ErrorHolder):
                        self.recorded.append(subject.id())
                    return method(test, *args, **kwargs)
                return record
            for name in ('addSuccess', 'addFailure', 'addError', 'addSkip',
                         'addExpectedFailure', 'addUnexpectedSuccess', 'addSubTest'):
                setattr(self, name, wrap(getattr(self, name)))
        def startTest(self, test):
            self.active[id(test)] = [test.id(), False]
            super().startTest(test)
        def stopTest(self, test):
            name, accounted = self.active.pop(id(test))
            if accounted:
                self.recorded.append(name)
            super().stopTest(test)
    # Capture only the CLI runner's stream, excluding nested runners and atexit.
    class ReportRunner(unittest.TextTestRunner):
        resultclass = ReportResult
        def run(self, suite):
            output, stream = [], self.stream
            class Tee:
                def write(self, text):
                    output.append(text)
                    return stream.write(text)
                def __getattr__(self, name):
                    return getattr(stream, name)
            self.stream = unittest.runner._WritelnDecorator(Tee())
            result = super().run(suite)
            with open(report, 'w') as target:
                target.write(ascii((''.join(output), result.recorded, result.fixture_skips)))
            return result
    def execute(program):
        if program is owner:
            program.testRunner = ReportRunner
        return original_run(program)
    unittest.TestProgram.runTests = execute
    top = os.path.abspath('tests')
    if top not in sys.path:
        sys.path.insert(0, top)
    load_name = unittest.defaultTestLoader.loadTestsFromName
    def load_module(name, module=None):
        try:
            return load_name(name, module)
        except unittest.SkipTest as exc:
            return unittest.loader._make_skipped_test(
                name, exc, unittest.defaultTestLoader.suiteClass)
    unittest.defaultTestLoader.loadTestsFromName = load_module
    sys.argv = ['unittest', '--', destination]
runpy.run_module('unittest', run_name='__main__', alter_sys=True)
'''


class Selection(dict):
    def __init__(self, groups, scopes):
        super().__init__(groups)
        self.scopes = scopes


def discover(root):
    with tempfile.TemporaryDirectory(prefix='integration-discovery-') as tmp:
        manifest = Path(tmp) / 'selected.txt'
        child = subprocess.run([sys.executable, '-B', '-c', BOOTSTRAP,
                                'discover', str(manifest)], cwd=root,
                               capture_output=True, text=True, errors='replace')
        if child.returncode:
            raise ValueError('discovery exited {}\n{}{}'.format(
                child.returncode, child.stdout, child.stderr))
        selected, scopes = ast.literal_eval(manifest.read_text())
        if (not isinstance(selected, dict) or any(
                not isinstance(module, str) or not module or not isinstance(names, list)
                or not names or any(not isinstance(name, str) or not name
                                   or name.startswith(('unittest.loader._FailedTest.',
                                                       'unittest.loader.ModuleImportFailure.'))
                                   for name in names)
                for module, names in selected.items())):
            raise ValueError('invalid discovery module manifest')
        return Selection(selected, scopes)


def worker_count(requested=None):
    affinity = getattr(os, 'sched_getaffinity', None)
    available = max(1, len(affinity(0)) if affinity else (os.cpu_count() or 1))
    return available if requested is None else max(1, min(available, requested))


def scheduled_modules(selected):
    return list(selected)


def run_module(root, module):
    with tempfile.TemporaryDirectory(prefix='integration-module-') as tmp:
        report = Path(tmp) / 'summary.txt'
        child = subprocess.run([sys.executable, '-B', '-c', BOOTSTRAP, 'module', module, str(report)],
                               cwd=root, capture_output=True, text=True, errors='replace')
        recorded = ast.literal_eval(report.read_text()) if report.exists() else ('', [], [])
        return child, recorded


def unaccounted(selected, module, recorded, fixture_skips):
    missing = Counter(selected.get(module, [])) - Counter(recorded)
    for name in list(missing):
        test_module, test_class = selected.scopes[name]
        if ('setUpClass ({})'.format(test_class) in fixture_skips
                or 'setUpModule ({})'.format(test_module) in fixture_skips):
            del missing[name]
    return missing


def run_suite(root, selected, workers=None):
    started = time.monotonic()
    completed, failures, total = [], 0, 0
    with ThreadPoolExecutor(max_workers=worker_count(workers)) as pool:
        futures = {pool.submit(run_module, root, name): name
                   for name in scheduled_modules(selected)}
        for future in as_completed(futures):
            name = futures[future]
            completed.append(name)
            try:
                child, (summary, recorded, fixture_skips) = future.result()
                output = child.stdout + '\n' + child.stderr
                print(name + ':\n' + output, file=sys.stderr)
                counts = re.findall(r'^Ran (\d+) tests? in .+$', summary, re.MULTILINE)
                count = int(counts[-1]) if counts else 0
                total += count
                missing = unaccounted(selected, name, recorded, fixture_skips)
                if missing:
                    print('ERROR: {}: unaccounted test ids: {!r}'.format(name, missing), file=sys.stderr)
                if child.returncode or missing:
                    failures += 1
            except Exception as exc:
                print('ERROR: {}: {}'.format(name, exc), file=sys.stderr)
                failures += 1
    if Counter(completed) != Counter(selected.keys()):
        print('ERROR: executed modules differ from discovery: expected {!r}, got {!r}'.format(
            list(selected), completed), file=sys.stderr)
        failures += 1
    print('Ran {} tests in {:.3f}s'.format(total, time.monotonic() - started), file=sys.stderr)
    print('FAILED (modules={})'.format(failures) if failures else 'OK', file=sys.stderr)
    return 1 if failures else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, add_help=False)
    parser.add_argument('--workers', type=int, help='lower the affinity-bounded worker count')
    args = parser.parse_args()
    try:
        selected = discover(os.getcwd())
        return run_suite(os.getcwd(), selected, args.workers)
    except (OSError, ValueError, SyntaxError) as exc:
        print('ERROR: discovery: {}\nRan 0 tests\nFAILED (discovery=1)'.format(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
