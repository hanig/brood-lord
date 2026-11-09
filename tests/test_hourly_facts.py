"""Hourly fixtures with a replaced PATH."""
import hashlib
import itertools
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "hanig-orchestrate" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import hourly_facts
import wait_report

SECTION = b"STALE  tracker\r\n\tunchanged   spaces\n```\nno final newline  "


class HourlyFacts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / "coordinator"
        self.state.mkdir()
        self.output = self.root / "reports"
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.tools = self.root / "tools"
        self.tools.mkdir()
        self.binding = self.root / "binding.json"
        self.binding.write_text("{}")
        self.audit = self.root / "audit.json"
        self.audit.write_text(json.dumps({"read_finished": "2026-11-09T01:02:03+00:00"}))
        skills = self.root / "installed"
        loaded = skills / "hanig-orchestrate"
        loaded.mkdir(parents=True)
        (loaded / "SKILL.md").write_text("fixture")
        project = skills / "hanig-project"
        (project / "scripts").mkdir(parents=True)
        (project / "SKILL.md").write_text("fixture")
        (project / "scripts" / "linear_sync.py").write_text(
            "import sys\nassert sys.argv[1:] == " + repr([
                "section", "--binding", str(self.binding), "--audit", str(self.audit)]) +
            "\nsys.stdout.buffer.write(" + repr(SECTION) + ")\n")
        self.env = dict(os.environ, PATH=str(self.tools), HANIG_ORCHESTRATE_DIR=str(loaded),
                        HANIG_SKILL_DEP_ROOTS="", PYTHONDONTWRITEBYTECODE="1")
        self.responses = {"agents": [], "prs": [], "runs": []}
        for tool in ("gh", "paseo"):
            path = self.tools / tool
            path.write_text("#!" + sys.executable + "\nimport json,sys\nfrom pathlib import Path\n"
                            "d=json.loads(Path(" + repr(str(self.root / "responses.json")) + ").read_text())\n"
                            "k='agents' if 'ls' in sys.argv else ('prs' if 'pr' in sys.argv else 'runs')\n"
                            "v=d[k]\nif isinstance(v,dict) and 'error' in v:\n print(json.dumps(v)); sys.exit(1)\n"
                            "print(json.dumps(v))\n")
            path.chmod(0o755)
        self.args = ["--state-root", str(self.state), "--output-dir", str(self.output),
                     "--repo", str(self.repo), "--binding", str(self.binding), "--audit", str(self.audit)]

    def plan(self, name="wave", units=None, plan=None, old=False):
        directory = self.state / name
        (directory / "state").mkdir(parents=True, exist_ok=True)
        sf = directory / "state" / "swarm-state.json"
        sf.write_text(json.dumps({"units": units or {}}))
        (directory / "plan.json").write_text(json.dumps({"units": plan or []}))
        if old:
            os.utime(sf, (time.time() - 100 * 3600,) * 2)
        return sf

    def collect(self):
        (self.root / "responses.json").write_text(json.dumps(self.responses))
        result = subprocess.run([sys.executable, str(SCRIPTS / "hourly_facts.py")] + self.args,
                                env=self.env, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        path = Path(result.stdout.decode().strip())
        self.assertEqual(path.read_bytes(), (self.output / "latest.md").read_bytes())
        return path.read_bytes()

    def test_parts_and_read_only(self):
        self.plan(units={"u": {"state": "RUNNING", "job_id": "a"}})
        self.responses["agents"] = [{"id": "a", "status": "running"}]
        def digest():
            return {str(p.relative_to(self.root)): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in self.root.rglob("*") if p.is_file() and
                    self.output not in p.parents and p.name != "responses.json"}
        before = digest()
        report = self.collect()
        self.assertEqual(before, digest())
        self.assertLess(report.index(b"## 1."), report.index(b"## 2."))
        self.assertLess(report.index(b"## 2."), report.index(b"## 3."))
        self.assertEqual(report.split(b"````\n", 1)[1].split(b"\n````", 1)[0], SECTION)
        self.assertRegex(report.decode(), r"Agents observed \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \S+")
        self.assertIn(b"audit read_finished ", report)
        self.assertIn(b"Open PRs (observed ", report)
        self.assertIn(b"Latest run on main (observed ", report)
        self.assertEqual({p.name for p in self.output.iterdir()}, {"latest.md"} | {p.name for p in self.output.glob("2*.md")})

    def test_quiet(self):
        self.plan(units={"done": {"state": "DONE"}})
        report = self.collect()
        self.assertIn(b"ATTENTION count: 0; live work: no.", report)
        self.assertFalse(any(line.startswith(b"ATTENTION ") for line in report.splitlines()))

    def test_triggers_exhaustive(self):
        # Exhaustive finite product: 8 unit states x 5 agent conditions = 40 cases.
        states = ["UNALLOCATED", "SUBMITTED", "RUNNING", "DONE", "ABANDONED", "SUPERSEDED", "FAILED", "NEEDS_HUMAN"]
        for state, agent in itertools.product(states, [None, "idle", "running", "working", "busy"]):
            with self.subTest(state=state, agent=agent):
                units = {"u": {"state": state, "job_id": "a"}}
                self.plan(units=units)
                agents = {} if agent is None else {"a": {"status": agent}}
                lines, ready, leftovers, live, active = hourly_facts.state_facts(self.state, 72, agents)
                expected = state in {"FAILED", "NEEDS_HUMAN"} or (state in hourly_facts.ACTIVE and agent in {None, "idle"})
                self.assertEqual(any(flag for flag, line in lines), expected)
        self.plan(units={"u": {"state": "SUBMITTED"}})
        self.assertTrue(any(f for f, _ in hourly_facts.state_facts(self.state, 72, {})[0]))

    def test_ready_and_old(self):
        self.plan("live", {"done": {"state": "DONE"}, "wait": {"state": "RUNNING", "job_id": "a"}},
                  [{"id": "ready", "needs": ["done"]}, {"id": "blocked", "needs": ["wait"]}], old=True)
        self.plan("old", {"invisible": {"state": "FAILED"}}, old=True)
        report = self.collect()
        self.assertIn(b"ATTENTION ready: live/ready", report)
        self.assertNotIn(b"ready: live/blocked", report)
        self.assertIn(b"bound agent a: ABSENT", report)
        self.assertIn(b"Older state dirs hold 1 non-terminal units", report)
        self.assertNotIn(b"invisible", report)

    def test_checks_all_failure_triggers(self):
        for status in sorted(hourly_facts.BAD):
            with self.subTest(status=status):
                self.responses["prs"] = [{"number": 1, "title": "test", "mergeable": "MERGEABLE",
                                         "statusCheckRollup": [{"conclusion": status}, {"state": "SUCCESS"}]}]
                self.responses["runs"] = [{"conclusion": status.lower(), "headSha": "abc"}]
                report = self.collect()
                self.assertIn(b"ATTENTION PR 1", report)
                ci = rb"(?m)^ATTENTION None " + status.lower().encode()
                if status in {"FAILURE", "TIMED_OUT"}:
                    self.assertRegex(report, ci)
                else:
                    self.assertNotRegex(report, ci)
                self.assertIn(status.encode() + b":1", report)
                self.assertIn(b"mergeable MERGEABLE", report)

    def test_unreadable_sources(self):
        self.plan(units={"u": {"state": "RUNNING", "job_id": "a"}})
        self.responses["agents"] = {"error": {"code": "DAEMON_DOWN"}}
        report = self.collect()
        self.assertIn(b"ATTENTION paseo ls unavailable: exit 1", report)
        self.responses["prs"] = {"error": "unreadable"}
        self.responses["runs"] = {"error": "unreadable"}
        self.audit.unlink()
        report = self.collect()
        self.assertIn(b"ATTENTION Tracker: UNAVAILABLE", report)
        self.assertIn(b"ATTENTION Open PRs UNAVAILABLE", report)
        self.assertIn(b"ATTENTION Latest run on main UNAVAILABLE", report)
        sf = self.plan()
        sf.write_text("{broken")
        self.assertIn(b"unreadable state/plan", self.collect())
        sf.write_text(json.dumps({"units": {"u": {"state": "DONE"}}}))
        (sf.parent.parent / "plan.json").write_text('{"units":[{"id":"u","needs":"oops"}]}')
        self.assertIn(b"needs a list", self.collect())

    def test_inputs_and_tools(self):
        self.responses["agents"] = {"error": "daemon down"}
        report = self.collect()
        self.assertNotIn(b"ATTENTION paseo", report)  # no bound active unit
        self.args.extend(["--paseo", "missing", "--gh", "missing", "--branch", "release"])
        self.assertIn(b"Latest run on release UNAVAILABLE", self.collect())
        self.env["HANIG_ORCHESTRATE_DIR"] = str(self.root / "missing")
        self.assertIn(b"Tracker: UNAVAILABLE", self.collect())

    def test_clock_change_delivery(self):
        import datetime
        self.addCleanup(time.tzset)
        self.env["TZ"] = "America/Los_Angeles"
        with mock.patch.dict(os.environ, self.env, clear=True):
            time.tzset()
            try:
                for index, stamp in enumerate(("2026-11-01T08:55:00+00:00", "2026-11-01T09:55:00+00:00")):
                    instant = datetime.datetime.fromisoformat(stamp)
                    class Clock(datetime.datetime):
                        @classmethod
                        def now(cls, tz=None):
                            return instant.astimezone(tz)
                    self.responses["agents"] = [{"id": "a", "status": "running"}] if index else []
                    (self.root / "responses.json").write_text(json.dumps(self.responses))
                    with mock.patch.object(time, "time", return_value=instant.timestamp()), mock.patch.object(datetime, "datetime", Clock):
                        hourly_facts.main(self.args)
                    result = wait_report.take_report(self.output)
                    self.assertEqual(result is not None, bool(index))
            finally:
                os.environ.pop("TZ", None)
                time.tzset()

    def test_pr_list_limit(self):
        gh = self.tools / "gh"
        gh.write_text(gh.read_text().replace("v=d[k]\n",
            "v=d[k]\nif k == 'prs': v=v[:int(sys.argv[sys.argv.index('--limit')+1])]\n"))
        self.responses["prs"] = [
            {"number": n, "title": str(n), "statusCheckRollup": [
                {"conclusion": "FAILURE" if n == 1 else "SUCCESS"}]}
            for n in range(1001, 0, -1)]
        self.assertIn(b"ATTENTION PR 1 ", self.collect())

    def test_unsearchable_plan(self):
        sf = self.plan(units={"u": {"state": "RUNNING"}})
        directory = sf.parent.parent
        directory.chmod(0)
        try:
            self.assertIn(b"ATTENTION wave ", self.collect())
        finally:
            directory.chmod(0o700)

    def test_cancelled_latest_run(self):
        self.responses["runs"] = [{"conclusion": "cancelled"}]
        self.assertIn(b"ATTENTION count: 0; live work: no.", self.collect())

    def test_continued_predecessor(self):
        self.plan(units={"old": {"state": "INCOMPLETE"}, "new": {"state": "DONE"}},
                  plan=[{"id": "old"}, {"id": "new", "author_history": [{"unit": "old"}]}])
        report = self.collect()
        self.assertIn(b"continued by new", report)
        self.assertFalse(any(line.startswith(b"ATTENTION ") for line in report.splitlines()))
        self.assertIn(b"ATTENTION count: 0; live work: no.", report)
        self.plan("other", {"old": {"state": "INCOMPLETE"}}, [{"id": "old", "author_history": [{"unit": "old"}]}])
        report = self.collect()
        self.assertRegex(report, rb"(?m)^ATTENTION other .*?/old: INCOMPLETE")

    def test_unreadable_state_root(self):
        for method in ("iterdir", "is_dir"):
            with mock.patch.object(Path, method, side_effect=PermissionError("fixture denied")):
                lines, ready, leftovers, live, active = hourly_facts.state_facts(self.state, 72, {})
            self.assertTrue(lines[0][0])
            self.assertIn("fixture denied", lines[0][1])

    def test_portable_required_paths(self):
        source = (SCRIPTS / "hourly_facts.py").read_text()
        for forbidden in ("/Users/", "/home/", "brood-lord", "chimera", "andromeda"):
            self.assertNotIn(forbidden, source)
        for script in ("hourly_facts.py", "wait_report.py"):
            result = subprocess.run([sys.executable, str(SCRIPTS / script)], env=self.env, capture_output=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn(b"usage:", result.stderr)

    def test_waiter(self):
        self.collect()
        quiet = self.output / "20000101-00.md"
        (self.output / "latest.md").replace(quiet)
        for path in self.output.glob("2*.md"):
            if path != quiet:
                path.unlink()
        cmd = [sys.executable, str(SCRIPTS / "wait_report.py"), "--output-dir", str(self.output), "--poll-seconds", "0.02"]
        with self.assertRaises(subprocess.TimeoutExpired):
            subprocess.run(cmd, env=self.env, capture_output=True, timeout=0.3)
        self.assertIn(quiet.name, json.loads((self.output / ".seen.json").read_bytes()))
        for index, (count, live) in enumerate([(1, "no"), (0, "yes")], 1):
            path = self.output / ("20000101-0{}.md".format(index))
            path.write_text("Status: ATTENTION count: {}; live work: {}.\n".format(count, live))
            result = subprocess.run(cmd, env=self.env, capture_output=True, timeout=3)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.decode().strip(), str(path))
            with self.assertRaises(subprocess.TimeoutExpired):
                subprocess.run(cmd, env=self.env, capture_output=True, timeout=0.3)

    def test_declaration_registry(self):
        registry = ROOT / "skills" / "hanig-swarm" / "scripts" / "declaration_registry.py"
        for command in ("check-body", "check-references"):
            result = subprocess.run([sys.executable, str(registry), command, "--skill-dir", str(ROOT / "skills/hanig-orchestrate")],
                                    cwd=str(ROOT), capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("report.scheduled-facts", (ROOT / "skills/hanig-orchestrate/SKILL.md").read_text())


if __name__ == "__main__":
    unittest.main()
