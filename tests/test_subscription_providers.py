"""Fail-closed CLI subscription transports (claude and codex); all tests offline."""

import ast
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills/hanig-review-gate/scripts"
sys.path.insert(0, str(SCRIPTS))
import committee as C
import model_family as MF
import resolve_models as RM
import review as R
RS = R._subs


class _FakeProc:
    def __init__(self, stdout="", stderr="", returncode=0, pid=42424,
                 timeout_exc=None):
        self._stdout = stdout.encode("utf-8") if isinstance(stdout, str) else stdout
        self._stderr = stderr.encode("utf-8") if isinstance(stderr, str) else stderr
        self.returncode = returncode
        self.pid = pid
        self._timeout_exc = timeout_exc
        self.stdin_received = None
        self.killed = False

    def communicate(self, input=None, timeout=None):
        if input is not None:
            self.stdin_received = input.decode("utf-8")
        if self._timeout_exc is not None:
            exc = self._timeout_exc
            self._timeout_exc = None
            raise exc
        return self._stdout, self._stderr

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        return self.returncode


CLAUDE_AUTH_OK = json.dumps({"loggedIn": True, "authMethod": "claude.ai"})
CODEX_AUTH_OK = json.dumps({
    "auth_mode": "chatgpt",
    "tokens": {"access_token": "tok-access-test", "refresh_token": "tok-refresh-test"},
})


class TestSubscriptionAvailability(unittest.TestCase):
    def test_claude_availability_requires_binary_config_dir_and_claude_ai_oauth(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "no-claude"
            present = Path(tmp) / "claude-cfg"
            present.mkdir()
            with mock.patch.object(RS.shutil, "which", return_value=None):
                self.assertIn("not found", RS.claude_availability())
            with mock.patch.object(RS.shutil, "which", return_value="/usr/bin/claude"), \
                    mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(missing)}):
                self.assertIn("claude auth login", RS.claude_availability())
            with mock.patch.object(RS.shutil, "which", return_value="/usr/bin/claude"), \
                    mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(present)}):
                with mock.patch.object(
                        RS, "_run_cli",
                        return_value=(0, json.dumps({"loggedIn": True, "authMethod": "console"}), "", None)):
                    self.assertIn("not an active 'claude.ai' subscription", RS.claude_availability())
                with mock.patch.object(
                        RS, "_run_cli",
                        return_value=(0, json.dumps({"loggedIn": True, "authMethod": "claude.ai",
                                                     "apiProvider": "bedrock"}), "", None)):
                    self.assertIn("not an active 'claude.ai' subscription", RS.claude_availability())
                with mock.patch.object(
                        RS, "_run_cli",
                        return_value=(0, CLAUDE_AUTH_OK, "", None)):
                    self.assertIsNone(RS.claude_availability())
                    self.assertIsNone(R.availability(
                        {"name": "s", "provider": "claude", "model": "m", "enabled": True}))

    def test_codex_availability_refuses_missing_and_apikey_and_accepts_keyring_chatgpt(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "no-codex"
            home = Path(tmp) / "codex"
            home.mkdir()
            with mock.patch.object(RS.shutil, "which", return_value=None):
                self.assertIn("not found", RS.codex_availability())
            with mock.patch.object(RS.shutil, "which", return_value="/usr/bin/codex"), \
                    mock.patch.dict(os.environ, {"CODEX_HOME": str(missing)}):
                self.assertIn("codex login", RS.codex_availability())
            with mock.patch.object(RS.shutil, "which", return_value="/usr/bin/codex"), \
                    mock.patch.dict(os.environ, {"CODEX_HOME": str(home)}):
                # Existing dir without auth.json falls back to `codex login status`.
                with mock.patch.object(
                        RS, "_run_cli", return_value=(1, "", "Not logged in\n", None)):
                    self.assertIn("codex login", RS.codex_availability())
                # Keyring credential store (even if stale apikey auth.json exists):
                # accepts ChatGPT, refuses API key, ignores section-scoped keys.
                (home / "auth.json").write_text(
                    json.dumps({"auth_mode": "apikey"}), encoding="utf-8")
                (home / "config.toml").write_text(
                    '[profiles.x]\ncli_auth_credentials_store = "keyring"\n',
                    encoding="utf-8")
                self.assertIsNone(RS._codex_credential_store_mode(home))
                self.assertIn("not 'chatgpt'", RS.codex_availability())
                (home / "config.toml").write_text(
                    '"cli_auth_credentials_store" = "keyring"\n', encoding="utf-8")
                with mock.patch.object(
                        RS, "_run_cli",
                        return_value=(0, "Logged in using ChatGPT\n", "", None)):
                    self.assertIsNone(RS.codex_availability())
                with mock.patch.object(
                        RS, "_run_cli",
                        return_value=(0, "Logged in using an API key\n", "", None)):
                    self.assertIn("not an active ChatGPT", RS.codex_availability())
                (home / "config.toml").unlink()
                for bad in ("{", "[]", json.dumps({"auth_mode": "apikey"}),
                            json.dumps({"auth_mode": None}), json.dumps({}),
                            json.dumps({"auth_mode": "chatgpt"}),
                            json.dumps({"auth_mode": "chatgpt", "tokens": {}}),
                            json.dumps({"auth_mode": "chatgpt", "tokens": {"access_token": ""}})):
                    (home / "auth.json").write_text(bad, encoding="utf-8")
                    why = RS.codex_availability()
                    self.assertIsNotNone(why, bad)
                (home / "auth.json").write_text(CODEX_AUTH_OK, encoding="utf-8")
                self.assertIsNone(RS.codex_availability())
                self.assertIsNone(R.availability(
                    {"name": "t", "provider": "codex", "model": "m", "enabled": True}))


class TestSubscriptionTransports(unittest.TestCase):
    CLAUDE_SEAT = {"name": "sonnet-sub", "provider": "claude",
                   "model": "claude-sonnet-5-5", "effort": "high",
                   "max_output_tokens": 128000}
    CODEX_SEAT = {"name": "terra-sub", "provider": "codex",
                  "model": "gpt-6.1-terra", "effort": "xhigh",
                  "max_output_tokens": 128000}

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.claude_dir = self.root / "claude"
        self.claude_dir.mkdir()
        self.codex_dir = self.root / "codex"
        self.codex_dir.mkdir()
        (self.codex_dir / "auth.json").write_text(CODEX_AUTH_OK, encoding="utf-8")
        env = mock.patch.dict(os.environ, {
            "CLAUDE_CONFIG_DIR": str(self.claude_dir),
            "CODEX_HOME": str(self.codex_dir),
            "ANTHROPIC_API_KEY": 'sk-ant-secret-"quoted\\1',
            "ANTHROPIC_AUTH_TOKEN": "tok-ant-secret-2",
            "ANTHROPIC_BASE_URL": "https://proxy.invalid",
            "CLAUDE_CODE_USE_BEDROCK": "1",
            "CLAUDECODE": "1",
            "CLAUDE_CODE_SESSION_ID": "parent-sess",
            "CODEX_API_KEY": "sk-codex-secret-override",
            "CODEX_ACCESS_TOKEN": "tok-codex-secret",
            "OPENAI_API_KEY": 'sk-openai-secret-"quoted\\3',
            "OPENAI_BASE_URL": "https://openai-proxy.invalid",
            "OPENAI_ORGANIZATION": "org-secret",
            "OPENROUTER_API_KEY": "sk-or-secret-4",
            "LINEAR_API_KEY": "lin_api_secret_5",
            "GH_TOKEN": "ghp_secret_6",
            "HANIG_ROUTING_SNAPSHOTS": "off",
        })
        env.start()
        self.addCleanup(env.stop)

    def _claude_json(self, result="OK", **extra):
        data = {
            "is_error": False,
            "result": result,
            "stop_reason": "end_turn",
            "terminal_reason": "completed",
            "uuid": "uuid-claude-1",
            "usage": {
                "input_tokens": 14,
                "output_tokens": 9,
                "output_tokens_details": {"thinking_tokens": 3},
            },
            "modelUsage": {
                "claude-sonnet-5-5": {
                    "canonicalModel": "claude-sonnet-5-5",
                    "maxOutputTokens": 128000,
                }
            },
        }
        data.update(extra)
        return json.dumps(data)

    def _claude_popen_router(self, reply_proc, auth_stdout=CLAUDE_AUTH_OK,
                             auth_rc=0, captured=None):
        """Route `claude auth status` separately from `claude -p` in Popen mocks."""
        def fake_popen(cmd, **kwargs):
            if len(cmd) >= 3 and cmd[1:3] == ["auth", "status"]:
                if captured is not None:
                    captured["auth_cmd"] = cmd
                    captured["auth_kwargs"] = kwargs
                return _FakeProc(stdout=auth_stdout, returncode=auth_rc)
            if captured is not None:
                captured["cmd"] = cmd
                captured["kwargs"] = kwargs
                captured["cwd_entries"] = list(Path(kwargs["cwd"]).iterdir())
            return reply_proc
        return fake_popen

    def _codex_jsonl(self, text="OK", *, thread_id="thr-codex-1",
                     in_tokens=20, out_tokens=7, reasoning=4,
                     before_complete=(), model=None):
        events = [
            {"type": "thread.started", "thread_id": thread_id},
            {"type": "turn.started"},
        ]
        events.extend(before_complete)
        if text is not None:
            events.append({"type": "item.completed", "item": {
                "id": "item_0", "type": "agent_message", "text": text}})
        done = {"type": "turn.completed", "usage": {
            "input_tokens": in_tokens, "output_tokens": out_tokens,
            "reasoning_output_tokens": reasoning}}
        if model is not None:
            done["model"] = model
        events.append(done)
        return "\n".join(json.dumps(e) for e in events) + "\n"

    def test_claude_strips_keys_and_claudecode_disables_settings_and_applies_max_output(self):
        captured = {}
        proc = _FakeProc(stdout=self._claude_json("verdict body"))

        with mock.patch.object(RS.shutil, "which", return_value="/bin/claude"), \
                mock.patch.object(RS.subprocess, "Popen",
                                  side_effect=self._claude_popen_router(proc, captured=captured)):
            res, err = R.PROVIDERS["claude"](self.CLAUDE_SEAT, "review diff", 30)

        self.assertIsNone(err)
        self.assertEqual(res, {
            "text": "verdict body", "in_tokens": 14, "out_tokens": 9,
            "reasoning_tokens": 3, "served_model": "claude-sonnet-5-5",
            "status": "completed", "response_id": "uuid-claude-1",
        })
        cmd = captured["cmd"]
        self.assertEqual(cmd[:4], ["/bin/claude", "-p", "--model", "claude-sonnet-5-5"])
        self.assertEqual(cmd[cmd.index("--tools") + 1], "")
        self.assertIn("--strict-mcp-config", cmd)
        self.assertIn("--no-session-persistence", cmd)
        self.assertEqual(cmd[cmd.index("--setting-sources") + 1], "")
        self.assertEqual(cmd[cmd.index("--effort") + 1], "high")
        self.assertEqual(cmd[cmd.index("--output-format") + 1], "json")
        self.assertEqual(proc.stdin_received, "review diff")
        child_env = captured["kwargs"]["env"]
        for var in RS.STRIPPED_ENV:
            self.assertNotIn(var, child_env, f"{var} leaked into claude child env")
        self.assertEqual(child_env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"], "128000")
        self.assertTrue(captured["kwargs"]["start_new_session"])
        self.assertEqual(captured["cwd_entries"], [])
        self.assertFalse(Path(captured["kwargs"]["cwd"]).exists())

    def test_claude_refuses_console_or_api_key_login_before_review_request(self):
        for auth_payload in (
                {"loggedIn": True, "authMethod": "console"},
                {"loggedIn": True, "authMethod": "api_key"},
                {"loggedIn": False, "authMethod": "none"}):
            with self.subTest(auth=auth_payload):
                captured = {}
                reply = _FakeProc(stdout=self._claude_json("should never run"))
                with mock.patch.object(RS.shutil, "which", return_value="/bin/claude"), \
                        mock.patch.object(
                            RS.subprocess, "Popen",
                            side_effect=self._claude_popen_router(
                                reply, auth_stdout=json.dumps(auth_payload),
                                captured=captured)):
                    res, err = R.PROVIDERS["claude"](self.CLAUDE_SEAT, "p", 10)
                self.assertIsNone(res)
                self.assertIn("not an active 'claude.ai' subscription", err)
                self.assertNotIn("cmd", captured)

    def test_claude_stop_sequence_with_completed_terminal_reason_and_large_system_prompt(self):
        captured = {}
        proc = _FakeProc(stdout=self._claude_json(
            "OK", stop_reason="stop_sequence", terminal_reason="completed"))
        big_system = "S" * (RS._MAX_ARGV_SYSTEM_BYTES + 100)
        with mock.patch.object(RS.shutil, "which", return_value="/bin/claude"), \
                mock.patch.object(RS.subprocess, "Popen",
                                  side_effect=self._claude_popen_router(proc, captured=captured)):
            res, err = R.claude_messages(
                self.CLAUDE_SEAT, [{"role": "user", "content": "ping"}],
                big_system, 10)
        self.assertIsNone(err)
        self.assertEqual(res["status"], "completed")
        cmd = captured["cmd"]
        self.assertLess(len(cmd[cmd.index("--system-prompt") + 1]), 200)
        self.assertIn("<system_instructions>", proc.stdin_received)
        self.assertIn(big_system, proc.stdin_received)

    def test_relative_config_dirs_and_relative_path_bins_resolve_before_tmpdir_cwd(self):
        rel_claude = "rel-claude"
        rel_codex = "rel-codex"
        (self.root / rel_claude).mkdir()
        (self.root / rel_codex).mkdir()
        (self.root / rel_codex / "auth.json").write_text(CODEX_AUTH_OK, encoding="utf-8")
        (self.root / "bin").mkdir()
        fake_claude = self.root / "bin/claude"
        fake_claude.write_text("#!/bin/sh\necho '" + CLAUDE_AUTH_OK + "'\n", encoding="utf-8")
        fake_claude.chmod(0o755)
        old_cwd = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, old_cwd)
        # Real subprocess invocation through relative PATH entry ("bin:...").
        with mock.patch.dict(os.environ, {
                "PATH": "bin:" + os.environ.get("PATH", ""),
                "CLAUDE_CONFIG_DIR": rel_claude}):
            self.assertIsNone(RS.claude_availability())
        captured_cl, captured_cx = {}, {}
        proc_cl = _FakeProc(stdout=self._claude_json("OK"))
        proc_cx = _FakeProc(stdout=self._codex_jsonl("OK"))
        with mock.patch.dict(os.environ, {
                "CLAUDE_CONFIG_DIR": rel_claude, "CODEX_HOME": rel_codex}), \
                mock.patch.object(RS.shutil, "which", return_value="bin/cli"):
            with mock.patch.object(
                    RS.subprocess, "Popen",
                    side_effect=self._claude_popen_router(proc_cl, captured=captured_cl)):
                res, err = R.PROVIDERS["claude"](self.CLAUDE_SEAT, "p", 10)
            self.assertIsNone(err)
            self.assertEqual(
                Path(captured_cl["cmd"][0]).resolve(), (self.root / "bin/cli").resolve())
            self.assertEqual(
                Path(captured_cl["kwargs"]["env"]["CLAUDE_CONFIG_DIR"]).resolve(),
                (self.root / rel_claude).resolve())
            self.assertEqual(
                Path(captured_cl["auth_kwargs"]["env"]["CLAUDE_CONFIG_DIR"]).resolve(),
                (self.root / rel_claude).resolve())
            def fake_cx(cmd, **kwargs):
                captured_cx["cmd"] = cmd
                captured_cx["kwargs"] = kwargs
                return proc_cx
            with mock.patch.object(RS.subprocess, "Popen", side_effect=fake_cx):
                res, err = R.PROVIDERS["codex"](self.CODEX_SEAT, "p", 10)
            self.assertIsNone(err)
            self.assertEqual(
                Path(captured_cx["cmd"][0]).resolve(), (self.root / "bin/cli").resolve())
            self.assertEqual(
                Path(captured_cx["kwargs"]["env"]["CODEX_HOME"]).resolve(),
                (self.root / rel_codex).resolve())

    def test_claude_max_output_tokens_api_error_uses_no_content_classifier(self):
        proc = _FakeProc(
            stdout=self._claude_json(
                is_error=True, api_error="max_output_tokens",
                result="API Error: exceeded output token maximum"),
            returncode=1)
        with mock.patch.object(RS.shutil, "which", return_value="/bin/claude"), \
                mock.patch.object(RS.subprocess, "Popen",
                                  side_effect=self._claude_popen_router(proc)):
            res, err = R.PROVIDERS["claude"](self.CLAUDE_SEAT, "p", 10)
        self.assertIsNone(res)
        self.assertIn("whole output budget", err)

    def test_codex_strips_all_api_keys_disables_tools_and_keeps_served_model_honest(self):
        captured = {}
        proc = _FakeProc(stdout=self._codex_jsonl("codex reply"))

        def fake_popen(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
            captured["cwd_entries"] = list(Path(kwargs["cwd"]).iterdir())
            return proc

        with mock.patch.object(RS.shutil, "which", return_value="/bin/codex"), \
                mock.patch.object(RS.subprocess, "Popen", side_effect=fake_popen):
            res, err = R.PROVIDERS["codex"](self.CODEX_SEAT, "review diff", 30)

        self.assertIsNone(err)
        self.assertEqual(res, {
            "text": "codex reply", "in_tokens": 20, "out_tokens": 7,
            "reasoning_tokens": 4, "served_model": None,
            "status": "completed", "response_id": "thr-codex-1",
        })
        cmd = captured["cmd"]
        self.assertEqual(cmd[:2], ["/bin/codex", "exec"])
        self.assertIn("--ephemeral", cmd)
        self.assertIn("--ignore-user-config", cmd)
        self.assertIn("--ignore-rules", cmd)
        disabled = [cmd[i + 1] for i, tok in enumerate(cmd[:-1]) if tok == "--disable"]
        self.assertEqual(tuple(disabled), RS.CODEX_DISABLED_FEATURES)
        self.assertEqual(cmd[cmd.index("--sandbox") + 1], "read-only")
        self.assertIn("--skip-git-repo-check", cmd)
        self.assertEqual(cmd[cmd.index("-C") + 1], captured["kwargs"]["cwd"])
        self.assertEqual(cmd[cmd.index("-m") + 1], "gpt-6.1-terra")
        c_args = [cmd[i + 1] for i, tok in enumerate(cmd[:-1]) if tok == "-c"]
        self.assertEqual(c_args, [
            'model_reasoning_effort="xhigh"',
            'cli_auth_credentials_store="file"',
        ])
        self.assertEqual(cmd[-1], "-")
        self.assertIn("review diff", proc.stdin_received)
        self.assertIn(R.SYSTEM, proc.stdin_received)
        child_env = captured["kwargs"]["env"]
        for var in RS.STRIPPED_ENV:
            self.assertNotIn(var, child_env, f"{var} leaked into codex child env")
        self.assertTrue(captured["kwargs"]["start_new_session"])
        self.assertEqual(captured["cwd_entries"], [])
        self.assertFalse(Path(captured["kwargs"]["cwd"]).exists())

        # Keyring store forwards cli_auth_credentials_store alongside --ignore-user-config.
        (self.codex_dir / "auth.json").unlink()
        (self.codex_dir / "config.toml").write_text(
            'cli_auth_credentials_store = "keyring"\n', encoding="utf-8")
        kr_cmds = []
        proc_kr = _FakeProc(stdout=self._codex_jsonl("keyring reply"))

        def fake_kr_popen(cmd, **kwargs):
            kr_cmds.append(cmd)
            if len(cmd) >= 3 and cmd[1:3] == ["login", "status"]:
                return _FakeProc(stdout="Logged in using ChatGPT\n", returncode=0)
            return proc_kr

        with mock.patch.object(RS.shutil, "which", return_value="/bin/codex"), \
                mock.patch.object(RS.subprocess, "Popen", side_effect=fake_kr_popen):
            res_kr, err_kr = R.PROVIDERS["codex"](self.CODEX_SEAT, "p", 10)
        self.assertIsNone(err_kr)
        self.assertEqual(res_kr["text"], "keyring reply")
        exec_cmd = kr_cmds[-1]
        kr_c = [exec_cmd[i + 1] for i, tok in enumerate(exec_cmd[:-1]) if tok == "-c"]
        self.assertIn('cli_auth_credentials_store="keyring"', kr_c)
        (self.codex_dir / "config.toml").unlink()
        (self.codex_dir / "auth.json").write_text(CODEX_AUTH_OK, encoding="utf-8")

        verdict = json.dumps({"verdict": "upheld", "findings": [], "claims": [
            {"claim_index": 0, "claim": "c", "status": "supported", "why": "ok ok ok"}]})
        proc_v = _FakeProc(stdout=self._codex_jsonl(verdict))
        with mock.patch.object(RS.shutil, "which", return_value="/bin/codex"), \
                mock.patch.object(RS.subprocess, "Popen", return_value=proc_v):
            rec, why = RM.probe_reviewer(self.CODEX_SEAT, "gpt-6.1-terra", 10)
        self.assertIsNone(rec)
        self.assertIn("served None", why)

        proc_m = _FakeProc(stdout=self._codex_jsonl(verdict, model="gpt-6.1-terra"))
        with mock.patch.object(RS.shutil, "which", return_value="/bin/codex"), \
                mock.patch.object(RS.subprocess, "Popen", return_value=proc_m):
            rec, why = RM.probe_reviewer(self.CODEX_SEAT, "gpt-6.1-terra", 10)
        self.assertIsNone(why)
        self.assertEqual(rec["model"], "gpt-6.1-terra")

    def test_codex_transient_error_recovers_on_turn_completed_and_empty_reply_cleanly_errors(self):
        proc_rec = _FakeProc(stdout=self._codex_jsonl(
            "OK", before_complete=({"type": "error", "message": "Reconnecting... 1/5"},)))
        with mock.patch.object(RS.shutil, "which", return_value="/bin/codex"), \
                mock.patch.object(RS.subprocess, "Popen", return_value=proc_rec):
            res, err = R.PROVIDERS["codex"](self.CODEX_SEAT, "p", 10)
        self.assertIsNone(err)
        self.assertEqual(res["text"], "OK")
        self.assertEqual(res["status"], "completed")

        proc_empty = _FakeProc(stdout=self._codex_jsonl(None))
        with mock.patch.object(RS.shutil, "which", return_value="/bin/codex"), \
                mock.patch.object(RS.subprocess, "Popen", return_value=proc_empty):
            res, err = R.PROVIDERS["codex"](self.CODEX_SEAT, "p", 10)
        self.assertIsNone(res)
        self.assertIn("no content", err)

    def test_timeout_kills_process_group_and_expired_deadline_never_spawns(self):
        with mock.patch.object(RS.shutil, "which", return_value="/bin/codex"), \
                mock.patch.object(RS.subprocess, "Popen") as popen:
            res, err = R.PROVIDERS["codex"](
                self.CODEX_SEAT, "p", 10, deadline=time.monotonic() - 1)
            self.assertIsNone(res)
            self.assertIn("deadline", err)
            popen.assert_not_called()

        proc = _FakeProc(timeout_exc=subprocess.TimeoutExpired(["codex"], 1))
        with mock.patch.object(RS.shutil, "which", return_value="/bin/codex"), \
                mock.patch.object(RS.subprocess, "Popen", return_value=proc), \
                mock.patch.object(RS.os, "killpg") as killpg:
            res, err = R.PROVIDERS["codex"](self.CODEX_SEAT, "p", 1)
        self.assertIsNone(res)
        self.assertIn("timed out", err)
        killpg.assert_called_once_with(proc.pid, RS.signal.SIGKILL)

    def test_errors_and_redaction_cover_both_subscription_transports(self):
        secret = os.environ["OPENAI_API_KEY"]
        proc_cl = _FakeProc(stdout=self._claude_json(result="leak " + secret,
                                                      uuid=secret))
        with mock.patch.object(RS.shutil, "which", return_value="/bin/claude"), \
                mock.patch.object(RS.subprocess, "Popen",
                                  side_effect=self._claude_popen_router(proc_cl)):
            res, err = R.PROVIDERS["claude"](self.CLAUDE_SEAT, "p", 10)
        self.assertIsNone(err)
        self.assertNotIn(secret, json.dumps(res))
        self.assertIn("OPENAI_API_KEY redacted", res["text"])
        self.assertIn("OPENAI_API_KEY redacted", res["response_id"])

        proc_cl2 = _FakeProc(stdout=self._claude_json(result="leak " + secret))
        with mock.patch.object(RS.shutil, "which", return_value="/bin/claude"), \
                mock.patch.object(RS.subprocess, "Popen",
                                  side_effect=self._claude_popen_router(proc_cl2)):
            text, err, _usage = C.ask_member(
                self.CLAUDE_SEAT, [], "p", 10, preserve_reply=True)
        self.assertIsNone(err)
        self.assertNotIn(secret, text)
        self.assertIn("OPENAI_API_KEY redacted", text)

        proc_cl_err = _FakeProc(
            stdout=self._claude_json(is_error=True, result="auth failed " + secret),
            returncode=1)
        with mock.patch.object(RS.shutil, "which", return_value="/bin/claude"), \
                mock.patch.object(RS.subprocess, "Popen",
                                  side_effect=self._claude_popen_router(proc_cl_err)):
            res, err = R.PROVIDERS["claude"](self.CLAUDE_SEAT, "p", 10)
        self.assertIsNone(res)
        self.assertNotIn(secret, err)
        self.assertIn("claude CLI error", err)

        failed_jsonl = "\n".join(json.dumps(e) for e in (
            {"type": "thread.started", "thread_id": "t1"},
            {"type": "turn.failed", "error": {"message": "bad model " + secret}},
        )) + "\n"
        proc_cx_err = _FakeProc(stdout=failed_jsonl, returncode=1)
        with mock.patch.object(RS.shutil, "which", return_value="/bin/codex"), \
                mock.patch.object(RS.subprocess, "Popen", return_value=proc_cx_err):
            res, err = R.PROVIDERS["codex"](self.CODEX_SEAT, "p", 10)
        self.assertIsNone(res)
        self.assertNotIn(secret, err)
        self.assertIn("codex CLI error", err)

        proc_empty = _FakeProc(stdout=self._claude_json(
            result="", stop_reason="max_tokens"))
        with mock.patch.object(RS.shutil, "which", return_value="/bin/claude"), \
                mock.patch.object(RS.subprocess, "Popen",
                                  side_effect=self._claude_popen_router(proc_empty)):
            res, err = R.PROVIDERS["claude"](self.CLAUDE_SEAT, "p", 10)
        self.assertIsNone(res)
        self.assertIn("whole output budget", err)

    def test_committee_multi_turn_works_for_claude_and_codex_and_checks_makers(self):
        history = [{"role": "user", "content": "turn 1 q"},
                   {"role": "assistant", "content": "turn 1 a"}]
        proc_cl = _FakeProc(stdout=self._claude_json("turn 2 claude"))
        with mock.patch.object(RS.shutil, "which", return_value="/bin/claude"), \
                mock.patch.object(RS.subprocess, "Popen",
                                  side_effect=self._claude_popen_router(proc_cl)):
            text, err, usage = C.ask_member(
                self.CLAUDE_SEAT, history, "turn 2 q", 10, require_complete=True)
        self.assertEqual((text, err, usage),
                         ("turn 2 claude", None, {"in_tokens": 14, "out_tokens": 9}))
        self.assertIn("turn 1 q", proc_cl.stdin_received)
        self.assertIn("turn 1 a", proc_cl.stdin_received)
        self.assertIn("turn 2 q", proc_cl.stdin_received)

        proc_cx = _FakeProc(stdout=self._codex_jsonl("turn 2 codex"))
        with mock.patch.object(RS.shutil, "which", return_value="/bin/codex"), \
                mock.patch.object(RS.subprocess, "Popen", return_value=proc_cx):
            text, err, usage = C.ask_member(
                self.CODEX_SEAT, history, "turn 2 q", 10, require_complete=True)
        self.assertEqual((text, err, usage),
                         ("turn 2 codex", None, {"in_tokens": 20, "out_tokens": 7}))
        self.assertIn("turn 1 a", proc_cx.stdin_received)
        self.assertIn("turn 2 q", proc_cx.stdin_received)

        plan_err = R._load_plan_review()._plan_panel_error([
            {"name": "terra", "provider": "codex", "model": "gpt-6.1-terra"},
            {"name": "astra", "provider": "openai", "model": "gpt-6.2-astra"},
        ])
        self.assertIsNotNone(plan_err)
        self.assertIn("share a model maker", plan_err)
        with self.assertRaises(SystemExit) as stopped:
            C.pick_members(["terra,astra"])
        self.assertIn("model maker", str(stopped.exception))

    def test_resolve_models_requires_paseo_listing_for_subscription_seats(self):
        good_claude = dict(self.CLAUDE_SEAT, family={
            "listing": "paseo:claude", "vendor": "", "prefix": "claude-sonnet-",
            "separator": "-", "suffix": "", "major": 5})
        good_codex = dict(self.CODEX_SEAT, family={
            "listing": "paseo:codex", "vendor": "", "prefix": "gpt-",
            "separator": ".", "suffix": "-terra", "major": 6})
        RM.validate_config([good_claude, good_codex], None)

        bad_claude = dict(good_claude, family=dict(good_claude["family"], listing="anthropic"))
        with self.assertRaises(MF.FamilyError):
            RM.validate_config([bad_claude], None)
        bad_codex = dict(good_codex, family=dict(good_codex["family"], listing="openai"))
        with self.assertRaises(MF.FamilyError):
            RM.validate_config([bad_codex], None)

    def test_module_does_not_import_review_facade(self):
        tree = ast.parse((SCRIPTS / "review_subscriptions.py").read_text(encoding="utf-8"))
        imports = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        imports |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import)
                    for a in n.names}
        self.assertNotIn("review", {str(n).split(".")[0] for n in imports})


if __name__ == "__main__":
    unittest.main()
