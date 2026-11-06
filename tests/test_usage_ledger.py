"""Usage fixtures contain synthetic content only; finite counter checks below."""
import itertools
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills/hanig-swarm/scripts"
sys.path.insert(0, str(SCRIPTS))
import usage_ledger as L
import swarm as S
import swarm_state as SS
import swarm_workspaces as SW

SENTINEL = "MESSAGE-TOOL-SECRET-MUST-NOT-LEAK"


def rollout(home, name="one", model="fixture-a", count=7):
    path = home / "sessions" / "nested" / ("rollout-" + name + ".jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {"type": "turn_context", "payload": {"model": model, "secret": SENTINEL}},
        {"type": "response_item", "payload": {"text": SENTINEL}},
        {"timestamp": "2026-11-06T03:00:00Z", "type": "event_msg",
         "payload": {"type": "token_count", "info": {"last_token_usage": {
             "input_tokens": count, "cached_input_tokens": 2,
             "cache_write_input_tokens": 1, "output_tokens": 3,
             "reasoning_output_tokens": 1}}, "secret": SENTINEL}}]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return path


class TestUsageLedger(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.state_dir = self.base / "state"
        self.home = self.state_dir / "codex-homes" / "attempt"
        self.home.mkdir(parents=True)
        info = self.home.stat()
        self.unit = {"id": "code", "kind": "code", "needs": [],
                     "provider": "codex/fixture-a"}
        self.state = {"units": {"code": {"state": "FAILED",
            "attempt_dir": str(self.base / "runs" / "attempt"),
            "attempts": [], "gpu_hours": 0,
            "attempt_codex_homes": {"attempt": {"path": str(self.home),
                "identity": {"device": info.st_dev, "inode": info.st_ino}}}}}}
        self.path = self.state_dir / "usage" / "attempt.json"
        ok, reason = S.acquire_lease(str(self.state_dir))
        self.assertTrue(ok, reason)
        self.addCleanup(S.release_lease, str(self.state_dir))

    def read(self):
        return json.loads(self.path.read_text())

    def test_sums_and_no_content(self):
        one = rollout(self.home)
        rollout(self.home, "two", "fixture-b", 11)
        # Two models also occur within a single rollout.
        with one.open("a") as handle:
            handle.write(json.dumps({"type": "turn_context", "payload": {"model": "fixture-b"}}) + "\n")
            handle.write((self.home / "sessions/nested/rollout-two.jsonl").read_text().splitlines()[-1] + "\n")
            handle.write("malformed " + SENTINEL + "\n")
        rec = L.harvest(self.home)
        self.assertEqual(rec["status"], "ok")
        self.assertEqual(rec["skipped_lines"], 1)
        self.assertEqual(rec["rollout_files"], 2)
        self.assertEqual(rec["models"]["fixture-a"]["input_tokens"], 7)
        b = rec["models"]["fixture-b"]
        self.assertEqual(b["calls"], 2)
        self.assertEqual(b["input_tokens"], 22)
        self.assertEqual(b["rollout_files"], 2)
        self.assertEqual(b["first_timestamp"], "2026-11-06T03:00:00+00:00")
        L.record(self.state_dir, "code", "attempt", self.home)
        self.assertNotIn(SENTINEL, json.dumps(rec))
        self.assertNotIn(SENTINEL, self.path.read_text())

    def test_none_and_unknown_format(self):
        self.assertEqual(L.harvest(self.home)["status"], "none")
        path = rollout(self.home)
        path.write_text(path.read_text().replace("last_token_usage", "future_usage"))
        self.assertEqual(L.harvest(self.home)["status"], "unknown")

    def test_cleanup_harvests_before_deletion(self):
        rollout(self.home)
        SW._cleanup_codex_home(self.state, self.unit, "attempt", [], str(self.state_dir))
        self.assertFalse(self.home.exists())
        rec = self.read()
        self.assertEqual(rec["status"], "ok")
        self.assertEqual(rec["models"]["fixture-a"]["input_tokens"], 7)
        self.assertEqual(rec["terminal_state"], "FAILED")
        self.assertEqual((rec["unit_id"], rec["attempt_id"]), ("code", "attempt"))
        self.assertIn("harvested_at", rec)

    def test_cleanup_harvest_failure_and_storage_failure(self):
        with mock.patch.object(L, "harvest", side_effect=RuntimeError(SENTINEL)):
            SW._cleanup_codex_home(self.state, self.unit, "attempt", [], str(self.state_dir))
        self.assertFalse(self.home.exists())
        self.assertEqual(self.read()["status"], "unknown")
        self.assertEqual(self.read()["error"], "RuntimeError")
        self.assertNotIn("models", self.read())
        self.assertNotIn(SENTINEL, self.path.read_text())
        # Storage failure is also nonblocking, even while removing a home.
        self.home.mkdir()
        info = self.home.stat()
        self.state["units"]["code"]["attempt_codex_homes"]["attempt"] = {
            "path": str(self.home), "identity": {"device": info.st_dev, "inode": info.st_ino}}
        with mock.patch.object(L, "_atomic", side_effect=OSError):
            SW._cleanup_codex_home(self.state, self.unit, "attempt", [], str(self.state_dir))
        self.assertFalse(self.home.exists())

    def test_advance_refresh_survives_manual_deletion(self):
        rollout(self.home)
        # Halted coordinator still refreshes surviving live homes, without dispatch.
        self.state["halted"] = "fixture"
        self.state["units"]["code"]["state"] = "RUNNING"
        with mock.patch.object(SW, "reconcile_orphan", return_value=(None, None)):
            S.advance({"units": []}, self.state, str(self.state_dir),
                      str(self.base / "runs"), False, max_new=0)
        self.assertEqual(self.read()["models"]["fixture-a"]["input_tokens"], 7)
        shutil.rmtree(self.home)
        self.assertEqual(self.read()["status"], "ok")
        L.refresh(self.state_dir, self.state)
        self.assertEqual(self.read()["status"], "regressed")
        self.assertEqual(self.read()["models"]["fixture-a"]["input_tokens"], 7)

    def test_regression_exhaustive(self):
        # Exhaustive itertools product: prior/current input counts in [0, 3].
        for before, after in itertools.product(range(4), repeat=2):
            with self.subTest(before=before, after=after):
                self.path.unlink(missing_ok=True)
                path = rollout(self.home, count=before)
                L.record(self.state_dir, "code", "attempt", self.home)
                rollout(self.home, count=after)
                L.record(self.state_dir, "code", "attempt", self.home)
                self.assertEqual(self.read()["models"]["fixture-a"]["input_tokens"], max(before, after))
                self.assertEqual(self.read()["status"], "regressed" if after < before else "ok")
                path.write_text("")
                L.record(self.state_dir, "code", "attempt", self.home)
                self.assertEqual(self.read()["status"], "unknown")
                self.assertEqual(self.read()["models"]["fixture-a"]["input_tokens"], max(before, after))

    def test_report_cli_days_totals_and_exclusions(self):
        rollout(self.home)
        L.record(self.state_dir, "code", "attempt", self.home)
        L.record(self.state_dir, "code", "bad", None)
        path = self.base / "plan.json"
        path.write_text(json.dumps({"units": [self.unit,
            {"id": "other", "kind": "code", "provider": "other/fixture"}]}))
        snapshot = self.path.read_bytes()
        env = dict(os.environ, HANIG_REPORT_TZ="America/Los_Angeles")
        proc = subprocess.run([sys.executable, str(SCRIPTS / "swarm.py"), "usage",
            str(path), "--state-dir", str(self.state_dir), "--json"], env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rep = json.loads(proc.stdout)
        self.assertEqual(rep["grand_total"], dict(calls=1, input_tokens=7,
            cached_input_tokens=2, cache_write_input_tokens=1, output_tokens=3,
            reasoning_output_tokens=1))
        self.assertEqual(rep["days"]["2026-11-05"], rep["grand_total"])
        self.assertEqual(rep["units"]["code"]["total"], rep["grand_total"])
        self.assertEqual(rep["models"]["fixture-a"], rep["grand_total"])
        self.assertEqual(len(rep["unknown"]), 1)
        self.assertEqual(rep["unmeasured"], ["other"])
        self.assertEqual(snapshot, self.path.read_bytes())
        rollout(self.home, count=1)
        L.record(self.state_dir, "code", "attempt", self.home)
        with mock.patch.dict(os.environ, {"HANIG_REPORT_TZ": "UTC"}):
            rep = L.report(self.state_dir, {"units": [self.unit]}, "codex/fixture")
        self.assertEqual(len(rep["regressed"]), 1)
        self.assertEqual(rep["grand_total"]["calls"], 0)

    def test_known_terminal_state_survives_later_refresh(self):
        # Exhaustive terminal-state cases over the coordinator cleanup set.
        for terminal in ("DONE", "FAILED", "FAILED_EVIDENCE", "PREEMPTED",
                         "READY_FOR_PR", "ABANDONED", "SUPERSEDED"):
            with self.subTest(terminal=terminal):
                self.path.unlink(missing_ok=True)
                rollout(self.home)
                L.record(self.state_dir, "code", "attempt", self.home, terminal)
                L.record(self.state_dir, "code", "attempt", self.home, "RUNNING")
                self.assertEqual(self.read()["terminal_state"], terminal)

    def test_atomic_write_fsync_before_replace(self):
        rollout(self.home)
        order = []
        fsync, replace = os.fsync, os.replace
        def synced(fd):
            order.append("fsync")
            fsync(fd)
        def replaced(source, destination):
            order.append("replace")
            self.assertEqual(json.loads(Path(source).read_text())["status"], "ok")
            replace(source, destination)
        with mock.patch.object(L.os, "fsync", side_effect=synced), mock.patch.object(L.os, "replace", side_effect=replaced):
            L.record(self.state_dir, "code", "attempt", self.home)
        self.assertEqual(order, ["fsync", "replace", "fsync"])
        self.assertEqual(list(self.path.parent.glob(".usage-*")), [])


if __name__ == "__main__":
    unittest.main()
