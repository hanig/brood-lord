"""Fail-closed CLI subscription transports (claude and codex); all tests offline."""

import ast
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills/hanig-review-gate/scripts"
sys.path.insert(0, str(SCRIPTS))
import committee as C
import model_family as MF
import resolve_models as RM
import review as R
import review_subscriptions as RS


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


class TestSubscriptionAvailability(unittest.TestCase):
    def test_claude_availability_requires_binary_and_config_dir(self):
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
                self.assertIsNone(RS.claude_availability())
                self.assertIsNone(R.availability(
                    {"name": "s", "provider": "claude", "model": "m", "enabled": True}))

    def test_codex_availability_refuses_missing_and_apikey_auth_modes(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            with mock.patch.object(RS.shutil, "which", return_value=None):
                self.assertIn("not found", RS.codex_availability())
            with mock.patch.object(RS.shutil, "which", return_value="/usr/bin/codex"), \
                    mock.patch.dict(os.environ, {"CODEX_HOME": str(home)}):
                self.assertIn("codex login", RS.codex_availability())
                for bad in ("{", "[]", json.dumps({"auth_mode": "apikey"}),
                            json.dumps({"auth_mode": None}), json.dumps({})):
                    (home / "auth.json").write_text(bad, encoding="utf-8")
                    why = RS.codex_availability()
                    self.assertIsNotNone(why, bad)
                (home / "auth.json").write_text(
                    json.dumps({"auth_mode": "chatgpt"}), encoding="utf-8")
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
        (self.codex_dir / "auth.json").write_text(
            json.dumps({"auth_mode": "chatgpt"}), encoding="utf-8")
        env = mock.patch.dict(os.environ, {
            "CLAUDE_CONFIG_DIR": str(self.claude_dir),
            "CODEX_HOME": str(self.codex_dir),
            "ANTHROPIC_API_KEY": 'sk-ant-secret-"quoted\\1',
            "ANTHROPIC_AUTH_TOKEN": "tok-ant-secret-2",
            "OPENAI_API_KEY": 'sk-openai-secret-"quoted\\3',
            "OPENROUTER_API_KEY": "sk-or-secret-4",
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

    def _codex_jsonl(self, text="OK", *, thread_id="thr-codex-1",
                     in_tokens=20, out_tokens=7, reasoning=4,
                     extra_events=()):
        events = [
            {"type": "thread.started", "thread_id": thread_id},
            {"type": "turn.started"},
            {"type": "item.completed", "item": {
                "id": "item_0", "type": "agent_message", "text": text}},
            {"type": "turn.completed", "usage": {
                "input_tokens": in_tokens, "output_tokens": out_tokens,
                "reasoning_output_tokens": reasoning}},
        ]
        events.extend(extra_events)
        return "\n".join(json.dumps(e) for e in events) + "\n"

    def test_claude_strips_api_keys_runs_in_empty_cwd_and_parses_result(self):
        captured = {}
        proc = _FakeProc(stdout=self._claude_json("verdict body"))

        def fake_popen(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
            captured["cwd_entries"] = list(Path(kwargs["cwd"]).iterdir())
            return proc

        with mock.patch.object(RS.shutil, "which", return_value="/bin/claude"), \
                mock.patch.object(RS.subprocess, "Popen", side_effect=fake_popen):
            res, err = R.PROVIDERS["claude"](self.CLAUDE_SEAT, "review diff", 30)

        self.assertIsNone(err)
        self.assertEqual(res, {
            "text": "verdict body", "in_tokens": 14, "out_tokens": 9,
            "reasoning_tokens": 3, "served_model": "claude-sonnet-5-5",
            "status": "completed", "response_id": "uuid-claude-1",
        })
        cmd = captured["cmd"]
        self.assertEqual(cmd[:4], ["/bin/claude", "-p", "--model", "claude-sonnet-5-5"])
        self.assertIn("--tools", cmd)
        self.assertEqual(cmd[cmd.index("--tools") + 1], "")
        self.assertIn("--strict-mcp-config", cmd)
        self.assertIn("--no-session-persistence", cmd)
        self.assertEqual(cmd[cmd.index("--setting-sources") + 1], "user")
        self.assertEqual(cmd[cmd.index("--effort") + 1], "high")
        self.assertEqual(cmd[cmd.index("--output-format") + 1], "json")
        self.assertEqual(proc.stdin_received, "review diff")
        self.assertNotIn("ANTHROPIC_API_KEY", captured["kwargs"]["env"])
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", captured["kwargs"]["env"])
        self.assertTrue(captured["kwargs"]["start_new_session"])
        self.assertEqual(captured["cwd_entries"], [])
        self.assertFalse(Path(captured["kwargs"]["cwd"]).exists())

    def test_codex_strips_openai_key_runs_in_empty_cwd_and_parses_jsonl(self):
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
            "reasoning_tokens": 4, "served_model": "gpt-6.1-terra",
            "status": "completed", "response_id": "thr-codex-1",
        })
        cmd = captured["cmd"]
        self.assertEqual(cmd[:2], ["/bin/codex", "exec"])
        self.assertIn("--ephemeral", cmd)
        self.assertIn("--ignore-user-config", cmd)
        self.assertIn("--ignore-rules", cmd)
        self.assertEqual(cmd[cmd.index("--sandbox") + 1], "read-only")
        self.assertIn("--skip-git-repo-check", cmd)
        self.assertEqual(cmd[cmd.index("-C") + 1], captured["kwargs"]["cwd"])
        self.assertEqual(cmd[cmd.index("-m") + 1], "gpt-6.1-terra")
        self.assertEqual(cmd[cmd.index("-c") + 1], 'model_reasoning_effort="xhigh"')
        self.assertEqual(cmd[-1], "-")
        self.assertIn("review diff", proc.stdin_received)
        self.assertIn(R.SYSTEM, proc.stdin_received)
        self.assertNotIn("OPENAI_API_KEY", captured["kwargs"]["env"])
        self.assertTrue(captured["kwargs"]["start_new_session"])
        self.assertEqual(captured["cwd_entries"], [])
        self.assertFalse(Path(captured["kwargs"]["cwd"]).exists())

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
        # Reflected secret in stdout/error must be redacted at the provider boundary.
        proc_cl = _FakeProc(stdout=self._claude_json(result="leak " + secret,
                                                      uuid=secret))
        with mock.patch.object(RS.shutil, "which", return_value="/bin/claude"), \
                mock.patch.object(RS.subprocess, "Popen", return_value=proc_cl):
            res, err = R.PROVIDERS["claude"](self.CLAUDE_SEAT, "p", 10)
        self.assertIsNone(err)
        self.assertNotIn(secret, json.dumps(res))
        self.assertIn("OPENAI_API_KEY redacted", res["text"])
        self.assertIn("OPENAI_API_KEY redacted", res["response_id"])

        # Claude is_error and Codex turn.failed both fail cleanly with redaction.
        proc_cl_err = _FakeProc(
            stdout=self._claude_json(is_error=True, result="auth failed " + secret),
            returncode=1)
        with mock.patch.object(RS.shutil, "which", return_value="/bin/claude"), \
                mock.patch.object(RS.subprocess, "Popen", return_value=proc_cl_err):
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

        # Empty text invokes no_content_error.
        proc_empty = _FakeProc(stdout=self._claude_json(
            result="", stop_reason="max_tokens"))
        with mock.patch.object(RS.shutil, "which", return_value="/bin/claude"), \
                mock.patch.object(RS.subprocess, "Popen", return_value=proc_empty):
            res, err = R.PROVIDERS["claude"](self.CLAUDE_SEAT, "p", 10)
        self.assertIsNone(res)
        self.assertIn("whole output budget", err)

    def test_committee_multi_turn_works_for_claude_and_codex_and_checks_makers(self):
        history = [{"role": "user", "content": "turn 1 q"},
                   {"role": "assistant", "content": "turn 1 a"}]
        proc_cl = _FakeProc(stdout=self._claude_json("turn 2 claude"))
        with mock.patch.object(RS.shutil, "which", return_value="/bin/claude"), \
                mock.patch.object(RS.subprocess, "Popen", return_value=proc_cl):
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

        # Codex + OpenAI share maker 'openai' and must be rejected as non-contrasting.
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
