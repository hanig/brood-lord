"""Fail-closed CLI subscription transports for Claude and Codex reviewers.

Routes review-gate and committee calls through local `claude` (claude.ai OAuth)
and `codex` (ChatGPT account) CLIs with API keys stripped from the child
environment so a subscription seat never silently falls back to billed API
credits. Runs inside a fresh empty temporary directory with tools, hooks, user
config, and session persistence disabled.

Python 3.8+, standard library only. Must never import review.py.
"""

import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time

CLAUDE_STRIPPED_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
CODEX_STRIPPED_ENV = ("OPENAI_API_KEY",)
CLAUDE_EFFORTS = ("low", "medium", "high", "max")
CODEX_EFFORTS = ("low", "medium", "high", "xhigh", "max")
SUBSCRIPTION_PROVIDERS = ("claude", "codex")


def claude_config_dir():
    """Directory holding Claude Code local state/login metadata."""
    explicit = (os.environ.get("CLAUDE_CONFIG_DIR") or "").strip()
    if explicit:
        return Path(os.path.expanduser(explicit))
    return Path.home() / ".claude"


def codex_home_dir():
    """Directory holding Codex CLI auth.json."""
    explicit = (os.environ.get("CODEX_HOME") or "").strip()
    if explicit:
        return Path(os.path.expanduser(explicit))
    return Path.home() / ".codex"


def claude_availability():
    """Offline check for Claude CLI subscription transport, or None if ready."""
    if not shutil.which("claude"):
        return "claude CLI not found on PATH"
    cfg = claude_config_dir()
    if not cfg.is_dir():
        return (f"claude subscription config dir not found at {cfg}; "
                f"run 'claude auth login'")
    return None


def codex_availability():
    """Offline check for Codex CLI ChatGPT subscription transport, or None."""
    if not shutil.which("codex"):
        return "codex CLI not found on PATH"
    auth_path = codex_home_dir() / "auth.json"
    if not auth_path.is_file():
        return (f"codex subscription auth not found at {auth_path}; "
                f"run 'codex login'")
    try:
        data = json.loads(auth_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return f"codex auth unreadable at {auth_path}: {type(exc).__name__}"
    if not isinstance(data, dict):
        return f"codex auth at {auth_path} is not a JSON object"
    mode = data.get("auth_mode")
    if mode != "chatgpt":
        return (f"codex auth_mode is {mode!r}, not 'chatgpt' subscription; "
                f"run 'codex login' with a ChatGPT account (refusing API-key fallback)")
    return None


def format_messages(messages):
    """Serialize one or more chat turns for stdin delivery without ARG_MAX limits."""
    if (len(messages) == 1 and isinstance(messages[0], dict)
            and messages[0].get("role") == "user"):
        return str(messages[0].get("content", ""))
    parts = ["<conversation_history>"]
    for msg in messages[:-1]:
        role = str(msg.get("role", "user"))
        content = str(msg.get("content", ""))
        parts.append(f'<turn role="{role}">\n{content}\n</turn>')
    parts.append("</conversation_history>")
    last = messages[-1] if messages else {"role": "user", "content": ""}
    parts.append(f"<current_user_message>\n{last.get('content', '')}\n</current_user_message>")
    return "\n\n".join(parts)


def _remaining_seconds(timeout, deadline):
    """Bounded remaining wall-clock seconds, or (None, error) if already expired."""
    rem = float(timeout)
    if deadline is not None:
        left = float(deadline) - time.monotonic()
        if left <= 0:
            return None, "total review deadline exceeded before CLI launch"
        rem = min(rem, left)
    if rem <= 0:
        return None, "CLI timeout must be positive"
    return rem, None


def _kill_process_group(proc):
    """Terminate the spawned process group so no descendant holds pipes open."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        try:
            proc.kill()
        except OSError:
            pass


def _run_cli(cmd, stdin_text, stripped_env, timeout, deadline=None):
    """Run CLI in a fresh empty directory with stripped API keys and group kill."""
    rem, err = _remaining_seconds(timeout, deadline)
    if err:
        return None, None, None, err
    env = dict(os.environ)
    for var in stripped_env:
        env.pop(var, None)
    with tempfile.TemporaryDirectory(prefix="hanig-review-cli-") as tmp_dir:
        resolved_cmd = [tmp_dir if arg == "__HANIG_EMPTY_CWD__" else arg for arg in cmd]
        try:
            proc = subprocess.Popen(
                resolved_cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=tmp_dir,
                env=env,
                start_new_session=True,
            )
        except OSError as exc:
            return None, None, None, f"failed to start {cmd[0]}: {exc}"
        try:
            out_b, err_b = proc.communicate(
                input=stdin_text.encode("utf-8"), timeout=rem)
        except subprocess.TimeoutExpired:
            _kill_process_group(proc)
            try:
                proc.communicate(timeout=5)
            except Exception:
                pass
            return None, None, None, f"{cmd[0]} timed out after {rem:.1f}s"
        except BaseException:
            _kill_process_group(proc)
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
            raise
    stdout = out_b.decode("utf-8", errors="replace")
    stderr = err_b.decode("utf-8", errors="replace")
    return proc.returncode, stdout, stderr, None


def _parse_json_object(stdout):
    """Parse a JSON object from stdout (whole output or last non-empty JSON line)."""
    text = (stdout or "").strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except ValueError:
        pass
    for line in reversed(text.splitlines()):
        s = line.strip()
        if not s.startswith("{"):
            continue
        try:
            parsed = json.loads(s)
            if isinstance(parsed, dict):
                return parsed
        except ValueError:
            continue
    return None


def _claude_served_model(data, requested):
    """Extract canonical served model from Claude CLI modelUsage when present."""
    model_usage = data.get("modelUsage")
    if not isinstance(model_usage, dict) or not model_usage:
        return None
    entry = model_usage.get(requested)
    if isinstance(entry, dict):
        return entry.get("canonicalModel") or requested
    if len(model_usage) == 1:
        sole_key, sole_val = next(iter(model_usage.items()))
        if isinstance(sole_val, dict):
            return sole_val.get("canonicalModel") or sole_key
        return sole_key
    for key, val in model_usage.items():
        if isinstance(val, dict) and val.get("canonicalModel") == requested:
            return requested
    return None


def claude_messages(rev, messages, system, timeout, deadline=None,
                    *, no_content_error=None):
    """Run one review or committee turn via Claude Code OAuth subscription."""
    why = claude_availability()
    if why:
        return None, why
    claude_bin = shutil.which("claude")
    cmd = [
        claude_bin, "-p",
        "--model", rev["model"],
        "--tools", "",
        "--strict-mcp-config",
        "--setting-sources", "user",
        "--no-session-persistence",
        "--system-prompt", system,
        "--output-format", "json",
    ]
    if rev.get("effort"):
        cmd.extend(["--effort", rev["effort"]])
    rc, stdout, stderr, run_err = _run_cli(
        cmd, format_messages(messages), CLAUDE_STRIPPED_ENV, timeout,
        deadline=deadline)
    if run_err:
        return None, run_err
    data = _parse_json_object(stdout)
    if data is None:
        detail = (stderr or stdout or f"exit {rc}").strip()[:200]
        return None, f"claude CLI returned non-JSON output (exit {rc}): {detail}"
    if data.get("is_error") or rc != 0:
        msg = data.get("result") or stderr or f"exit {rc}"
        return None, f"claude CLI error: {str(msg).strip()[:300]}"
    text = data.get("result")
    if text is None:
        text = ""
    elif not isinstance(text, str):
        return None, f"claude CLI unexpected result type: {type(text).__name__}"
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    detail = (usage.get("output_tokens_details")
              if isinstance(usage.get("output_tokens_details"), dict) else {})
    thinking = detail.get("thinking_tokens")
    if thinking is None:
        thinking = usage.get("reasoning_tokens")
    stop = data.get("stop_reason")
    terminal = data.get("terminal_reason")
    if stop == "end_turn" or (stop is None and terminal == "completed"):
        status = "completed"
    else:
        status = stop or terminal
    out_tokens = usage.get("output_tokens")
    if no_content_error is not None:
        empty_err = no_content_error(
            rev, text,
            finish_reason="max_output_tokens" if stop == "max_tokens" else stop,
            reasoning_tokens=thinking, completion_tokens=out_tokens)
        if empty_err:
            return None, empty_err
    return {
        "text": text,
        "in_tokens": usage.get("input_tokens"),
        "out_tokens": out_tokens,
        "reasoning_tokens": thinking,
        "served_model": _claude_served_model(data, rev["model"]),
        "status": status,
        "response_id": data.get("uuid") or data.get("session_id"),
    }, None


def codex_messages(rev, messages, system, timeout, deadline=None,
                   *, no_content_error=None):
    """Run one review or committee turn via Codex CLI ChatGPT subscription."""
    why = codex_availability()
    if why:
        return None, why
    codex_bin = shutil.which("codex")
    cmd = [
        codex_bin, "exec",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--sandbox", "read-only",
        "--skip-git-repo-check",
        "-C", "__HANIG_EMPTY_CWD__",
        "--json",
        "-m", rev["model"],
    ]
    if rev.get("effort"):
        cmd.extend(["-c", f"model_reasoning_effort={json.dumps(rev['effort'])}"])
    cmd.append("-")
    body = (f"{system}\n\n"
            f"(Analysis only: do not run shell commands or tools; answer "
            f"directly from the provided text.)\n\n---\n\n"
            f"{format_messages(messages)}")
    rc, stdout, stderr, run_err = _run_cli(
        cmd, body, CODEX_STRIPPED_ENV, timeout, deadline=deadline)
    if run_err:
        return None, run_err
    thread_id = None
    agent_messages = []
    usage = None
    completed = False
    turn_error = None
    parsed_any = False
    for line in (stdout or "").splitlines():
        s = line.strip()
        if not s.startswith("{"):
            continue
        try:
            ev = json.loads(s)
        except ValueError:
            continue
        if not isinstance(ev, dict):
            continue
        parsed_any = True
        etype = ev.get("type")
        if etype == "thread.started" and isinstance(ev.get("thread_id"), str):
            thread_id = ev["thread_id"]
        elif etype == "item.completed":
            item = ev.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message":
                msg_text = item.get("text")
                if isinstance(msg_text, str):
                    agent_messages.append(msg_text)
        elif etype == "turn.completed":
            completed = True
            if isinstance(ev.get("usage"), dict):
                usage = ev["usage"]
        elif etype == "turn.failed":
            err_obj = ev.get("error")
            if isinstance(err_obj, dict) and err_obj.get("message"):
                turn_error = str(err_obj["message"])
            else:
                turn_error = str(err_obj or "turn.failed")
        elif etype == "error" and ev.get("message"):
            turn_error = str(ev["message"])
    if turn_error or rc != 0 or not parsed_any:
        detail = turn_error or (stderr or stdout or f"exit {rc}").strip()[:300]
        return None, f"codex CLI error: {detail}"
    text = agent_messages[-1] if agent_messages else ""
    usage = usage or {}
    out_tokens = usage.get("output_tokens")
    reasoning = usage.get("reasoning_output_tokens")
    status = "completed" if completed else "incomplete"
    if no_content_error is not None:
        empty_err = no_content_error(
            rev, text, status=status,
            reasoning_tokens=reasoning, completion_tokens=out_tokens)
        if empty_err:
            return None, empty_err
    return {
        "text": text,
        "in_tokens": usage.get("input_tokens"),
        "out_tokens": out_tokens,
        "reasoning_tokens": reasoning,
        "served_model": rev["model"],
        "status": status,
        "response_id": thread_id,
    }, None
