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
    self.state = self.root / "first"
    self.state.mkdir()
    self.snapshot = self.root / "wait-event-snapshot.json"
    self.put(self.state, "RUNNING")

  def put(self, directory, state):
    (directory / "swarm-state.json").write_text(json.dumps({"units": {"unit": {"state": state}}}))

  def cli(self, *extra):
    return subprocess.run([sys.executable, str(SCRIPTS / "wait_event.py"),
                           "--state-dir", str(self.state)] + list(extra),
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          universal_newlines=True, timeout=10,
                          env=dict(os.environ, PATH="", PYTHONDONTWRITEBYTECODE="1"))

  def invoke(self, *extra):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
      code = wait_event.main(["--state-dir", str(self.state)] + list(extra))
    return code, out.getvalue(), err.getvalue()

  def test_first_run_and_no_event_once(self):
    result = self.cli("--once")
    self.assertEqual((result.returncode, result.stdout, result.stderr), (3, "", ""))
    self.assertEqual(json.loads(self.snapshot.read_text())[str(self.state)], {"unit": "RUNNING"})
    self.assertEqual(stat.S_IMODE(self.snapshot.stat().st_mode), 0o600)
    self.put(self.state, "READY_FOR_PR")
    self.snapshot.unlink()
    result = self.cli("--once")
    self.assertEqual(result.returncode, 0, result.stderr)
    self.assertIn("STATE ", result.stdout)
    self.assertNotIn("TRANSITION", result.stdout)
    self.assertRegex(result.stdout, r"STATE \d{4}-\d\d-\d\d .* first unit:")
    self.assertRegex(result.stdout, r"\(end of pass .*, 1 line\(s\)\)\n$")

  def test_transition_and_report_once_across_processes(self):
    self.assertEqual(self.cli("--once").returncode, 3)
    self.put(self.state, "NEEDS_HUMAN")
    result = self.cli("--once")
    self.assertEqual(result.returncode, 0, result.stderr)
    self.assertIn("RUNNING -> NEEDS_HUMAN", result.stdout)
    self.assertEqual(len(result.stdout.splitlines()), 3)
    result = self.cli("--once")
    self.assertEqual((result.returncode, result.stdout), (3, ""))
    self.put(self.state, "RUNNING")
    self.assertEqual(self.cli("--once").returncode, 0)
    self.put(self.state, "NEEDS_HUMAN")
    self.assertIn("STATE ", self.cli("--once").stdout)

  def test_all_states_and_arrivals_exhaustively(self):
    # Exhaustive finite bound: all length-three sequences over these 9 states
    # (729 sequences), including an unknown future operator-needed state.
    values = ("UNALLOCATED", "SUBMITTED", "RUNNING", "DONE", "ABANDONED",
              "SUPERSEDED", "READY_FOR_PR", "FAILED", "FUTURE_STATE")
    quiet = set(values[:6])
    for sequence in itertools.product(values, repeat=3):
      snapshot = {}
      previous = None
      for state in sequence:
        self.put(self.state, state)
        events, readable = wait_event.observe([self.state], snapshot, set())
        expected = (["TRANSITION"] if previous is not None and previous != state else [])
        if state not in quiet and previous != state:
          expected.append("STATE")
        self.assertEqual([e["event"] for e in events], expected, sequence)
        self.assertEqual(readable, 1)
        previous = state

  def test_two_directories_all_events_and_json(self):
    other = self.root / "second"
    other.mkdir()
    self.put(self.state, "READY_FOR_PR")
    self.put(other, "FAILED")
    result = self.cli("--once", "--state-dir", str(other), "--json")
    self.assertEqual(result.returncode, 0, result.stderr)
    rows = [json.loads(line) for line in result.stdout.splitlines()]
    self.assertEqual([r["directory"] for r in rows[:-1]], ["first", "second"])
    self.assertTrue(all(r["event"] == "STATE" and r["observed"] for r in rows[:-1]))
    self.assertEqual(rows[-1]["event"], "END")
    self.assertEqual(rows[-1]["lines"], 2)

  def test_unreadable_among_readable_and_all_unreadable(self):
    missing = self.root / "missing"
    self.put(self.state, "FAILED")
    result = self.cli("--once", "--state-dir", str(missing))
    self.assertEqual(result.returncode, 0)
    self.assertIn("STATE", result.stdout)
    self.assertEqual(result.stderr.count("UNREADABLE"), 1)
    bad_values = (b"{", b"\xff", b"[]", b'{}', b'{"units": []}',
                  b'{"units": {"x": []}}', b'{"units": {"x": {"state": 1}}}')
    for data in bad_values:
      (self.state / "swarm-state.json").write_bytes(data)
      result = self.cli("--once")
      self.assertEqual((result.returncode, result.stdout), (2, ""), data)
      self.assertEqual(result.stderr.count("UNREADABLE"), 1, data)

  def test_timeout_and_warning_once_and_first_event_ends_polling(self):
    missing = self.root / "missing"
    with mock.patch.object(wait_event.time, "monotonic", side_effect=[0, 0, 1, 1, 2]), \
         mock.patch.object(wait_event.time, "sleep") as sleep:
      code, out, err = self.invoke("--timeout", "2", "--interval", "1", "--state-dir", str(missing))
    self.assertEqual((code, out), (3, ""))
    self.assertEqual(err.count("UNREADABLE"), 1)
    self.assertEqual(sleep.call_count, 2)
    with mock.patch.object(wait_event.time, "sleep", side_effect=lambda _: self.put(self.state, "DONE")) as sleep:
      code, out, err = self.invoke()
    self.assertEqual(code, 0, err)
    self.assertIn("TRANSITION", out)
    sleep.assert_called_once_with(60)
    result = self.cli("--timeout", "0.02", "--interval", "0.01")
    self.assertEqual((result.returncode, result.stdout), (3, ""))

  def test_event_after_deadline_is_not_reported(self):
    with mock.patch.object(wait_event.time, "monotonic", side_effect=[0, 0, 2]), \
         mock.patch.object(wait_event.time, "sleep", side_effect=lambda _: self.put(self.state, "FAILED")):
      code, out, err = self.invoke("--timeout", "1")
    self.assertEqual((code, out, err), (3, "", ""))

  def test_unreadable_does_not_erase_previous_observation(self):
    self.cli("--once")
    (self.state / "swarm-state.json").write_text("{")
    self.assertEqual(self.cli("--once").returncode, 2)
    self.put(self.state, "DONE")
    self.assertIn("RUNNING -> DONE", self.cli("--once").stdout)

  def test_state_directories_byte_identical_no_new_files(self):
    other = self.root / "second"
    other.mkdir()
    self.put(other, "FAILED")
    (self.state / "nested").mkdir()
    (self.state / "nested" / "receipt").write_bytes(b"preserved\x00\xff")
    def fingerprint():
      return {str(p): p.read_bytes() if p.is_file() else None
              for directory in (self.state, other) for p in directory.rglob("*")}
    before = fingerprint()
    result = self.cli("--once", "--state-dir", str(other))
    self.assertEqual(result.returncode, 0, result.stderr)
    self.assertEqual(fingerprint(), before)
    self.assertTrue(self.snapshot.exists())

  def test_snapshot_is_atomic_private_and_custom_location(self):
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
    self.assertFalse(self.snapshot.exists())
    self.assertFalse(list(self.root.glob(".wait-event-*")))

  def test_refuse_snapshot_inside_state_and_invalid_arguments(self):
    original = (self.state / "swarm-state.json").read_bytes()
    alias = self.root / "alias"
    alias.symlink_to(self.state, target_is_directory=True)
    for path in (self.state / "snapshot", alias / "snapshot", self.state / "swarm-state.json"):
      result = self.cli("--once", "--snapshot", str(path))
      self.assertEqual(result.returncode, 2)
    self.assertEqual((self.state / "swarm-state.json").read_bytes(), original)
    for flag, value in (("--interval", "0"), ("--interval", "nan"), ("--timeout", "inf"), ("--timeout", "-1")):
      self.assertEqual(self.cli(flag, value).returncode, 2)


if __name__ == "__main__":
  unittest.main()
