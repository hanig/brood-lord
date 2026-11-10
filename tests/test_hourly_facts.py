import hashlib
import itertools
import json
import ntpath
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
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
    plan_units = [{"kind": "code", **unit} for unit in
                  ([{"id": uid} for uid in (units or {})] if plan is None else plan)]
    (directory / "plan.json").write_text(json.dumps({"units": plan_units}))
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

  def test_foreign_agents_do_not_wake(self):
    # Exhaustive bound: 3 running statuses x 3 foreign cwd forms.
    for status, cwd in itertools.product(
        ("running", "working", "busy"),
        (None, str(self.root / "foreign"), str(self.repo) + "2")):
      with self.subTest(status=status, cwd=cwd):
        self.responses["agents"] = [{"id": "foreign", "status": status, "cwd": cwd}]
        report = self.collect()
        self.assertNotIn(b"agent foreign", report)
        self.assertIn(b"ATTENTION count: 0; live work: no.", report)
        summaries = [line for line in report.splitlines() if b"agents of other projects" in line]
        self.assertEqual(summaries, [b"- 1 agents of other projects not listed"])

  def test_agent_containment_uses_directory_identity(self):
    # Simulate a case-insensitive filesystem without case-folding decisions.
    base = self.root.resolve()
    root = str(base / "Project")
    child = str(base / "project" / "child")
    def samefile(left, right):
      return str(left) == root and str(right) == str(base / "project")
    with mock.patch.object(hourly_facts.os.path, "samefile", side_effect=samefile):
      self.assertTrue(hourly_facts.project_agent({"id": "ours", "cwd": child}, set(), [root]))

  def test_case_variant_project_cwd_on_case_insensitive_volume(self):
    alternate = self.root / "REPO"
    if not alternate.exists() or not os.path.samefile(alternate, self.repo):
      self.skipTest("requires a case-insensitive fixture volume")
    self.responses["agents"] = [{"id": "ours", "status": "running", "cwd": str(alternate / "child")}]
    report = self.collect()
    self.assertIn(b"- agent ours : running", report)
    self.assertIn(b"ATTENTION count: 0; live work: yes.", report)

  def test_agent_roots_on_distinct_drives(self):
    # Synthetic Windows path semantics on this POSIX operator host.
    with mock.patch.object(hourly_facts, "os", SimpleNamespace(path=ntpath)):
      self.assertTrue(hourly_facts.project_agent(
        {"id": "ours", "cwd": r"D:\agents\work"}, set(),
        [r"C:\repo", r"C:\state", r"D:\agents"]))

  def test_deep_symlink_agent_paths_do_not_abort_collection(self):
    # Fixed bound: interpreter recursion limit + 100 symlinks, two sources.
    target = self.root / "target"
    target.mkdir()
    chain = self.root / "chain"
    chain.mkdir()
    count = sys.getrecursionlimit() + 100
    for i in range(count):
      (chain / str(i)).symlink_to(str(i + 1) if i + 1 < count else str(target))
    self.responses["agents"] = [{"id": "foreign", "status": "idle", "cwd": str(chain / "0")}]
    for extra_root in (False, True):
      with self.subTest(extra_root=extra_root):
        if extra_root:
          self.args += ["--agent-root", str(chain / "0" / "other")]
        report = self.collect()
        self.assertNotIn(b"agent foreign", report)
        self.assertIn(b"- 1 agents of other projects not listed", report)
        self.assertIn(b"ATTENTION count: 0; live work: no.", report)

  def test_agent_cwd_roots(self):
    extra, second = self.root / "extra", self.root / "second"
    self.args += ["--agent-root", str(extra), "--agent-root", str(second)]
    # Exhaustive bound: 4 roots x equal/descendant x absolute/tilde = 16.
    self.env["HOME"] = str(self.root)
    for root, child, tilde in itertools.product(
        (self.repo, self.state, extra, second), (False, True), (False, True)):
      cwd = root / "child" if child else root
      if tilde:
        cwd = "~/" + str(cwd.relative_to(self.root))
      with self.subTest(root=root, child=child, tilde=tilde):
        self.responses["agents"] = [{"id": "ours", "status": "running", "cwd": str(cwd)}]
        report = self.collect()
        self.assertIn(b"- agent ours : running", report)
        self.assertIn(b"ATTENTION count: 0; live work: yes.", report)
        self.assertNotIn(b"agents of other projects", report)
    alias = self.root / "alias"
    alias.symlink_to(self.repo, target_is_directory=True)
    self.responses["agents"] = [{"id": "linked", "status": "running", "cwd": str(alias / "child")}]
    self.assertIn(b"- agent linked : running", self.collect())

  def test_only_active_code_bindings_scope_agents(self):
    # Exhaustive bound: 3 kinds x 4 states x 2 job-id types x 2 cwd forms = 48.
    for kind, state, job_id, cwd in itertools.product(
        ("code", "slurm", "pipeline"), ("RUNNING", "SUBMITTED", "DONE", "FAILED"),
        (123, "123"), (None, str(self.root / "foreign"))):
      with self.subTest(kind=kind, state=state, job_id=job_id, cwd=cwd):
        self.plan(units={"work": {"state": state, "job_id": job_id}},
                  plan=[{"id": "work", "kind": kind}])
        self.responses["agents"] = [{"id": str(job_id), "status": "idle", "cwd": cwd}]
        report = self.collect()
        belongs = kind == "code" and state in {"RUNNING", "SUBMITTED"}
        self.assertEqual(b"- agent 123 : idle" in report, belongs)
        self.assertEqual(b"- 1 agents of other projects not listed" in report, not belongs)
        if belongs:
          self.assertIn(b"ATTENTION count: 1; live work: yes.", report)
          self.assertIn(b"bound agent 123: idle", report)

  def test_out_of_plan_units_are_one_quiet_summary(self):
    for old in (False, True):
      with self.subTest(old=old):
        self.plan(units={"done": {"state": "DONE"},
          "left-held": {"state": "HELD"}, "left-held2": {"state": "HELD"},
          "left-failed": {"state": "FAILED"}, "left-human": {"state": "NEEDS_HUMAN"},
          "left-active": {"state": "RUNNING"}}, plan=[{"id": "done"}], old=old)
        report = self.collect()
        summaries = [line for line in report.splitlines() if b"not in plan.json:" in line]
        self.assertEqual(len(summaries), 1)
        self.assertTrue(summaries[0].startswith(b"- wave "))
        self.assertIn(b"1 FAILED, 2 HELD, 1 NEEDS_HUMAN, 1 RUNNING", summaries[0])
        self.assertNotIn(b"left-", report)
        self.assertIn(b"ATTENTION count: 0; live work: no.", report)

  def test_held_waiting_and_failed_chain(self):
    # Exhaustive declared roots: both active states, DONE, all four decision
    # states, and READY_FOR_PR (which alerts itself but not its HELD followers).
    for root in ("RUNNING", "SUBMITTED", "DONE", "FAILED", "FAILED_EVIDENCE",
                 "NEEDS_HUMAN", "INCOMPLETE", "READY_FOR_PR"):
      with self.subTest(root=root):
        self.plan(units={"root": {"state": root, "job_id": "a"},
                         "first": {"state": "HELD"}, "last": {"state": "HELD"}},
          plan=[{"id": "root"}, {"id": "first", "needs": ["root"]},
                {"id": "last", "needs": ["first"]}])
        self.responses["agents"] = [{"id": "a", "status": "running"}]
        report = self.collect()
        for uid in (b"first", b"last"):
          line = next(line for line in report.splitlines() if b"/" + uid + b": HELD" in line)
          self.assertEqual(line.startswith(b"ATTENTION "),
                           root in {"FAILED", "FAILED_EVIDENCE", "NEEDS_HUMAN", "INCOMPLETE"})
          self.assertIn(b"waiting for", line)
        if root != "DONE":
          self.assertIn(("waiting for root ({})".format(root)).encode(), report)

  def test_held_graphs_exhaustive(self):
    # Exhaustive bound: 4**2 state pairs x 2**4 directed edge sets = 256 graphs,
    # including self loops and two-node cycles. Oracle searches each HELD's
    # reachable HELD needs for FAILED, independently of propagation direction.
    ids = ("a", "b")
    edges = tuple(itertools.product(ids, repeat=2))
    for statuses, bits in itertools.product(
        itertools.product(("HELD", "FAILED", "RUNNING", "DONE"), repeat=2),
        itertools.product((False, True), repeat=4)):
      states = dict(zip(ids, statuses))
      needs = {uid: [v for (u, v), bit in zip(edges, bits) if bit and u == uid] for uid in ids}
      self.plan(units={uid: {"state": status, "job_id": "agent"} for uid, status in states.items()},
                plan=[{"id": uid, "needs": needs[uid]} for uid in ids])
      lines = hourly_facts.state_facts(self.state, 72, {"agent": {"status": "running"}})[0]
      for uid in ids:
        if states[uid] != "HELD":
          continue
        seen, pending, expected = set(), list(needs[uid]), False
        while pending:
          need = pending.pop()
          if need in seen:
            continue
          seen.add(need)
          expected = expected or states[need] == "FAILED"
          if states[need] == "HELD":
            pending.extend(needs[need])
        flag, line = next((flag, line) for flag, line in lines if "/{}: HELD".format(uid) in line)
        self.assertEqual(flag, expected, (states, needs, uid, line))

  def test_held_long_chain_and_cycle(self):
    # Fixed bound beyond the interpreter recursion limit: 1,200 HELD units.
    for failed in (False, True):
      units = {str(i): {"state": "HELD"} for i in range(1200)}
      units["root"] = {"state": "FAILED" if failed else "DONE"}
      plan = [{"id": str(i), "needs": [str(i + 1) if i < 1199 else "root"]}
              for i in range(1200)] + [{"id": "root"}]
      self.plan(units=units, plan=plan)
      lines = hourly_facts.state_facts(self.state, 72, {})[0]
      self.assertEqual(sum(flag for flag, _ in lines), 1201 if failed else 0)
    self.plan(units={"a": {"state": "HELD"}, "b": {"state": "HELD"}},
              plan=[{"id": "a", "needs": ["b"]}, {"id": "b", "needs": ["a"]}])
    self.assertIn(b"ATTENTION count: 0", self.collect())

  def test_optional_tracker_pair(self):
    self.args = self.args[:6]
    report = self.collect()
    self.assertEqual(report.split(b"## 2. Tracker\n\n")[1].split(b"\n## 3.")[0],
                     b"Tracker: UNAVAILABLE (no binding configured)\n")
    self.assertIn(b"ATTENTION count: 0; live work: no.", report)
    for option, value in (("--binding", self.binding), ("--audit", self.audit)):
      result = subprocess.run([sys.executable, str(SCRIPTS / "hourly_facts.py")] +
        self.args + [option, str(value)], env=self.env, capture_output=True, timeout=10)
      self.assertEqual(result.returncode, 2)
      self.assertIn(b"--binding and --audit must be given together", result.stderr)
    self.args += ["--binding", str(self.binding), "--audit", str(self.audit)]
    self.audit.unlink()
    self.assertIn(b"ATTENTION Tracker: UNAVAILABLE", self.collect())

  def test_out_of_plan_dependencies_keep_recorded_states(self):
    # Exhaustive bound: DONE and all four decision-root states outside the plan.
    for status in ("DONE", "FAILED", "FAILED_EVIDENCE", "NEEDS_HUMAN", "INCOMPLETE"):
      with self.subTest(status=status):
        self.plan(units={"done": {"state": "DONE"}, "old": {"state": status},
                         "held": {"state": "HELD"}, "held_done": {"state": "HELD"}},
          plan=[{"id": "ready", "needs": ["done"]},
                {"id": "held", "needs": ["old"]}, {"id": "held_done", "needs": ["done"]}])
        report = self.collect()
        self.assertIn(b"ATTENTION ready: wave/ready", report)
        line = next(line for line in report.splitlines() if b"/held: HELD" in line)
        self.assertEqual(line.startswith(b"ATTENTION "), status != "DONE")
        if status != "DONE":
          self.assertIn(("old ({})".format(status)).encode(), line)
        line = next(line for line in report.splitlines() if b"/held_done: HELD" in line)
        self.assertTrue(line.startswith(b"- "))
        self.assertIn(b"no non-DONE needs", line)
        self.assertNotIn(b"(UNALLOCATED)", report)
        self.assertNotRegex(report, rb"(?m)^ATTENTION .*?/(old|done):")
        self.assertEqual(sum(b"not in plan.json:" in line for line in report.splitlines()), 1)

  def test_unreadable_plan_preserves_active_diagnostics(self):
    # Exhaustive bound: unreadable and invalid plans x both active states.
    for denied, state in itertools.product((False, True), ("RUNNING", "SUBMITTED")):
      with self.subTest(denied=denied, state=state):
        sf = self.plan(units={"u": {"state": state, "job_id": "absent"}}, old=True)
        plan_path = sf.parent.parent / "plan.json"
        if not denied:
          plan_path.write_text("{broken")
        read_bytes = Path.read_bytes
        def read(path):
          if denied and path == plan_path:
            raise PermissionError("fixture denied")
          return read_bytes(path)
        (self.root / "responses.json").write_text(json.dumps(self.responses))
        with mock.patch.object(Path, "read_bytes", read), mock.patch.dict(
            os.environ, self.env, clear=True), mock.patch("builtins.print"):
          hourly_facts.main(self.args)
        report = (self.output / "latest.md").read_bytes()
        self.assertRegex(report, rb"(?m)^ATTENTION wave .*unreadable plan")
        self.assertNotIn(b"/u:", report)
        self.assertIn(b"plan membership unavailable: 1 " + state.encode(), report)
        self.assertNotIn(b"bound agent", report)
        self.assertIn(b"live work: yes.", report)
        self.assertNotIn(b"not in plan.json", report)

  def test_agent_liveness_only_for_code(self):
    # Exhaustive bound: 3 kinds x 2 active states x 2 job-id types x 2 agent
    # observations = 24 fixtures, including scheduler ids absent from Paseo.
    for kind, state, job_id, present in itertools.product(
        ("code", "slurm", "pipeline"), ("RUNNING", "SUBMITTED"),
        (3684904, "3684904"), (False, True)):
      with self.subTest(kind=kind, state=state, job_id=job_id, present=present):
        self.plan(units={"work": {"state": state, "job_id": job_id}},
                  plan=[{"id": "work", "kind": kind}])
        self.responses["agents"] = [{"id": str(job_id), "status": "running"}] if present else []
        report = self.collect()
        line = next(line for line in report.splitlines() if b"/work: " in line)
        self.assertEqual(line.startswith(b"ATTENTION "), kind == "code" and not present)
        self.assertIn(b"3684904", line)
        if kind == "code":
          self.assertIn(b"bound agent", line)
        else:
          self.assertIn(b"job id 3684904", line)
          self.assertNotIn(b"bound agent", line)
          self.assertNotIn(b"ABSENT", line)
          self.assertIn(b"ATTENTION count: 0", report)

  def test_scheduler_root_keeps_held_chain_waiting(self):
    self.plan(units={"seg1": {"state": "RUNNING", "job_id": 3684904},
                     "seg2": {"state": "HELD"}, "seg3": {"state": "HELD"}},
      plan=[{"id": "seg1", "kind": "slurm"},
            {"id": "seg2", "kind": "slurm", "needs": ["seg1"]},
            {"id": "seg3", "kind": "slurm", "needs": ["seg2"]}])
    report = self.collect()
    self.assertIn(b"ATTENTION count: 0", report)
    self.assertIn(b"waiting for seg1 (RUNNING)", report)
    self.assertIn(b"waiting for seg2 (HELD)", report)
    self.assertIn(b"job id 3684904", report)
    self.assertNotIn(b"bound agent", report)

  def test_unreadable_plan_never_infers_membership_or_held_decision(self):
    # Exhaustive bound: empty plan with a leftover FAILED unit, and HELD over
    # DONE. Permission denial makes membership/needs unknown in both cases.
    for status, plan in (("FAILED", []),
                         ("HELD", [{"id": "u", "needs": ["done"]}])):
      with self.subTest(status=status):
        sf = self.plan(units={"u": {"state": status}, "done": {"state": "DONE"}}, plan=plan)
        plan_path = sf.parent.parent / "plan.json"
        read_bytes = Path.read_bytes
        def read(path):
          if path == plan_path:
            raise PermissionError("fixture denied")
          return read_bytes(path)
        (self.root / "responses.json").write_text(json.dumps(self.responses))
        with mock.patch.object(Path, "read_bytes", read), mock.patch.dict(
            os.environ, self.env, clear=True), mock.patch("builtins.print"):
          hourly_facts.main(self.args)
        report = (self.output / "latest.md").read_bytes()
        self.assertRegex(report, rb"(?m)^ATTENTION wave .*unreadable plan")
        self.assertNotIn(b"/u:", report)
        self.assertNotIn(b"not in plan.json", report)
        self.assertIn(b"plan membership unavailable:", report)
        self.assertIn(("1 " + status).encode(), report)

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

  def test_old_coordinator_shapes_are_quiet(self):
    self.plan(units={"null": {"state": None}, "missing": {},
                     "done": {"state": "DONE"}},
         plan=[{"id": "null"}, {"id": "missing"}, {"id": "done"}], old=True)
    state = self.state / "journals" / "state"
    state.mkdir(parents=True)
    (state / "outbox.jsonl").write_text("{}\n")
    report = self.collect()
    self.assertFalse(any(line.startswith(b"ATTENTION ") for line in report.splitlines()))
    self.assertIn(b"ATTENTION count: 0; live work: no.", report)
    self.assertIn(b"Older state dirs hold 2 non-terminal units", report)

  def test_non_plan_state_directories_are_counted(self):
    for name in ("journals", "other"):
      state = self.state / name / "state"
      state.mkdir(parents=True)
      (state / "outbox.jsonl").write_text("{}\n")
    report = self.collect()
    self.assertFalse(any(line.startswith(b"ATTENTION ") for line in report.splitlines()))
    counts = [line for line in report.splitlines() if b"without state/swarm-state.json" in line]
    self.assertEqual(len(counts), 1)
    self.assertTrue(counts[0].startswith(b"- Skipped 2 directories without state/swarm-state.json"))

  def test_unallocated_shapes_have_identical_readiness(self):
    # Exhaustive finite bound: 3 unallocated representations x 2 dependency states.
    for unit, dependency in itertools.product(
        ({"state": None}, {}, {"state": "UNALLOCATED"}), ("DONE", "RUNNING")):
      with self.subTest(unit=unit, dependency=dependency):
        self.plan(units={"work": unit, "dependency": {"state": dependency, "job_id": "a"}},
             plan=[{"id": "work", "needs": ["dependency"]}, {"id": "dependency"}])
        self.responses["agents"] = [{"id": "a", "status": "running"}]
        report = self.collect()
        attention = [line for line in report.splitlines() if line.startswith(b"ATTENTION ")]
        self.assertEqual(len(attention), int(dependency == "DONE"))
        if attention:
          self.assertTrue(attention[0].startswith(b"ATTENTION ready: wave/work"))
        self.assertIn(b"/work: UNALLOCATED", report)
        self.assertNotIn(b"unreadable state", report)

  def test_malformed_unit_states_still_need_attention(self):
    # Exhaustive declared malformed fixtures: 5 non-string/non-null states,
    # plus 3 non-object units (8 cases total).
    for unit in ({"state": 3}, {"state": False}, {"state": []},
                 {"state": {}}, {"state": 1.5}, None, [], "DONE"):
      with self.subTest(unit=unit):
        self.plan(units={"u": unit})
        self.assertRegex(self.collect(), rb"(?m)^ATTENTION wave .*unreadable state/plan")

  def test_existing_unreadable_state_file_needs_attention(self):
    sf = self.plan()
    (self.root / "responses.json").write_text(json.dumps(self.responses))
    read_bytes = Path.read_bytes
    def denied(path):
      if path == sf:
        raise PermissionError("fixture denied")
      return read_bytes(path)
    # Inject the read error rather than assuming chmod removes read access:
    # privileged collectors can read mode-000 files. Exercise the real CLI
    # and inspect its written report, including the ATTENTION prefix.
    with mock.patch.object(Path, "read_bytes", denied), mock.patch.dict(
        os.environ, self.env, clear=True), mock.patch("builtins.print"):
      hourly_facts.main(self.args)
    report = (self.output / "latest.md").read_bytes()
    self.assertRegex(report, rb"(?m)^ATTENTION wave .*unreadable state/plan.*fixture denied")

  def test_existing_state_directory_needs_attention(self):
    sf = self.plan()
    sf.unlink()
    sf.mkdir()
    self.assertRegex(self.collect(), rb"(?m)^ATTENTION wave .*unreadable state/plan")

  def test_triggers_exhaustive(self):
    # Exhaustive finite product: 11 unit states x 5 agent conditions = 55 cases.
    states = ["UNALLOCATED", "SUBMITTED", "RUNNING", "DONE", "ABANDONED", "SUPERSEDED", "FAILED", "NEEDS_HUMAN", "FAILED_EVIDENCE", "INCOMPLETE", "READY_FOR_PR"]
    for state, agent in itertools.product(states, [None, "idle", "running", "working", "busy"]):
      with self.subTest(state=state, agent=agent):
        units = {"u": {"state": state, "job_id": "a"}}
        self.plan(units=units)
        agents = {} if agent is None else {"a": {"status": agent}}
        lines, ready, leftovers, live, active = hourly_facts.state_facts(self.state, 72, agents)
        expected = state in {"FAILED", "NEEDS_HUMAN", "FAILED_EVIDENCE", "INCOMPLETE", "READY_FOR_PR"} or (state in hourly_facts.ACTIVE and agent in {None, "idle"})
        self.assertEqual(any(flag for flag, line in lines), expected)
    self.plan(units={"u": {"state": "SUBMITTED"}})
    self.assertTrue(any(f for f, _ in hourly_facts.state_facts(self.state, 72, {})[0]))

  def test_ready_and_old(self):
    self.plan("live", {"done": {"state": "DONE"}, "wait": {"state": "RUNNING", "job_id": "a"}},
         [{"id": "done"}, {"id": "wait"}, {"id": "ready", "needs": ["done"]}, {"id": "blocked", "needs": ["wait"]}], old=True)
    self.plan("old", {"invisible": {"state": "FAILED"}}, old=True)
    report = self.collect()
    self.assertIn(b"ATTENTION ready: live/ready", report)
    self.assertNotIn(b"ready: live/blocked", report)
    self.assertIn(b"bound agent a: ABSENT", report)
    self.assertIn(b"Older state dirs hold 1 non-terminal units", report)
    self.assertNotIn(b"invisible", report)

  def test_checks_all_failure_triggers(self):
    for status in ("FAILURE", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "ERROR"):
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
    self.assertIn(b"ATTENTION paseo", report)
    self.args.extend(["--paseo", "missing", "--gh", "missing", "--branch", "release"])
    self.assertIn(b"Latest run on release UNAVAILABLE", self.collect())
    self.env["HANIG_ORCHESTRATE_DIR"] = str(self.root / "missing")
    self.assertIn(b"Tracker: UNAVAILABLE", self.collect())

  def test_agent_source_unknown(self):
    self.plan(units={"u": {"state": "RUNNING", "job_id": "a"}})
    self.responses["agents"] = {"error": "daemon down"}
    report = self.collect()
    self.assertIn(b"bound agent a: UNKNOWN", report)
    self.assertNotIn(b"bound agent a: ABSENT", report)
  def test_unbound_source(self):
    self.responses["agents"] = {"error": "daemon down"}
    self.assertIn(b"ATTENTION paseo ls unavailable", self.collect())

  def test_implicit_units(self):
    sf = self.plan(units={"gate": {"state": "ABANDONED"}},
           plan=[{"id": "gate"}, {"id": "work", "needs": ["gate"]}])
    report = self.collect()
    with self.subTest("live implicit"):
      self.assertIn(b"/work: UNALLOCATED", report)
      self.assertIn(b"live work: yes.", report)
    os.utime(sf, (time.time() - 100 * 3600,) * 2)
    report = self.collect()
    with self.subTest("old count"):
      self.assertIn(b"Older state dirs hold 1 non-terminal", report)
      self.assertNotIn(b"/work:", report)
    with self.subTest("observation"):
      self.assertIn(b"not listed (observed ", report)
    self.plan(units={"new": {"state": "DONE"}},
         plan=[{"id": "old"}, {"id": "new", "author_history": [{"unit": "old"}]}])
    report = self.collect()
    with self.subTest("continued"):
      self.assertIn(b"/old: UNALLOCATED; continued by new", report)
    with self.subTest("continued ready"):
      self.assertIn(b"- ready: wave/old", report)
      self.assertIn(b"ATTENTION count: 0; live work: no.", report)

  def test_surrogate_state(self):
    self.plan(units={"u": {"state": chr(0xd800)}})
    report = self.collect()
    self.assertIn(b"ATTENTION wave ", report)
    self.assertIn(b"\\ud800", report)

  def test_positive_offset_order(self):
    self.output.mkdir()
    names = ["20261025-02+0200.md", "20261025-02+0100.md"]
    for name in names:
      (self.output / name).write_text("Status: ATTENTION count: 1; live work: no.\n")
    self.assertEqual([wait_report.take_report(self.output).name for _ in names], names)

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
          self.responses["agents"] = [{"id": "a", "status": "running", "cwd": str(self.repo)}] if index else []
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

  def test_startup_failure(self):
    self.responses["runs"] = [{"conclusion": "startup_failure"}]
    self.assertRegex(self.collect(), rb"(?m)^ATTENTION None startup_failure")

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

  def test_active_continued_predecessor_wakes(self):
    # Exhaustive bound: both active states, both present and absent bound agent.
    for state, present in itertools.product(("RUNNING", "SUBMITTED"), (True, False)):
      with self.subTest(state=state, present=present):
        self.plan(units={"old": {"state": state, "job_id": "a"}, "new": {"state": "DONE"}},
          plan=[{"id": "old"}, {"id": "new", "author_history": [{"unit": "old"}]}], old=True)
        self.responses["agents"] = [{"id": "a", "status": "idle"}] if present else []
        report = self.collect()
        self.assertIn(b"live work: yes.", report)
        self.assertRegex(report, rb"(?m)^ATTENTION wave .*?/old: " + state.encode())
        self.assertNotIn(b"continued by new", report)
        # A new waiter lineage for each fixture; persisted record must consume once.
        seen = self.output / ".seen.json"
        if seen.exists():
          seen.unlink()
        self.assertIsNotNone(wait_report.take_report(self.output))
        self.assertIsNone(wait_report.take_report(self.output))

  def test_deep_json_sources(self):
    # Fixed adversarial bound: 2,000 nested JSON arrays, including Python 3.9.
    deep = "[" * 2000 + "]" * 2000
    sf = self.plan()
    for source, message in ((sf, b"unreadable state/plan"),
                            (sf.parent.parent / "plan.json", b"unreadable plan"),
                            (self.audit, b"Tracker: UNAVAILABLE")):
      with self.subTest(source=source):
        original = source.read_bytes()
        try:
          source.write_text(deep)
          self.assertRegex(self.collect(), rb"(?m)^ATTENTION .*" + message)
        finally:
          source.write_bytes(original)
    self.output.mkdir(exist_ok=True)
    seen = self.output / ".seen.json"
    seen.write_text(deep)
    with self.assertRaisesRegex(ValueError, "invalid seen record"):
      wait_report.take_report(self.output)
    self.assertEqual(seen.read_text(), deep)

  def test_calendar_report_validation(self):
    self.output.mkdir()
    valid = self.output / "20261109-06+0000.md"
    valid.write_text("Status: ATTENTION count: 1; live work: no.\n")
    self.assertEqual(wait_report.take_report(self.output), valid)
    seen = self.output / ".seen.json"
    before = seen.read_bytes()
    # Exhaustive declared invalid date/hour/offset/structure fixtures (9).
    for name in ("20261399-99+9999.md", "20260229-06+0000.md",
                 "20261131-06+0000.md", "20261109-24+0000.md",
                 "20261109-06+2400.md", "20261109-06+0060.md",
                 "20261109-06-2460.md", "20261109-06.md",
                 "2026119-06+0000.md"):
      with self.subTest(name=name):
        invalid = self.output / name
        invalid.write_bytes(valid.read_bytes())
        self.assertIsNone(wait_report.take_report(self.output))
        self.assertEqual(seen.read_bytes(), before)
        seen.write_text(json.dumps([name]))
        corrupted = seen.read_bytes()
        with self.assertRaisesRegex(ValueError, "invalid seen record"):
          wait_report.take_report(self.output)
        self.assertEqual(seen.read_bytes(), corrupted)
        seen.write_bytes(before)
        invalid.unlink()
    # Valid leap day and edge offsets must still deliver.
    for name in ("20240229-23+2359.md", "20261109-00-2359.md"):
      path = self.output / name
      path.write_bytes(valid.read_bytes())
      self.assertEqual(wait_report.take_report(self.output), path)

  def test_completed_pr_conclusions(self):
    # Exhaustive declared 12 conclusions, plus 3 pending representations.
    for conclusion in ("SUCCESS", "NEUTRAL", "SKIPPED", "STARTUP_FAILURE",
                       "FAILURE", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED",
                       "ERROR", "STALE", "UNKNOWN_FUTURE", ""):
      with self.subTest(conclusion=conclusion):
        self.responses["prs"] = [{"number": 1, "title": "test",
          "statusCheckRollup": [{"status": "COMPLETED", "conclusion": conclusion}]}]
        report = self.collect()
        self.assertEqual(b"ATTENTION PR 1 " in report,
                         conclusion not in {"SUCCESS", "NEUTRAL", "SKIPPED"})
    for check in ({"status": "IN_PROGRESS"}, {"status": "QUEUED"}, {"state": "PENDING"}):
      self.responses["prs"][0]["statusCheckRollup"] = [check]
      self.assertNotIn(b"ATTENTION PR 1 ", self.collect())

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
    quiet = self.output / "20000101-00+0000.md"
    (self.output / "latest.md").replace(quiet)
    for path in self.output.glob("2*.md"):
      if path != quiet:
        path.unlink()
    cmd = [sys.executable, "-c", "import runpy,sys,time;time.sleep(.5);sys.path.insert(0,sys.argv[1].rsplit('/',1)[0]);runpy.run_path(sys.argv.pop(1),run_name='__main__')", str(SCRIPTS / "wait_report.py"), "--output-dir", str(self.output), "--poll-seconds", "0.02"]
    with self.assertRaises(subprocess.TimeoutExpired):
      subprocess.run(cmd, env=self.env, capture_output=True, timeout=3)
    self.assertIn(quiet.name, json.loads((self.output / ".seen.json").read_bytes()))
    for index, (count, live) in enumerate([(1, "no"), (0, "yes")], 1):
      path = self.output / ("20000101-0{}+0000.md".format(index))
      path.write_text("Status: ATTENTION count: {}; live work: {}.\n".format(count, live))
      result = subprocess.run(cmd, env=self.env, capture_output=True, timeout=3)
      self.assertEqual(result.returncode, 0, result.stderr)
      self.assertEqual(result.stdout.decode().strip(), str(path))
      with self.assertRaises(subprocess.TimeoutExpired):
        subprocess.run(cmd, env=self.env, capture_output=True, timeout=3)

  def test_invalid_seen_record(self):
    self.output.mkdir()
    report = self.output / "20261109-06+0000.md"
    report.write_text("Status: ATTENTION count: 1; live work: no.\n")
    self.assertEqual(wait_report.take_report(self.output), report)
    seen = self.output / ".seen.json"
    # Exhaustive declared malformed fixtures: JSON types, entries and syntax.
    for data in (b"{}", b'"20261109-06+0000.md"', b"null", b"1", b"true",
                 b"[1]", b"[null]", b"[[]]", b'["../report.md"]', b"{", b"\xff"):
      with self.subTest(data=data):
        seen.write_bytes(data)
        before = {p.name: p.read_bytes() for p in self.output.iterdir()}
        result = subprocess.run(
          [sys.executable, str(SCRIPTS / "wait_report.py"),
           "--output-dir", str(self.output), "--poll-seconds", "0.02"],
          env=self.env, capture_output=True, timeout=3)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout, b"")
        self.assertIn(str(seen).encode(), result.stderr)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.output.iterdir()})

  def test_declaration_registry(self):
    registry = ROOT / "skills" / "hanig-swarm" / "scripts" / "declaration_registry.py"
    for command in ("check-body", "check-references"):
      result = subprocess.run([sys.executable, str(registry), command, "--skill-dir", str(ROOT / "skills/hanig-orchestrate")],
                  cwd=str(ROOT), capture_output=True)
      self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
    self.assertIn("report.scheduled-facts", (ROOT / "skills/hanig-orchestrate/SKILL.md").read_text())


if __name__ == "__main__":
  unittest.main()
