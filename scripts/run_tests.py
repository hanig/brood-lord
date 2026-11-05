#!/usr/bin/env python3
"""Run the unittest suite in parallel, one process per test module.

    python3 scripts/run_tests.py                 # whole suite, one worker per CPU
    python3 scripts/run_tests.py -j 2            # two workers
    python3 scripts/run_tests.py test_outbox test_contract
    python3 scripts/run_tests.py --update-durations

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

Workers take the slowest modules first, using tests/durations.json (seconds
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


def run_module(module, timeout=None, root=None, python=sys.executable):
    """Run one module in its own clean environment; returns a result dict."""
    root = ROOT if root is None else Path(root)
    scratch = Path(tempfile.mkdtemp(prefix="run-tests-%s-" % module))
    started = time.monotonic()
    try:
        try:
            proc = subprocess.run(
                [python, "-B", "-m", "unittest", module], cwd=str(root),
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
    return {"module": module, "ok": ok, "code": code, "ran": ran,
            "skipped": skipped, "status": status or "no unittest summary",
            "seconds": round(time.monotonic() - started, 1), "output": output}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("modules", nargs="*",
                        help="test modules to run (default: all of tests/test*.py)")
    parser.add_argument("-j", "--jobs", type=int, default=os.cpu_count() or 1,
                        help="parallel workers (default: CPU count)")
    parser.add_argument("--timeout", type=float, default=None,
                        help="seconds allowed per module (default: none)")
    parser.add_argument("--update-durations", action="store_true",
                        help="record each module's wall time in tests/durations.json")
    args = parser.parse_args(argv)

    available = discover_modules()
    modules = args.modules or available
    unknown = sorted(set(modules) - set(available))
    if unknown:
        parser.error("not a tests/test*.py module: %s" % ", ".join(unknown))
    durations = load_durations()
    order = schedule(modules, durations)
    jobs = max(1, min(args.jobs, len(order)))
    print("run_tests: %d modules, %d workers" % (len(order), jobs), flush=True)

    lock = threading.Lock()
    results = []
    started = time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(jobs) as pool:
        futures = [pool.submit(run_module, m, args.timeout, ROOT) for m in order]
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            with lock:
                results.append(result)
                print("[%3d/%d] %-4s %7.1fs  %-45s %s" % (
                    len(results), len(order), "ok" if result["ok"] else "FAIL",
                    result["seconds"], result["module"], result["status"]),
                    flush=True)
    wall = time.monotonic() - started

    failed = sorted((r for r in results if not r["ok"]), key=lambda r: r["module"])
    for r in failed:
        print("\n" + "=" * 70 + "\n%s: exit %s, %s\n" % (
            r["module"], r["code"], r["status"]) + "=" * 70)
        print(r["output"].rstrip())
    ran = sum(r["ran"] for r in results)
    skipped = sum(r["skipped"] for r in results)
    print("\nRan %d tests in %d modules in %.1fs with %d workers: %s" % (
        ran, len(results), wall, jobs,
        "OK" if not failed else "FAILED (%d modules: %s)" % (
            len(failed), ", ".join(r["module"] for r in failed))) +
        (" (skipped=%d)" % skipped if skipped else ""))

    if args.update_durations:
        merged = dict(durations)
        merged.update({r["module"]: r["seconds"] for r in results})
        merged = {k: merged[k] for k in sorted(merged) if k in set(available)}
        DURATIONS.write_text(json.dumps(merged, indent=1, sort_keys=True) + "\n")
        print("run_tests: wrote %s" % DURATIONS)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
