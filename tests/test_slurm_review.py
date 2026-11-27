"""Execution-class placement, fail-closed Lor routing, receipts and roster.

Offline only: fake sbatch/squeue/scancel/sacct, claude, codex and lor CLIs.
No real Slurm, network or provider is ever touched (R._post raises).
"""

import io
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import time
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills/hanig-review-gate/scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT / "tests"))
import review as R  # noqa: E402
import test_lor_provider as TL  # noqa: E402  (module object only: its fake lor CLI)
from tests.watchdog_guard import guarded_main  # noqa: E402

RS = R._subs
SL = RS._slurm
L = R._lor
SECRETS = {"LOR_SESSION_TOKEN": "lor-sess-SECRET", "LOR_ROUTER_TOKEN": "lor-router-SECRET",
           "LOR_SESSION_REFRESH_PROOF": "lor-proof-SECRET", "OPENAI_API_KEY": "sk-openai-SECRET",
           "OPENROUTER_API_KEY": "sk-or-SECRET", "ANTHROPIC_API_KEY": "sk-ant-SECRET",
           "GH_TOKEN": "gh-SECRET", "FOO_API_KEY": "foo-SECRET"}

# Fake Slurm: sbatch logs argv, env, script text and the stage dir mode, then
# runs the job script detached under a bare environment (like --export=NONE).
SBATCH = r'''#!%(py)s
import fcntl, json, os, stat, subprocess, sys
d = %(d)r
mode = open(d + "/mode").read().strip()
script = sys.argv[-1]
st = os.stat(os.path.dirname(script))
rec = {"argv": sys.argv[1:], "env": dict(os.environ), "script": open(script).read(),
       "stage": os.path.dirname(script), "stage_mode": stat.S_IMODE(st.st_mode),
       "stdin_exists": os.path.exists(os.path.join(os.path.dirname(script), "stdin.txt"))}
with open(d + "/sbatch.log", "a+") as log:  # concurrent seats: allocate ids atomically
    fcntl.flock(log, fcntl.LOCK_EX)
    log.seek(0)
    jid = str(4001 + len(log.read().splitlines()))
    log.write(json.dumps(dict(rec, jid=jid)) + "\n")
    log.flush()
if mode == "submit-fail":
    sys.exit("sbatch: error: invalid partition")
job_env = {"SLURM_JOB_ID": jid, "PATH": "/usr/bin:/bin"}
if os.path.exists(d + "/slurm-tmpdir"):  # node-local scratch the scheduler exports
    job_env["SLURM_TMPDIR"] = open(d + "/slurm-tmpdir").read()
if mode == "ok":
    p = subprocess.Popen(["bash", script], env=job_env,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    open(d + "/pid-" + jid, "w").write(str(p.pid))
print(jid + ";cluster")
'''
SQUEUE = r'''#!%(py)s
import os, sys
d = %(d)r
mode = open(d + "/mode").read().strip()
jid = sys.argv[sys.argv.index("-j") + 1]
open(d + "/squeue.log", "a").write(jid + "\n")
if mode == "hang":
    print("PENDING")
elif mode == "ok" and os.path.exists(d + "/pid-" + jid):
    try:
        os.kill(int(open(d + "/pid-" + jid).read()), 0)
        print("RUNNING")
    except OSError:
        pass
'''
SCANCEL = r'''#!%(py)s
import os, signal, sys
d = %(d)r
open(d + "/scancel.log", "a").write(sys.argv[1] + "\n")
try:
    os.killpg(int(open(d + "/pid-" + sys.argv[1]).read()), signal.SIGTERM)
except (OSError, ValueError):
    pass
'''
SACCT = r'''#!%(py)s
print("FAILED")
'''
# Fake sinfo/scontrol: log argv; output, exit code and delay come from files.
# The default (no sinfo-out file) is a one-partition site with no _batch sibling.
SINFO = r'''#!%(py)s
import os, sys, time
d = %(d)r
open(d + "/sinfo.log", "a").write(" ".join(sys.argv[1:]) + "\n")
if os.path.exists(d + "/sinfo-sleep"):
    time.sleep(float(open(d + "/sinfo-sleep").read()))
out = open(d + "/sinfo-out").read() if os.path.exists(d + "/sinfo-out") else "all* up\n"
sys.stdout.write(out)
sys.exit(int(open(d + "/sinfo-rc").read()) if os.path.exists(d + "/sinfo-rc") else 0)
'''
SCONTROL = r'''#!%(py)s
import os, sys
d = %(d)r
open(d + "/scontrol.log", "a").write(" ".join(sys.argv[1:]) + "\n")
if os.path.exists(d + "/scontrol-out"):
    sys.stdout.write(open(d + "/scontrol-out").read())
else:
    print("PartitionName=%%s\n   QoS=N/A DefaultTime=NONE" %% sys.argv[-1])
'''
# Fake claude/codex: log where and with what environment they ran.
CLAUDE = r'''#!%(py)s
import json, os, sys
d = %(d)r
auth = open(d + "/claude-auth").read()
verdict = open(d + "/verdict").read() if os.path.exists(d + "/verdict") else %(verdict)r
open(d + "/cli.log", "a").write(json.dumps({"cli": "claude", "argv": sys.argv[1:], "cwd": os.getcwd(),
    "env": dict(os.environ)}) + "\n")
if sys.argv[1:3] == ["auth", "status"]:
    print(auth)
    sys.exit(0)
sys.stdin.read()
print(json.dumps({"type": "result", "result": verdict, "stop_reason": "end_turn",
                  "usage": {"input_tokens": 11, "output_tokens": 7},
                  "modelUsage": {"claude-sonnet-5-5": {"canonicalModel": "claude-sonnet-5-5"}},
                  "uuid": "u-1"}))
'''
CODEX = r'''#!%(py)s
import json, os, sys
d = %(d)r
open(d + "/cli.log", "a").write(json.dumps({"cli": "codex", "argv": sys.argv[1:], "cwd": os.getcwd(),
    "env": dict(os.environ)}) + "\n")
verdict = open(d + "/verdict").read() if os.path.exists(d + "/verdict") else %(verdict)r
sys.stdin.read()
for ev in ({"type": "thread.started", "thread_id": "thr-1"},
           {"type": "item.completed", "item": {"type": "agent_message", "text": verdict}},
           {"type": "turn.completed", "usage": {"input_tokens": 5, "output_tokens": 3}}):
    print(json.dumps(ev))
'''
VERDICT = json.dumps({"verdict": "upheld", "findings": [], "claims": [
    {"claim_index": 0, "claim": "c", "status": "supported",
     "why": "the staged change matches the asserted claim in every inspected path"}],
    "notes": ""})
LOR_FAKE = TL.FAKE.replace('else "OK"', "else %r" % VERDICT)
CLAUDE_AI = json.dumps({"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty"})
PLAN_VERDICT = json.dumps({"verdict": "upheld", "findings": [], "claims": [
    {"claim_index": 0, "claim": R.HONEST_RUN_CLAIM, "status": "supported",
     "why": "the plan keeps every honest run on its unchanged success path"}],
    "notes": "", "own_findings": [], "peer_responses": []})
# A review seat run under the real SIGALRM/SIGTERM handlers, as a panel runs it.
SIGTERM_CHILD = r'''
import concurrent.futures, sys
sys.path.insert(0, sys.argv[1])
import review as R
seat = {r["name"]: r for r in R.load_reviewers()}[sys.argv[2]]
R.arm_watchdog(3600)
with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
    print(ex.submit(R.run_one, seat, "review", 120).result())
R.disarm_watchdog()
'''
SAFE_KEYS = {"HOME", "USER", "PATH", "LANG", "LC_ALL", "TMPDIR", "CLAUDE_CONFIG_DIR",
             "CLAUDE_CODE_MAX_OUTPUT_TOKENS", "CODEX_HOME"}
# Apple's Python runtime injects these into os.environ even under `env -i`;
# only on darwin may a seat's env carry them. Linux keeps SAFE_KEYS exactly.
DARWIN_RUNTIME_KEYS = frozenset({"__CF_USER_TEXT_ENCODING", "__PYVENV_LAUNCHER__"})
SEAT_ENV_KEYS = SAFE_KEYS | (DARWIN_RUNTIME_KEYS if sys.platform == "darwin" else frozenset())


def seats():
    return {r["name"]: r for r in R.load_reviewers()}


class SlurmHost(unittest.TestCase):
    """A simulated Slurm login node with a shared state dir and fake CLIs."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="slurm-review-")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        self.d, self.bin, self.state = root / "log", root / "bin", root / "shared-state"
        self.home, self.node_tmp = root / "home", root / "login-tmp"
        for path in (self.d, self.bin, self.state, self.home / ".claude", self.home / ".codex",
                     self.node_tmp):
            path.mkdir(parents=True)
        (self.home / ".codex" / "auth.json").write_text(json.dumps(
            {"auth_mode": "chatgpt", "tokens": {"access_token": "chatgpt-tok"}}))
        fmt = {"py": sys.executable, "d": str(self.d), "verdict": VERDICT}
        for name, body in (("sbatch", SBATCH), ("squeue", SQUEUE), ("scancel", SCANCEL),
                           ("sacct", SACCT), ("sinfo", SINFO), ("scontrol", SCONTROL),
                           ("claude", CLAUDE), ("codex", CODEX),
                           ("lor", LOR_FAKE % sys.executable)):
            (self.bin / name).write_text(body % fmt if name != "lor" else body)
            (self.bin / name).chmod(0o755)
        self.mode("ok")
        (self.d / "claude-auth").write_text(CLAUDE_AI)
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("LOR_", "BROOD_REVIEW_", "HANIG_REVIEW_", "SLURM_")) and not RS._strip_default_key(k)}
        env.update(SECRETS, PATH=f"{self.bin}{os.pathsep}/usr/bin{os.pathsep}/bin",
                   HOME=str(self.home), XDG_STATE_HOME=str(self.state), TMPDIR=str(self.node_tmp),
                   USER="tester", LOR_SESSION_ID="parent", FAKE_LOR=str(self.d),
                   HANIG_ROUTING_SNAPSHOTS="off")
        for patcher in (mock.patch.dict(os.environ, env, clear=True),
                        mock.patch.object(R, "_post", side_effect=AssertionError("paid API call")),
                        mock.patch.object(RS, "_run_cli",
                                          side_effect=AssertionError("CLI spawned on login node")),
                        mock.patch.multiple(SL, POLL_SECONDS=0.05, SQUEUE_SECONDS=0.1,
                                            GRACE_SECONDS=0.3)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.kill_jobs)

    def kill_jobs(self):
        for pid in self.d.glob("pid-*"):
            try:
                os.killpg(int(pid.read_text()), signal.SIGKILL)
            except (OSError, ValueError):
                pass

    def mode(self, mode):
        (self.d / "mode").write_text(mode)

    def log(self, name):
        path = self.d / name
        return [json.loads(x) if x.startswith("{") else x
                for x in path.read_text().splitlines()] if path.exists() else []

    def stage_base(self):
        return self.state / "hanig-review-gate" / "slurm-seats"

    def assert_cleaned(self, cancelled):
        self.assertEqual(list(self.stage_base().iterdir()) if self.stage_base().exists() else [], [],
                         "staging dir left on disk")
        self.assertEqual(self.log("scancel.log"), cancelled)


class TestPlacementDecision(SlurmHost):
    def test_offload_modes_and_routes(self):
        claude_seat, lor_seat = seats()["sonnet-5.5"], seats()["astra"]
        self.assertTrue(RS.use_slurm_offload())
        self.assertEqual((R.seat_route(claude_seat), R.seat_route(seats()["terra"]),
                          R.seat_route(lor_seat)), ("cli_slurm", "cli_slurm", "lor"))
        cases = (({"SLURM_JOB_ID": "77"}, False), ({"SLURM_JOB_ID": " "}, True),
                 ({"HANIG_REVIEW_SLURM": "off"}, False), ({"HANIG_REVIEW_SLURM": "No"}, False),
                 ({"HANIG_REVIEW_SLURM": "0"}, False), ({"HANIG_REVIEW_SLURM": "false"}, False),
                 ({"PATH": "/usr/bin:/bin"}, False),
                 ({"PATH": "/usr/bin:/bin", "HANIG_REVIEW_SLURM": "force"}, True),
                 ({"SLURM_JOB_ID": "77", "HANIG_REVIEW_SLURM": "1"}, True))
        for env, want in cases:
            with self.subTest(env=env), mock.patch.dict(os.environ, env):
                self.assertIs(RS.use_slurm_offload(), want)
                self.assertEqual(R.seat_route(claude_seat), "cli_slurm" if want else "cli_local")
                self.assertEqual(R.seat_route(lor_seat), "lor")
        (self.bin / "scancel").unlink()  # every tool is needed for auto offload
        self.assertFalse(RS.use_slurm_offload())
        with mock.patch.dict(os.environ, {"HANIG_REVIEW_SLURM": "force"}):
            self.assertIn("scancel not found", RS.claude_availability())
            self.assertIn("scancel not found", RS.codex_availability())

    def test_login_node_availability_never_spawns_a_cli(self):
        # RS._run_cli raises: a spawned `claude auth status` would fail this test.
        self.assertIsNone(RS.claude_availability())
        self.assertIsNone(RS.codex_availability())
        self.assertIsNone(R.availability(seats()["sonnet-5.5"]))
        auth = self.home / ".codex" / "auth.json"
        for bad, why in (({"auth_mode": "apikey", "OPENAI_API_KEY": "sk"}, "not 'chatgpt'"),
                         ({"auth_mode": "chatgpt", "tokens": {}}, "no active tokens")):
            auth.write_text(json.dumps(bad))
            self.assertIn(why, RS.codex_availability())
        auth.unlink()
        self.assertIn("codex login", RS.codex_availability())
        (self.home / ".codex" / "config.toml").write_text('cli_auth_credentials_store = "keyring"\n')
        self.assertIn("cannot be verified for Slurm offload", RS.codex_availability())
        os.rmdir(self.home / ".claude")
        self.assertIn("claude auth login", RS.claude_availability())


class TestSlurmExecution(SlurmHost):
    def test_mixed_panel_runs_lor_in_process_and_cli_seats_as_jobs(self):
        roster = seats()
        results = {name: R.run_one(roster[name], "review this", 30, 1, ["c"])
                   for name in ("sonnet-5.5", "terra", "astra")}
        for name in ("sonnet-5.5", "terra"):
            with self.subTest(seat=name):
                got = results[name]
                self.assertTrue(got["ok"], got)
                self.assertEqual((got["route"], got["provider"]),
                                 ("cli_slurm", roster[name]["provider"]))
                self.assertRegex(got["slurm_job_id"], r"^40\d\d$")
        self.assertEqual((results["astra"]["ok"], results["astra"]["route"],
                          results["astra"]["provider"]), (True, "lor", "lor"))
        self.assertNotIn("slurm_job_id", results["astra"])
        jobs = {next(a for a in r["argv"] if a.startswith("--job-name=")).split("=", 1)[1]: r
                for r in self.log("sbatch.log")}
        self.assertEqual(set(jobs), {"hanig-rev-sonnet-5.5", "hanig-rev-terra"})
        clis = self.log("cli.log")
        self.assertEqual([c["argv"][:2] for c in clis if c["cli"] == "claude"],
                         [["auth", "status"], ["-p", "--model"]])  # auth runs in the job first
        for call in clis:
            self.assertTrue(Path(call["cwd"]).name.startswith("hanig-review-seat."), call["cwd"])
            self.assertEqual(call["env"]["TMPDIR"], call["cwd"])
            for _ in range(100):  # the job's EXIT trap may still be running
                if not Path(call["cwd"]).exists():
                    break
                time.sleep(0.02)
            self.assertFalse(Path(call["cwd"]).exists(), "node-local TMPDIR not removed")
            self.assertLessEqual(set(call["env"]), SEAT_ENV_KEYS, call["env"])
            # Platform-independent: no Lor key and no API/billing key, ever.
            self.assertFalse([k for k in call["env"] if k.upper().startswith("LOR_")])
            self.assertFalse(set(SECRETS) & set(call["env"]))
            self.assertFalse([k for k in call["env"] if "API_KEY" in k.upper()
                              or k.upper().endswith(("_TOKEN", "_SECRET", "BILLING"))], call["env"])
            self.assertFalse(set(SECRETS.values()) & set(call["env"].values()))
        codex = next(c for c in clis if c["cli"] == "codex")
        self.assertEqual(codex["argv"][codex["argv"].index("-C") + 1], codex["cwd"])
        self.assert_cleaned([results[n]["slurm_job_id"] for n in ("sonnet-5.5", "terra")])

    def test_symlinked_node_tmpdir_yields_one_canonical_work_path(self):
        # macOS /tmp -> /private/tmp reproduced on any host: the scheduler's
        # node-local scratch is a symlink, so mktemp returns an unresolved path.
        real = self.d.parent / "node-scratch-real"
        real.mkdir()
        link = self.d.parent / "node-scratch-link"
        link.symlink_to(real, target_is_directory=True)
        (self.d / "slurm-tmpdir").write_text(str(link))
        roster = seats()
        for name in ("sonnet-5.5", "terra"):
            got = R.run_one(roster[name], "review this", 30, 1, ["c"])
            self.assertTrue(got["ok"], got)
            self.assertEqual(got["route"], "cli_slurm")
        clis = self.log("cli.log")
        self.assertEqual(sorted({c["cli"] for c in clis}), ["claude", "codex"])
        for call in clis:
            with self.subTest(cli=call["cli"], argv=call["argv"][:2]):
                cwd = call["cwd"]
                self.assertTrue(os.path.isabs(cwd), cwd)
                # physical, not via the link, even if self.d.parent itself
                # traverses a symlink (macOS /var -> /private/var)
                self.assertEqual(Path(cwd).parent, real.resolve())
                self.assertNotEqual(Path(cwd).parent, link)
                self.assertTrue(Path(cwd).name.startswith("hanig-review-seat."), cwd)
                self.assertEqual(call["env"]["TMPDIR"], cwd)
                if call["cli"] == "codex":
                    self.assertEqual(call["argv"][call["argv"].index("-C") + 1], cwd)
        for _ in range(100):  # the job's EXIT trap may still be running
            if not any(real.iterdir()):
                break
            time.sleep(0.02)
        self.assertEqual(list(real.iterdir()), [], "node-local work dir not removed on exit")
        self.assertTrue(link.is_symlink())  # the trap removed the work dir, not the link

    def test_submission_never_carries_secrets_and_stage_is_private_shared_state(self):
        result, err = R.call_claude(seats()["sonnet-5.5"], "review", 30)
        self.assertIsNone(err)
        self.assertEqual(result["route"], "cli_slurm")
        (sub,) = self.log("sbatch.log")
        argv = sub["argv"]
        self.assertEqual(argv[:2], ["--parsable", "--export=NONE"])
        for flag in ("--cpus-per-task=2", "--mem=8G", "--time=00:20:00"):
            self.assertIn(flag, argv)
        self.assertFalse(any(a.startswith(("--partition", "--account", "--qos")) for a in argv))
        self.assertFalse([k for k in sub["env"] if k.upper().startswith("LOR_")])
        self.assertFalse(set(SECRETS) & set(sub["env"]))
        self.assertFalse(set(SECRETS.values()) & set(sub["env"].values()))
        for value in SECRETS.values():
            self.assertNotIn(value, sub["script"])
        self.assertNotIn("LOR_", sub["script"])
        self.assertIn("env -i ", sub["script"])
        stage = Path(sub["stage"])
        self.assertEqual(sub["stage_mode"], 0o700)
        self.assertEqual(stage.parent, self.stage_base())
        self.assertFalse(str(stage).startswith(str(self.node_tmp)))
        self.assertTrue(sub["stdin_exists"])
        self.assert_cleaned([result["slurm_job_id"]])
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": "relative/state"}):
            self.assertEqual(SL.stage_root(), self.home / ".local/state/hanig-review-gate/slurm-seats")
        with mock.patch.dict(os.environ, {"HANIG_REVIEW_SLURM_STAGE_DIR": str(self.home / "s")}):
            self.assertEqual(SL.stage_root(), self.home / "s")

    def test_site_overrides_reach_sbatch(self):
        with mock.patch.dict(os.environ, {
                "HANIG_REVIEW_SLURM_PARTITION": "p1", "HANIG_REVIEW_SLURM_ACCOUNT": "a1",
                "HANIG_REVIEW_SLURM_QOS": "q1", "HANIG_REVIEW_SLURM_MEM": "16G",
                "HANIG_REVIEW_SLURM_CPUS": "4", "HANIG_REVIEW_SLURM_TIME": "00:05:00",
                "HANIG_REVIEW_SLURM_EXTRA_ARGS": "--gres=none --comment 'a b'"}):
            self.assertIsNone(R.call_codex(seats()["terra"], "review", 30)[1])
        argv = self.log("sbatch.log")[0]["argv"]
        for flag in ("--partition=p1", "--account=a1", "--qos=q1", "--mem=16G",
                     "--cpus-per-task=4", "--time=00:05:00", "--gres=none"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--comment") + 1], "a b")

    def test_timeout_vanish_and_submit_failure_cancel_and_clean_up(self):
        seat = seats()["sonnet-5.5"]
        self.mode("hang")
        result = R.run_one(seat, "review", 1)
        self.assertFalse(result["ok"])
        self.assertIn("timed out", result["error"])
        self.assertEqual((result["provider"], result["route"]), ("claude", "cli_slurm"))
        self.assert_cleaned(["4001"])
        self.mode("vanish")
        _res, err = R.call_claude(seat, "review", 10)
        self.assertIn("slurm job 4002 ended (FAILED) without writing rc.txt", err)
        self.assert_cleaned(["4001", "4002"])
        self.mode("submit-fail")
        _res, err = R.call_claude(seat, "review", 10)
        self.assertIn("sbatch submission failed", err)
        self.assert_cleaned(["4001", "4002"])  # no job id: nothing to cancel
        self.assertEqual(self.log("cli.log"), [])

    def test_compute_node_preflight_refuses_a_non_subscription_login(self):
        for auth in ({"loggedIn": True, "authMethod": "console"},
                     {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "bedrock"},
                     {"loggedIn": False, "authMethod": "claude.ai"}):
            with self.subTest(auth=auth):
                (self.d / "cli.log").unlink(missing_ok=True)
                (self.d / "claude-auth").write_text(json.dumps(auth))
                result, err = R.call_claude(seats()["sonnet-5.5"], "review", 10)
                self.assertIsNone(result)
                self.assertIn("not an active 'claude.ai' subscription", err)
                self.assertEqual([c["argv"][:2] for c in self.log("cli.log")],
                                 [["auth", "status"]], "claude -p ran after a refused login")
        self.assertEqual(list(self.stage_base().iterdir()), [])

    def test_watchdog_exit_cancels_submitted_jobs(self):
        self.mode("hang")
        stage = SL._private_dir(SL.stage_root())
        tools = SL.slurm_tools()
        SL._ACTIVE["4999"] = (tools, SL.submission_env(RS._strip_default_key), stage)
        saved = signal.getsignal(signal.SIGALRM)
        self.addCleanup(signal.signal, signal.SIGALRM, saved)
        R.arm_watchdog(3600)
        signal.alarm(0)
        handler = signal.getsignal(signal.SIGALRM)
        with mock.patch.object(R.os, "_exit", side_effect=SystemExit(2)), \
                redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            handler(signal.SIGALRM, None)
        self.assertEqual(self.log("scancel.log"), ["4999"])
        self.assertFalse(stage.exists())
        self.assertEqual(SL._ACTIVE, {})

    def test_batch_script_is_valid_bash_and_rejects_unmanaged_overrides(self):
        script = SL.batch_script(["/bin/echo", SL.EMPTY_CWD, "it's"], Path("/s t"),
                                 SL.runtime_env({"CODEX_HOME": "/c"}),
                                 (["/bin/true"], RS.CLAUDE_AUTH_GATE))
        subprocess.run(["bash", "-n"], input=script.encode(), check=True)
        self.assertIn('"$work"', script)
        for key in ("OPENAI_API_KEY", "LOR_ROUTER_TOKEN", "HOME", "TMPDIR"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                SL.runtime_env({key: "x"})

class TestAgentHostDetection(SlurmHost):
    AGENT = {"SLURM_JOB_ID": "999", "SLURM_JOB_NAME": "sh_agent"}

    def test_agent_host_job_shapes(self):
        cases = ((self.AGENT, True),
                 ({"SLURM_JOB_ID": "999", "SLURM_JOB_NAME": " SH_AGENT "}, True),
                 ({"SLURM_JOB_ID": "999", "SLURM_JOB_NAME": "sh_agent-2"}, True),
                 ({"SLURM_JOB_ID": "999", "SLURM_JOB_NAME": "sh_agent_x"}, True),
                 ({"SLURM_JOB_ID": "999", "SLURM_PTY_PORT": "39995"}, True),
                 ({"SLURM_JOB_ID": "999", "SLURM_PTY_PORT": "39995", "SLURM_JOB_NAME": "bash"}, True),
                 ({"SLURM_JOB_ID": "999", "SLURM_JOB_NAME": "sh_agentx"}, False),
                 ({"SLURM_JOB_ID": "999", "SLURM_PTY_PORT": "39995", "LOR_SESSION_ID": ""}, False),
                 ({"SLURM_JOB_ID": "999", "SLURM_PTY_PORT": "39995",
                   "SLURM_JOB_NAME": "hanig-rev-terra"}, False),
                 ({"SLURM_JOB_ID": "999"}, False),
                 ({"SLURM_JOB_NAME": "sh_agent"}, False),
                 ({"SLURM_PTY_PORT": "39995"}, False))
        for env, want in cases:
            with self.subTest(env=env), mock.patch.dict(os.environ, env):
                self.assertIs(SL.is_agent_host_job(), want)
                self.assertIs(SL.is_agent_host_job(dict(os.environ)), want)

    def test_auto_offloads_from_agent_host_but_never_from_a_compute_job(self):
        claude_seat = seats()["sonnet-5.5"]
        cases = ((self.AGENT, True),
                 ({"SLURM_JOB_ID": "999", "SLURM_PTY_PORT": "39995"}, True),
                 ({"SLURM_JOB_ID": "999"}, False),
                 ({"SLURM_JOB_ID": "999", "SLURM_JOB_NAME": "hanig-rev-terra"}, False),
                 ({"SLURM_JOB_ID": "999", "SLURM_JOB_NAME": "hanig-rev-terra",
                   "SLURM_PTY_PORT": "39995"}, False),
                 ({"SLURM_JOB_ID": "999", "SLURM_PTY_PORT": "39995", "LOR_SESSION_ID": ""}, False),
                 (dict(self.AGENT, HANIG_REVIEW_SLURM="off"), False),
                 (dict(self.AGENT, PATH="/usr/bin:/bin"), False),
                 (dict(self.AGENT, PATH="/usr/bin:/bin", HANIG_REVIEW_SLURM="force"), True))
        for env, want in cases:
            with self.subTest(env=env), mock.patch.dict(os.environ, env):
                self.assertIs(RS.use_slurm_offload(), want)
                self.assertEqual(R.seat_route(claude_seat), "cli_slurm" if want else "cli_local")

    def test_agent_host_seat_runs_as_a_job(self):
        with mock.patch.dict(os.environ, dict(self.AGENT, SLURM_JOB_PARTITION="all")):
            result, err = R.call_codex(seats()["terra"], "review", 30)
        self.assertIsNone(err)
        self.assertEqual((result["route"], result["slurm_job_id"]), ("cli_slurm", "4001"))
        self.assert_cleaned(["4001"])


class TestBatchPartition(SlurmHost):
    AGENT = {"SLURM_JOB_ID": "999", "SLURM_JOB_NAME": "sh_agent", "SLURM_JOB_PARTITION": "cpu"}
    SPLIT = "cpu* up\ncpu_batch up\n"

    def sinfo(self, out=None, rc=None, sleep=None):
        for name, value in (("sinfo-out", out), ("sinfo-rc", rc), ("sinfo-sleep", sleep)):
            if value is not None:
                (self.d / name).write_text(str(value))

    def submit(self, env=()):
        for name in ("sbatch.log", "sinfo.log", "scontrol.log"):
            (self.d / name).unlink(missing_ok=True)
        with mock.patch.dict(os.environ, dict(env)):
            self.assertIsNone(R.call_codex(seats()["terra"], "review", 30)[1])
        (sub,) = self.log("sbatch.log")
        return [a for a in sub["argv"] if a.startswith("--partition") or a == "-p"
                or (a.startswith("-p") and not a.startswith("--"))]

    def test_agent_host_on_interactive_default_gets_its_batch_sibling(self):
        self.sinfo(self.SPLIT)
        self.assertEqual(self.submit(self.AGENT), ["--partition=cpu_batch"])
        self.assertEqual(self.log("sinfo.log"), ["-h -o %P %a"])
        self.assertEqual(self.log("scontrol.log"), [])  # the job shape decided
        self.assertEqual(self.submit(dict(self.AGENT, SLURM_JOB_PARTITION="")),
                         ["--partition=cpu_batch"])

    def test_interactive_qos_selects_the_batch_sibling_from_a_login_node(self):
        self.sinfo(self.SPLIT)
        (self.d / "scontrol-out").write_text(
            "PartitionName=cpu\n   AllowGroups=ALL Default=YES QoS=cpu_interact\n")
        self.assertEqual(self.submit(), ["--partition=cpu_batch"])
        self.assertEqual(self.log("scontrol.log"), ["show partition cpu"])
        # An agent host on another partition defers to the same QoS check.
        self.assertEqual(self.submit(dict(self.AGENT, SLURM_JOB_PARTITION="gpu")),
                         ["--partition=cpu_batch"])
        (self.d / "scontrol-out").write_text("PartitionName=cpu\n   QoS=normal\n")
        self.assertEqual(self.submit(), [])
        self.assertEqual(self.submit(dict(self.AGENT, SLURM_JOB_PARTITION="gpu")), [])

    def test_sites_without_a_batch_sibling_are_untouched(self):
        self.assertEqual(self.submit(self.AGENT), [])  # default fake: "all* up" only
        self.assertEqual(self.submit(), [])
        for out in ("cpu* up\ncpu_batch down\n", "cpu up\ncpu_batch up\n",
                    "gpu* up\ncpu_batch up\n", "", "garbage\n"):
            with self.subTest(out=out):
                self.sinfo(out)
                self.assertEqual(self.submit(self.AGENT), [])

    def test_explicit_partition_always_wins(self):
        self.sinfo(self.SPLIT)
        self.assertEqual(self.submit(dict(self.AGENT, HANIG_REVIEW_SLURM_PARTITION="custom")),
                         ["--partition=custom"])
        self.assertEqual(self.log("sinfo.log"), [], "probed a configured partition")
        for extra in ("-p custom", "-pcustom", "--partition custom", "--partition=custom",
                      "--gres=none -p custom"):
            with self.subTest(extra=extra):
                got = self.submit(dict(self.AGENT, HANIG_REVIEW_SLURM_EXTRA_ARGS=extra))
                self.assertNotIn("--partition=cpu_batch", got)
                self.assertTrue(got)
                self.assertEqual(self.log("sinfo.log"), [])
        self.assertEqual(self.submit(dict(self.AGENT, HANIG_REVIEW_SLURM_EXTRA_ARGS="--gres=none")),
                         ["--partition=cpu_batch"])

    def test_probe_failures_fail_safe_to_no_partition(self):
        self.sinfo(self.SPLIT, rc=1)
        self.assertEqual(self.submit(self.AGENT), [])
        (self.d / "sinfo-rc").unlink()
        self.sinfo(sleep=3)
        with mock.patch.object(SL, "PARTITION_PROBE_SECONDS", 1.0):
            started = time.monotonic()
            self.assertEqual(self.submit(self.AGENT), [])
            self.assertLess(time.monotonic() - started, 2.9)
        (self.d / "sinfo-sleep").unlink()
        (self.bin / "sinfo").unlink()
        tools = dict(SL.slurm_tools(), sinfo=None)
        self.assertIsNone(SL.auto_batch_partition(tools, dict(os.environ, **self.AGENT), None))
        self.assertIsNone(SL.slurm_tools(dict(os.environ, PATH=str(self.bin)))["sinfo"])
        self.assertEqual(self.submit(self.AGENT), [])  # offload still submits
        self.assertTrue(RS.use_slurm_offload())

    def test_availability_checks_never_probe_partitions(self):
        self.sinfo(self.SPLIT)
        with mock.patch.dict(os.environ, self.AGENT):
            for rev in seats().values():
                R.availability(rev)
                R.seat_route(rev)
        self.assertEqual(self.log("sinfo.log"), [])
        self.assertEqual(self.log("scontrol.log"), [])


class TestPlanSlurmReceipts(SlurmHost):
    def test_plan_receipts_carry_every_turns_slurm_job(self):
        jroot = self.d.parent / (R.JOURNAL_TEST_ROOT_PREFIX + "plan")
        jroot.mkdir()
        plan = jroot / "plan.md"
        plan.write_text("Read records sequentially and emit them without sorting.\n")
        (self.d / "verdict").write_text(PLAN_VERDICT)
        roster = seats()
        members = [dict(roster[n], profiles=["plan"]) for n in ("sonnet-5.5", "terra")]
        argv = ["review.py", "--kind", "plan", "--file", str(plan), "--json",
                "--claim", R.HONEST_RUN_CLAIM]
        out = io.StringIO()
        state = jroot / "state"
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(state),
                                          R.JOURNAL_TEST_MARKER: str(jroot)}), \
                mock.patch.object(sys, "argv", argv), \
                mock.patch.object(R, "load_effective_reviewers", return_value=members), \
                redirect_stdout(out), redirect_stderr(io.StringIO()), \
                self.assertRaises(SystemExit) as stopped:
            guarded_main(R.main)
        report = json.loads(out.getvalue())
        self.assertEqual((stopped.exception.code, report["state"]), (0, "PLAN_PASS"))
        jobs = {}
        for sub in self.log("sbatch.log"):  # in submission order per seat
            name = next(a for a in sub["argv"] if a.startswith("--job-name="))
            jobs.setdefault(name.split("hanig-rev-", 1)[1], []).append(sub["jid"])
        self.assertEqual({k: len(v) for k, v in jobs.items()}, {"sonnet-5.5": 2, "terra": 2})
        record = [json.loads(x) for x in Path(report["journal"]["path"]).read_text().splitlines()]
        self.assertEqual(len(record), 1)
        for source in (report["seat_routes"], record[0]["seat_routes"]):
            for seat, ids in jobs.items():
                with self.subTest(seat=seat):
                    self.assertEqual(source[seat], {
                        "provider": roster[seat]["provider"], "model": roster[seat]["model"],
                        "route": "cli_slurm", "status": "completed",
                        "slurm_job_id": ids[-1], "slurm_job_ids": ids})
        for seat, ids in jobs.items():
            usages = [a["usage"] for a in report["plan_exchange"]["members"][seat]["answers"]]
            self.assertEqual([(u["route"], u["slurm_job_id"]) for u in usages],
                             [("cli_slurm", i) for i in ids])
        self.assertEqual(sorted(self.log("scancel.log")), sorted(sum(jobs.values(), [])))
        self.assertEqual(list((state / "hanig-review-gate" / "slurm-seats").iterdir()), [])

    def test_plan_job_ids_come_from_usage_or_a_failed_turns_error(self):
        ids = R._cli.plan_slurm_job_ids
        self.assertEqual(ids([
            {"usage": {"slurm_job_id": "11", "route": "cli_slurm"}, "error": None},
            {"usage": None, "error": "slurm job 12 ended (FAILED) without writing rc.txt"},
            {"usage": {"slurm_job_id": "11"}, "error": None},
            {"usage": {"in_tokens": 1}, "error": "codex CLI error: boom"},
            {"usage": None, "error": "xslurm job 99 is not a job reference"},
            "not an answer"]), ["11", "12"])
        self.assertEqual(ids([]), [])
        routes = R.build_seat_routes(failed=[{"name": "s", "provider": "codex", "model": "m",
                                              "route": "cli_slurm", "slurm_job_id": "12",
                                              "slurm_job_ids": ["11", "12"]}])
        self.assertEqual(routes["s"]["slurm_job_ids"], ["11", "12"])

    def test_ledger_rejects_malformed_slurm_job_ids(self):
        good = {"provider": "codex", "model": "m", "route": "cli_slurm", "status": "completed",
                "slurm_job_id": "4002", "slurm_job_ids": ["4001", "4002"]}
        record, line = R.prepare_review_journal("implementation", 1, [], "REVIEW_PASS", [],
                                                seat_routes={"s": good})
        self.assertEqual(json.loads(line)["seat_routes"]["s"]["slurm_job_ids"], ["4001", "4002"])
        for bad in ({"slurm_job_ids": []}, {"slurm_job_ids": "4002"},
                    {"slurm_job_ids": ["4001", ""]}, {"slurm_job_ids": ["4001", 4002]},
                    {"slurm_job_ids": ["4002", "4001"]}, {"slurm_job_id": ""},
                    {"slurm_job_id": 4002, "slurm_job_ids": [4002]}):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, "slurm_job_id"):
                R.prepare_review_journal("implementation", 1, [], "REVIEW_PASS", [],
                                         seat_routes={"s": dict(good, **bad)})


class TestSigtermCleanup(SlurmHost):
    def wait_for(self, predicate, seconds=30.0):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if predicate():
                return True
            time.sleep(0.05)
        return False

    def test_sigterm_cancels_the_job_removes_staging_and_exits_143(self):
        self.mode("hang")
        proc = subprocess.Popen([sys.executable, "-c", SIGTERM_CHILD, str(SCRIPTS), "sonnet-5.5"],
                                env=dict(os.environ), stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        # squeue runs only after the job is registered as active.
        self.assertTrue(self.wait_for(lambda: self.log("squeue.log")), "seat never polled")
        stages = list(self.stage_base().iterdir())
        self.assertEqual(len(stages), 1)
        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=60)
        self.assertEqual(proc.returncode, 128 + signal.SIGTERM, err.decode())
        self.assertIn("SIGTERM", err.decode())
        self.assertEqual(out.decode(), "")  # no verdict was printed
        self.assert_cleaned(["4001"])
        self.assertFalse(stages[0].exists())

    def test_cancel_removes_a_stage_whose_job_was_never_registered(self):
        stage = SL._private_dir(SL.stage_root())
        self.assertIn(stage, SL._ACTIVE_STAGES)
        self.assertNotIn(stage, [v[2] for v in SL._ACTIVE.values()])
        SL.cancel_active_jobs()
        SL.cancel_active_jobs()  # idempotent
        self.assertFalse(stage.exists())
        self.assertEqual((SL._ACTIVE, SL._ACTIVE_STAGES), ({}, set()))
        self.assertEqual(self.log("scancel.log"), [])

    def test_shutdown_during_sbatch_cancels_the_late_job(self):
        self.mode("hang")
        real_call = SL._call

        def sigterm_during_sbatch(argv, *args, **kwargs):
            got = real_call(argv, *args, **kwargs)
            if os.path.basename(argv[0]) == "sbatch":
                SL.cancel_active_jobs()  # lands before the job id is registered
            return got

        with mock.patch.object(SL, "_call", side_effect=sigterm_during_sbatch):
            result, err = R.call_claude(seats()["sonnet-5.5"], "review", 30)
        self.assertIsNone(result)
        self.assertIn("slurm job 4001 cancelled: shutdown during submission", err)
        self.assert_cleaned(["4001"])
        self.assertEqual((SL._ACTIVE, SL._ACTIVE_STAGES), ({}, set()))

    @unittest.skipUnless(hasattr(signal, "SIGALRM"), "requires POSIX alarms")
    def test_arm_installs_and_disarm_restores_the_previous_handler(self):
        saved = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGALRM)}
        for sig, handler in saved.items():
            self.addCleanup(signal.signal, sig, handler)
        self.addCleanup(signal.alarm, 0)

        def previous(_s, _f):
            pass

        for seconds in (3600, 0):
            with self.subTest(seconds=seconds):
                signal.signal(signal.SIGTERM, previous)
                R.arm_watchdog(seconds)
                R.arm_watchdog(seconds)  # re-arming never captures our own handler
                self.assertIs(signal.getsignal(signal.SIGTERM), R._on_sigterm)
                R.disarm_watchdog()
                self.assertIs(signal.getsignal(signal.SIGTERM), previous)
                R.disarm_watchdog()
                self.assertIs(signal.getsignal(signal.SIGTERM), previous)
        import threading
        signal.signal(signal.SIGTERM, previous)
        worker = threading.Thread(target=R.arm_watchdog, args=(0,))
        worker.start()
        worker.join()
        self.assertIs(signal.getsignal(signal.SIGTERM), previous)  # main thread only
        R.disarm_watchdog()
        self.assertIs(signal.getsignal(signal.SIGTERM), previous)

    def test_handler_cancels_active_jobs_and_exits_143(self):
        stage = SL._private_dir(SL.stage_root())
        SL._ACTIVE["4998"] = (SL.slurm_tools(), SL.submission_env(RS._strip_default_key), stage)
        saved = signal.getsignal(signal.SIGTERM)
        self.addCleanup(signal.signal, signal.SIGTERM, saved)
        with mock.patch.object(R.os, "_exit", side_effect=SystemExit) as exit_, \
                redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit):
            R._on_sigterm(signal.SIGTERM, None)
        exit_.assert_called_once_with(143)
        self.assertIn("SIGTERM", err.getvalue())
        self.assertEqual(self.log("scancel.log"), ["4998"])
        self.assertFalse(stage.exists())
        self.assertEqual((SL._ACTIVE, SL._ACTIVE_STAGES), ({}, set()))


class TestFailClosedLorRouting(SlurmHost):
    def test_session_unset_blocks_every_lor_seat_without_any_api_call(self):
        with mock.patch.dict(os.environ, {"LOR_SESSION_ID": ""}):
            for name, rev in seats().items():
                if not rev.get("lor_model") or not rev.get("enabled", True):
                    continue
                with self.subTest(seat=name):
                    self.assertEqual((R.effective_provider(rev), R.seat_route(rev)), ("lor", "lor"))
                    self.assertIn("LOR_SESSION_ID unset", R.availability(rev))
                    got = R.run_one(rev, "review", 5)
                    self.assertEqual((got["ok"], got["provider"], got["route"]),
                                     (False, "lor", "lor"))
                    self.assertIn("lor routing required", got["error"])
        self.assertEqual(self.log("cli.log"), [])
        self.assertFalse((self.d / "calls").exists(), "lor ran without a session")

    def test_only_an_explicit_opt_out_selects_the_paid_api(self):
        astra = seats()["astra"]
        for value in ("", "0", "no", "false", "maybe"):
            with self.subTest(value=value), mock.patch.dict(os.environ, {L.OPT_OUT: value}):
                self.assertEqual(R.effective_provider(astra), "lor")
        with mock.patch.dict(os.environ, {L.OPT_OUT: "1", "LOR_SESSION_ID": ""}), \
                mock.patch.object(R, "_post", return_value=(None, "HTTP 500: stub")) as post:
            self.assertEqual((R.effective_provider(astra), R.seat_route(astra)), ("openai", "api"))
            got = R.run_one(astra, "review", 5)
        self.assertEqual((got["ok"], got["provider"], got["route"]), (False, "openai", "api"))
        self.assertTrue(post.called)
        resolved = dict(astra, _resolved_from=astra["model"])
        self.assertEqual(R.seat_route(resolved), "api")


class TestReceipts(unittest.TestCase):
    COMPLETED = {"name": "sonnet-5.5", "ok": True, "provider": "claude", "model": "m1",
                 "route": "cli_slurm", "slurm_job_id": "4001", "verdict": "upheld",
                 "findings": [], "claims": []}
    FAILED = {"name": "terra", "ok": False, "error": "boom", "provider": "codex",
              "model": "m2", "route": "cli_slurm"}
    UNAVAILABLE = {"name": "kimi-k3", "reason": "lor routing required", "provider": "lor",
                   "model": "moonshotai/kimi-k3", "route": "lor"}

    def test_seat_routes_cover_completed_failed_and_unavailable(self):
        routes = R.build_seat_routes([self.COMPLETED], [self.FAILED], [self.UNAVAILABLE])
        self.assertEqual(routes, {
            "sonnet-5.5": {"provider": "claude", "model": "m1", "route": "cli_slurm",
                           "status": "completed", "slurm_job_id": "4001"},
            "terra": {"provider": "codex", "model": "m2", "route": "cli_slurm", "status": "failed"},
            "kimi-k3": {"provider": "lor", "model": "moonshotai/kimi-k3", "route": "lor",
                        "status": "unavailable"}})
        record, line = R.prepare_review_journal(
            "implementation", 1, ["sonnet-5.5"], "REVIEW_PARTIAL", [R.HONEST_RUN_CLAIM],
            results=[self.COMPLETED], seat_routes=routes)
        self.assertEqual(json.loads(line)["seat_routes"], routes)
        self.assertEqual(record["results"][0]["route"], "cli_slurm")
        # Completed results reach the receipt even when the caller passed no map.
        record, _ = R.prepare_review_journal(
            "implementation", 1, ["sonnet-5.5"], "REVIEW_PASS", [], results=[self.COMPLETED])
        self.assertEqual(record["seat_routes"]["sonnet-5.5"]["route"], "cli_slurm")
        for bad in ({"x": {"provider": "p", "model": "m", "route": "moon", "status": "completed"}},
                    {"x": {"provider": "p", "model": "m", "route": "lor", "status": "ran"}},
                    {"x": "lor"}, ["lor"]):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, "seat_routes"):
                R.prepare_review_journal("implementation", 1, [], "REVIEW_PASS", [],
                                         seat_routes=bad)

    def test_cli_json_report_and_journal_carry_routes(self):
        roster = {r["name"]: r for r in R.load_reviewers()}

        def answer(rev, *_a):
            if rev["name"] == "terra":
                return dict(self.FAILED, elapsed_s=0)
            return dict(self.COMPLETED, name=rev["name"], elapsed_s=0,
                        provider=R.effective_provider(rev), route=R.seat_route(rev),
                        **({"slurm_job_id": "4001"} if R.seat_route(rev) == "cli_slurm" else {}))

        recorded = {}

        def record(args, completed, verdict):
            recorded.update(R.prepare_review_journal(
                args.kind, args.round, [c["name"] for c in completed], verdict, args.claim,
                results=completed, seat_routes=args.seat_routes)[0])
            return {"path": None, "written": False}

        args = types.SimpleNamespace(
            json=True, quorum=2, escalate=False, panel_policy=None, input_redactions=[],
            kind="implementation", round=1, claim=[R.HONEST_RUN_CLAIM], watchdog=0,
            timeout=5, premise_ack=None)
        real_availability = R.availability
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"LOR_SESSION_ID": "", "HANIG_REVIEW_SLURM": "off",
                                          "HANIG_ROUTING_SNAPSHOTS": "off"}), \
                mock.patch.object(R, "availability", side_effect=lambda rev: (
                    real_availability(rev) if rev["name"] == "kimi-k3" else None)), \
                mock.patch.object(R, "run_one", side_effect=answer), \
                mock.patch.object(R, "_post", side_effect=AssertionError("paid API call")), \
                mock.patch.object(R, "record_review_round", side_effect=record), \
                mock.patch.object(R, "release_round"), \
                mock.patch.object(R, "_premise_module") as premise, \
                redirect_stdout(out), self.assertRaises(SystemExit):
            premise.return_value.final.return_value = None
            panel = [roster[n] for n in ("sonnet-5.5", "terra", "kimi-k3")]
            completed, failed, unavailable, tiers = R._cli._execute_implementation_panel(
                args, panel, "p", "body", "lbl", False, "standard", "r", [], "", R)
            R._cli._collect_and_emit_verdict(args, completed, failed, unavailable, tiers,
                                             "lbl", False, "standard", "r", [], "", "t", R)
        report = json.loads(out.getvalue())
        want = {"sonnet-5.5": ("claude", "cli_local", "completed"),
                "terra": ("codex", "cli_slurm", "failed"),
                "kimi-k3": ("lor", "lor", "unavailable")}
        for source in (report["seat_routes"], recorded["seat_routes"]):
            self.assertEqual({n: (r["provider"], r["route"], r["status"]) for n, r in source.items()},
                             want)
        self.assertEqual(report["unavailable"][0]["route"], "lor")
        self.assertIn("LOR_SESSION_ID unset", report["unavailable"][0]["reason"])
        self.assertEqual((report["failed"][0]["provider"], report["failed"][0]["route"]),
                         ("codex", "cli_slurm"))
        self.assertEqual(report["results"][0]["route"], "cli_local")


class TestZeroApiRoster(unittest.TestCase):
    def test_every_enabled_seat_is_enterprise_cli_or_lor_served(self):
        roster = json.loads((ROOT / "skills/hanig-review-gate/reviewers.json").read_text())
        enabled = [r for r in roster["reviewers"] if r.get("enabled", True)]
        self.assertTrue(enabled)
        for rev in enabled:
            with self.subTest(seat=rev["name"]):
                self.assertTrue(rev["provider"] in ("claude", "codex") or rev["provider"] == "lor"
                                or (isinstance(rev.get("lor_model"), str) and rev["lor_model"].strip()),
                                f"{rev['name']} would spend {rev['provider']} API credit")
                with mock.patch.dict(os.environ, {"HANIG_REVIEW_NO_LOR": "", "LOR_SESSION_ID": ""}):
                    self.assertIn(R.effective_provider(dict(rev)), ("claude", "codex", "lor"))
        by_name = {r["name"]: r for r in roster["reviewers"]}
        self.assertEqual(by_name["kimi-k3"]["profiles"], ["plan", "standard", "deep"])
        self.assertIs(by_name["kimi-k2.7-code"]["enabled"], False)
        self.assertEqual(by_name["kimi-k2.7-code"]["profiles"], [])
        for profile in ("standard", "deep"):
            self.assertIn("kimi-k3", roster["_profiles"][profile])
        self.assertNotIn("kimi-k2.7-code", json.dumps({k: v for k, v in roster["reserves"].items()}))


if __name__ == "__main__":
    unittest.main()
