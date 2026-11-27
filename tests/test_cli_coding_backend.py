"""Direct-CLI coding workers (claude, codex, pi) beside Lor and Paseo, offline.

Only service executables are fakes: `claude`, `codex`, `pi`, `lor`, `paseo`
and the Slurm tools are scripts on a closed PATH that record every call. The
turn wrapper, detached process groups, receipts, the coordinator, unit.py,
Git worktrees and state persistence are real. No live model call is possible.
"""
import contextlib
import fcntl
import io
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "hanig-swarm" / "scripts"
ORCH = ROOT / "skills" / "hanig-orchestrate" / "scripts"
for _path in (SCRIPTS, ORCH):
    sys.path.insert(0, str(_path))
import swarm as S  # noqa: E402
import cli_adapters as CA  # noqa: E402
import cli_io as CIO  # noqa: E402
import swarm_authors as SA  # noqa: E402
import swarm_evidence as SE  # noqa: E402
import swarm_plan_rules as SPR  # noqa: E402
import wait_event  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scheduler_fixture import (  # noqa: E402
    closed_bin, codex_environment, isolated_module_path, state_environment)
from scheduler_fixture import cleanup_module_path as tearDownModule  # noqa: E402,F401

ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@x",
           GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@x")
OPENROUTER_SECRET = "fake-openrouter-key-SECRET-0123456789abcdef"
ANTHROPIC_SECRET = "fake-anthropic-key-SECRET-0123456789abcdef"
OPENAI_SECRET = "fake-openai-key-SECRET-0123456789abcdef"
LOR_SECRET = "fake-lor-token-SECRET-0123456789abcdef"
AUTH_TOKEN_SECRET = "fake-anthropic-auth-token-SECRET-0123456789"
PARENT = "4ef0c1b2-0000-4000-8000-000000000001"
# Fixture model ids: deliberately not real model names.
CLAUDE_MODEL = "fixture-claude-model"
CODEX_MODEL = "fixture-codex-model"
PI_MODEL = "openrouter/fixturevendor/fixture-pi-model"
LOR_MODEL = "fixturevendor/fixture-lor-model"
SECRETS = (OPENROUTER_SECRET, ANTHROPIC_SECRET, OPENAI_SECRET, LOR_SECRET,
           AUTH_TOKEN_SECRET)


def setUpModule():
    isolated_module_path()


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          env=ENV, capture_output=True, text=True).stdout.strip()


def wait_until(predicate, timeout=20.0, step=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(step)
    return predicate()


# One fake for all three CLIs. Control and log live beside the script, so the
# fake works even under the Slurm script's `env -i`.
FAKE_CLI = r'''
import json, os, sys, time
here = os.path.dirname(os.path.abspath(sys.argv[0]))
name = os.path.basename(sys.argv[0])
ctl_path = os.path.join(here, "..", "cli-control.json")
log_path = os.path.join(here, "..", "cli-calls.jsonl")
ctl = json.load(open(ctl_path)) if os.path.exists(ctl_path) else {}
argv = sys.argv[1:]
prompt = sys.stdin.read()
rec = {"name": name, "argv": argv, "cwd": os.getcwd(), "pid": os.getpid(),
       "pgid": os.getpgid(0), "env": sorted(os.environ), "prompt": prompt,
       "openrouter": os.environ.get("OPENROUTER_API_KEY")}
with open(log_path, "a") as fh:
    fh.write(json.dumps(rec) + "\n")
mine = ctl.get(name, {})
if mine.get("hang"):
    release = os.path.join(here, "..", "release-" + name)
    end = time.time() + 60
    while time.time() < end and not os.path.exists(release):
        time.sleep(0.05)
def opt(flag):
    return argv[argv.index(flag) + 1] if flag in argv else None
if mine.get("echo_key"):
    # Print the delivered key split across two flushed writes, so the
    # wrapper's pump sees it straddle a read boundary.
    key = os.environ.get("OPENROUTER_API_KEY") or ""
    half = len(key) // 2
    sys.stdout.write("leak=" + key[:half]); sys.stdout.flush()
    time.sleep(0.3)
    sys.stdout.write(key[half:] + "\n"); sys.stdout.flush()
    sys.stderr.write("err " + key[:half]); sys.stderr.flush()
    time.sleep(0.3)
    sys.stderr.write(key[half:] + "\n"); sys.stderr.flush()
if mine.get("background_sleep"):
    # A descendant that inherits stdout/stderr and outlives the CLI, so the
    # wrapper's pumps never see EOF before their join timeout.
    import subprocess
    sleeper = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(%r)" % mine["background_sleep"]])
    with open(os.path.join(here, "..", "sleeper-" + name + ".pid"), "w") as fh:
        fh.write(str(sleeper.pid))
if name == "claude":
    sid = opt("--session-id") or opt("--resume")
    print(json.dumps({"type": "result", "subtype": "success", "session_id": sid}))
elif name == "codex":
    sid = argv[2] if argv[:2] == ["exec", "resume"] else "0199aaaa-1111-7000-8000-00000000c0de"
    print(json.dumps({"type": "thread.started", "thread_id": sid}))
    print(json.dumps({"type": "turn.completed"}))
elif name == "pi":
    sid = opt("--session-id") or opt("--session")
    print(json.dumps({"type": "session", "id": sid}))
    print(json.dumps({"type": "agent_end"}))
sys.exit(int(mine.get("rc", 0)))
'''

FAKE_SLURM = r'''
import json, os, sys
here = os.path.dirname(os.path.abspath(sys.argv[0]))
name = os.path.basename(sys.argv[0])
ctl_path = os.path.join(here, "..", "slurm-control.json")
log_path = os.path.join(here, "..", "slurm-calls.jsonl")
ctl = json.load(open(ctl_path)) if os.path.exists(ctl_path) else {}
argv = sys.argv[1:]
rec = {"name": name, "argv": argv, "env": sorted(os.environ)}
if name == "sbatch":
    script = argv[-1]
    rec["script"] = open(script).read()
with open(log_path, "a") as fh:
    fh.write(json.dumps(rec) + "\n")
job = None
if "-j" in argv:
    job = argv[argv.index("-j") + 1]
if name == "sbatch":
    if ctl.get("sbatch_fail"):
        sys.stderr.write("sbatch: error: invalid partition specified\n")
        sys.exit(1)
    print(ctl.get("job_id", "4242"))
elif name == "squeue":
    if ctl.get("squeue_fail"):
        sys.stderr.write("squeue: error: socket timed out\n"); sys.exit(1)
    state = (ctl.get("squeue") or {}).get(job)
    if state:
        print(state)
elif name == "scontrol":
    job = argv[-1]
    final = (ctl.get("final") or {}).get(job)
    if not final:
        sys.exit(1)
    print("JobId=%s JobName=x JobState=%s Reason=None" % (job, final))
elif name == "sacct":
    final = (ctl.get("final") or {}).get(job)
    if final:
        print(final)
elif name == "scancel":
    pass
'''

FAKE_LOR = r'''
import json, os, re, sys
path = os.environ["FAKE_LOR_STATE"]
with open(path) as fh:
    c = json.load(fh)
argv = sys.argv[1:]
stdin = sys.stdin.read() if "@-" in argv else ""
c.setdefault("calls", []).append({"argv": argv})
def opt(name):
    return argv[argv.index(name) + 1] if name in argv else None
def save():
    with open(path + ".tmp", "w") as fh:
        json.dump(c, fh)
    os.replace(path + ".tmp", path)
if argv[:2] == ["session", "start"]:
    sid = c["next_ids"].pop(0)
    c["sessions"].append({"session_id": sid,
        "parent_session_id": os.environ.get("LOR_SESSION_ID"),
        "title": opt("--title"), "sandbox": {"working_directory": opt("--working-directory")},
        "running": True, "last_finish_reason": None})
    save(); print(json.dumps({"session_id": sid}))
elif argv[:2] == ["session", "list"]:
    parent, pattern = opt("--parent-session-id"), opt("--title-pattern")
    found = [s for s in c["sessions"] if s.get("parent_session_id") == parent
             and (pattern is None or re.search(pattern, s.get("title") or ""))]
    save(); print(json.dumps({"sessions": found}))
else:
    save(); print("{}")
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
    print(json.dumps({"agentId": agent, "cwd": cwd}))
elif argv[:1] == ["inspect"]:
    save(); print(json.dumps({"status": "running"}))
elif argv[:2] in (["ls", "--json"], ["workspace", "ls"]):
    save(); print("[]")
else:
    save(); print("{}")
'''


class CliFixture(unittest.TestCase):
    slurm = False

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cli-backend-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.bin = Path(closed_bin(self.tmp / "bin"))
        for name in ("claude", "codex", "pi"):
            self._script(name, FAKE_CLI)
        self._script("lor", FAKE_LOR)
        self._script("paseo", FAKE_PASEO)
        if self.slurm:
            for name in ("sbatch", "squeue", "scancel", "sacct", "scontrol"):
                self._script(name, FAKE_SLURM)
        self.lor_state = self.tmp / "lor-state.json"
        self.lor_state.write_text(json.dumps({"sessions": [], "calls": [], "next_ids": [
            "51a0b2c3-0000-4000-8000-0000000000%02d" % i for i in range(1, 20)]}))
        self.paseo_state = self.tmp / "paseo-state.json"
        self.paseo_state.write_text(json.dumps({"calls": [], "next_ids": [
            "aaaaaaaa-0000-4000-8000-0000000000%02d" % i for i in range(1, 20)]}))
        environ = {
            "HOME": str(self.tmp), "PATH": str(self.bin),
            "ANTHROPIC_API_KEY": ANTHROPIC_SECRET, "OPENAI_API_KEY": OPENAI_SECRET,
            "OPENROUTER_API_KEY": OPENROUTER_SECRET,
            "ANTHROPIC_AUTH_TOKEN": AUTH_TOKEN_SECRET,
            "LOR_SESSION_TOKEN": LOR_SECRET,
            "FAKE_LOR_STATE": str(self.lor_state),
            "FAKE_PASEO_STATE": str(self.paseo_state),
            **state_environment(self.tmp / "state-home"),
            **codex_environment(self.tmp / "codex-fixture"),
        }
        patch = mock.patch.dict(os.environ, environ)
        patch.start()
        self.addCleanup(patch.stop)
        for name in ("LOR_SESSION_ID", "HANIG_SWARM_CLI_SLURM", "SLURM_JOB_ID",
                     "SLURM_JOB_NAME", "HANIG_SWARM_CLI_SLURM_PARTITION"):
            os.environ.pop(name, None)
        self.addCleanup(self.release_all)
        self.state_dir = str(self.tmp / "state")
        self.root = str(self.tmp / "runs")
        self.plan_path = self.tmp / "plan.json"
        self.worktree = self.tmp / "wt"
        self.worktree.mkdir()

    def _script(self, name, body):
        script = self.bin / name
        if script.exists() or script.is_symlink():
            script.unlink()
        script.write_text("#!" + sys.executable + "\n" + body)
        script.chmod(0o755)

    # -- fake control -----------------------------------------------------
    def control(self, **per_cli):
        (self.tmp / "cli-control.json").write_text(json.dumps(per_cli))

    def slurm_control(self, **values):
        (self.tmp / "slurm-control.json").write_text(json.dumps(values))

    def release(self, name):
        (self.tmp / f"release-{name}").write_text("go")

    def release_all(self):
        for name in ("claude", "codex", "pi"):
            try:
                self.release(name)
            except OSError:
                pass

    def calls(self, name=None):
        path = self.tmp / "cli-calls.jsonl"
        if not path.exists():
            return []
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        return [r for r in rows if name is None or r["name"] == name]

    def slurm_calls(self, name=None):
        path = self.tmp / "slurm-calls.jsonl"
        if not path.exists():
            return []
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        return [r for r in rows if name is None or r["name"] == name]

    def launch(self, adapter, model, attempt="att-1", thinking=None, prompt="do it"):
        run, error = CIO.launch(adapter, model, thinking, str(self.worktree), prompt,
                                self.state_dir, attempt)
        self.assertIsNone(error)
        return run

    def inspect(self, attempt="att-1"):
        return CIO.inspect_run(self.state_dir, attempt)

    def wait_state(self, state, attempt="att-1"):
        obs = wait_until(lambda: self.inspect(attempt)["state"] == state
                         and self.inspect(attempt))
        self.assertTrue(obs, f"never reached {state}: {self.inspect(attempt)}")
        return obs

    def rundir(self, attempt="att-1"):
        return Path(self.state_dir) / "cli-runs" / attempt

    def all_run_text(self, attempt="att-1"):
        return "\n".join(p.read_text(errors="replace")
                         for p in sorted(self.rundir(attempt).rglob("*")) if p.is_file())


class TestAdapters(unittest.TestCase):
    def test_argv_for_each_adapter_fresh_and_resume(self):
        sid = "e73c852f-6d65-4b6c-a8e0-577cc75d920d"
        self.assertEqual(
            CA.build_argv("claude", "/b/claude", model="m1", worktree="/w",
                          session_id=sid, thinking="high"),
            ["/b/claude", "-p", "--output-format", "json", "--model", "m1",
             "--permission-mode", "bypassPermissions", "--effort", "high",
             "--session-id", sid])
        self.assertEqual(
            CA.build_argv("claude", "/b/claude", model="m1", worktree="/w",
                          session_id=sid, resume=True)[-2:], ["--resume", sid])
        self.assertEqual(
            CA.build_argv("codex", "/b/codex", model="m2", worktree="/w",
                          thinking="xhigh"),
            ["/b/codex", "exec", "--json", "-C", "/w", "-m", "m2",
             "--dangerously-bypass-approvals-and-sandbox", "--skip-git-repo-check",
             "-c", "model_reasoning_effort=xhigh", "-"])
        resumed = CA.build_argv("codex", "/b/codex", model="m2", worktree="/w",
                                session_id=sid, resume=True)
        self.assertEqual(resumed[:4], ["/b/codex", "exec", "resume", sid])
        self.assertNotIn("-C", resumed)
        self.assertEqual(resumed[-1], "-")
        pi = CA.build_argv("pi", "/b/pi", model="openrouter/v/m3", worktree="/w",
                           session_id=sid, thinking="high", session_dir="/r/s")
        self.assertEqual(pi, ["/b/pi", "-p", "--mode", "json", "--session-dir", "/r/s",
                              "--session-id", sid, "--provider", "openrouter",
                              "--model", "v/m3", "--no-extensions", "--no-skills",
                              "--no-mcp", "--thinking", "high"])
        pi_resume = CA.build_argv("pi", "/b/pi", model="openrouter/v/m3", worktree="/w",
                                  session_id=sid, resume=True, session_dir="/r/s")
        self.assertIn("--session", pi_resume)
        self.assertNotIn("--session-id", pi_resume)
        for bad in ("-x", "a b", "", "a//b"):
            with self.assertRaises(CA.AdapterError):
                CA.build_argv("claude", "/b/claude", model=bad, worktree="/w",
                              session_id=sid)

    def test_no_credential_ever_appears_in_argv(self):
        for adapter, model in (("claude", "m"), ("codex", "m"), ("pi", "openrouter/v/m")):
            argv = CA.build_argv(adapter, "/b/x", model=model, worktree="/w",
                                 session_id="e73c852f-6d65-4b6c-a8e0-577cc75d920d",
                                 session_dir="/r")
            self.assertFalse([a for a in argv if "KEY" in a or "TOKEN" in a])

    def test_env_policies(self):
        always = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY",
                  "OPENROUTER_API_KEY", "LOR_SESSION_TOKEN", "LOR_SESSION_ID")
        for adapter in CA.CLI_BACKENDS:
            for name in always:
                self.assertTrue(CA.strip_name(adapter, name), (adapter, name))
            for kept in ("PATH", "HOME", "CODEX_HOME", "CLAUDE_CONFIG_DIR", "HF_TOKEN"):
                self.assertFalse(CA.strip_name(adapter, kept), (adapter, kept))
        self.assertTrue(CA.strip_name("claude", "ANTHROPIC_BASE_URL"))
        self.assertTrue(CA.strip_name("codex", "CODEX_API_KEY"))

    def test_session_parsing_never_guesses(self):
        sid = "e73c852f-6d65-4b6c-a8e0-577cc75d920d"
        self.assertEqual(CA.parse_session("claude", json.dumps({"session_id": sid})), sid)
        self.assertEqual(CA.parse_session("claude", "not json"), None)
        self.assertEqual(CA.parse_session(
            "codex", '{"type":"thread.started","thread_id":"%s"}\n{"type":"x"}' % sid), sid)
        self.assertEqual(CA.parse_session(
            "pi", '{"type":"agent_start"}\n{"type":"session","id":"%s"}' % sid), sid)
        self.assertIsNone(CA.parse_session("pi", '{"type":"session","id":"-bad"}'))

    def test_provider_parsing_and_author_identity(self):
        self.assertEqual(CA.parse_provider("claude", "claude/m1"), "m1")
        self.assertEqual(CA.parse_provider("codex", None, "m2"), "m2")
        self.assertEqual(CA.parse_provider("pi", "pi/openrouter/v/m3"), "openrouter/v/m3")
        for backend, provider in (("claude", "codex/m"), ("pi", "pi/onlyone"),
                                  ("codex", "codex/-x")):
            with self.assertRaises(CA.AdapterError):
                CA.parse_provider(backend, provider)
        self.assertEqual(CA.author_identity("claude", "m1"), "claude/m1")
        self.assertEqual(CA.author_identity("codex", "m2"), "codex/m2")
        self.assertEqual(CA.author_identity("pi", "openrouter/vendor/m3"), "vendor/m3")
        self.assertEqual(CA.author_identity("pi", "vendor/m3"), "vendor/m3")
        for backend, provider, expected in (
                ("claude", "claude/m1", "claude/m1"),
                ("pi", "pi/openrouter/vendor/m3", "vendor/m3")):
            identity = SA.intent_identity({"backend": backend, "provider": provider})
            self.assertEqual(identity, expected)
            self.assertTrue(SA._valid_identity(identity))
        # The launch-intent view shape (route + model) resolves identically.
        self.assertEqual(SA.intent_identity(
            {"backend": "pi", "provider": "pi", "model": "openrouter/vendor/m3"}), "vendor/m3")
        # A Paseo claude/<m> provider without a backend is unchanged.
        self.assertEqual(SA.intent_identity({"provider": "claude/m1"}), "claude/m1")


class TestLocalExecution(CliFixture):
    def test_running_then_stopped_with_receipt_and_contained_env(self):
        self.control(claude={"hang": True})
        run = self.launch("claude", CLAUDE_MODEL, thinking="high")
        self.assertEqual(run["mode"], "local")
        self.assertTrue(isinstance(run["pid_start_us"], int))
        self.assertEqual(run["pgid"], run["pid"])
        self.assertEqual(self.inspect()["state"], "running")
        (call,) = wait_until(lambda: self.calls("claude"))
        self.assertEqual(call["cwd"], str(self.worktree))
        self.assertEqual(call["prompt"], "do it")
        self.assertEqual(call["pgid"], run["pgid"])
        for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY",
                     "OPENROUTER_API_KEY", "LOR_SESSION_TOKEN"):
            self.assertNotIn(name, call["env"])
        self.assertIn("--session-id", call["argv"])
        self.release("claude")
        obs = self.wait_state("stopped")
        self.assertEqual((obs["returncode"], obs["session_id"]), (0, run["session_id"]))
        receipt = json.loads((self.rundir() / "turn-1.exit.json").read_text())
        self.assertEqual(set(receipt), {"turn", "returncode", "started_at",
                                        "finished_at", "session_id", "error", "signalled"})
        self.assertEqual(stat.S_IMODE(self.rundir().stat().st_mode), 0o700)
        for name in ("turn-1.prompt.txt", "turn-1.stdout.log", "turn-1.stderr.log",
                     "run.json", "turn-1.exit.json"):
            self.assertEqual(stat.S_IMODE((self.rundir() / name).stat().st_mode), 0o600, name)
        text = self.all_run_text()
        for secret in SECRETS:
            self.assertNotIn(secret, text)

    def test_survives_the_launching_process_exiting(self):
        self.control(codex={"hang": True})
        code = ("import sys, json; sys.path.insert(0, %r); import cli_io as C; "
                "run, err = C.launch('codex', %r, None, %r, 'p', %r, 'att-1'); "
                "print(json.dumps([run and run['pid'], err]))"
                % (str(SCRIPTS), CODEX_MODEL, str(self.worktree), self.state_dir))
        out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                             text=True, timeout=60, env=dict(os.environ))
        pid, error = json.loads(out.stdout.strip().splitlines()[-1])
        self.assertIsNone(error, out.stderr)
        self.assertEqual(self.inspect()["state"], "running")
        self.release("codex")
        obs = self.wait_state("stopped")
        self.assertEqual(obs["session_id"], "0199aaaa-1111-7000-8000-00000000c0de")
        self.assertEqual(json.loads((self.rundir() / "run.json").read_text())["pid"], pid)

    def test_nonzero_exit_is_failed_never_stopped(self):
        self.control(claude={"rc": 3})
        self.launch("claude", CLAUDE_MODEL)
        obs = self.wait_state("failed")
        self.assertEqual(obs["returncode"], 3)
        notes = []
        verdict, status = CIO.code_lifecycle(
            {"job_id": "att-1", "backend": "claude", "cli_run_dir": str(self.rundir())}, notes)
        self.assertEqual((verdict, status), ("FAILED", "failed"))

    def test_pid_reuse_guard_prevents_false_running_and_wrong_signal(self):
        self.control(claude={"hang": True})
        run = self.launch("claude", CLAUDE_MODEL)
        wait_until(lambda: self.calls("claude"))
        record = json.loads((self.rundir() / "run.json").read_text())
        record["pid_start_us"] = run["pid_start_us"] - 5000000
        (self.rundir() / "run.json").write_text(json.dumps(record))
        obs = self.inspect()
        self.assertEqual(obs["state"], "incomplete")
        self.assertNotEqual(obs["state"], "stopped")
        ok, detail = CIO.cancel(self.state_dir, "att-1", grace=0.2)
        self.assertTrue(ok)
        self.assertIn("not running", detail)
        # The real process was never signalled: it is still alive.
        ident = CIO.process_identity(run["pid"])
        self.assertTrue(isinstance(ident, tuple) and ident[0] == run["pid_start_us"]
                        and not ident[1], ident)
        record["pid_start_us"] = run["pid_start_us"]
        record["status"] = "launched"
        (self.rundir() / "run.json").write_text(json.dumps(record))
        self.assertEqual(self.inspect()["state"], "running")

    def test_dead_without_receipt_is_incomplete_never_done(self):
        self.control(claude={"hang": True})
        run = self.launch("claude", CLAUDE_MODEL)
        wait_until(lambda: self.calls("claude"))
        os.killpg(run["pgid"], signal.SIGKILL)
        obs = self.wait_state("incomplete")
        self.assertIsNone(obs["returncode"])
        notes = []
        verdict, status = CIO.code_lifecycle(
            {"job_id": "att-1", "backend": "claude", "cli_run_dir": str(self.rundir())}, notes)
        self.assertEqual(verdict, "INCOMPLETE")
        self.assertNotEqual(verdict, "DONE")

    def test_cancel_terminates_the_whole_process_group(self):
        self.control(pi={"hang": True})
        run = self.launch("pi", PI_MODEL)
        (call,) = wait_until(lambda: self.calls("pi"))
        child = call["pid"]
        self.assertNotEqual(child, run["pid"])
        ok, detail = CIO.cancel(self.state_dir, "att-1", grace=5)
        self.assertTrue(ok, detail)

        def gone(pid):
            CIO._reap(pid)
            ident = CIO.process_identity(pid)
            return ident is None or (isinstance(ident, tuple) and ident[1])
        self.assertTrue(wait_until(lambda: gone(child) and gone(run["pid"])))
        obs = self.inspect()
        self.assertIn(obs["state"], ("failed", "incomplete"))
        self.assertTrue(obs["cancelled"])
        self.assertEqual(CIO.cancel(self.state_dir, "att-1")[0], True)

    def test_resume_passes_session_and_refuses_while_running(self):
        self.control(claude={"hang": True})
        run = self.launch("claude", CLAUDE_MODEL)
        refused, why = CIO.resume(self.state_dir, "att-1", "next")
        self.assertIsNone(refused)
        self.assertIn("running", why)
        self.release("claude")
        self.wait_state("stopped")
        (self.tmp / "release-claude").unlink()
        turn2, error = CIO.resume(self.state_dir, "att-1", "continue please")
        self.assertIsNone(error)
        self.assertEqual(turn2["turn"], 2)
        self.assertEqual(self.inspect()["state"], "running")
        second = wait_until(lambda: self.calls("claude")[1:])[0]
        self.assertEqual(second["argv"][second["argv"].index("--resume") + 1],
                         run["session_id"])
        self.assertNotIn("--session-id", second["argv"])
        self.assertEqual(second["prompt"], "continue please")
        self.release("claude")
        obs = self.wait_state("stopped")
        self.assertEqual(obs["turn"], 2)
        turns = [json.loads(l) for l in (self.rundir() / "turns.jsonl").read_text().splitlines()]
        self.assertEqual([t["turn"] for t in turns], [1, 2])
        self.assertEqual(turns[1]["resume_of"], run["session_id"])

    def test_codex_resume_uses_the_parsed_thread_id(self):
        self.launch("codex", CODEX_MODEL)
        self.wait_state("stopped")
        _run, error = CIO.resume(self.state_dir, "att-1", "again")
        self.assertIsNone(error)
        self.wait_state("stopped")
        second = self.calls("codex")[1]
        self.assertEqual(second["argv"][:3],
                         ["exec", "resume", "0199aaaa-1111-7000-8000-00000000c0de"])

    def test_resume_without_a_session_refuses(self):
        self.control(codex={"rc": 1})
        # codex prints a thread id even on failure; remove it from the record.
        self.launch("codex", CODEX_MODEL)
        self.wait_state("failed")
        receipt_path = self.rundir() / "turn-1.exit.json"
        receipt = json.loads(receipt_path.read_text())
        receipt["session_id"] = None
        receipt_path.write_text(json.dumps(receipt))
        run, error = CIO.resume(self.state_dir, "att-1", "again")
        self.assertIsNone(run)
        self.assertIn("no recorded codex session id", error)

    def test_pi_key_is_delivered_through_a_private_file_only(self):
        self.control(pi={"hang": True})
        run = self.launch("pi", PI_MODEL, thinking="high")
        (call,) = wait_until(lambda: self.calls("pi"))
        self.assertEqual(call["openrouter"], OPENROUTER_SECRET)
        self.assertFalse((self.rundir() / "turn-1.key").exists())
        for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY",
                     "LOR_SESSION_TOKEN"):
            self.assertNotIn(name, call["env"])
        self.assertFalse([a for a in call["argv"] if OPENROUTER_SECRET in a])
        self.assertEqual(call["argv"][call["argv"].index("--provider") + 1], "openrouter")
        self.assertEqual(call["argv"][call["argv"].index("--session-dir") + 1],
                         run["session_dir"])
        self.release("pi")
        self.wait_state("stopped")
        text = self.all_run_text()
        for secret in SECRETS:
            self.assertNotIn(secret, text)

    def test_pi_without_openrouter_key_fails_closed_before_any_side_effect(self):
        os.environ.pop("OPENROUTER_API_KEY")
        run, error = CIO.launch("pi", PI_MODEL, None, str(self.worktree), "p",
                                self.state_dir, "att-1")
        self.assertIsNone(run)
        self.assertIn("OPENROUTER_API_KEY", error)
        self.assertFalse(self.rundir().exists())
        self.assertEqual(self.calls(), [])

    def test_launch_refuses_a_second_run_for_one_attempt(self):
        self.launch("claude", CLAUDE_MODEL)
        run, error = CIO.launch("claude", CLAUDE_MODEL, None, str(self.worktree), "p",
                                self.state_dir, "att-1")
        self.assertIsNone(run)
        self.assertIn("already exists", error)

    def _stopped_claude_run(self):
        self.launch("claude", CLAUDE_MODEL)
        self.wait_state("stopped")
        return self.rundir()

    def _first_callers_turn_2(self, rdir):
        files = {rdir / "turn-2.prompt.txt": "first caller's prompt",
                 rdir / "turn-2.json": '{"first": true}'}
        for path, text in files.items():
            path.write_text(text)
        return files

    def test_competing_resume_is_refused_without_touching_the_first_callers_files(self):
        rdir = self._stopped_claude_run()
        # The first resume holds the run lock and has created turn 2.
        files = self._first_callers_turn_2(rdir)
        fd = os.open(str(rdir / ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        run, error = CIO.resume(self.state_dir, "att-1", "second caller")
        self.assertIsNone(run)
        self.assertIn("refusing to race", error)
        for path, text in files.items():
            self.assertEqual(path.read_text(), text, path.name)
        self.assertEqual(json.loads((rdir / "run.json").read_text())["turn"], 1)
        self.assertEqual(len(self.calls("claude")), 1)
        # cancel waits for the lock too; it never acts while it is held.
        with mock.patch.object(CIO, "LOCK_WAIT_SECONDS", 0.2):
            ok, detail = CIO.cancel(self.state_dir, "att-1")
        self.assertFalse(ok)
        self.assertIn("refusing to race", detail)

    def test_resume_never_unlinks_turn_files_it_did_not_create(self):
        rdir = self._stopped_claude_run()
        files = self._first_callers_turn_2(rdir)
        run, error = CIO.resume(self.state_dir, "att-1", "second caller")
        self.assertIsNone(run)
        self.assertIn("already exist", error)
        for path, text in files.items():
            self.assertEqual(path.read_text(), text, path.name)
        record = json.loads((rdir / "run.json").read_text())
        self.assertEqual((record["turn"], record["status"]), (1, "launched"))
        self.assertEqual(len(self.calls("claude")), 1)
        # Only turn 2's own json exists: the prompt is ours, the json is not.
        (rdir / "turn-2.prompt.txt").unlink()
        run, error = CIO.resume(self.state_dir, "att-1", "second caller")
        self.assertIsNone(run)
        self.assertFalse((rdir / "turn-2.prompt.txt").exists())
        self.assertEqual((rdir / "turn-2.json").read_text(), '{"first": true}')
        (rdir / "turn-2.json").unlink()
        run, error = CIO.resume(self.state_dir, "att-1", "second caller")
        self.assertIsNone(error)
        self.assertEqual(run["turn"], 2)

    def test_loaded_openrouter_key_is_redacted_from_logs_and_receipt(self):
        self.control(pi={"echo_key": True})
        self.launch("pi", PI_MODEL)
        self.wait_state("stopped")
        (call,) = self.calls("pi")
        self.assertEqual(call["openrouter"], OPENROUTER_SECRET)
        rdir = self.rundir()
        stdout = (rdir / "turn-1.stdout.log").read_text()
        stderr = (rdir / "turn-1.stderr.log").read_text()
        receipt = (rdir / "turn-1.exit.json").read_text()
        # The CLI did print the key on both streams, split across writes.
        self.assertIn("leak=[REDACTED]\n", stdout)
        self.assertIn("err [REDACTED]\n", stderr)
        for name, text in (("stdout", stdout), ("stderr", stderr), ("exit", receipt)):
            self.assertNotIn(OPENROUTER_SECRET, text, name)
            self.assertNotIn(OPENROUTER_SECRET[:len(OPENROUTER_SECRET) // 2], text, name)
        # The session line after the key still parses from the redacted log.
        self.assertEqual(json.loads(receipt)["session_id"],
                         json.loads((rdir / "run.json").read_text())["session_id"])
        for secret in SECRETS:
            self.assertNotIn(secret, self.all_run_text())

    def test_redacting_sink_handles_every_chunk_boundary(self):
        secret = b"sk-or-v1-0123456789abcdef"
        stream = b"a" + secret + b"b" + secret + secret + b"tail" + secret[:7]
        for size in range(1, len(stream) + 1):
            with self.subTest(size=size):
                out = io.BytesIO()
                out.close = lambda: None
                sink = CIO._RedactingSink(out, secret)
                for i in range(0, len(stream), size):
                    sink.write(stream[i:i + size])
                sink.close()
                self.assertEqual(out.getvalue(),
                                 b"a[REDACTED]b[REDACTED][REDACTED]tail" + secret[:7])

    def test_pump_join_timeout_flushes_held_back_output_before_receipt(self):
        # The session message is shorter than the secret, so the redacting
        # sink holds all of it back; a background descendant keeps the pipes
        # open past the join timeout. The flush must still land it in the
        # log before the receipt is written.
        secret = "sk-or-v1-" + "f" * 200
        sid = "e73c852f-6d65-4b6c-a8e0-577cc75d920d"
        self.control(pi={"background_sleep": 30})
        pid_file = self.tmp / "sleeper-pi.pid"

        def kill_sleeper():
            try:
                os.kill(int(pid_file.read_text()), signal.SIGKILL)
            except (OSError, ValueError):
                pass
        self.addCleanup(kill_sleeper)
        rdir = self.tmp / "direct-run"
        rdir.mkdir()
        (rdir / "turn-1.prompt.txt").write_text("direct")
        (rdir / "turn-1.key").write_text(secret)
        (rdir / "turn-1.json").write_text(json.dumps({
            "turn": 1, "adapter": "pi",
            "argv": [str(self.bin / "pi"), "--session-id", sid],
            "cwd": str(self.worktree), "stdin": str(rdir / "turn-1.prompt.txt"),
            "stdout": str(rdir / "turn-1.stdout.log"),
            "stderr": str(rdir / "turn-1.stderr.log"),
            "exit": str(rdir / "turn-1.exit.json"),
            "key_file": str(rdir / "turn-1.key"),
            "key_env": CA.OPENROUTER_KEY, "fallback_session_id": None}))
        handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGHUP)}
        self.addCleanup(lambda: [signal.signal(k, v) for k, v in handlers.items()])
        seen = {}
        real_write = CIO._write_atomic

        def receipt_write(path, record):
            if str(path).endswith("exit.json"):
                seen["stdout"] = (rdir / "turn-1.stdout.log").read_text()
            return real_write(path, record)
        started = time.monotonic()
        with mock.patch.object(CIO, "PUMP_DRAIN_SECONDS", 0.5), \
                mock.patch.object(CIO, "_write_atomic", receipt_write):
            self.assertEqual(CIO.run_turn(rdir, 1), 0)
        self.assertLess(time.monotonic() - started, 20)
        self.assertTrue(pid_file.exists(), "the fake CLI never started its sleeper")
        message = json.dumps({"type": "session", "id": sid})
        self.assertLess(len(message), len(secret))
        self.assertIn(message, seen["stdout"])
        receipt = json.loads((rdir / "turn-1.exit.json").read_text())
        self.assertEqual((receipt["returncode"], receipt["session_id"]), (0, sid))
        self.assertNotIn(secret, self.all_text(rdir))

    def all_text(self, rdir):
        return "\n".join(p.read_text(errors="replace")
                         for p in sorted(Path(rdir).rglob("*")) if p.is_file())

    def test_redacting_sink_close_is_idempotent_and_drops_late_writes(self):
        out = io.BytesIO()
        closes = []
        out.close = lambda: closes.append(True)
        sink = CIO._RedactingSink(out, b"0123456789")
        sink.write(b"short")
        sink.close()
        sink.write(b"late")
        sink.close()
        self.assertEqual((out.getvalue(), len(closes)), (b"short", 1))

    def test_run_turn_called_directly_still_withholds_adapter_names(self):
        os.environ["ANTHROPIC_BASE_URL"] = "https://fixture.invalid"
        rdir = self.tmp / "direct-run"
        rdir.mkdir()
        (rdir / "turn-1.prompt.txt").write_text("direct")
        (rdir / "turn-1.json").write_text(json.dumps({
            "turn": 1, "adapter": "claude", "argv": [str(self.bin / "claude"), "-p"],
            "cwd": str(self.worktree), "stdin": str(rdir / "turn-1.prompt.txt"),
            "stdout": str(rdir / "turn-1.stdout.log"),
            "stderr": str(rdir / "turn-1.stderr.log"),
            "exit": str(rdir / "turn-1.exit.json"), "key_file": None,
            "key_env": None, "fallback_session_id": None}))
        handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGHUP)}
        self.addCleanup(lambda: [signal.signal(k, v) for k, v in handlers.items()])
        self.assertIn("ANTHROPIC_API_KEY", os.environ)
        self.assertEqual(CIO.run_turn(rdir, 1), 0)
        (call,) = self.calls("claude")
        for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY",
                     "OPENROUTER_API_KEY", "LOR_SESSION_TOKEN", "ANTHROPIC_BASE_URL"):
            self.assertNotIn(name, call["env"])


class TestSlurmExecution(CliFixture):
    slurm = True

    def test_preexisting_batch_script_is_refused_and_left_in_place(self):
        self.slurm_control(job_id="4242", squeue={"4242": "PENDING"})
        run = self.launch("pi", PI_MODEL)
        rdir = self.rundir()
        script = rdir / "turn-2.sbatch.sh"
        script.write_text("#!/bin/sh\necho someone else's\n")
        updates, error = CIO._start_turn(rdir, run, 2, "next", key_value="k")
        self.assertIsNone(updates)
        self.assertIn("already exists", error)
        self.assertEqual(script.read_text(), "#!/bin/sh\necho someone else's\n")
        self.assertEqual(len(self.slurm_calls("sbatch")), 1)
        for name in ("prompt.txt", "key", "json"):
            self.assertFalse((rdir / f"turn-2.{name}").exists(), name)

    def test_cleanup_that_cannot_unlink_never_raises(self):
        run = self.launch_record_only()
        with mock.patch.object(Path, "unlink",
                               side_effect=PermissionError("read-only")):
            updates, error = CIO._start_turn(self.rundir(), run, 2, "next",
                                             key_value="k")
        self.assertIsNone(updates)
        self.assertIn("sbatch refused", error)

    def launch_record_only(self):
        self.slurm_control(job_id="4242", squeue={"4242": "PENDING"})
        run = self.launch("pi", PI_MODEL)
        self.slurm_control(sbatch_fail=True)
        return run

    def test_refused_submit_removes_the_batch_script_this_call_wrote(self):
        self.slurm_control(sbatch_fail=True)
        run, error = CIO.launch("pi", PI_MODEL, None, str(self.worktree), "do it",
                                self.state_dir, "att-1")
        self.assertIsNone(run)
        self.assertFalse((self.rundir() / "turn-1.sbatch.sh").exists())
        self.assertFalse((self.rundir() / "turn-1.key").exists())

    def test_login_host_submits_with_export_none_and_never_spawns_locally(self):
        self.slurm_control(job_id="4242", squeue={"4242": "PENDING"})
        self.assertEqual(CIO.placement(), ("slurm", None))
        with mock.patch.object(CIO, "_spawn_local",
                               side_effect=AssertionError("local spawn")):
            run = self.launch("pi", PI_MODEL)
        self.assertEqual((run["mode"], run["slurm_job_id"], run["pid"]), ("slurm", "4242", None))
        (submit,) = self.slurm_calls("sbatch")
        self.assertIn("--export=NONE", submit["argv"])
        self.assertIn("--no-requeue", submit["argv"])
        self.assertTrue(any(a.startswith("--mem=") for a in submit["argv"]))
        for name in ("OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
                     "LOR_SESSION_TOKEN", "ANTHROPIC_AUTH_TOKEN"):
            self.assertNotIn(name, submit["env"])
        self.assertIn("env -i", submit["script"])
        for secret in SECRETS:
            self.assertNotIn(secret, submit["script"])
            self.assertNotIn(secret, json.dumps(submit))
        self.assertTrue((self.rundir() / "turn-1.key").exists())
        self.assertEqual(stat.S_IMODE((self.rundir() / "turn-1.key").stat().st_mode), 0o600)
        self.assertEqual(self.inspect()["state"], "running")
        # Run the generated script as the compute node would.
        script = self.rundir() / "turn-1.sbatch.sh"
        done = subprocess.run(["bash", str(script)], env={"PATH": str(self.bin)},
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertFalse((self.rundir() / "turn-1.key").exists())
        (call,) = self.calls("pi")
        self.assertEqual(call["openrouter"], OPENROUTER_SECRET)
        self.assertNotIn("ANTHROPIC_API_KEY", call["env"])
        self.slurm_control(job_id="4242", squeue={})
        obs = self.inspect()
        self.assertEqual((obs["state"], obs["returncode"]), ("stopped", 0))
        text = self.all_run_text()
        for secret in SECRETS:
            self.assertNotIn(secret, text)

    def test_sbatch_failure_fails_closed(self):
        self.slurm_control(sbatch_fail=True)
        with mock.patch.object(CIO, "_spawn_local",
                               side_effect=AssertionError("local spawn")):
            run, error = CIO.launch("pi", PI_MODEL, None, str(self.worktree), "p",
                                    self.state_dir, "att-1")
        self.assertIsNone(run)
        self.assertIn("failing closed", error)
        self.assertFalse((self.rundir() / "turn-1.key").exists())
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.inspect()["state"], "failed")
        self.assertEqual(json.loads((self.rundir() / "run.json").read_text())["status"],
                         "launch_failed")

    def test_scheduler_states_map_truthfully(self):
        self.slurm_control(job_id="4242", squeue={"4242": "RUNNING"})
        self.launch("claude", CLAUDE_MODEL)
        self.assertEqual(self.inspect()["state"], "running")
        for final, expected in (("PREEMPTED", "preempted"), ("FAILED", "failed"),
                                ("NODE_FAIL", "failed"), ("CANCELLED+", "failed"),
                                ("COMPLETED", "incomplete"), (None, "incomplete")):
            with self.subTest(final=final):
                self.slurm_control(job_id="4242", squeue={},
                                   final={"4242": final} if final else {})
                self.assertEqual(self.inspect()["state"], expected)
        self.slurm_control(squeue_fail=True)
        self.assertEqual(self.inspect()["state"], "unknown")
        self.slurm_control(squeue={}, final={"4242": "PREEMPTED"})
        verdict, _status = CIO.code_lifecycle(
            {"job_id": "att-1", "backend": "claude", "cli_run_dir": str(self.rundir())}, [])
        self.assertEqual(verdict, "PREEMPTED")

    def test_failed_resume_submission_leaves_turn_retryable(self):
        self.slurm_control(job_id="4242", squeue={})
        self.launch("claude", CLAUDE_MODEL)
        receipt = {"turn": 1, "returncode": 0, "session_id": None, "error": None}
        (self.rundir() / "turn-1.exit.json").write_text(json.dumps(receipt))
        self.slurm_control(sbatch_fail=True)
        run, error = CIO.resume(self.state_dir, "att-1", "again")
        self.assertIsNone(run)
        self.assertIn("failing closed", error)
        self.assertFalse((self.rundir() / "turn-2.prompt.txt").exists())
        self.assertEqual(json.loads((self.rundir() / "run.json").read_text())["turn"], 1)
        self.slurm_control(job_id="4243", squeue={"4243": "PENDING"})
        run, error = CIO.resume(self.state_dir, "att-1", "again")
        self.assertIsNone(error)
        self.assertEqual((run["turn"], run["slurm_job_id"]), (2, "4243"))

    def test_cancel_uses_scancel(self):
        self.slurm_control(job_id="4242", squeue={"4242": "RUNNING"})
        self.launch("claude", CLAUDE_MODEL)
        ok, detail = CIO.cancel(self.state_dir, "att-1")
        self.assertTrue(ok, detail)
        (cancel,) = self.slurm_calls("scancel")
        self.assertEqual(cancel["argv"], ["4242"])

    def test_placement_modes(self):
        os.environ["HANIG_SWARM_CLI_SLURM"] = "off"
        self.assertEqual(CIO.placement(), ("local", None))
        os.environ["HANIG_SWARM_CLI_SLURM"] = "auto"
        os.environ["SLURM_JOB_ID"] = "77"
        os.environ["SLURM_JOB_NAME"] = "batch-work"
        self.assertEqual(CIO.placement(), ("local", None))
        os.environ["SLURM_JOB_NAME"] = "sh_agent"
        self.assertEqual(CIO.placement(), ("slurm", None))
        for name in ("SLURM_JOB_ID", "SLURM_JOB_NAME"):
            os.environ.pop(name)
        (self.bin / "sbatch").unlink()
        self.assertEqual(CIO.placement(), ("local", None))
        os.environ["HANIG_SWARM_CLI_SLURM"] = "on"
        mode, problem = CIO.placement()
        self.assertIsNone(mode)
        self.assertIn("sbatch", problem)
        run, error = CIO.launch("claude", CLAUDE_MODEL, None, str(self.worktree), "p",
                                self.state_dir, "att-1")
        self.assertIsNone(run)
        self.assertFalse(self.rundir().exists())

    def test_agent_host_job_without_slurm_tools_fails_closed(self):
        os.environ["SLURM_JOB_ID"] = "123"
        os.environ["SLURM_JOB_NAME"] = "sh_agent"
        self.assertEqual(CIO.placement(), ("slurm", None))
        for gone in (("sbatch",), ("squeue",), ("scancel",),
                     ("sbatch", "squeue", "scancel")):
            with self.subTest(missing=gone):
                for name in gone:
                    (self.bin / name).rename(self.tmp / name)
                try:
                    mode, problem = CIO.placement()
                    self.assertIsNone(mode)
                    self.assertNotEqual(mode, "local")
                    for name in gone:
                        self.assertIn(name, problem)
                    self.assertIn("agent-host", problem)
                    run, error = CIO.launch("claude", CLAUDE_MODEL, None,
                                            str(self.worktree), "p",
                                            self.state_dir, "att-1")
                    self.assertIsNone(run)
                    self.assertEqual(error, problem)
                    self.assertFalse(self.rundir().exists())
                    self.assertEqual(self.calls(), [])
                finally:
                    for name in gone:
                        (self.tmp / name).rename(self.bin / name)
        # An explicit off still runs it here, as the refusal says.
        (self.bin / "sbatch").unlink()
        os.environ["HANIG_SWARM_CLI_SLURM"] = "off"
        self.assertEqual(CIO.placement(), ("local", None))

    def test_resume_of_a_local_run_refuses_on_a_slurm_placement_host(self):
        os.environ["HANIG_SWARM_CLI_SLURM"] = "off"
        run = self.launch("claude", CLAUDE_MODEL)
        self.assertEqual(run["mode"], "local")
        self.wait_state("stopped")
        hosts = ({"HANIG_SWARM_CLI_SLURM": "auto", "SLURM_JOB_ID": "123",
                  "SLURM_JOB_NAME": "sh_agent"},
                 {"HANIG_SWARM_CLI_SLURM": "auto"},
                 {"HANIG_SWARM_CLI_SLURM": "on"})
        for host in hosts:
            with self.subTest(host=host), mock.patch.dict(os.environ, host), \
                    mock.patch.object(CIO, "_spawn_local",
                                      side_effect=AssertionError("local spawn")):
                self.assertEqual(CIO.placement()[0], "slurm")
                again, error = CIO.resume(self.state_dir, "att-1", "next")
                self.assertIsNone(again)
                self.assertIn("local placement", error)
                self.assertEqual(self.slurm_calls("sbatch"), [])
                self.assertEqual(len(self.calls("claude")), 1)
                self.assertFalse((self.rundir() / "turn-2.prompt.txt").exists())
                self.assertEqual(json.loads((self.rundir() / "run.json")
                                            .read_text())["turn"], 1)
        again, error = CIO.resume(self.state_dir, "att-1", "next")
        self.assertIsNone(error)
        self.assertEqual((again["turn"], again["mode"]), (2, "local"))
        self.assertEqual(self.slurm_calls("sbatch"), [])

    def test_key_file_that_cannot_be_unlinked_refuses_to_start_pi(self):
        if os.geteuid() == 0:
            self.skipTest("root ignores directory write permission")
        self.slurm_control(job_id="4242", squeue={"4242": "PENDING"})
        self.launch("pi", PI_MODEL)
        rdir = self.rundir()
        spec_path = rdir / "turn-1.json"
        spec = json.loads(spec_path.read_text())
        # Move the 0600 key into a read-only directory: readable, not removable.
        locked = rdir / "read-only"
        locked.mkdir(mode=0o700)
        key = locked / "turn-1.key"
        os.replace(spec["key_file"], str(key))
        spec["key_file"] = str(key)
        spec_path.write_text(json.dumps(spec))
        locked.chmod(0o500)
        self.addCleanup(locked.chmod, 0o700)
        self.assertEqual(stat.S_IMODE(key.stat().st_mode), 0o600)
        done = subprocess.run(["bash", str(rdir / "turn-1.sbatch.sh")],
                              env={"PATH": str(self.bin)},
                              capture_output=True, text=True, timeout=60)
        self.assertNotEqual(done.returncode, 0)
        self.assertEqual(self.calls("pi"), [])
        self.assertTrue(key.exists())
        receipt = json.loads((rdir / "turn-1.exit.json").read_text())
        self.assertNotEqual(receipt["returncode"], 0)
        self.assertIn("cannot remove the private key file", receipt["error"])
        self.assertIn("refusing to start the CLI", receipt["error"])
        self.slurm_control(job_id="4242", squeue={})
        self.assertEqual(self.inspect()["state"], "failed")
        self.assertNotIn(OPENROUTER_SECRET, (rdir / "turn-1.exit.json").read_text())

    def test_partition_override_and_auto_batch_need_no_names_in_code(self):
        os.environ["HANIG_SWARM_CLI_SLURM_PARTITION"] = "fixturepart"
        argv = CIO.sbatch_argv("/b/sbatch", "att-1", "/r", 1, "/r/s.sh",
                               auto_partition="ignored_batch")
        self.assertIn("--partition=fixturepart", argv)
        self.assertNotIn("--partition=ignored_batch", argv)


class SwarmCliFixture(CliFixture):
    def setUp(self):
        super().setUp()
        os.environ["HANIG_SWARM_CLI_SLURM"] = "off"
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

    def unit(self, uid, **extra):
        u = {"id": uid, "kind": "code", "repo": str(self.repo),
             "target_branch": "main", "prompt": "change tracked.txt",
             "inputs": [], "outputs": ["evidence.md", "test-output.txt"]}
        u.update(extra)
        return u

    def write_plan(self, plan):
        self.plan_path.write_text(json.dumps(plan, indent=1))
        return plan

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

    def intent(self, state, uid):
        us = state["units"][uid]
        return us["attempt_launch_intents"][Path(us["attempt_dir"]).name]

    def paseo_calls(self):
        return json.loads(self.paseo_state.read_text())["calls"]


class TestSwarmIntegration(SwarmCliFixture):
    def test_set_agent_claude_dispatches_binds_judges_and_cleans_up(self):
        self.control(claude={"hang": True})
        plan = self.write_plan({"name": "p", "units": [self.unit("c", mode="full-access")]})
        plan_bytes = self.plan_path.read_bytes()
        digest = SPR.plan_digest(plan)
        rc, out, err = self.set_agent(backend="claude", model=CLAUDE_MODEL, thinking="high")
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out)["project_override"],
                         {"backend": "claude", "provider": "claude/" + CLAUDE_MODEL,
                          "thinking": "high"})
        self.assertEqual(self.plan_path.read_bytes(), plan_bytes)
        state, report, dispatched, _h = self.advance(plan)
        self.assertEqual(dispatched, 1, report)
        self.assertEqual(state["plan_digest"], digest)
        us = state["units"]["c"]
        attempt_dir = Path(us["attempt_dir"])
        attempt = attempt_dir.name
        intent = self.intent(state, "c")
        self.assertEqual((intent["backend"], intent["cli_model"], intent["effort"],
                          intent["cli_placement"], intent["author_identity"]),
                         ("claude", CLAUDE_MODEL, "high", "local", "claude/" + CLAUDE_MODEL))
        self.assertEqual(us["job_id"], attempt)
        spec = json.loads((attempt_dir / "unit.json").read_text())
        self.assertEqual((spec["backend"], spec["cli_run_dir"]),
                         ("claude", str(Path(self.state_dir).absolute() / "cli-runs" / attempt)))
        self.assertEqual(us["code_terminal_watches"][attempt]["status"], "cli_turn_receipt")
        (call,) = wait_until(lambda: self.calls("claude"))
        workspace = Path(S.trusted_launch_facts(state, "c", str(attempt_dir))["execution_workspace"])
        self.assertEqual(call["cwd"], str(workspace))
        self.assertIn("SWARM ATTEMPT CONTEXT", call["prompt"])
        self.assertIn("--author claude/" + CLAUDE_MODEL, call["prompt"])
        for secret in SECRETS:
            self.assertNotIn(secret, call["prompt"])
        self.assertNotIn("ANTHROPIC_API_KEY", call["env"])
        self.assertEqual(git(workspace, "status", "--porcelain", "--untracked-files=all"), "")
        rows = {r["id"]: r for r in S.status_report(plan, state, self.state_dir)["units"]}
        self.assertEqual(rows["c"]["backend"], "claude")
        # Still running: advance judges RUNNING, never touching Paseo.
        state, report, _d, _h = self.advance(plan)
        self.assertEqual(state["units"]["c"]["state"], "RUNNING", report)
        # Turn exits 0 with no outputs: INCOMPLETE/NO_OUTPUTS, never DONE.
        self.release("claude")
        wait_until(lambda: CIO.inspect_run(self.state_dir, attempt)["state"] == "stopped")
        state, report, _d, _h = self.advance(plan)
        self.assertEqual(state["units"]["c"]["state"], "INCOMPLETE", report)
        self.assertEqual(SE._receipt_reason(state, "c", str(attempt_dir)),
                         S.U.REASON_NO_OUTPUTS)
        # Outputs plus a pushed produced change: READY_FOR_PR through the same checks.
        facts = S.trusted_launch_facts(state, "c", str(attempt_dir))
        (workspace / "tracked.txt").write_text("changed by claude\n")
        git(workspace, "commit", "-qam", "claude work")
        git(workspace, "push", "-q", "origin", f"HEAD:refs/heads/{facts['branch']}")
        for output in ("evidence.md", "test-output.txt"):
            (attempt_dir / output).write_text("fixture\n")
        state, report, _d, _h = self.advance(plan)
        self.assertEqual(state["units"]["c"]["state"], "READY_FOR_PR", report)
        self.assertEqual(self.paseo_calls(), [])

    def test_continuation_resumes_the_same_session(self):
        plan = self.write_plan({"name": "p", "units": [self.unit(
            "c", backend="claude", model=CLAUDE_MODEL, continuation={"max": 1})]})
        state, report, dispatched, _h = self.advance(plan)
        self.assertEqual(dispatched, 1, report)
        attempt = Path(state["units"]["c"]["attempt_dir"]).name
        wait_until(lambda: CIO.inspect_run(self.state_dir, attempt)["state"] == "stopped")
        state, report, _d, _h = self.advance(plan)
        self.assertEqual(state["units"]["c"]["state"], "INCOMPLETE", report)
        ok, why = S.acquire_lease(self.state_dir)
        self.assertTrue(ok, why)
        try:
            st = S.load_state(self.state_dir)
            st["units"]["c"]["incomplete_since"] = 1.0
            S.save_state(self.state_dir, st)
        finally:
            S.release_lease(self.state_dir)
        state, report, _d, _h = self.advance(plan)
        self.assertEqual(state["units"]["c"]["continuations"][0]["backend"], "claude")
        self.assertTrue(state["units"]["c"]["continuations"][0]["sent"], report)
        second = wait_until(lambda: self.calls("claude")[1:])[0]
        first = self.calls("claude")[0]
        sid = first["argv"][first["argv"].index("--session-id") + 1]
        self.assertEqual(second["argv"][second["argv"].index("--resume") + 1], sid)
        self.assertIn("declared outputs", second["prompt"])
        self.assertEqual(self.paseo_calls(), [])

    def test_mixed_backends_in_flight_coexist(self):
        os.environ["LOR_SESSION_ID"] = PARENT
        self.control(claude={"hang": True}, codex={"hang": True}, pi={"hang": True})
        plan = self.write_plan({"name": "mixed", "units": [
            self.unit("a", mode="full-access"),
            self.unit("l", provider="lor/" + LOR_MODEL),
            self.unit("c", backend="claude", model=CLAUDE_MODEL),
            self.unit("x", backend="codex", provider="codex/" + CODEX_MODEL, thinking="xhigh"),
            self.unit("p", backend="pi", model=PI_MODEL)]})
        state, report, dispatched, _h = self.advance(plan)
        self.assertEqual(dispatched, 5, report)
        backends = {uid: self.intent(state, uid)["backend"] for uid in "alcxp"}
        self.assertEqual(backends, {"a": "paseo", "l": "lor", "c": "claude",
                                    "x": "codex", "p": "pi"})
        self.assertEqual(self.intent(state, "p")["author_identity"],
                         "fixturevendor/fixture-pi-model")
        self.assertEqual(self.intent(state, "x")["effort"], "xhigh")
        wait_until(lambda: len(self.calls()) == 3)
        self.assertEqual(sorted(c["name"] for c in self.calls()), ["claude", "codex", "pi"])
        paseo_runs = [c for c in self.paseo_calls() if c[:1] == ["run"]]
        self.assertEqual(len(paseo_runs), 1)
        rows = {r["id"]: r for r in S.status_report(plan, state, self.state_dir)["units"]}
        self.assertEqual({uid: rows[uid]["backend"] for uid in "alcxp"}, backends)
        state, report, _d, _h = self.advance(plan)
        for uid in "alcxp":
            self.assertEqual(state["units"][uid]["state"], "RUNNING", (uid, report))
        inspected = [c for c in self.paseo_calls() if c[:1] == ["inspect"]]
        for uid in "cxp":
            run_id = state["units"][uid]["job_id"]
            self.assertTrue(all(run_id not in c for c in inspected))
        # Finish only codex; the others stay RUNNING on their own backends.
        self.release("codex")
        attempt = state["units"]["x"]["job_id"]
        wait_until(lambda: CIO.inspect_run(self.state_dir, attempt)["state"] == "stopped")
        state, report, _d, _h = self.advance(plan)
        self.assertEqual(state["units"]["x"]["state"], "INCOMPLETE", report)
        for uid in "alcp":
            self.assertEqual(state["units"][uid]["state"], "RUNNING", (uid, report))

    def test_set_agent_fail_early_for_pi_without_key_changes_nothing(self):
        self.write_plan({"name": "p", "units": [self.unit("c", mode="full-access")]})
        os.environ.pop("OPENROUTER_API_KEY")
        rc, _out, err = self.set_agent(backend="pi", model=PI_MODEL)
        self.assertEqual(rc, S.EXIT_USAGE)
        self.assertIn("OPENROUTER_API_KEY", err)
        self.assertIn("nothing was changed", err)
        self.assertFalse((Path(self.state_dir) / "agent-overrides.json").exists())
        rc, _out, err = self.set_agent(provider="claude/" + CLAUDE_MODEL, mode="full-access")
        self.assertEqual(rc, 0, err)
        record, _problem = S.SS.load_agent_overrides(self.state_dir)
        self.assertEqual(record["project"]["backend"], "paseo")

    def test_plan_validation_for_direct_cli_units(self):
        ok = {"name": "p", "units": [self.unit("c", backend="claude", model=CLAUDE_MODEL)]}
        S.validate_plan(ok)
        for bad, needle in (
                (self.unit("c", backend="claude"), "names neither"),
                (self.unit("c", backend="pi", model="onlyone"), "pi model"),
                (self.unit("c", backend="codex", model=CODEX_MODEL, mode="plan"), "restrictive"),
                (self.unit("c", backend="claude", provider="codex/" + CLAUDE_MODEL), "needs provider")):
            with self.subTest(needle=needle):
                with self.assertRaises(S.PlanError) as caught:
                    S.validate_plan({"name": "p", "units": [bad]})
                self.assertIn(needle, str(caught.exception))
        agent = S.SR.resolve_effective_agent({"code_agent": {"backend": "codex",
                                                             "model": CODEX_MODEL}},
                                             self.unit("c"))
        self.assertEqual((agent["backend"], agent["cli_model"], agent["source"]),
                         ("codex", CODEX_MODEL, "plan-code_agent"))
        # Legacy: a claude/<m> provider with no backend is still Paseo.
        legacy = S.SR.resolve_effective_agent({}, self.unit("c", provider="claude/" + CLAUDE_MODEL))
        self.assertEqual(legacy["backend"], "paseo")

    def test_bind_refuses_inconsistent_cli_flags(self):
        plan = {"name": "p", "units": [self.unit("c", backend="claude", model=CLAUDE_MODEL)]}
        directory, error = S._allocate(plan, plan["units"][0], self.root, self.state_dir)
        self.assertIsNone(error)
        name = Path(directory).name
        unit_py = [sys.executable, str(SCRIPTS / "unit.py"), "bind", directory,
                   "--state-dir", self.state_dir]
        good_dir = str(Path(self.state_dir).absolute() / "cli-runs" / name)
        for extra, needle in (
                (["--job-id", name, "--backend", "claude"], "--cli-run-dir"),
                (["--job-id", name, "--backend", "claude", "--cli-run-dir", "relative"],
                 "--cli-run-dir"),
                (["--job-id=-x", "--backend", "codex", "--cli-run-dir", good_dir],
                 "implausible"),
                (["--job-id", name, "--backend", "claude", "--cli-run-dir", good_dir,
                  "--parent-session-id", PARENT], "only to --backend lor"),
                (["--job-id", "aaaaaaaa-0000-4000-8000-000000000001",
                  "--cli-run-dir", good_dir], "only to --backend")):
            with self.subTest(extra=extra):
                result = subprocess.run(unit_py + extra, capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(needle, result.stderr)

    def test_abandon_cancels_a_running_cli_turn(self):
        self.control(claude={"hang": True})
        plan = self.write_plan({"name": "p", "units": [
            self.unit("c", backend="claude", model=CLAUDE_MODEL)]})
        state, report, dispatched, _h = self.advance(plan)
        self.assertEqual(dispatched, 1, report)
        (call,) = wait_until(lambda: self.calls("claude"))
        args = Namespace(command="abandon", plan=str(self.plan_path), unit="c",
                         approver="tester", reason="obsolete", by=None,
                         state_dir=self.state_dir)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(S.cmd_stop_unit(args), S.EXIT_OK)
        state = S.load_state(self.state_dir)
        us = state["units"]["c"]
        self.assertEqual(us["state"], "ABANDONED")
        attempt = Path(us["attempt_dir"]).name
        self.assertTrue(us["attempt_cli_stops"][attempt]["ok"])
        self.assertEqual(us["terminal_resource_release"]["job_id"], attempt)
        run = json.loads((Path(self.state_dir) / "cli-runs" / attempt / "run.json").read_text())
        self.assertEqual(run["status"], "cancelled")
        self.assertTrue(wait_until(lambda: CIO.process_identity(call["pid"]) is None
                                   or CIO.process_identity(call["pid"])[1]))
        self.assertEqual(self.paseo_calls(), [])

    def test_orphan_run_is_recovered_not_relaunched(self):
        plan = self.write_plan({"name": "p", "units": [
            self.unit("c", backend="claude", model=CLAUDE_MODEL)]})
        state, _report, _d, _h = self.advance(plan)
        us = state["units"]["c"]
        job, note = S.reconcile_orphan(us["attempt_dir"], kind="code",
                                       intent=self.intent(state, "c"))
        self.assertEqual(job, Path(us["attempt_dir"]).name)
        self.assertIn("never bound", note)
        fresh = dict(self.intent(state, "c"))
        self.assertEqual(S.reconcile_orphan(str(Path(self.root) / "c" / "nope"),
                                            kind="code", intent=fresh), (None, None))
        self.assertEqual(S.reconcile_orphan(us["attempt_dir"], kind="code",
                                            intent=dict(fresh, cli_runs_root=None)),
                         (None, "UNKNOWN"))


class TestSwarmFrozenPlacement(SwarmCliFixture):
    slurm = True

    def test_frozen_local_retry_refuses_when_this_host_forces_slurm(self):
        plan = self.write_plan({"name": "p", "units": [
            self.unit("c", backend="claude", model=CLAUDE_MODEL)]})
        original = S.SSB._launch_cli_agent
        seen = {}

        def retried_on_a_slurm_host(*args, **kwargs):
            # The intent is frozen (local); the host that launches it now
            # forces Slurm placement.
            os.environ["HANIG_SWARM_CLI_SLURM"] = "on"
            seen["intent"] = dict(args[5])
            seen["result"] = original(*args, **kwargs)
            return seen["result"]
        with mock.patch.object(S.SSB, "_launch_cli_agent", retried_on_a_slurm_host), \
                mock.patch.object(CIO, "_spawn_local",
                                  side_effect=AssertionError("local spawn")):
            state, report, dispatched, _h = self.advance(plan)
        self.assertEqual(seen["intent"]["cli_placement"], "local")
        job, error = seen["result"]
        self.assertIsNone(job)
        self.assertIn("frozen to local", error)
        self.assertIn("HANIG_SWARM_CLI_SLURM=off", error)
        self.assertEqual(dispatched, 0, report)
        self.assertEqual(self.slurm_calls(), [])
        self.assertEqual(self.calls(), [])
        runs = Path(self.state_dir) / "cli-runs"
        self.assertEqual(list(runs.iterdir()) if runs.exists() else [], [])


class TestSwarmFrozenPlacementRefused(SwarmCliFixture):
    slurm = False

    def test_frozen_local_retry_refuses_when_this_host_refuses_placement(self):
        plan = self.write_plan({"name": "p", "units": [
            self.unit("c", backend="claude", model=CLAUDE_MODEL)]})
        original = S.SSB._launch_cli_agent
        seen = {}

        def retried_where_placement_is_refused(*args, **kwargs):
            # Frozen local at dispatch; this host now forces Slurm without
            # the Slurm tools, so placement itself refuses.
            os.environ["HANIG_SWARM_CLI_SLURM"] = "on"
            seen["intent"] = dict(args[5])
            seen["unit_dir"] = Path(args[1])
            seen["result"] = original(*args, **kwargs)
            return seen["result"]
        with mock.patch.object(S.SSB, "_launch_cli_agent",
                               retried_where_placement_is_refused), \
                mock.patch.object(CIO, "_spawn_local",
                                  side_effect=AssertionError("local spawn")):
            state, report, dispatched, _h = self.advance(plan)
        self.assertEqual(seen["intent"]["cli_placement"], "local")
        job, error = seen["result"]
        self.assertIsNone(job)
        self.assertIn("HANIG_SWARM_CLI_SLURM=on but", error)
        self.assertEqual(dispatched, 0, report)
        self.assertEqual(self.calls(), [])
        # Refused before any attempt context was written, and recorded as a
        # placement refusal rather than a failed launch.
        self.assertFalse((seen["unit_dir"] / "context.json").exists())
        (meta,) = state["units"]["c"]["attempt_workspaces"].values()
        self.assertEqual((meta["verification"], meta["verification_problem"]),
                         ("refused", "claude placement refused"))
        runs = Path(self.state_dir) / "cli-runs"
        self.assertEqual(list(runs.iterdir()) if runs.exists() else [], [])


class TestWaitEventPollCli(CliFixture):
    def state_with_cli_attempt(self, directory):
        directory.mkdir(parents=True)
        (directory / "swarm-state.json").write_text(json.dumps({"units": {"c": {
            "state": "RUNNING", "attempt_dir": "/runs/c/att-1", "job_id": "att-1",
            "attempt_launch_intents": {"att-1": {
                "backend": "claude",
                "cli_runs_root": str(Path(self.state_dir).absolute() / "cli-runs")}}}}}))

    def wait(self, directory, *extra):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = wait_event.main(["--state-dir", str(directory),
                                    "--snapshot", str(self.tmp / "snap.json")] + list(extra))
        return code, out.getvalue(), err.getvalue()

    def test_terminal_turn_wakes_once_without_paseo(self):
        directory = self.tmp / "orch-state"
        self.state_with_cli_attempt(directory)
        self.control(claude={"hang": True})
        self.launch("claude", CLAUDE_MODEL)
        code, out, _err = self.wait(directory, "--poll-cli", "--once", "--json")
        self.assertEqual((code, out), (3, ""))
        self.release("claude")
        self.wait_state("stopped")
        code, out, _err = self.wait(directory, "--poll-cli", "--once", "--json")
        self.assertEqual(code, 0)
        event = json.loads(out.splitlines()[0])
        self.assertEqual((event["event"], event["unit"], event["backend"], event["turn"],
                          event["returncode"]), ("cli_run_finished", "c", "claude", 1, 0))
        code, out, _err = self.wait(directory, "--poll-cli", "--once", "--json")
        self.assertEqual((code, out), (3, ""))
        # Without --poll-cli nothing about the run is read or reported.
        code, out, _err = self.wait(directory, "--once")
        self.assertEqual(code, 3)
        self.assertEqual(json.loads(self.paseo_state.read_text())["calls"], [])

    def test_failed_and_preempted_turns_have_distinct_events(self):
        directory = self.tmp / "orch-state"
        self.state_with_cli_attempt(directory)
        self.control(claude={"rc": 2})
        self.launch("claude", CLAUDE_MODEL)
        self.wait_state("failed")
        code, out, _err = self.wait(directory, "--poll-cli", "--once", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.splitlines()[0])["event"], "cli_run_failed")
        self.assertEqual(wait_event.CLI_EVENTS["preempted"], "cli_run_preempted")


if __name__ == "__main__":
    unittest.main()
