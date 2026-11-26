"""Native lor/<model> coding agents beside Paseo, offline.

Only the two service CLIs are fakes: a `lor` and a `paseo` script on a closed
PATH that record every call. The coordinator, unit.py (as a subprocess), Git
worktrees, bindings, judging and state persistence are real. No live Lor or
Paseo call is possible from this module.
"""
import contextlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "hanig-swarm" / "scripts"
ORCH = ROOT / "skills" / "hanig-orchestrate" / "scripts"
GATE = ROOT / "skills" / "hanig-review-gate" / "scripts"
for _path in (SCRIPTS, ORCH, GATE):
    sys.path.insert(0, str(_path))
import swarm as S  # noqa: E402
import lor_io as LIO  # noqa: E402
import swarm_authors as SA  # noqa: E402
import swarm_routing as SR  # noqa: E402
import swarm_evidence as SE  # noqa: E402
import swarm_plan_rules as SPR  # noqa: E402
import review_panels  # noqa: E402
import wait_event  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scheduler_fixture import (  # noqa: E402
    closed_bin, codex_environment, isolated_module_path, state_environment)
from scheduler_fixture import cleanup_module_path as tearDownModule  # noqa: E402,F401

ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@x",
           GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@x")
PARENT = "4ef0c1b2-0000-4000-8000-000000000001"
LOR_MODEL = "openai/gpt-6.2-sol"
LOR_PROVIDER = "lor/" + LOR_MODEL
SECRET = "fake-lor-session-token-SECRET-0123456789abcdef"
API_SECRET = "fake-openai-api-key-SECRET-0123456789abcdef"
AUTH_FAILURE = "401 Unauthenticated: session token expired"


def setUpModule():
    isolated_module_path()


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          env=ENV, capture_output=True, text=True).stdout.strip()


FAKE_LOR = r'''
import json, os, re, sys
path = os.environ["FAKE_LOR_STATE"]
with open(path) as fh:
    c = json.load(fh)
argv = sys.argv[1:]
stdin = sys.stdin.read() if "@-" in argv else ""
c.setdefault("calls", []).append({"argv": argv, "stdin": stdin,
                                  "env": sorted(os.environ)})
def opt(name):
    return argv[argv.index(name) + 1] if name in argv else None
def save():
    with open(path + ".tmp", "w") as fh:
        json.dump(c, fh)
    os.replace(path + ".tmp", path)
if c.get("fail"):
    save(); sys.stderr.write(c["fail"] + "\n"); sys.exit(1)
if argv[:2] == ["session", "start"]:
    sid = c["next_ids"].pop(0)
    c["sessions"].append({
        "session_id": sid, "parent_session_id": os.environ.get("LOR_SESSION_ID"),
        "title": opt("--title"), "canonical_model_id": opt("--canonical-model-id"),
        "sandbox": {"working_directory": opt("--working-directory")},
        "running": True, "last_finish_reason": None})
    save(); print(json.dumps({"session_id": sid}))
elif argv[:2] == ["session", "list"]:
    parent, pattern = opt("--parent-session-id"), opt("--title-pattern")
    found = [s for s in c["sessions"] if s.get("parent_session_id") == parent
             and (pattern is None or re.search(pattern, s.get("title") or ""))]
    save(); print(json.dumps({"sessions": found}))
elif argv[:2] == ["session", "stop"]:
    for s in c["sessions"]:
        if s["session_id"] == opt("--session-id"):
            s.update(running=False, stopped_at="2026-11-26T00:00:00Z",
                     last_finish_reason="cancelled")
    save(); print("{}")
elif argv[:2] == ["session", "send"]:
    save(); print(json.dumps({"message_id": "m-1", "outcome": "queued"}))
else:
    save(); sys.exit(2)
'''

FAKE_PASEO = r'''
import json, os, sys
path = os.environ["FAKE_PASEO_STATE"]
with open(path) as fh:
    c = json.load(fh)
argv = sys.argv[1:]
c.setdefault("calls", []).append(argv)
def save():
    with open(path + ".tmp", "w") as fh:
        json.dump(c, fh)
    os.replace(path + ".tmp", path)
if argv[:1] == ["run"]:
    cwd = argv[argv.index("--cwd") + 1]
    agent = c["next_ids"].pop(0)
    save()
    print("Created workspace wks_%s - fixture" % os.path.basename(cwd))
    print(json.dumps({"agentId": agent, "cwd": cwd}))
elif argv[:1] == ["inspect"]:
    agent = [a for a in argv[1:] if not a.startswith("--")][0]
    save(); print(json.dumps({"status": c["status"].get(agent, "running")}))
elif argv[:2] in (["ls", "--json"], ["workspace", "ls"]):
    save(); print("[]")
elif argv[:1] == ["send"]:
    save(); print("{}")
else:
    save(); sys.exit(127)
'''


class LorFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="lor-backend-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.bin = Path(closed_bin(self.tmp / "bin"))
        for name, body in (("lor", FAKE_LOR), ("paseo", FAKE_PASEO)):
            script = self.bin / name
            script.write_text("#!" + sys.executable + "\n" + body)
            script.chmod(0o755)
        self.lor_state = self.tmp / "lor-state.json"
        self.paseo_state = self.tmp / "paseo-state.json"
        self.lor_state.write_text(json.dumps({
            "sessions": [], "calls": [],
            "next_ids": ["51a0b2c3-0000-4000-8000-0000000000%02d" % i
                         for i in range(1, 20)]}))
        self.paseo_state.write_text(json.dumps({
            "status": {}, "calls": [],
            "next_ids": ["aaaaaaaa-0000-4000-8000-0000000000%02d" % i
                         for i in range(1, 20)]}))
        patch = mock.patch.dict(os.environ, {
            "HOME": str(self.tmp),
            "PATH": str(self.bin),
            "LOR_SESSION_ID": PARENT,
            "LOR_SESSION_TOKEN": SECRET,
            "OPENAI_API_KEY": API_SECRET,
            "FAKE_LOR_STATE": str(self.lor_state),
            "FAKE_PASEO_STATE": str(self.paseo_state),
            **state_environment(self.tmp / "state-home"),
            **codex_environment(self.tmp / "codex-fixture"),
        })
        patch.start()
        self.addCleanup(patch.stop)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True, env=ENV)
        (self.repo / "tracked.txt").write_text("base\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "base")
        remote = self.tmp / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True, env=ENV)
        git(self.repo, "remote", "add", "origin", str(remote))
        git(self.repo, "branch", "-M", "main")
        git(self.repo, "push", "-qu", "origin", "main")
        self.state_dir = str(self.tmp / "state")
        self.root = str(self.tmp / "runs")
        self.plan_path = self.tmp / "plan.json"

    # -- helpers ----------------------------------------------------------
    def unit(self, uid, **extra):
        u = {"id": uid, "kind": "code", "repo": str(self.repo),
             "target_branch": "main", "prompt": "change tracked.txt",
             "inputs": [], "outputs": ["evidence.md", "test-output.txt"]}
        u.update(extra)
        return u

    def write_plan(self, plan):
        self.plan_path.write_text(json.dumps(plan, indent=1))
        return plan

    def lor(self):
        return json.loads(self.lor_state.read_text())

    def set_lor(self, **changes):
        data = self.lor()
        data.update(changes)
        self.lor_state.write_text(json.dumps(data))

    def set_session(self, sid, **changes):
        data = self.lor()
        for session in data["sessions"]:
            if session["session_id"] == sid:
                session.update(changes)
        self.lor_state.write_text(json.dumps(data))

    def paseo(self):
        return json.loads(self.paseo_state.read_text())

    def set_paseo_status(self, agent, status):
        data = self.paseo()
        data["status"][agent] = status
        self.paseo_state.write_text(json.dumps(data))

    def lor_calls(self, verb=None):
        return [c for c in self.lor()["calls"]
                if verb is None or c["argv"][:2] == ["session", verb]]

    def advance(self, plan, max_new=None):
        ok, why = S.acquire_lease(self.state_dir)
        self.assertTrue(ok, why)
        try:
            state = S.load_state(self.state_dir)
            report, dispatched, halted = S.advance(
                plan, state, self.state_dir, self.root, False, max_new,
                plan_path=str(self.plan_path))
        finally:
            S.release_lease(self.state_dir)
        return state, report, dispatched, halted

    def set_agent(self, **flags):
        args = Namespace(plan=str(self.plan_path), state_dir=self.state_dir,
                         provider=None, backend=None, model=None, thinking=None,
                         mode=None, unit=None, clear=False, show=False, json=True)
        for key, value in flags.items():
            setattr(args, key, value)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = S.cmd_set_agent(args)
        return rc, out.getvalue(), err.getvalue()

    def overrides(self):
        record, problem = S.SS.load_agent_overrides(self.state_dir)
        self.assertIsNone(problem)
        return record

    def worktrees(self):
        root = Path(self.state_dir) / "code-worktrees"
        return sorted(p.name for p in root.iterdir()) if root.is_dir() else []

    def attempt_name(self, state, uid):
        return Path(state["units"][uid]["attempt_dir"]).name

    def intent(self, state, uid):
        us = state["units"][uid]
        return us["attempt_launch_intents"][Path(us["attempt_dir"]).name]


class TestLorAdapter(unittest.TestCase):
    def test_provider_parsing_and_canonical_identity(self):
        self.assertTrue(LIO.is_lor_provider("lor"))
        self.assertTrue(LIO.is_lor_provider("lor/openai/gpt-6.2-sol"))
        self.assertFalse(LIO.is_lor_provider("lorx/foo"))
        self.assertFalse(LIO.is_lor_provider("codex/gpt-6.2-sol"))
        self.assertEqual(LIO.parse_lor_model("lor/openai/gpt-6.2-sol"), "openai/gpt-6.2-sol")
        self.assertEqual(LIO.parse_lor_model("lor/deepseek-v4-pro"), "deepseek-v4-pro")
        self.assertEqual(LIO.parse_lor_model("lor", "openai/gpt-6.2-sol"), "openai/gpt-6.2-sol")
        for bad in (("lor",), ("lor/",), ("lor/ x",), ("lor/-x",), ("lor/a//b",),
                    ("lor", "  "), ("lor/a\nb",)):
            with self.subTest(bad=bad), self.assertRaises(LIO.LorError):
                LIO.parse_lor_model(*bad)
        for spec, expected in (("lor/openai/gpt-6.2-sol", "openai/gpt-6.2-sol"),
                               ("lor/google/gemini-4-neon", "google/gemini-4-neon"),
                               ("lor/anthropic/claude-opus-5-5", "anthropic/claude-opus-5-5"),
                               ("lor/deepseek-v4-pro", "deepseek-v4-pro")):
            self.assertEqual(LIO.canonical_author_identity(spec), expected)

    def test_availability_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "lor").write_text("#!/bin/sh\nexit 0\n")
            Path(tmp, "lor").chmod(0o755)
            self.assertIsNone(LIO.lor_availability({"LOR_SESSION_ID": PARENT, "PATH": tmp}))
            for env in ({"PATH": tmp}, {"LOR_SESSION_ID": "  ", "PATH": tmp},
                        {"LOR_SESSION_ID": PARENT, "PATH": str(Path(tmp) / "none")}):
                with self.subTest(env=env):
                    problem = LIO.lor_availability(env)
                    self.assertIsNotNone(problem)
                    self.assertIn("No Paseo fallback", problem)

    def test_child_env_keeps_lor_and_strips_third_party_secrets(self):
        env = LIO.build_lor_env({
            "LOR_SESSION_TOKEN": "t", "LOR_SESSION_ID": "s", "PATH": "/bin",
            "OPENAI_API_KEY": "x", "OPENROUTER_BASE": "x", "ANTHROPIC_FOO": "x",
            "GITHUB_TOKEN": "x", "SOME_API_KEY": "x", "AWS_SECRET_ACCESS_KEY": "x",
            "SWARM_UNIT_DIR": "x", "HOME": "/h"})
        self.assertEqual(sorted(env), ["HOME", "LOR_SESSION_ID", "LOR_SESSION_TOKEN", "PATH"])

    def test_session_classification_never_guesses(self):
        cases = [({"running": True}, "running"),
                 ({"running": False, "last_finish_reason": "stop"}, "idle"),
                 ({"running": False, "last_finish_reason": "error"}, "failed"),
                 ({"running": False, "stopped_at": "t", "last_finish_reason": "stop"}, "cancelled"),
                 ({"running": False, "last_finish_reason": None}, "unknown"),
                 ({"running": False, "last_finish_reason": "mystery"}, "unknown"),
                 ({}, "unknown")]
        for record, expected in cases:
            with self.subTest(record=record):
                self.assertEqual(LIO.classify_session(record)["state"], expected)


class TestAuthorIdentity(unittest.TestCase):
    def test_lor_prefix_is_stripped_for_review_exclusion(self):
        self.assertEqual(SA.intent_identity({"provider": LOR_PROVIDER}), LOR_MODEL)
        self.assertEqual(SA.intent_identity({"provider": "lor", "model": LOR_MODEL}), LOR_MODEL)
        self.assertEqual(SA.intent_identity({"provider": "lor/google/gemini-4-neon"}),
                         "google/gemini-4-neon")
        # No maker segment: the transport is kept so the gate fails closed.
        self.assertEqual(SA.intent_identity({"provider": "lor/deepseek-v4-pro"}),
                         "lor/deepseek-v4-pro")
        # Non-Lor identities are untouched.
        self.assertEqual(SA.intent_identity({"provider": "codex/gpt-6.2-sol"}), "codex/gpt-6.2-sol")
        self.assertEqual(SA.intent_identity({"provider": "claude", "model": "claude-opus-5-5"}),
                         "claude/claude-opus-5-5")
        text = SA.author_instruction({"provider": LOR_PROVIDER, "author_history": [
            {"unit": "u", "attempt": "a", "identity": "lor/anthropic/claude-opus-5-5"}]})
        self.assertIn("--author openai/gpt-6.2-sol", text)
        self.assertIn("--author anthropic/claude-opus-5-5", text)
        self.assertNotIn("--author lor/", text)

    def test_identity_is_what_review_panels_excludes(self):
        flag = SA.intent_identity({"provider": LOR_PROVIDER})
        self.assertEqual(review_panels.author_model_ids([flag]), {("openai", "gpt-6.2-sol")})
        self.assertEqual(review_panels.author_model_ids([flag]),
                         review_panels.author_model_ids(["codex/gpt-6.2-sol"]))
        seat = {"name": "sol", "provider": "openai", "model": "gpt-6.2-sol"}
        kept, excluded = review_panels.exclude_authors(
            [seat], review_panels.author_model_ids([flag]))
        self.assertEqual((kept, len(excluded)), ([], 1))


class TestPrecedenceAndPlan(LorFixture):
    def test_five_level_precedence(self):
        plan = {"units": [self.unit("u", mode="full-access")],
                "code_agent": {"provider": "lor/vendor/plan-level"}}
        u = plan["units"][0]
        default = SR.resolve_effective_agent({"units": [u]}, u)
        self.assertEqual((default["source"], default["backend"]), ("default", "paseo"))
        self.assertIs(default["unit"], u)
        self.assertEqual(SR.resolve_effective_agent(plan, u)["provider"], "lor/vendor/plan-level")
        ov = {"revision": 3, "project": {"backend": "lor", "provider": "lor/vendor/project"},
              "units": {}}
        self.assertEqual(SR.resolve_effective_agent(plan, u, ov)["source"], "project-override")
        pinned = dict(u, provider="claude", model="claude-sonnet-5-5")
        agent = SR.resolve_effective_agent(plan, pinned, ov)
        self.assertEqual((agent["source"], agent["backend"]), ("unit", "paseo"))
        ov["units"]["u"] = {"backend": "paseo", "provider": "claude/claude-sonnet-5-5",
                            "thinking": None}
        agent = SR.resolve_effective_agent(plan, pinned, ov)
        self.assertEqual((agent["source"], agent["provider"], agent["thinking"],
                          agent["override_revision"]),
                         ("unit-override", "claude/claude-sonnet-5-5", None, 3))
        self.assertEqual((agent["unit"]["provider"], agent["unit"]["model"]),
                         ("claude", "claude-sonnet-5-5"))

    def test_plan_digest_includes_code_agent_only_when_present(self):
        plan = {"units": [{"id": "u", "kind": "code", "prompt": "p", "mode": "full-access",
                           "repo": "/r", "target_branch": "main"}],
                "root": "/r", "orchestrator": "codex"}
        # Computed by the unmodified plan_digest at base 1375abea: a plan with
        # no code_agent keeps its digest byte for byte.
        self.assertEqual(SPR.plan_digest(plan),
                         "940285a5f035ae562fb17c2cb45b2028258e0f0c7c0193a5d14553ab6cfc2e39")
        with_agent = dict(plan, code_agent={"provider": LOR_PROVIDER})
        self.assertNotEqual(SPR.plan_digest(with_agent), SPR.plan_digest(plan))

    def test_validation_mode_is_conditional_on_backend(self):
        lor_unit = self.unit("l", provider=LOR_PROVIDER)
        with mock.patch.dict(os.environ, {"PATH": str(self.tmp / "nothing")}):
            # A lor-only plan needs neither 'mode' nor paseo on PATH.
            S.validate_plan({"units": [lor_unit]})
            S.validate_plan({"units": [self.unit("p")],
                             "code_agent": {"backend": "lor", "model": LOR_MODEL}})
            with self.assertRaisesRegex(S.PlanError, "paseo is not on PATH"):
                S.validate_plan({"units": [self.unit("p", mode="full-access")]})
        for mode in ("plan", "read-only"):
            with self.subTest(mode=mode), self.assertRaisesRegex(S.PlanError, "restrictive mode"):
                S.validate_plan({"units": [dict(lor_unit, mode=mode)]})
            with self.subTest(code_agent_mode=mode), \
                    self.assertRaisesRegex(S.PlanError, "restrictive mode"):
                S.validate_plan({"units": [self.unit("p")],
                                 "code_agent": {"provider": LOR_PROVIDER, "mode": mode}})
        with self.assertRaisesRegex(S.PlanError, "declares no 'mode'"):
            S.validate_plan({"units": [self.unit("p")]})
        S.validate_plan({"units": [self.unit("p")],
                         "code_agent": {"provider": "claude/claude-sonnet-5-5",
                                        "mode": "bypass"}})
        with self.assertRaisesRegex(S.PlanError, "unknown field"):
            S.validate_plan({"units": [lor_unit], "code_agent": {"provider": LOR_PROVIDER,
                                                                 "effort": "x"}})
        with self.assertRaisesRegex(S.PlanError, "no valid Lor canonical model"):
            S.validate_plan({"units": [self.unit("l", provider="lor")]})


class TestFailClosedLaunch(LorFixture):
    def test_unset_session_id_refuses_before_any_side_effect(self):
        plan = self.write_plan({"name": "p", "units": [self.unit("l", provider=LOR_PROVIDER)]})
        with mock.patch.dict(os.environ):
            del os.environ["LOR_SESSION_ID"]
            state, report, dispatched, halted = self.advance(plan)
            self.assertEqual(dispatched, 0)
            self.assertIn("LOR_SESSION_ID", halted)
            self.assertIn("REFUSING dispatch", "\n".join(report))
            self.assertIsNone(state["units"]["l"]["attempt_dir"])
            self.assertFalse(Path(self.root).exists() and any(Path(self.root).rglob("unit.json")))
            # The launcher itself also refuses (defence in depth).
            attempt = self.tmp / "runs-direct" / "l" / "a1"
            attempt.mkdir(parents=True)
            job, error = S._submit(plan["units"][0], str(attempt), False,
                                   {"units": {}}, self.state_dir, plan=plan)
            self.assertIsNone(job)
            self.assertIn("LOR_SESSION_ID", error)
        self.assertEqual(self.worktrees(), [])
        self.assertEqual(self.lor_calls(), [])
        self.assertEqual(self.paseo()["calls"], [])
        self.assertNotIn("swarm-a1", git(self.repo, "branch", "--list"))

    def test_missing_lor_binary_refuses(self):
        plan = self.write_plan({"name": "p", "units": [self.unit("l", provider=LOR_PROVIDER)]})
        (self.bin / "lor").unlink()
        state, report, dispatched, halted = self.advance(plan)
        self.assertEqual(dispatched, 0)
        self.assertIn("`lor` CLI is not on PATH", halted)
        self.assertEqual(self.worktrees(), [])
        self.assertEqual(self.paseo()["calls"], [])


class TestLorLaunch(LorFixture):
    def test_isolated_launch_with_allowlisted_context_and_no_secret(self):
        up_dir = str(self.tmp / "runs" / "up" / "up-attempt")
        lor_unit = self.unit("l", provider=LOR_PROVIDER, needs=["up"], thinking="xhigh")
        plan = self.write_plan({"name": "p", "units": [lor_unit]})
        directory, error = S._allocate(plan, lor_unit, self.root, self.state_dir)
        self.assertIsNone(error)
        state = {"units": {"up": {"attempt_dir": up_dir, "state": "DONE"}}}
        job, error = S._submit(lor_unit, directory, False, state, self.state_dir,
                               plan=plan, plan_path=str(self.plan_path))
        self.assertIsNone(error)
        self.assertEqual(job, "51a0b2c3-0000-4000-8000-000000000001")
        attempt = Path(directory).name
        us = state["units"]["l"]
        intent = us["attempt_launch_intents"][attempt]
        self.assertEqual((intent["backend"], intent["parent_session_id"],
                          intent["canonical_model_id"], intent["provider"],
                          intent["effort"], intent["author_identity"]),
                         ("lor", PARENT, LOR_MODEL, LOR_PROVIDER, "xhigh", LOR_MODEL))
        # Coordinator-owned worktree at the trusted base; nothing written into it.
        facts = S.trusted_launch_facts(state, "l", directory)
        workspace = Path(facts["execution_workspace"])
        self.assertTrue(str(workspace).startswith(str(Path(self.state_dir) / "code-worktrees")))
        self.assertEqual(git(workspace, "status", "--porcelain", "--untracked-files=all"), "")
        self.assertEqual(git(workspace, "rev-parse", "HEAD"), intent["base_commit"])
        # The Lor CLI contract: no --tools/--env/--mode, prompt on stdin.
        (start,) = self.lor_calls("start")
        argv = start["argv"]
        for flag in ("--tools", "--env", "--mode", "--fork", "--type"):
            self.assertNotIn(flag, argv)
        self.assertEqual(argv[argv.index("--canonical-model-id") + 1], LOR_MODEL)
        self.assertEqual(argv[argv.index("--working-directory") + 1], str(workspace))
        self.assertEqual(argv[argv.index("--effort") + 1], "xhigh")
        self.assertEqual(argv[argv.index("--title") + 1], f"[swarm] l {attempt}")
        self.assertEqual(argv[argv.index("--task") + 1], "@-")
        prompt = start["stdin"]
        self.assertIn("SWARM ATTEMPT CONTEXT", prompt)
        self.assertIn("export SWARM_UNIT_ID=l", prompt)
        self.assertIn(f"export SWARM_UNIT_DIR={directory}", prompt)
        self.assertIn(f"export SWARM_DEP_UP={up_dir}", prompt)
        self.assertIn("sbatch", prompt)
        self.assertIn("Chimera", prompt)
        self.assertIn("--author openai/gpt-6.2-sol", prompt)
        self.assertTrue(prompt.rstrip().endswith(prompt.rstrip().split(
            S.CODE_COMPLETION_PROTOCOL_MARKER)[-1]))
        for leaked in (SECRET, API_SECRET, "LOR_SESSION_TOKEN", "OPENAI_API_KEY",
                       "LOR_SESSION_ID", PARENT):
            self.assertNotIn(leaked, prompt)
        # The CLI child got Lor credentials but no third-party API key.
        self.assertIn("LOR_SESSION_TOKEN", start["env"])
        self.assertNotIn("OPENAI_API_KEY", start["env"])
        context_path = Path(directory) / "context.json"
        self.assertEqual(stat.S_IMODE(context_path.stat().st_mode), 0o600)
        context = json.loads(context_path.read_text())
        self.assertEqual(context["values"], {"SWARM_UNIT_ID": "l",
                                             "SWARM_UNIT_DIR": directory,
                                             "SWARM_DEP_UP": up_dir})
        self.assertNotIn(SECRET, context_path.read_text())
        # Frozen binding: unit.json records backend + parent.
        self.assertIsNone(S.SADV._bind_attempt(state, "l", directory, job, self.state_dir))
        spec = json.loads((Path(directory) / "unit.json").read_text())
        self.assertEqual((spec["job_id"], spec["backend"], spec["parent_session_id"]),
                         (job, "lor", PARENT))
        # Native parent wake: recorded, and no watcher process is ever spawned.
        self.assertEqual(us["code_terminal_watches"][attempt]["status"],
                         "lor_native_parent_wake")
        us.update(job_id=job, state="SUBMITTED", attempt_dir=directory)
        del us["code_terminal_watches"]
        args = Namespace(plan=str(self.plan_path), state_dir=self.state_dir, root=self.root)
        report = []
        with mock.patch.object(S.subprocess, "Popen",
                               side_effect=AssertionError("watcher spawned")) as popen:
            S._start_code_terminal_watchers(plan, state, args, report)
        popen.assert_not_called()
        self.assertEqual(us["code_terminal_watches"][attempt]["status"],
                         "lor_native_parent_wake")
        self.assertEqual(self.paseo()["calls"], [])

    def test_bind_refuses_inconsistent_backend_flags(self):
        plan = {"name": "p", "units": [self.unit("l", provider=LOR_PROVIDER)]}
        directory, error = S._allocate(plan, plan["units"][0], self.root, self.state_dir)
        self.assertIsNone(error)
        unit_py = [sys.executable, str(SCRIPTS / "unit.py"), "bind", directory,
                   "--state-dir", self.state_dir]
        for extra, needle in ((["--job-id", "51a0b2c3-0000-4000-8000-000000000001",
                                "--backend", "lor"], "parent-session-id"),
                              (["--job-id", "x", "--backend", "lor",
                                "--parent-session-id", PARENT], "implausible lor"),
                              (["--job-id", "aaaaaaaa-0000-4000-8000-000000000001",
                                "--parent-session-id", PARENT], "only to --backend lor")):
            result = subprocess.run(unit_py + extra, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(needle, result.stderr)


class TestMixedBackends(LorFixture):
    def test_switch_mid_project_and_judge_both_backends_independently(self):
        plan = self.write_plan({"name": "mixed", "units": [
            self.unit("a", mode="full-access"),
            self.unit("b", mode="full-access", continuation={"max": 1})]})
        plan_bytes = self.plan_path.read_bytes()
        digest = SPR.plan_digest(plan)

        state, report, dispatched, _halted = self.advance(plan, max_new=1)
        self.assertEqual(dispatched, 1, report)
        self.assertEqual(self.intent(state, "a")["backend"], "paseo")
        paseo_agent = state["units"]["a"]["job_id"]
        self.assertEqual(paseo_agent, "aaaaaaaa-0000-4000-8000-000000000001")
        self.assertEqual(self.lor_calls(), [])

        rc, out, err = self.set_agent(provider=LOR_PROVIDER)
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out)["revision"], 1)
        self.assertEqual(self.plan_path.read_bytes(), plan_bytes)
        self.assertEqual(SPR.plan_digest(json.loads(plan_bytes)), digest)

        state, report, dispatched, halted = self.advance(plan, max_new=1)
        self.assertEqual(dispatched, 1, report)
        self.assertIsNone(halted)
        self.assertEqual(state["plan_digest"], digest)
        self.assertEqual(self.intent(state, "a")["backend"], "paseo")
        self.assertEqual(state["units"]["a"]["job_id"], paseo_agent)
        b_intent = self.intent(state, "b")
        self.assertEqual((b_intent["backend"], b_intent["override_revision"],
                          b_intent["agent_source"]), ("lor", 1, "project-override"))
        session = state["units"]["b"]["job_id"]
        self.assertEqual(session, "51a0b2c3-0000-4000-8000-000000000001")
        b_dir = Path(state["units"]["b"]["attempt_dir"])
        spec = json.loads((b_dir / "unit.json").read_text())
        self.assertEqual((spec["backend"], spec["parent_session_id"]), ("lor", PARENT))
        a_spec = json.loads((Path(state["units"]["a"]["attempt_dir"]) / "unit.json").read_text())
        self.assertNotIn("backend", a_spec)
        rows = {r["id"]: r for r in S.status_report(plan, state, self.state_dir)["units"]}
        self.assertEqual((rows["a"]["backend"], rows["b"]["backend"]), ("paseo", "lor"))
        self.assertEqual(rows["b"]["terminal_watch"]["status"], "lor_native_parent_wake")

        # Lor turn ends without outputs: INCOMPLETE with the NO_OUTPUTS reason;
        # the Paseo attempt is still judged through Paseo, independently.
        self.set_session(session, running=False, last_finish_reason="stop")
        state, report, _d, _h = self.advance(plan)
        self.assertEqual(state["units"]["b"]["state"], "INCOMPLETE", report)
        self.assertEqual(SE._receipt_reason(state, "b", str(b_dir)), S.U.REASON_NO_OUTPUTS)
        self.assertEqual(state["units"]["a"]["state"], "RUNNING")
        inspected = [c for c in self.paseo()["calls"] if c[:1] == ["inspect"]]
        self.assertTrue(inspected)
        self.assertTrue(all(session not in c for c in inspected))

        # Auth/CLI failure is retryable INCOMPLETE, never FAILED or DONE.
        self.set_lor(fail=AUTH_FAILURE)
        state, report, _d, _h = self.advance(plan)
        self.assertEqual(state["units"]["b"]["state"], "INCOMPLETE", report)
        self.set_lor(fail=None)

        # Bounded continuation after the settle window goes through Lor.
        ok, why = S.acquire_lease(self.state_dir)
        self.assertTrue(ok, why)
        try:
            st = S.load_state(self.state_dir)
            st["units"]["b"]["incomplete_since"] = 1.0
            S.save_state(self.state_dir, st)
        finally:
            S.release_lease(self.state_dir)
        state, report, _d, _h = self.advance(plan)
        (send,) = self.lor_calls("send")
        self.assertEqual(send["argv"][send["argv"].index("--recipient-session-id") + 1], session)
        self.assertIn("--delivery", send["argv"])
        self.assertEqual(send["argv"][send["argv"].index("--delivery") + 1], "queue")
        self.assertIn("declared outputs", send["stdin"])
        self.assertFalse([c for c in self.paseo()["calls"] if c[:1] == ["send"]])
        self.assertEqual(state["units"]["b"]["continuations"][0]["backend"], "lor")

        # Outputs plus a pushed produced change: READY_FOR_PR via the same checks.
        facts = S.trusted_launch_facts(state, "b", str(b_dir))
        workspace = Path(facts["execution_workspace"])
        (workspace / "tracked.txt").write_text("changed by lor\n")
        git(workspace, "commit", "-qam", "lor work")
        git(workspace, "push", "-q", "origin", f"HEAD:refs/heads/{facts['branch']}")
        for output in ("evidence.md", "test-output.txt"):
            (b_dir / output).write_text("fixture\n")
        self.set_session(session, running=False, last_finish_reason="stop")
        state, report, _d, _h = self.advance(plan)
        self.assertEqual(state["units"]["b"]["state"], "READY_FOR_PR", report)
        self.assertEqual(state["units"]["a"]["state"], "RUNNING")

        # Cleanup never removes a Lor worktree while its session is running.
        self.set_session(session, running=True)
        state, report, _d, _h = self.advance(plan)
        meta = state["units"]["b"]["attempt_workspaces"][b_dir.name]
        self.assertTrue(workspace.is_dir())
        self.assertFalse(meta.get("archived"))
        self.assertIn("Lor session is still running", meta.get("cleanup_liveness", ""))
        self.set_lor(fail=AUTH_FAILURE)
        state, report, _d, _h = self.advance(plan)
        self.assertTrue(workspace.is_dir())
        self.assertIn("Lor session status unknown",
                      state["units"]["b"]["attempt_workspaces"][b_dir.name]["cleanup_liveness"])
        self.set_lor(fail=None)
        self.set_session(session, running=False, last_finish_reason="stop")
        state, report, _d, _h = self.advance(plan)
        self.assertFalse(workspace.exists(), report)
        self.assertTrue(state["units"]["b"]["attempt_workspaces"][b_dir.name]["archived"])
        self.assertFalse([c for c in self.paseo()["calls"] if c[:2] == ["workspace", "archive"]])
        self.assertEqual(self.plan_path.read_bytes(), plan_bytes)


class TestLifecycleOps(LorFixture):
    def launch_lor(self, uid="l"):
        plan = self.write_plan({"name": "ops", "units": [
            self.unit(uid, provider=LOR_PROVIDER)]})
        state, report, dispatched, _h = self.advance(plan)
        self.assertEqual(dispatched, 1, report)
        return plan, state

    def test_abandon_stops_the_lor_session(self):
        plan, state = self.launch_lor()
        session = state["units"]["l"]["job_id"]
        args = Namespace(command="abandon", plan=str(self.plan_path), unit="l",
                         approver="tester", reason="obsolete", by=None,
                         state_dir=self.state_dir)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(S.cmd_stop_unit(args), S.EXIT_OK)
        (stop,) = self.lor_calls("stop")
        self.assertEqual(stop["argv"][stop["argv"].index("--session-id") + 1], session)
        state = S.load_state(self.state_dir)
        us = state["units"]["l"]
        self.assertEqual(us["state"], "ABANDONED")
        self.assertEqual(us["terminal_resource_release"]["job_id"], session)
        self.assertFalse(S.SS._occupies_live_resources(plan["units"][0], us))
        self.assertEqual(self.paseo()["calls"], [])

    def test_provider_resume_never_inspects_a_lor_session_through_paseo(self):
        _plan, state = self.launch_lor()
        u = dict(self.unit("l", provider=LOR_PROVIDER), provider_resume={"max": 2})
        self.assertFalse(S.SPRS.maybe_resume(self.state_dir, "l", u,
                                             state["units"]["l"], [], state))
        self.assertEqual(self.paseo()["calls"], [])

    def test_orphan_session_is_recovered_by_title_not_relaunched(self):
        plan, state = self.launch_lor()
        us = state["units"]["l"]
        session, attempt = us["job_id"], us["attempt_dir"]
        intent = self.intent(state, "l")
        job, note = S.reconcile_orphan(attempt, kind="code", intent=intent)
        self.assertEqual(job, session)
        self.assertIn("never bound", note)
        self.set_lor(fail=AUTH_FAILURE)
        self.assertEqual(S.reconcile_orphan(attempt, kind="code", intent=intent),
                         (None, "UNKNOWN"))
        self.set_lor(fail=None)
        self.assertEqual(S.reconcile_orphan(attempt, kind="code",
                                            intent=dict(intent, parent_session_id=None)),
                         (None, "UNKNOWN"))
        self.assertIsNone(S._recover_code_launch(state, plan["units"][0], attempt, session))
        self.assertEqual(len(self.lor_calls("start")), 1)
        self.assertEqual(self.paseo()["calls"], [])

    def test_recovered_lor_session_must_own_the_recorded_inode(self):
        plan, state = self.launch_lor()
        us = state["units"]["l"]
        session, attempt_dir = us["job_id"], us["attempt_dir"]
        attempt = Path(attempt_dir).name
        recorded = (us.get("attempt_launch_facts") or {}).get(attempt, {}).get(
            "workspace_identity") or us["attempt_workspaces"][attempt][
            "workspace_identity"]
        self.assertIsInstance(recorded, dict)
        forged = dict(recorded, inode=int(recorded["inode"]) + 1)
        us.setdefault("attempt_launch_facts", {}).setdefault(attempt, {})[
            "workspace_identity"] = forged
        us["attempt_workspaces"][attempt]["workspace_identity"] = forged
        us.get("attempt_cleanup_agents", {}).pop(attempt, None)
        problem = S._recover_code_launch(state, plan["units"][0], attempt_dir,
                                         session)
        self.assertIsNotNone(problem)
        self.assertIn("does not own the recorded device/inode", problem)
        self.assertIn(f"lor session {session}", problem)
        self.assertNotIn(attempt, us.get("attempt_cleanup_agents", {}))
        self.assertEqual(self.paseo()["calls"], [])


    def test_redispatch_refuses_when_lor_cannot_say_whether_a_session_exists(self):
        lor_unit = self.unit("l", provider=LOR_PROVIDER)
        plan = self.write_plan({"name": "p", "units": [lor_unit]})
        directory, error = S._allocate(plan, lor_unit, self.root, self.state_dir)
        self.assertIsNone(error)
        state = {"units": {}}
        job, error = S._submit(lor_unit, directory, False, state, self.state_dir, plan=plan)
        self.assertIsNone(error)
        # Crash before bind; the next dispatch of the SAME attempt asks Lor
        # whether a session exists. An unanswerable listing must not start a
        # second one.
        self.set_lor(fail=AUTH_FAILURE)
        job2, error = S._submit(lor_unit, directory, False, state, self.state_dir, plan=plan)
        self.assertIsNone(job2)
        self.assertIn("refusing to start another", error)
        self.set_lor(fail=None)
        job3, error = S._submit(lor_unit, directory, False, state, self.state_dir, plan=plan)
        self.assertIsNone(error)
        self.assertEqual(job3, job)
        self.assertEqual(len(self.lor_calls("start")), 1)

    def unlaunched_lor_attempt(self):
        """A Lor attempt whose `lor session start` never returned a session:
        the frozen intent (parent PARENT) and refused worktree persist, and
        no attempt_cleanup_agents entry or job_id exists."""
        lor_unit = self.unit("l", provider=LOR_PROVIDER)
        plan = self.write_plan({"name": "p", "units": [lor_unit]})
        directory, error = S._allocate(plan, lor_unit, self.root, self.state_dir)
        self.assertIsNone(error)
        state = {"units": {}}
        with mock.patch.object(LIO, "start_session",
                               return_value=(None, "lor session start failed: crash")):
            job, error = S._submit(lor_unit, directory, False, state,
                                   self.state_dir, plan=plan)
        self.assertIsNone(job)
        self.assertIn("lor session start failed", error)
        us = state["units"]["l"]
        attempt = Path(directory).name
        self.assertEqual(us["attempt_launch_intents"][attempt]["parent_session_id"], PARENT)
        self.assertNotIn(attempt, us.get("attempt_cleanup_agents") or {})
        self.assertIsNone(us.get("job_id"))
        meta = us["attempt_workspaces"][attempt]
        self.assertTrue(meta["cleanup_pending"])
        self.assertEqual(self.lor_calls("start"), [])
        return plan, lor_unit, directory, state

    def test_cleanup_of_lor_attempt_with_unknown_agent_retains_worktree(self):
        # Regression (kimi-k3 R1): a Lor attempt with cleanup_pending and no
        # recorded agent must be retained as "attempt agent is unknown", never
        # crash cleanup (NameError on the Lor liveness observation).
        _plan, lor_unit, directory, state = self.unlaunched_lor_attempt()
        attempt = Path(directory).name
        us = state["units"]["l"]
        us["attempt_dir"] = directory
        meta = us["attempt_workspaces"][attempt]
        workspace = Path(meta["path"])
        self.assertTrue(workspace.is_dir())
        report = []
        S._preserve_and_archive_code_worktree(state, lor_unit, directory, report,
                                              self.state_dir)
        self.assertTrue(workspace.is_dir())
        self.assertFalse(meta.get("archived"))
        self.assertEqual(meta.get("cleanup_liveness"), "attempt agent is unknown")
        self.assertTrue(any("attempt agent is unknown" in line for line in report), report)
        self.assertEqual(self.lor_calls("list"), [])
        self.assertEqual(self.paseo()["calls"], [])

    def test_redispatch_under_a_different_parent_session_refuses_before_start(self):
        # Regression (terra + sonnet-5.5 R1): an unlaunched intent frozen under
        # PARENT must not start a subagent under another LOR_SESSION_ID, where
        # every later poll (which uses the recorded parent) could not see it.
        plan, lor_unit, directory, state = self.unlaunched_lor_attempt()
        other = "4ef0c1b2-0000-4000-8000-000000000002"
        with mock.patch.dict(os.environ, {"LOR_SESSION_ID": other}):
            job, error = S._submit(lor_unit, directory, False, state,
                                   self.state_dir, plan=plan)
        self.assertIsNone(job)
        self.assertIn("different parent", error)
        self.assertIn(PARENT, error)
        self.assertIn(other, error)
        self.assertIn("fresh attempt", error)
        self.assertEqual(self.lor_calls("start"), [])
        self.assertFalse([s for s in self.lor()["sessions"]
                          if s.get("parent_session_id") == other])
        # The recorded parent can still launch it, under that same parent.
        job, error = S._submit(lor_unit, directory, False, state,
                               self.state_dir, plan=plan)
        self.assertIsNone(error)
        (start,) = self.lor_calls("start")
        (session,) = self.lor()["sessions"]
        self.assertEqual((session["session_id"], session["parent_session_id"]),
                         (job, PARENT))


class TestSetAgent(LorFixture):
    def plan(self, **extra):
        return self.write_plan(dict({"name": "sa", "units": [
            self.unit("a", mode="full-access"),
            self.unit("b", mode="full-access"),
            self.unit("pinned", mode="full-access", provider="claude",
                      model="claude-sonnet-5-5"),
            {"id": "job", "kind": "slurm", "command": "true"}]}, **extra))

    def test_project_unit_clear_show_and_monotonic_revision(self):
        plan = self.plan()
        plan_bytes = self.plan_path.read_bytes()
        digest = SPR.plan_digest(plan)
        rc, out, err = self.set_agent(show=True)
        self.assertEqual(rc, 0, err)
        shown = json.loads(out)
        self.assertEqual(shown["revision"], 0)
        self.assertFalse((Path(self.state_dir) / S.SS.AGENT_OVERRIDES_FILE).exists())
        self.assertEqual({r["unit"] for r in shown["units"]}, {"a", "b", "pinned"})

        rc, out, err = self.set_agent(provider=LOR_PROVIDER)
        self.assertEqual(rc, 0, err)
        rows = {r["unit"]: r for r in json.loads(out)["units"]}
        self.assertEqual(rows["a"]["effective"]["backend"], "lor")
        self.assertEqual(rows["pinned"]["effective"]["backend"], "paseo")
        self.assertTrue(rows["pinned"]["pinned_in_plan"])

        rc, out, err = self.set_agent(backend="paseo", model="claude/claude-sonnet-5-5",
                                      unit="b", thinking="none")
        self.assertEqual(rc, 0, err)
        rows = {r["unit"]: r for r in json.loads(out)["units"]}
        self.assertEqual((rows["b"]["effective"]["backend"], rows["b"]["effective"]["source"],
                          rows["b"]["effective"]["thinking"]),
                         ("paseo", "unit-override", None))
        rc, out, err = self.set_agent(backend="lor", model="google/gemini-4-neon", unit="pinned")
        self.assertEqual(rc, 0, err)
        rows = {r["unit"]: r for r in json.loads(out)["units"]}
        self.assertEqual(rows["pinned"]["effective"]["provider"], "lor/google/gemini-4-neon")

        record = self.overrides()
        self.assertEqual(record["revision"], 3)
        self.assertEqual([h["revision"] for h in record["history"]], [1, 2, 3])
        self.assertEqual(stat.S_IMODE((Path(self.state_dir) / S.SS.AGENT_OVERRIDES_FILE)
                                      .stat().st_mode), 0o600)

        rc, _out, err = self.set_agent(clear=True, unit="b")
        self.assertEqual(rc, 0, err)
        rc, out, err = self.set_agent(clear=True)
        self.assertEqual(rc, 0, err)
        record = self.overrides()
        self.assertEqual((record["revision"], record["project"], sorted(record["units"])),
                         (5, None, ["pinned"]))
        rows = {r["unit"]: r for r in json.loads(out)["units"]}
        self.assertEqual(rows["a"]["effective"]["source"], "default")
        rc, out, _err = self.set_agent(clear=True)
        self.assertEqual((rc, self.overrides()["revision"]), (0, 5))
        rc, out, err = self.set_agent(show=True, json=False)
        self.assertIn("agent overrides revision 5", out)
        self.assertEqual(self.overrides()["revision"], 5)
        self.assertEqual(self.plan_path.read_bytes(), plan_bytes)
        self.assertEqual(SPR.plan_digest(json.loads(plan_bytes)), digest)

    def test_fail_early_checks_change_nothing(self):
        self.plan(orchestrator="claude")
        with mock.patch.dict(os.environ):
            del os.environ["LOR_SESSION_ID"]
            rc, _out, err = self.set_agent(provider=LOR_PROVIDER)
        self.assertNotEqual(rc, 0)
        self.assertIn("LOR_SESSION_ID", err)
        rc, _out, err = self.set_agent(provider="lor/anthropic/claude-opus-5-5")
        self.assertNotEqual(rc, 0)
        self.assertIn("orchestrator is 'claude'", err)
        for unit, needle in (("nope", "unknown unit"), ("job", "kind='slurm'")):
            rc, _out, err = self.set_agent(provider=LOR_PROVIDER, unit=unit)
            self.assertNotEqual(rc, 0)
            self.assertIn(needle, err)
        rc, _out, err = self.set_agent(provider="lor/x", mode="read-only")
        self.assertNotEqual(rc, 0)
        self.assertIn("restrictive mode", err)
        self.assertFalse((Path(self.state_dir) / S.SS.AGENT_OVERRIDES_FILE).exists())

    def test_switching_to_paseo_requires_a_mode(self):
        self.write_plan({"name": "m", "units": [self.unit("a")],
                         "code_agent": {"provider": LOR_PROVIDER}})
        rc, _out, err = self.set_agent(backend="paseo", model="claude/claude-sonnet-5-5")
        self.assertNotEqual(rc, 0)
        self.assertIn("no 'mode'", err)
        self.assertFalse((Path(self.state_dir) / S.SS.AGENT_OVERRIDES_FILE).exists())
        rc, out, err = self.set_agent(backend="paseo", model="claude/claude-sonnet-5-5",
                                      mode="bypass")
        self.assertEqual(rc, 0, err)
        row = json.loads(out)["units"][0]
        self.assertEqual((row["effective"]["backend"], row["effective"]["mode"]),
                         ("paseo", "bypass"))

    def test_terminal_unit_override_is_refused(self):
        self.plan()
        ok, why = S.acquire_lease(self.state_dir)
        self.assertTrue(ok, why)
        try:
            state = S.load_state(self.state_dir)
            state["units"]["a"] = {"state": "DONE", "attempt_dir": None, "attempts": []}
            S.save_state(self.state_dir, state)
        finally:
            S.release_lease(self.state_dir)
        rc, _out, err = self.set_agent(provider=LOR_PROVIDER, unit="a")
        self.assertNotEqual(rc, 0)
        self.assertIn("is DONE", err)
        rc, out, err = self.set_agent(provider=LOR_PROVIDER)
        self.assertEqual(rc, 0, err)
        rows = {r["unit"]: r for r in json.loads(out)["units"]}
        self.assertEqual(rows["a"]["disposition"], "terminal: unchanged")

    def test_cli_entrypoint(self):
        self.plan()
        result = subprocess.run(
            [sys.executable, str(SCRIPTS / "swarm.py"), "set-agent", str(self.plan_path),
             "--state-dir", self.state_dir, "--backend", "lor", "--model", LOR_MODEL,
             "--json"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.overrides()["project"],
                         {"backend": "lor", "provider": LOR_PROVIDER})


class TestWaitEventPollLor(LorFixture):
    def state_with_lor_attempt(self, directory, session="51a0b2c3-0000-4000-8000-000000000009"):
        directory.mkdir(parents=True)
        (directory / "swarm-state.json").write_text(json.dumps({"units": {"l": {
            "state": "RUNNING", "attempt_dir": "/runs/l/att1", "job_id": session,
            "attempt_launch_intents": {"att1": {"backend": "lor",
                                                "parent_session_id": PARENT}}}}}))
        data = self.lor()
        data["sessions"].append({"session_id": session, "parent_session_id": PARENT,
                                 "title": "[swarm] l att1", "running": True})
        self.lor_state.write_text(json.dumps(data))
        return session

    def wait(self, directory, *extra):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = wait_event.main(["--state-dir", str(directory),
                                    "--snapshot", str(self.tmp / "snap.json")] + list(extra))
        return code, out.getvalue(), err.getvalue()

    def test_reports_finish_once_and_auth_error_distinctly(self):
        directory = self.tmp / "orch-state"
        session = self.state_with_lor_attempt(directory)
        code, out, _err = self.wait(directory, "--poll-lor", "--once", "--json")
        self.assertEqual((code, out), (3, ""))
        self.set_session(session, running=False, last_finish_reason="stop")
        code, out, _err = self.wait(directory, "--poll-lor", "--once", "--json")
        self.assertEqual(code, 0)
        event = json.loads(out.splitlines()[0])
        self.assertEqual((event["event"], event["unit"], event["session_id"],
                          event["lor_state"]), ("lor_session_finished", "l", session, "idle"))
        code, out, _err = self.wait(directory, "--poll-lor", "--once", "--json")
        self.assertEqual((code, out), (3, ""))
        self.set_lor(fail=AUTH_FAILURE)
        code, out, err = self.wait(directory, "--poll-lor", "--once", "--json")
        self.assertEqual(code, wait_event.EXIT_LOR_AUTH)
        self.assertNotEqual(code, 3)
        self.assertEqual(json.loads(out.splitlines()[0])["event"], "lor_auth_error")
        self.assertIn("not 'still running'", err)

    def test_bounded_polling_and_opt_in(self):
        directory = self.tmp / "orch-state"
        self.state_with_lor_attempt(directory)
        code, _out, _err = self.wait(directory, "--once")
        self.assertEqual(code, 3)
        self.assertEqual(self.lor_calls(), [])
        sleeps = []
        with mock.patch.object(wait_event.time, "sleep", side_effect=sleeps.append):
            code, _out, _err = self.wait(directory, "--poll-lor", "--interval", "5",
                                         "--timeout", "0")
        self.assertEqual(code, 3)
        self.assertLessEqual(len(self.lor_calls("list")), 1)


if __name__ == "__main__":
    unittest.main()
