#!/usr/bin/env python3
"""Run the unittest suite in parallel, one process per test module.

    python3 scripts/run_tests.py                 # whole suite, one worker per usable CPU
    python3 scripts/run_tests.py -j 2            # two workers
    python3 scripts/run_tests.py test_outbox test_contract
    python3 scripts/run_tests.py --update-durations
    python3 scripts/run_tests.py --no-split      # never split a module
    python3 scripts/run_tests.py --fast          # leave out tests/slow_tests.json

The module set is what `python3 -m unittest discover -s tests` loads: every
top-level `tests/test*.py` whose name is a valid module name, discovery's
default pattern. Discovery would also recurse into a subdirectory with an
`__init__.py`; tests/ has none, and tests/test_run_tests.py keeps it that way. Each module runs
as `python3 -B -m unittest <module>` from the repository root, with the same
import path discovery gives it (the root, for `from tests import ...`, and
tests/, for `import scheduler_fixture`), in a clean environment like release
validation's: PATH is kept, and HOME, XDG_CONFIG_HOME and TMPDIR point into a
fresh temporary directory per module, so modules running at the same time
share no home, config or temp state.

A module listed in SPLIT runs as several processes instead of one, each
running an interleaved share of its test ids
(`python3 -B -m unittest mod.Class.test_a mod.Class.test_b ...`), so one slow
module no longer sets the floor for the whole run. Its module fixtures
(setUpModule) run once per process. Only modules whose tests share nothing
across tests may be listed; tests/test_run_tests.py refuses a listed module
that defines setUpClass or load_tests. If a split module's tests cannot be
listed, it runs whole, so an import error is still reported.

--fast leaves out the tests listed in tests/slow_tests.json (each took at
least SLOW_SECONDS when last measured) and runs everything else; modules whose
tests are all slow are skipped. It is for an agent's inner loop. Only the full
run, without --fast, is evidence that the suite passes.
--update-durations records per-test times with unittest's --durations, which
needs Python 3.12 or later; on older Pythons it updates module times only.

Workers take the slowest modules and chunks first, using tests/durations.json (seconds
of wall time per module, written by --update-durations). A module missing from
that file is scheduled as if it were the slowest, so a new module never starts
last. The run fails if any module fails, errors, times out or runs no tests.
Standard library only; Python 3.9 compatible.
"""

import argparse
import concurrent.futures
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
DURATIONS = TESTS / "durations.json"
SLOW = TESTS / "slow_tests.json"
SLOW_SECONDS = 5.0
# Module -> number of processes. Keep each chunk near 100 s on a 4-vCPU host.
SPLIT = {"test_remote_verification": 6, "test_project": 3}
LIST_TESTS = (
    "import sys, unittest\n"
    "def walk(s):\n"
    "    for t in s:\n"
    "        if isinstance(t, unittest.TestSuite):\n"
    "            yield from walk(t)\n"
    "        else:\n"
    "            yield t\n"
    "tests = list(walk(unittest.defaultTestLoader.loadTestsFromName(sys.argv[1])))\n"
    "if any(type(t).__name__ in ('_FailedTest', 'ModuleImportFailure') for t in tests):\n"
    "    sys.exit(3)\n"
    "print('\\n'.join(t.id() for t in tests))\n")
TIMED = re.compile(r"^\s*([\d.]+)s\s+\S+\s+\(([\w.]+)\)\s*$", re.M)
RAN = re.compile(r"^Ran (\d+) tests? in ", re.M)
SKIPPED = re.compile(r"skipped=(\d+)")
STATUS = re.compile(r"^(?:OK|FAILED|NO TESTS RAN)\b.*$", re.M)


def discover_modules(tests_dir=None):
    """Module names discovery would load: top-level tests/test*.py."""
    tests_dir = TESTS if tests_dir is None else Path(tests_dir)
    return sorted(p.stem for p in tests_dir.glob("test*.py")
                  if p.stem.isidentifier())


def load_durations(path=None):
    path = DURATIONS if path is None else Path(path)
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return {k: float(v) for k, v in data.items()
            if isinstance(k, str) and isinstance(v, (int, float))}


def parse_timings(output):
    """{test id: seconds} from unittest --durations output (Python 3.12+)."""
    marker = output.rfind("Slowest test durations")
    if marker < 0:
        return {}
    return {test: float(sec) for sec, test in TIMED.findall(output[marker:])}


def slow_entries(timed, modules):
    """Timed ids at or over SLOW_SECONDS that --fast can leave out: ids of a
    test module (a delegated worker's own id, say, cannot be selected)."""
    modules = set(modules)
    return {t: round(v, 1) for t, v in timed.items()
            if v >= SLOW_SECONDS and t.split(".")[0] in modules}


def schedule(modules, durations):
    """Slowest first; unknown modules count as the slowest."""
    slowest = max(durations.values(), default=0.0)
    return sorted(modules, key=lambda m: (-durations.get(m, slowest + 1), m))


def module_env(home, base=None, root=None):
    """The clean per-module environment (see the module docstring)."""
    base = os.environ if base is None else base
    root = ROOT if root is None else root
    home = Path(home)
    for name in ("home", "config", "tmp"):
        (home / name).mkdir(parents=True, exist_ok=True)
    return {
        "PATH": base.get("PATH", os.defpath),
        "HOME": str(home / "home"),
        "XDG_CONFIG_HOME": str(home / "config"),
        "TMPDIR": str(home / "tmp"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": os.pathsep.join([str(root), str(Path(root) / "tests")]),
    }


def parse_result(output):
    """(tests run, skipped, final status line) from unittest's summary."""
    ran = RAN.findall(output)
    status = STATUS.findall(output)
    skipped = SKIPPED.findall(status[-1]) if status else []
    return (int(ran[-1]) if ran else 0,
            int(skipped[0]) if skipped else 0,
            status[-1] if status else "")


def module_ok(code, ran):
    """Exit 0 alone is not a pass: Python before 3.12 exits 0 when a module
    collects no tests at all."""
    return code == 0 and ran > 0


def chunk(ids, parts):
    """Interleave ids into at most `parts` nonempty chunks, every id once."""
    return [c for c in (ids[i::parts] for i in range(max(1, parts))) if c]


def list_tests(module, root=None, python=sys.executable):
    """The module's test ids, listed in a clean child; None if unlistable."""
    root = ROOT if root is None else Path(root)
    scratch = Path(tempfile.mkdtemp(prefix="run-tests-list-%s-" % module))
    try:
        proc = subprocess.run(
            [python, "-B", "-c", LIST_TESTS, module], cwd=str(root),
            env=module_env(scratch, root=root), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            timeout=300)
    except subprocess.TimeoutExpired:
        return None
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    ids = proc.stdout.split()
    return ids if proc.returncode == 0 and ids else None


def plan_jobs(modules, durations, split=None, root=None, exclude=()):
    """(label, module, test ids or None, weight) per process, slowest first,
    and how many listed tests `exclude` removed."""
    split = SPLIT if split is None else split
    exclude = frozenset(exclude)
    slowest = max(durations.values(), default=0.0) + 1
    jobs, removed = [], 0
    for module in schedule(modules, durations):
        weight = durations.get(module, slowest)
        parts = split.get(module, 1)
        excluding = any(t.startswith(module + ".") for t in exclude)
        ids = list_tests(module, root=root) if parts > 1 or excluding else None
        if ids and excluding:
            kept = [t for t in ids if t not in exclude]
            removed += len(ids) - len(kept)
            if not kept:
                continue
            weight *= len(kept) / len(ids)
            ids = kept
        chunks = chunk(ids, parts) if ids else []
        if not chunks or (len(chunks) == 1 and not excluding):
            jobs.append((module, module, None, weight))
            continue
        for i, part in enumerate(chunks):
            label = module if len(chunks) == 1 else "%s[%d/%d]" % (module, i + 1, len(chunks))
            jobs.append((label, module, part, weight * len(part) / len(ids)))
    return sorted(jobs, key=lambda j: (-j[3], j[0])), removed


def run_module(module, timeout=None, root=None, python=sys.executable, ids=None,
               label=None, timings=False):
    """Run one module, or the given test ids from it, in its own clean
    environment; returns a result dict."""
    root = ROOT if root is None else Path(root)
    scratch = Path(tempfile.mkdtemp(prefix="run-tests-%s-" % module))
    started = time.monotonic()
    try:
        try:
            proc = subprocess.run(
                [python, "-B", "-m", "unittest"]
                + (["--durations", "0"] if timings and sys.version_info >= (3, 12) else [])
                + list(ids or [module]), cwd=str(root),
                env=module_env(scratch, root=root), stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                timeout=timeout)
            code, output = proc.returncode, proc.stdout
        except subprocess.TimeoutExpired as exc:
            out = exc.output or ""
            if isinstance(out, bytes):
                out = out.decode(errors="replace")
            code, output = None, out + "\n[run_tests] timed out after %ss\n" % timeout
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    ran, skipped, status = parse_result(output)
    ok = module_ok(code, ran)
    return {"label": label or module, "module": module, "ok": ok, "code": code,
            "ran": ran,
            "skipped": skipped, "status": status or "no unittest summary",
            "seconds": round(time.monotonic() - started, 1), "output": output,
            "timings": parse_timings(output) if timings else {}}


def default_workers():
    """CPUs available to this process, with a positive machine-count fallback."""
    if hasattr(os, "sched_getaffinity"):
        return max(1, len(os.sched_getaffinity(0)))
    return max(1, os.cpu_count() or 1)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("modules", nargs="*",
                        help="test modules to run (default: all of tests/test*.py)")
    parser.add_argument("-j", "--jobs", type=int, default=None,
                        help="parallel workers (default: len(os.sched_getaffinity(0)) "
                        "where available, otherwise os.cpu_count() or 1; minimum 1)")
    parser.add_argument("--timeout", type=float, default=None,
                        help="seconds allowed per module (default: none)")
    parser.add_argument("--no-split", action="store_true",
                        help="run every module as one process")
    parser.add_argument("--fast", action="store_true",
                        help="leave out the tests in tests/slow_tests.json")
    parser.add_argument("--update-durations", action="store_true",
                        help="record module times in tests/durations.json and "
                        "slow tests in tests/slow_tests.json")
    args = parser.parse_args(argv)
    if args.jobs is None:
        args.jobs = default_workers()
    elif args.jobs < 1:
        parser.error("--jobs must be at least 1")
    if args.fast and args.update_durations:
        parser.error("--update-durations needs the slow tests; drop --fast")

    available = discover_modules()
    modules = args.modules or available
    unknown = sorted(set(modules) - set(available))
    if unknown:
        parser.error("not a tests/test*.py module: %s" % ", ".join(unknown))
    durations = load_durations()
    slow = load_durations(SLOW)
    plan, removed = plan_jobs(modules, durations, split={} if args.no_split else None,
                              root=ROOT, exclude=slow if args.fast else ())
    if not plan:
        print("run_tests: nothing to run")
        return 1
    jobs = max(1, min(args.jobs, len(plan)))
    print("run_tests: %d modules in %d processes, %d workers%s" % (
        len(modules), len(plan), jobs,
        ", --fast leaves out %d slow tests" % removed if args.fast else ""), flush=True)

    lock = threading.Lock()
    results = []
    started = time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(jobs) as pool:
        futures = [pool.submit(run_module, module, args.timeout, ROOT, ids=ids,
                               label=label, timings=args.update_durations)
                   for label, module, ids, _ in plan]
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            with lock:
                results.append(result)
                print("[%3d/%d] %-4s %7.1fs  %-45s %s" % (
                    len(results), len(plan), "ok" if result["ok"] else "FAIL",
                    result["seconds"], result["label"], result["status"]),
                    flush=True)
    wall = time.monotonic() - started

    failed = sorted((r for r in results if not r["ok"]), key=lambda r: r["label"])
    for r in failed:
        print("\n" + "=" * 70 + "\n%s: exit %s, %s\n" % (
            r["label"], r["code"], r["status"]) + "=" * 70)
        print(r["output"].rstrip())
    ran = sum(r["ran"] for r in results)
    skipped = sum(r["skipped"] for r in results)
    failed_modules = sorted({r["module"] for r in failed})
    print("\nRan %d tests in %d modules (%d processes) in %.1fs with %d workers: %s" % (
        ran, len(modules), len(results), wall, jobs,
        "OK" if not failed else "FAILED (%d modules: %s)" % (
            len(failed_modules), ", ".join(failed_modules))) +
        (" (skipped=%d)" % skipped if skipped else "") +
        (" [--fast: %d slow tests not run]" % removed if args.fast else ""))

    if args.update_durations:
        merged = dict(durations)
        measured = {}
        for r in results:  # a split module's time is the sum of its chunks
            measured[r["module"]] = round(measured.get(r["module"], 0) + r["seconds"], 1)
        merged.update(measured)
        merged = {k: merged[k] for k in sorted(merged) if k in set(available)}
        DURATIONS.write_text(json.dumps(merged, indent=1, sort_keys=True) + "\n")
        print("run_tests: wrote %s" % DURATIONS)
        timed = {}
        for r in results:
            timed.update(r["timings"])
        if timed:
            ran_modules = {r["module"] for r in results}
            kept = {t: v for t, v in slow.items() if t.split(".")[0] not in ran_modules}
            kept.update(slow_entries(timed, available))
            SLOW.write_text(json.dumps(kept, indent=1, sort_keys=True) + "\n")
            print("run_tests: wrote %s (%d tests of %.0fs or more)" % (
                SLOW, len(kept), SLOW_SECONDS))
        else:
            print("run_tests: per-test times need Python 3.12+; %s unchanged" % SLOW)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
