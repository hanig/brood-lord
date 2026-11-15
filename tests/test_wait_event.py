"""Synthetic local coordinator observations; no scheduler or agent is needed."""
import contextlib
import io
import itertools
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "hanig-orchestrate" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import wait_event


class WaitEvent(unittest.TestCase):
  def setUp(self):
    self.tmp = tempfile.TemporaryDirectory()
    self.addCleanup(self.tmp.cleanup)
    self.root = Path(self.tmp.name).resolve()
    self.d = self.root / "first"
    self.d.mkdir()
    self.snap = self.root / "wait-event-snapshot.json"
    self.put(self.d, "RUNNING")

  def put(self, directory, state):
    (directory / "swarm-state.json").write_text(json.dumps({"units": {"unit": {"state": state}}}))

  def cli(self, *extra):
    return subprocess.run([sys.executable, str(SCRIPTS / "wait_event.py"),
                           "--state-dir", str(self.d)] + list(extra),
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          universal_newlines=True, timeout=10,
                          env=dict(os.environ, PATH="", PYTHONDONTWRITEBYTECODE="1"))

  def invoke(self, *extra):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
      code = wait_event.main(["--state-dir", str(self.d)] + list(extra))
    return code, out.getvalue(), err.getvalue()

  def test_first_once(self):
    result = self.cli("--once")
    self.assertEqual((result.returncode, result.stdout, result.stderr), (3, "", ""))
    self.assertEqual(json.loads(self.snap.read_text())[str(self.d)], {"unit": "RUNNING"})
    self.assertEqual(stat.S_IMODE(self.snap.stat().st_mode), 0o600)
    self.put(self.d, "READY_FOR_PR")
    self.snap.unlink()
    result = self.cli("--once")
    self.assertEqual(result.returncode, 0, result.stderr)
    self.assertIn("STATE ", result.stdout)
    self.assertNotIn("TRANSITION", result.stdout)
    self.assertRegex(result.stdout, r"STATE \d{4}-\d\d-\d\d .* first unit:")
    self.assertRegex(result.stdout, r"\(end of pass .*, 1 line\(s\)\)\n$")

  def test_report_once(self):
    self.assertEqual(self.cli("--once").returncode, 3)
    self.put(self.d, "NEEDS_HUMAN")
    result = self.cli("--once")
    self.assertEqual(result.returncode, 0, result.stderr)
    self.assertIn("RUNNING -> NEEDS_HUMAN", result.stdout)
    self.assertEqual(len(result.stdout.splitlines()), 3)
    result = self.cli("--once")
    self.assertEqual((result.returncode, result.stdout), (3, ""))
    self.put(self.d, "RUNNING")
    self.assertEqual(self.cli("--once").returncode, 0)
    self.put(self.d, "NEEDS_HUMAN")
    self.assertIn("STATE ", self.cli("--once").stdout)

  def test_sequences(self):
    # Exhaustive bound: 9**3 = 729 state sequences, including a future state.
    values = ("UNALLOCATED", "SUBMITTED", "RUNNING", "DONE", "ABANDONED",
              "SUPERSEDED", "READY_FOR_PR", "FAILED", "FUTURE_STATE")
    quiet = set(values[:6])
    for sequence in itertools.product(values, repeat=3):
      snapshot = {}
      previous = None
      for state in sequence:
        self.put(self.d, state)
        events, readable = wait_event.observe([self.d], snapshot, set())
        expected = (["TRANSITION"] if previous is not None and previous != state else [])
        if state not in quiet and previous != state:
          expected.append("STATE")
        self.assertEqual([e["event"] for e in events], expected, sequence)
        self.assertEqual(readable, 1)
        previous = state

  def test_two_json(self):
    other = self.root / "second"
    other.mkdir()
    self.put(self.d, "READY_FOR_PR")
    self.put(other, "FAILED")
    result = self.cli("--once", "--state-dir", str(other), "--json")
    self.assertEqual(result.returncode, 0, result.stderr)
    rows = [json.loads(line) for line in result.stdout.splitlines()]
    self.assertEqual([r["directory"] for r in rows[:-1]], ["first", "second"])
    self.assertTrue(all(r["event"] == "STATE" and r["observed"] for r in rows[:-1]))
    self.assertEqual(rows[-1]["event"], "END")
    self.assertEqual(rows[-1]["lines"], 2)

  def test_unreadable(self):
    missing = self.root / "missing"
    self.put(self.d, "FAILED")
    result = self.cli("--once", "--state-dir", str(missing))
    self.assertEqual(result.returncode, 0)
    self.assertIn("STATE", result.stdout)
    self.assertEqual(result.stderr.count("UNREADABLE"), 1)
    bad_values = (b"{", b"\xff", b"[]", b'{}', b'{"units": []}',
                  b'{"units": {"x": []}}', b'{"units": {"x": {"state": 1}}}')
    for data in bad_values:
      (self.d / "swarm-state.json").write_bytes(data)
      result = self.cli("--once")
      self.assertEqual((result.returncode, result.stdout), (2, ""), data)
      self.assertEqual(result.stderr.count("UNREADABLE"), 1, data)

  def test_timeout_poll(self):
    missing = self.root / "missing"
    with mock.patch.object(wait_event.time, "monotonic", side_effect=[0, 0, 1, 1, 2]), \
         mock.patch.object(wait_event.time, "sleep") as sleep:
      code, out, err = self.invoke("--timeout", "2", "--interval", "1", "--state-dir", str(missing))
    self.assertEqual((code, out), (3, ""))
    self.assertEqual(err.count("UNREADABLE"), 1)
    self.assertEqual(sleep.call_count, 2)
    with mock.patch.object(wait_event.time, "sleep", side_effect=lambda _: self.put(self.d, "DONE")) as sleep:
      code, out, err = self.invoke()
    self.assertEqual(code, 0, err)
    self.assertIn("TRANSITION", out)
    sleep.assert_called_once_with(60)
    result = self.cli("--timeout", "0.02", "--interval", "0.01")
    self.assertEqual((result.returncode, result.stdout), (3, ""))

  def test_deadline(self):
    with mock.patch.object(wait_event.time, "monotonic", side_effect=[0, 0, 2]), \
         mock.patch.object(wait_event.time, "sleep", side_effect=lambda _: self.put(self.d, "FAILED")):
      code, out, err = self.invoke("--timeout", "1")
    self.assertEqual((code, out, err), (3, "", ""))

  def test_unreadable_retains(self):
    self.cli("--once")
    (self.d / "swarm-state.json").write_text("{")
    self.assertEqual(self.cli("--once").returncode, 2)
    self.put(self.d, "DONE")
    self.assertIn("RUNNING -> DONE", self.cli("--once").stdout)

  def test_read_only(self):
    other = self.root / "second"
    other.mkdir()
    self.put(other, "FAILED")
    (self.d / "nested").mkdir()
    (self.d / "nested" / "receipt").write_bytes(b"preserved\x00\xff")
    def fingerprint():
      return {str(p): p.read_bytes() if p.is_file() else None
              for directory in (self.d, other) for p in directory.rglob("*")}
    before = fingerprint()
    result = self.cli("--once", "--state-dir", str(other))
    self.assertEqual(result.returncode, 0, result.stderr)
    self.assertEqual(fingerprint(), before)
    self.assertTrue(self.snap.exists())

  def test_atomic_private(self):
    path = self.root / "custom.json"
    path.write_text("{}")
    path.chmod(0o644)
    replace = os.replace
    def check(source, target):
      self.assertEqual(path.read_text(), "{}")
      self.assertEqual(stat.S_IMODE(Path(source).stat().st_mode), 0o600)
      replace(source, target)
    with mock.patch.object(wait_event.os, "replace", side_effect=check) as operation:
      code, _, err = self.invoke("--once", "--snapshot", str(path))
    self.assertEqual(code, 3, err)
    operation.assert_called_once()
    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
    self.assertFalse(self.snap.exists())
    self.assertFalse(list(self.root.glob(".wait-event-*")))

  def test_arguments(self):
    original = (self.d / "swarm-state.json").read_bytes()
    alias = self.root / "alias"
    alias.symlink_to(self.d, target_is_directory=True)
    for path in (self.d / "snapshot", alias / "snapshot", self.d / "swarm-state.json"):
      result = self.cli("--once", "--snapshot", str(path))
      self.assertEqual(result.returncode, 2)
    self.assertEqual((self.d / "swarm-state.json").read_bytes(), original)
    for flag, value in (("--interval", "0"), ("--interval", "nan"), ("--timeout", "inf"), ("--timeout", "-1")):
      self.assertEqual(self.cli(flag, value).returncode, 2)

  def test_snapshot_alias(self):
    target = self.root / "coordinator.json"
    target.write_text('{"units":{}}')
    source = self.d / "swarm-state.json"
    source.unlink()
    source.symlink_to(target)
    result = self.cli("--once", "--snapshot", str(target))
    self.assertEqual(target.read_text(), '{"units":{}}')
    self.assertEqual(result.returncode, 2)

  def test_loop_source(self):
    loop = self.root / "loop"
    loop.symlink_to(loop)
    self.put(self.d, "READY_FOR_PR")
    result = self.cli("--once", "--state-dir", str(loop))
    self.assertEqual(result.returncode, 0, result.stderr)
    self.assertIn("STATE ", result.stdout)
    self.assertEqual(result.stderr.count("UNREADABLE"), 1)

  def test_new_unit(self):
    for state in ("UNALLOCATED", "RUNNING", "READY_FOR_PR"):
      with self.subTest(state=state):
        if self.snap.exists():
          self.snap.unlink()
        (self.d / "swarm-state.json").write_text('{"units":{}}')
        self.assertEqual(self.cli("--once").returncode, 3)
        self.put(self.d, state)
        result = self.cli("--once")
        self.assertEqual(result.returncode, 0 if state == "READY_FOR_PR" else 3)
        self.assertNotIn("TRANSITION", result.stdout)

  def test_omitted_unit(self):
    self.put(self.d, "FAILED")
    self.assertEqual(self.cli("--once").returncode, 0)
    (self.d / "swarm-state.json").write_text('{"units":{}}')
    self.assertEqual(self.cli("--once").returncode, 3)
    self.put(self.d, "FAILED")
    result = self.cli("--once")
    self.assertEqual((result.returncode, result.stdout), (3, ""))
    self.put(self.d, "DONE")
    self.assertIn("FAILED -> DONE", self.cli("--once").stdout)

  def test_output_failure(self):
    self.put(self.d, "READY_FOR_PR")
    failed = mock.Mock()
    failed.write.side_effect = OSError("synthetic output failure")
    with contextlib.redirect_stdout(failed), contextlib.redirect_stderr(io.StringIO()):
      code = wait_event.main(["--state-dir", str(self.d), "--once"])
    self.assertEqual(code, 2)
    self.assertFalse(self.snap.exists())
    self.assertIn("STATE ", self.cli("--once").stdout)

  def test_filesystem_alias(self):
    alias = self.root / "alias"
    alias.symlink_to(self.d, target_is_directory=True)
    (self.d / "swarm-state.json").write_text('{"units":{}}')
    resolve = Path.resolve
    def mount_path(path, *args, **kwargs):
      # Safe bind-mount equivalent: alias paths keep their lexical spelling,
      # while actual reads, writes and samefile use the shared directory.
      return path if alias == path or alias in path.parents else resolve(path, *args, **kwargs)
    with mock.patch.object(Path, "resolve", mount_path), self.assertRaises(SystemExit) as stopped:
      self.invoke("--once", "--snapshot", str(alias / "swarm-state.json"))
    self.assertEqual((self.d / "swarm-state.json").read_text(), '{"units":{}}')
    self.assertEqual(stopped.exception.code, 2)

  def test_umask(self):
    mask = os.umask(0o777)
    try:
      result = self.cli("--once")
    finally:
      os.umask(mask)
    self.assertEqual(result.returncode, 3, result.stderr)
    self.assertEqual(stat.S_IMODE(self.snap.stat().st_mode), 0o600)
    self.put(self.d, "DONE")
    self.assertEqual(self.cli("--once").returncode, 0)

  def test_duplicate_names(self):
    other = self.root / "other" / self.d.name
    other.mkdir(parents=True)
    self.put(other, "DONE")
    args = ("--once", "--state-dir", str(other))
    self.assertEqual(self.cli(*args).returncode, 3)
    self.put(self.d, "DONE")
    result = self.cli(*args)
    self.assertEqual(result.returncode, 0, result.stderr)
    self.assertIn(str(self.d), result.stdout)

  def test_source_mount_alias(self):
    real = self.root / "real"
    real.mkdir()
    target = real / "coord.json"
    target.write_text('{"units":{}}')
    source = self.d / "swarm-state.json"
    source.unlink()
    source.symlink_to(target)
    alias = self.root / "alias"
    alias.symlink_to(real, target_is_directory=True)
    resolve = Path.resolve
    def mount(path, *args, **kwargs):
      return path if alias == path or alias in path.parents else resolve(path, *args, **kwargs)
    with mock.patch.object(Path, "resolve", mount), self.assertRaises(SystemExit) as stopped:
      self.invoke("--once", "--snapshot", str(alias / "coord.json"))
    self.assertEqual(stopped.exception.code, 2)
    self.assertEqual(target.read_text(), '{"units":{}}')

  def test_alias_history(self):
    alias = self.root / "alias"
    alias.symlink_to(self.d, target_is_directory=True)
    resolve = Path.resolve
    def mount(path, *args, **kwargs):
      return path if alias == path or alias in path.parents else resolve(path, *args, **kwargs)
    def run(directory):
      return wait_event.main(["--state-dir", str(directory), "--once", "--snapshot", str(self.snap)])
    with mock.patch.object(Path, "resolve", mount), contextlib.redirect_stdout(io.StringIO()):
      self.assertEqual(run(self.d), 3)
      self.assertEqual(run(alias), 3)
      self.put(self.d, "DONE")
      self.assertEqual(run(self.d), 0)
      self.put(self.d, "RUNNING")
      self.assertEqual(run(alias), 0)
      self.put(self.d, "FAILED")
      self.assertEqual(run(self.d), 0)
      self.assertEqual(run(alias), 3)
      self.assertEqual(len(json.loads(self.snap.read_text())), 1)
      # Conflicting histories persisted by the old implementation cannot be
      # ordered without evidence: refuse intact and request a fresh snapshot.
      self.snap.write_text(json.dumps({str(self.d): {"unit": "DONE"},
                                      str(alias): {"unit": "RUNNING"}}))
      before = self.snap.read_bytes()
      with contextlib.redirect_stderr(io.StringIO()) as err:
        self.assertEqual(run(self.d), 2)
      self.assertIn("fresh --snapshot", err.getvalue())
      self.assertEqual(self.snap.read_bytes(), before)

  def test_large_interval(self):
    def wake(delay):
      self.assertLessEqual(delay, 86400)
      self.put(self.d, "DONE")
    with mock.patch.object(wait_event.time, "sleep", side_effect=wake):
      code, out, err = self.invoke("--interval", "1e20", "--timeout", "1e20")
    self.assertEqual(code, 0, err)
    self.assertIn("TRANSITION", out)

  def test_snapshot_failure_after_output(self):
    self.assertEqual(self.cli("--once").returncode, 3)
    before = self.snap.read_bytes()
    self.put(self.d, "READY_FOR_PR")
    # Fail the atomic replace after the temporary snapshot has been written.
    with mock.patch.object(wait_event.os, "replace", side_effect=OSError("synthetic snapshot failure")):
      code, out, err = self.invoke("--once")
    self.assertEqual(code, 2)
    self.assertIn("TRANSITION ", out)
    self.assertIn("STATE ", out)
    self.assertIn("event may repeat", err)
    self.assertEqual(self.snap.read_bytes(), before)
    self.assertFalse(list(self.root.glob(".wait-event-*")))
    result = self.cli("--once")
    self.assertEqual(result.returncode, 0, result.stderr)
    self.assertIn("RUNNING -> READY_FOR_PR", result.stdout)
    self.assertIn("STATE ", result.stdout)
    self.assertEqual(json.loads(self.snap.read_text())[str(self.d)], {"unit": "READY_FOR_PR"})
    result = self.cli("--once")
    self.assertEqual((result.returncode, result.stdout), (3, ""))


if __name__ == "__main__":
  unittest.main()
