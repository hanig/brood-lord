#!/usr/bin/env python3
"""Run the candidate suite as whole modules using only these pinned bytes.

Tests and implementation are candidate bytes: this is integration validation,
not an immutable test-corpus guarantee or an OS security boundary. Modules
share the checkout and environment. Per-module processes lose detection of
cross-module pollution; accepted 2026-11-05 because required CI retains serial
shared-process discovery on the merge ref. The coordinator's process-group
kill and overall deadline cover discovery and every module child.
"""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time


# Both modes invoke unittest's CLI. Discovery replaces only execution with
# enumeration. Capturing by the imported module (not test.id's prefix) also
# handles imported TestCases and load_tests returning tests defined elsewhere.
BOOTSTRAP = r'''
import json, os, runpy, sys, unittest
mode, destination = sys.argv[1:]
# -m starts with the absolute cwd, whereas -c starts with the empty string.
sys.path[0] = os.getcwd()
if mode == 'discover':
    loader = unittest.defaultTestLoader
    original = loader.loadTestsFromModule
    groups = {}
    depth = 0
    def ids(suite):
        if isinstance(suite, unittest.TestSuite):
            return [name for child in suite for name in ids(child)]
        name = suite.id()
        if isinstance(suite, unittest.loader._FailedTest) or 'ModuleImportFailure' in name:
            raise ValueError('discovery failure: ' + name)
        return [name]
    def capture(module, *args, **kwargs):
        global depth
        depth += 1
        try:
            suite = original(module, *args, **kwargs)
            if depth == 1:
                selected = ids(suite)
                if selected:
                    groups.setdefault(module.__name__, []).extend(selected)
            return suite
        finally:
            depth -= 1
    loader.loadTestsFromModule = capture
    def enumerate_only(program):
        selected = ids(program.test)
        if loader.errors:
            raise ValueError('\n'.join(loader.errors))
        from collections import Counter
        if Counter(selected) != Counter(name for names in groups.values() for name in names):
            raise ValueError('discovery module accounting differs from selected suite')
        with open(destination, 'w') as stream:
            json.dump(groups, stream)
    unittest.TestProgram.runTests = enumerate_only
    sys.argv = ['unittest', 'discover', '-s', 'tests']
else:
    # discover('tests') defaults top_level_dir to the start directory and
    # inserts it ahead of cwd, unless already present. Do the identical step.
    top = os.path.abspath('tests')
    if top not in sys.path:
        sys.path.insert(0, top)
    sys.argv = ['unittest', destination]
runpy.run_module('unittest', run_name='__main__', alter_sys=True)
'''


def discover(root):
    """Collect in one disposable child; a missing manifest is never success."""
    with tempfile.TemporaryDirectory(prefix='integration-discovery-') as tmp:
        manifest = Path(tmp) / 'selected.json'
        child = subprocess.run([sys.executable, '-B', '-c', BOOTSTRAP,
                                'discover', str(manifest)], cwd=root,
                               capture_output=True, text=True, errors='replace')
        if child.returncode:
            raise ValueError('discovery exited {}\n{}{}'.format(
                child.returncode, child.stdout, child.stderr))
        selected = json.loads(manifest.read_text())
        if (not isinstance(selected, dict) or any(
                not isinstance(module, str) or not module or not isinstance(names, list)
                or not names or any(not isinstance(name, str) or not name
                                   or '_FailedTest' in name or 'ModuleImportFailure' in name
                                   for name in names)
                for module, names in selected.items())):
            raise ValueError('invalid discovery module manifest')
        return selected


def worker_count(requested=None):
    affinity = getattr(os, 'sched_getaffinity', None)
    available = max(1, len(affinity(0)) if affinity else (os.cpu_count() or 1))
    return available if requested is None else max(1, min(available, requested))


def scheduled_modules(selected):
    return list(selected)


def run_module(root, module):
    return subprocess.run([sys.executable, '-B', '-c', BOOTSTRAP, 'module', module],
                          cwd=root, capture_output=True, text=True, errors='replace')


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
                child = future.result()
                output = child.stdout + '\n' + child.stderr
                print(name + ':\n' + output, file=sys.stderr)
                counts = re.findall(r'^Ran (\d+) tests? in .+$', output, re.MULTILINE)
                count = int(counts[-1]) if counts else 0
                total += count
                if child.returncode or not counts or count < len(selected.get(name, [])):
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workers', type=int, help='lower the affinity-bounded worker count')
    args = parser.parse_args()
    try:
        selected = discover(os.getcwd())
        return run_suite(os.getcwd(), selected, args.workers)
    except (OSError, ValueError) as exc:
        print('ERROR: discovery: {}\nRan 0 tests\nFAILED (discovery=1)'.format(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
