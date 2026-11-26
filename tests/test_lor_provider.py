"""Fail-closed Lor subagent transport; offline, through a fake `lor` CLI."""

import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/hanig-review-gate/scripts"
sys.path.insert(0, str(SCRIPTS))
import committee as C  # noqa: E402
import review as R  # noqa: E402
L = R._lor
FAKE = '''#!%s
import json, os, subprocess, sys, time
d = os.environ["FAKE_LOR"]; mode = open(d + "/mode").read(); verb = sys.argv[2]
open(d + "/calls", "a").write(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd(), "ls": os.listdir("."),
    "env": sorted(os.environ), "stdin": sys.stdin.read() if verb == "start" else ""}) + "\\n")
ev = lambda e: print(json.dumps({"recorded": {"event": e}}), flush=True)
if verb == "start" and mode == "start-fail": sys.exit("refused " + os.environ["LOR_SESSION_TOKEN"])
if verb == "start": print('{\\n  "session_id": "0d6f1c2e-1111-4222-8333-944455556666"\\n}')
if verb == "watch":
    kid = subprocess.Popen(["sleep", "60"], stdout=subprocess.DEVNULL)
    open(d + "/pids", "w").write("%%d %%d" %% (os.getpid(), kid.pid))
    if mode == "timeout": time.sleep(60)
    ev({"tool_call": {"name": "pass"}} if mode == "pass" else {"model_response_id": "r1",
       "message": {"role": "MESSAGE_ROLE_ASSISTANT", "content": [{"text": "OK" if mode == "ok" else ""}]},
       "provider_metadata_json": json.dumps({"providerModelId": "served-x"})})
    ev({"metrics": {"usage": {"input_tokens": "7", "output_tokens": "5", "reasoning_tokens": "3"}}})
    if mode == "drop": sys.exit(0)
    if mode == "error": ev({"error": {"message": "router down"}})
    ev({"finish": {"reason": "FINISH_REASON_CANCELLED" if mode == "cancel" else "FINISH_REASON_STOP"}})
    time.sleep(60)
'''


def seats():
    return {r["name"]: r for r in R.load_reviewers()}


class TestLor(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.d = Path(tmp.name)
        (self.d / "lor").write_text(FAKE % sys.executable)
        (self.d / "lor").chmod(0o755)
        self.mode("ok")
        env = {k: v for k, v in os.environ.items() if not k.startswith(("LOR_", "HANIG_REVIEW_NO_LOR"))
               and not k.endswith("_API_KEY")}
        env.update(FAKE_LOR=str(self.d), PATH=f"{self.d}{os.pathsep}{os.environ['PATH']}",
                   LOR_SESSION_ID="parent", LOR_SESSION_TOKEN="lor-sess-secret", GH_TOKEN="gh-x",
                   LOR_ROUTER_TOKEN="lor-router-secret", LOR_SESSION_REFRESH_PROOF="lor-proof-secret",
                   OPENAI_API_KEY="sk-paid", HANIG_ROUTING_SNAPSHOTS="off")
        for patcher in (mock.patch.dict(os.environ, env, clear=True),
                        mock.patch.object(R, "_post", side_effect=AssertionError("paid API"))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def mode(self, mode):
        (self.d / "mode").write_text(mode)
        (self.d / "calls").unlink(missing_ok=True)

    def calls(self):
        path = self.d / "calls"
        return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []

    def assert_stopped_and_reaped(self):
        self.assertEqual([c["argv"][1] for c in self.calls()], ["start", "watch", "stop"])
        self.assertNotIn("OPENAI_API_KEY", self.calls()[0]["env"])
        for pid in map(int, (self.d / "pids").read_text().split()):
            for _ in range(50):
                try:
                    os.kill(pid, 0)
                    time.sleep(0.05)
                except ProcessLookupError:
                    break
            else:
                self.fail(f"watch group member {pid} survived")

    def test_env_session_availability_and_parse(self):
        env = L.build_lor_child_env({"OPENAI_API_KEY": "a", "GH_TOKEN": "b", "X_SESSION_TOKEN": "c",
                                     "AWS_REGION": "d", "LOR_SESSION_TOKEN": "e", "HOME": "/h", "PATH": "::r"})
        self.assertEqual(env, {"LOR_SESSION_TOKEN": "e", "HOME": "/h", "PATH": os.path.abspath("r")})
        for sid, opt, want in (("s", "", True), ("s", "1", False), ("s", "ON", False), ("", "", False)):
            self.assertIs(L.lor_in_session({"LOR_SESSION_ID": sid, L.OPT_OUT: opt}), want)
        self.assertIsNone(L.lor_availability())
        self.assertIn("LOR_SESSION_ID", L.lor_availability({"PATH": str(self.d)}))
        self.assertIn("not found", L.lor_availability({"LOR_SESSION_ID": "s", "PATH": "/nonexistent"}))
        msg = {"role": "MESSAGE_ROLE_ASSISTANT", "content": [{"text": "a"}, {"text": "b"}]}
        events = [{"event": {"message": msg, "provider_metadata_json": '{"providerModelId": "m"}'}},
                  {"recorded": {"event": {"metrics": {"usage": {"input_tokens": "2", "reasoning_tokens": "4"}}}}},
                  {"recorded": {"event": {"metrics": {"usage": {"input_tokens": "3", "output_tokens": "x"}}}}},
                  {"recorded": {"event": {"finish": {"reason": "FINISH_REASON_STOP"}}}},
                  {"recorded": {"event": {"error": "after finish"}}}]
        got = L.parse_lor_events(events, "sid")
        self.assertEqual([got[k] for k in ("text", "served_model", "in_tokens", "out_tokens",
                                           "reasoning_tokens", "status", "response_id", "error")],
                         ["ab", "m", 5, None, 4, "completed", "sid", None])
        tail = L.parse_lor_events(events[:1] + [{"event": {"message": {"role": msg["role"]}}}])
        self.assertEqual((tail["text"], tail["served_model"], tail["status"]), ("", None, "incomplete"))

    def test_multi_turn_zero_tool_subagent_is_always_stopped(self):
        msgs = [{"role": "user", "content": "q1"}, {"role": "assistant", "content": "a1"},
                {"role": "user", "content": "q2"}]
        astra = seats()["astra"]
        result, err = R.lor_messages(astra, msgs, "SYS", 20)
        self.assertEqual((err, result["text"], result["served_model"], result["in_tokens"], result["status"]),
                         (None, "OK", "served-x", 7, "completed"))
        start = self.calls()[0]
        argv = start["argv"]
        for flag, value in (("--canonical-model-id", astra["lor_model"]), ("--tools", "pass"),
                            ("--working-directory", start["cwd"]), ("--effort", astra["effort"])):
            self.assertEqual(argv[argv.index(flag) + 1], value)
        self.assertEqual((argv.count("--tools"), start["ls"], Path(start["cwd"]).exists()), (1, [], False))
        self.assertTrue(start["stdin"].startswith("SYS\n\n" + L.NO_TOOLS))
        self.assertRegex(start["stdin"], r'<turn_[0-9a-f]{8} role="assistant">\na1\n</turn_')
        self.assertIn("LOR_SESSION_TOKEN", start["env"])
        self.assertNotIn("GH_TOKEN", start["env"])
        self.assert_stopped_and_reaped()
        for mode, want in (("timeout", "timed out"), ("drop", "watch ended"), ("pass", "no content"),
                           ("error", "router down"), ("cancel", "CANCELLED"), ("empty", "no content")):
            with self.subTest(mode=mode):
                self.mode(mode)
                result = R.run_one(astra, "review", 1.5)
                self.assertIn(want, result["error"])
                self.assert_stopped_and_reaped()

    def test_start_failure_and_secret_scrubbing(self):
        self.mode("start-fail")
        self.assertEqual(R.call_lor(seats()["gemini-neon"], "p", 10),
                         (None, "lor session start failed: refused <LOR_SESSION_TOKEN redacted>"))
        self.assertEqual(len(self.calls()), 1)
        for var in L.LOR_SECRET_ENV:
            self.assertEqual(R.redact(f"x {os.environ[var]}"), f"x <{var} redacted>")
        with mock.patch.object(R.subprocess, "Popen", side_effect=OSError) as popen, \
                self.assertRaises(OSError):
            R._run_journal_append([], "{}\n")
        self.assertFalse(set(R.SECRET_ENV) & set(popen.call_args[1]["env"]))

    def test_routing_inside_outside_and_resolved(self):
        lor_seats = {n for n, r in seats().items() if r.get("lor_model")}
        self.assertEqual(len(lor_seats), 8)
        for name, rev in seats().items():
            want = ("lor", rev["lor_model"]) if name in lor_seats else (rev["provider"], rev["model"])
            self.assertEqual((R.effective_provider(rev), R.effective_model(rev)), want)
            resolved = dict(rev, _resolved_from=rev["model"])
            self.assertEqual(R.effective_provider(resolved), rev["provider"])
            if name in lor_seats and rev.get("enabled", True):
                self.assertIsNone(R.availability(rev))
        self.assertEqual(R.availability(seats()["kimi-k2.7-code"]), "OPENROUTER_API_KEY not set")
        out = io.StringIO()
        with redirect_stdout(out), mock.patch.object(R._subs, "claude_availability", return_value=None), \
                mock.patch.object(R._subs, "codex_availability", return_value=None):
            R.cmd_list(R.load_reviewers(), probe=False)
        rows = dict(line.split()[:2] for line in out.getvalue().splitlines()[1:] if line.strip())
        self.assertEqual({n for n, p in rows.items() if p == "lor"}, lor_seats)
        self.assertEqual((rows["terra"], rows["sonnet-5.5"]), ("codex", "claude"))
        explicit = {"name": "x", "provider": "lor", "model": "m"}
        for env in ({"LOR_SESSION_ID": ""}, {L.OPT_OUT: "1"}):
            with self.subTest(env=env), mock.patch.dict(os.environ, env, OPENAI_API_KEY=""):
                self.assertEqual(R.effective_provider(seats()["astra"]), "openai")
                self.assertEqual(R.availability(seats()["astra"]), "OPENAI_API_KEY not set")
                self.assertIn("lor provider disabled", R.availability(explicit))
                self.assertIn("lor provider disabled", R.call_lor(explicit, "p", 5)[1])

    def test_committee_routes_lor_and_contrasts_effective_providers(self):
        history = [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}]
        self.assertEqual(C.ask_member(seats()["gemini-neon"], history, "p2", 20),
                         ("OK", None, {"in_tokens": 7, "out_tokens": 5}))
        self.assertIn("<conversation_history_", self.calls()[0]["stdin"])
        self.mode("timeout")
        self.assertIn("timed out", C.ask_member(seats()["gemini-neon"], [], "p", 1)[1])
        for profile in ("committee", "planning", "plan"):
            for env in ({}, {"LOR_SESSION_ID": ""}):
                with self.subTest(profile=profile, env=env), mock.patch.dict(os.environ, env):
                    panels = [[r for r in R.load_reviewers() if R.in_profile(r, profile)]]
                    if profile != "plan":
                        panels += [C.pick_members([], authors=[a], profile=profile) for a in
                                   ("claude/claude-sonnet-5-5", "codex/gpt-6.1-terra", "openai/none")]
                    for panel in panels:
                        self.assertGreaterEqual(len(C.transports(panel)), 2)
                        self.assertGreaterEqual(len(R._panels.panel_makers(panel)), 2)
                    self.assertEqual("lor" in C.transports(panels[0]), not env)
        with mock.patch("sys.stderr", io.StringIO()), self.assertRaisesRegex(SystemExit, "provider 'lor'"):
            C.pick_members(["gemini-neon,muse-wonder"])
        data = json.loads(R.CONFIG.read_text())
        for extra, want in (({"lor_model": " "}, "'lor_model'"), ({"provider": "lor", "effort": "huge"}, "huge")):
            revs = [dict(data["reviewers"][0], **extra)] + data["reviewers"][1:]
            (self.d / "r.json").write_text(json.dumps(dict(data, reviewers=revs)))
            with mock.patch.object(R, "CONFIG", self.d / "r.json"), \
                    mock.patch("sys.stderr", io.StringIO()) as err, self.assertRaises(SystemExit):
                R.load_reviewers()
            self.assertIn(want, err.getvalue())


if __name__ == "__main__":
    unittest.main()
