"""Fail-closed CLI subscription transports for Claude and Codex reviewers.

Routes review-gate and committee calls through local `claude` (claude.ai OAuth)
and `codex` (ChatGPT account) CLIs via a single sanitized `build_child_env()`
that strips provider/billing/session prefixes (`ANTHROPIC_*`, `AWS_*`,
`AZURE_*`, `CLAUDE_*`, `CLAUDECODE*`, `CODEX_*`, `OPENAI_*`, `OPENROUTER_*`,
`GEMINI_*`, `GOOGLE_API_*`, `GOOGLE_APPLICATION_CREDENTIALS`, `GOOGLE_GENAI_*`,
`VERTEX_*`, `CLOUD_ML_*`), credential/endpoint suffixes (`*_API_KEY`,
`*_AUTH_TOKEN`, `*_BASE_URL`, `*_ACCESS_KEY_ID`, `*_SECRET_ACCESS_KEY`,
`*_SESSION_TOKEN`, `*_CLIENT_SECRET`), and `STRIPPED_ENV` while re-injecting only
explicitly managed overrides (`CLAUDE_CONFIG_DIR`,
`CLAUDE_CODE_MAX_OUTPUT_TOKENS`, `CODEX_HOME`). Runs inside a fresh empty
temporary directory with tools, user/project/local settings and hooks, and
session persistence disabled.

Declared limits:
- Claude CLI `--setting-sources ""` disables user, project, and local settings
  and hooks; host-administrator managed policies (e.g.
  `/etc/claude-code/managed-settings.json`) remain governed by the host.
- Claude CLI enforces `max_output_tokens` via `CLAUDE_CODE_MAX_OUTPUT_TOKENS`,
  verifies `authMethod == "claude.ai"` via `claude auth status` before every
  call (refusing Console / API-key / cloud logins), and reports `canonicalModel`
  in `modelUsage`.
- Codex CLI (`codex exec --json`) disables interactive/shell/exec/app/browser/
  sleep tools via `--disable`, enforces `-c forced_login_method="chatgpt"` and
  `-c cli_auth_credentials_store=...` alongside `--ignore-user-config`, exposes
  no client-side `max_output_tokens` cap, and currently omits the served model
  id from JSONL events, so `codex_messages` reports `served_model: None` unless
  an event explicitly carries a `model` string (fail-closed for exact-model
  resolution).

Python 3.8+, standard library only. Must never import review.py.
"""

import importlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time

STRIPPED_ENV = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_AWS_API_KEY",
    "ANTHROPIC_FOUNDRY_API_KEY",
    "ANTHROPIC_FOUNDRY_AUTH_TOKEN",
    "CLAUDE_CODE_API_BASE_URL",
    "CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_SESSION_ID",
    "CODEX_API_KEY",
    "CODEX_ACCESS_TOKEN",
    "CODEX_OSS_BASE_URL",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_ORGANIZATION",
    "OPENAI_PROJECT",
    "OPENROUTER_API_KEY",
    "GOOGLE_API_KEY",
    "GEMINI_API_KEY",
    "LINEAR_API_KEY",
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "WANDB_API_KEY",
)
STRIPPED_ENV_PREFIXES = (
    "ANTHROPIC_",
    "AWS_",
    "AZURE_",
    "CLAUDE_",
    "CLAUDECODE",
    "CODEX_",
    "OPENAI_",
    "OPENROUTER_",
    "GEMINI_",
    "GOOGLE_API_",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_GENAI_",
    "VERTEX_",
    "CLOUD_ML_",
)
STRIPPED_ENV_SUFFIXES = (
    "_API_KEY",
    "_AUTH_TOKEN",
    "_BASE_URL",
    "_ACCESS_KEY_ID",
    "_SECRET_ACCESS_KEY",
    "_SESSION_TOKEN",
    "_CLIENT_SECRET",
)
CLAUDE_STRIPPED_ENV = STRIPPED_ENV
CODEX_STRIPPED_ENV = STRIPPED_ENV
CLAUDE_EFFORTS = ("low", "medium", "high", "max")
CODEX_EFFORTS = ("low", "medium", "high", "xhigh", "max")
CODEX_DISABLED_FEATURES = (
    "shell_tool",
    "unified_exec",
    "apps",
    "computer_use",
    "browser_use",
    "sleep_tool",
)
SUBSCRIPTION_PROVIDERS = ("claude", "codex")
_MAX_ARGV_SYSTEM_BYTES = 65536
_CRED_STORE_KEY_RE = re.compile(
    r'^\s*(?:cli_auth_credentials_store|"cli_auth_credentials_store"'
    r"|'cli_auth_credentials_store')\s*=\s*(.*)$"
)
_TOML_SECTION_RE = re.compile(r'^\s*\[')


def _sanitized_path(raw_path=None):
    """Drop empty PATH segments and absolutize relative entries before tmpdir chdir."""
    value = os.environ.get("PATH", "") if raw_path is None else raw_path
    parts = []
    for entry in value.split(os.pathsep):
        if not entry:
            continue
        parts.append(os.path.abspath(os.path.expanduser(entry)))
    return os.pathsep.join(parts)


def _should_strip_env_key(key, explicit_names):
    if key in explicit_names:
        return True
    upper = key.upper()
    return (any(upper.startswith(p) for p in STRIPPED_ENV_PREFIXES)
            or any(upper.endswith(s) for s in STRIPPED_ENV_SUFFIXES))


def build_child_env(stripped_env=STRIPPED_ENV, extra_env=None):
    """Build sanitized child env shared by availability probes and CLI calls."""
    explicit = set(stripped_env)
    env = {k: v for k, v in os.environ.items()
           if not _should_strip_env_key(k, explicit)}
    if "PATH" in env:
        env["PATH"] = _sanitized_path(env["PATH"])
    if extra_env:
        env.update(extra_env)
    return env


def _resolve_cli_bin(name):
    """Locate CLI binary as an absolute path using sanitized PATH."""
    if "PATH" in os.environ:
        clean_path = _sanitized_path(os.environ["PATH"])
        if not clean_path:
            return None
        found = shutil.which(name, path=clean_path)
    else:
        found = shutil.which(name)
    return os.path.abspath(found) if found else None


def claude_config_dir():
    """Absolute directory holding Claude Code local state/login metadata."""
    explicit = (os.environ.get("CLAUDE_CONFIG_DIR") or "").strip()
    if explicit:
        return Path(os.path.abspath(os.path.expanduser(explicit)))
    return Path.home() / ".claude"


def codex_home_dir():
    """Absolute directory holding Codex CLI auth.json / config.toml."""
    explicit = (os.environ.get("CODEX_HOME") or "").strip()
    if explicit:
        return Path(os.path.abspath(os.path.expanduser(explicit)))
    return Path.home() / ".codex"


def _abs_env_override(var_name):
    """If var_name is set and non-empty, return {var_name: abspath(expanduser)}."""
    raw = (os.environ.get(var_name) or "").strip()
    if not raw:
        return {}
    return {var_name: os.path.abspath(os.path.expanduser(raw))}


def claude_availability(timeout=15.0, deadline=None):
    """Check Claude CLI subscription transport (`claude.ai` OAuth only).

    Verifies binary presence, config directory existence, and an active
    `authMethod == "claude.ai"` login via `claude auth status` with all API
    and cloud billing env vars stripped (refusing Console / API-key logins).
    """
    claude_bin = _resolve_cli_bin("claude")
    if not claude_bin:
        return "claude CLI not found on PATH"
    cfg = claude_config_dir()
    if not cfg.is_dir():
        return (f"claude subscription config dir not found at {cfg}; "
                f"run 'claude auth login'")
    return _verify_claude_subscription(
        claude_bin, _abs_env_override("CLAUDE_CONFIG_DIR"),
        timeout, deadline=deadline)


def _parse_cred_store_regex(text):
    """Python 3.8-3.10 stdlib fallback for top-level cli_auth_credentials_store."""
    valid = ("keyring", "auto", "file")
    in_triple = None
    capturing_key = False
    captured = []
    for line in text.splitlines():
        if in_triple is not None:
            idx = line.find(in_triple)
            if idx != -1:
                if capturing_key:
                    captured.append(line[:idx])
                    tail = line[idx + 3:].strip()
                    val = "\n".join(captured)
                    if val.startswith("\n"):
                        val = val[1:]
                    capturing_key = False
                    captured = []
                    in_triple = None
                    if (not tail or tail.startswith("#")) and val in valid:
                        return val
                else:
                    in_triple = None
            elif capturing_key:
                captured.append(line)
            continue
        if _TOML_SECTION_RE.match(line):
            break
        m = _CRED_STORE_KEY_RE.match(line)
        if m:
            rhs = m.group(1).lstrip()
            for delim in ('"""', "'''"):
                if rhs.startswith(delim):
                    rest = rhs[3:]
                    end = rest.find(delim)
                    if end != -1:
                        tail = rest[end + 3:].strip()
                        val = rest[:end]
                        if (not tail or tail.startswith("#")) and val in valid:
                            return val
                        break
                    in_triple = delim
                    capturing_key = True
                    captured = [rest]
                    break
            else:
                for q in ('"', "'"):
                    if rhs.startswith(q):
                        rest = rhs[1:]
                        end = rest.find(q)
                        if end != -1:
                            tail = rest[end + 1:].strip()
                            val = rest[:end]
                            if (not tail or tail.startswith("#")) and val in valid:
                                return val
                        break
            continue
        stripped = line.split("#", 1)[0]
        for delim in ('"""', "'''"):
            if stripped.count(delim) % 2 == 1:
                in_triple = delim
                break
    return None


def _codex_credential_store_mode(home_dir):
    """Read top-level `cli_auth_credentials_store` from Codex config.toml."""
    cfg = home_dir / "config.toml"
    if not cfg.is_file():
        return None
    try:
        text = cfg.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if sys.version_info >= (3, 11):
        try:
            tomllib = importlib.import_module("tomllib")
            parsed = tomllib.loads(text)
            val = parsed.get("cli_auth_credentials_store") if isinstance(parsed, dict) else None
            return val if val in ("keyring", "auto", "file") else None
        except Exception:
            return None
    return _parse_cred_store_regex(text)


def _verify_codex_keyring_login(codex_bin, home_dir, timeout=10.0, deadline=None):
    """Check `codex login status` for a ChatGPT keyring login (fail closed)."""
    rc, stdout, stderr, run_err = _run_cli(
        [codex_bin, "login", "status"], "", STRIPPED_ENV, timeout,
        deadline=deadline, extra_env={"CODEX_HOME": str(home_dir)})
    if run_err:
        return f"codex keyring auth check failed: {run_err}"
    combined = (stdout or "") + "\n" + (stderr or "")
    if rc == 0 and "Logged in using ChatGPT" in combined:
        return None
    detail = combined.strip()[:200] or f"exit {rc}"
    return (f"codex keyring login is not an active ChatGPT subscription "
            f"({detail}); run 'codex login' with a ChatGPT account")


def codex_availability(timeout=10.0, deadline=None):
    """Check Codex CLI ChatGPT subscription auth (`auth.json` or keyring)."""
    codex_bin = _resolve_cli_bin("codex")
    if not codex_bin:
        return "codex CLI not found on PATH"
    home_dir = codex_home_dir()
    auth_path = home_dir / "auth.json"
    store_mode = _codex_credential_store_mode(home_dir)
    if store_mode in ("keyring", "auto"):
        return _verify_codex_keyring_login(
            codex_bin, home_dir, timeout=timeout, deadline=deadline)
    if auth_path.is_file():
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
        tokens = data.get("tokens")
        if not isinstance(tokens, dict) or not any(
                isinstance(tokens.get(k), str) and tokens.get(k).strip()
                for k in ("access_token", "refresh_token")):
            return (f"codex auth at {auth_path} has auth_mode='chatgpt' but no "
                    f"active tokens; run 'codex login' with a ChatGPT account")
        return None
    if home_dir.is_dir() and store_mode != "file":
        return _verify_codex_keyring_login(
            codex_bin, home_dir, timeout=timeout, deadline=deadline)
    return (f"codex subscription auth not found at {auth_path}; "
            f"run 'codex login'")


def format_messages(messages):
    """Serialize one or more chat turns for stdin delivery without ARG_MAX limits.

    Single-turn user prompts pass through raw text unchanged. Multi-turn
    conversation histories use a per-call random nonce tag so literal `</turn>`
    content in diffs or replies cannot close a turn boundary without escaping
    or mutating code bytes.
    """
    if (len(messages) == 1 and isinstance(messages[0], dict)
            and messages[0].get("role") == "user"):
        return str(messages[0].get("content", ""))
    all_text = "\n".join(str(m.get("content", "")) for m in messages if isinstance(m, dict))
    nonce = secrets.token_hex(4)
    while nonce in all_text:
        nonce = secrets.token_hex(4)
    tag = f"turn_{nonce}"
    parts = [f"<conversation_history_{nonce}>"]
    for msg in messages[:-1]:
        role = str(msg.get("role", "user"))
        content = str(msg.get("content", ""))
        parts.append(f'<{tag} role="{role}">\n{content}\n</{tag}>')
    parts.append(f"</conversation_history_{nonce}>")
    last = messages[-1] if messages else {"role": "user", "content": ""}
    parts.append(
        f"<current_user_message_{nonce}>\n"
        f"{last.get('content', '')}\n"
        f"</current_user_message_{nonce}>"
    )
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


def _run_cli(cmd, stdin_text, stripped_env, timeout, deadline=None,
             *, extra_env=None):
    """Run CLI in a fresh empty directory with sanitized env and group kill."""
    rem, err = _remaining_seconds(timeout, deadline)
    if err:
        return None, None, None, err
    env = build_child_env(stripped_env, extra_env=extra_env)
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


def _extract_result_event(parsed):
    """Return the final `type == 'result'` dict from a Claude CLI event array."""
    for item in reversed(parsed):
        if isinstance(item, dict) and item.get("type") == "result":
            return item
    return None


def _parse_json_object(stdout, *, allow_event_list=False):
    """Parse a JSON object (or Claude CLI event array when allowed) from stdout."""
    text = (stdout or "").strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
        if allow_event_list and isinstance(parsed, list):
            return _extract_result_event(parsed)
        return None
    except ValueError:
        pass
    for line in reversed(text.splitlines()):
        s = line.strip()
        if s.startswith("{"):
            try:
                parsed = json.loads(s)
                if isinstance(parsed, dict):
                    return parsed
            except ValueError:
                continue
        elif allow_event_list and s.startswith("["):
            try:
                parsed = json.loads(s)
                if isinstance(parsed, list):
                    found = _extract_result_event(parsed)
                    if found is not None:
                        return found
            except ValueError:
                continue
    return None


def _verify_claude_subscription(claude_bin, extra_env, timeout, deadline=None):
    """Require an active claude.ai subscription login (refuse Console/API key)."""
    check_timeout = min(float(timeout), 15.0)
    rc, stdout, stderr, run_err = _run_cli(
        [claude_bin, "auth", "status"], "", STRIPPED_ENV, check_timeout,
        deadline=deadline, extra_env=extra_env)
    if run_err:
        return f"claude auth status failed: {run_err}"
    data = _parse_json_object(stdout)
    if data is None:
        detail = (stderr or stdout or f"exit {rc}").strip()[:200]
        return f"claude auth status returned non-JSON output (exit {rc}): {detail}"
    if (data.get("loggedIn") is not True
            or data.get("authMethod") != "claude.ai"
            or data.get("apiProvider") not in (None, "firstParty")):
        method = data.get("authMethod")
        provider = data.get("apiProvider")
        return (f"claude authMethod is {method!r} (loggedIn={data.get('loggedIn')!r}, "
                f"apiProvider={provider!r}), not an active 'claude.ai' subscription; "
                f"run 'claude auth login' (refusing Console/API-key billing)")
    return None


def _claude_served_model(data, requested):
    """Extract canonical served model from Claude CLI modelUsage (fail closed)."""
    model_usage = data.get("modelUsage")
    if not isinstance(model_usage, dict) or not model_usage:
        return None
    entry = model_usage.get(requested)
    if isinstance(entry, dict):
        canon = entry.get("canonicalModel")
        return canon.strip() if isinstance(canon, str) and canon.strip() else None
    if len(model_usage) == 1:
        _sole_key, sole_val = next(iter(model_usage.items()))
        if isinstance(sole_val, dict):
            canon = sole_val.get("canonicalModel")
            return canon.strip() if isinstance(canon, str) and canon.strip() else None
        return None
    for _key, val in model_usage.items():
        if isinstance(val, dict) and val.get("canonicalModel") == requested:
            return requested
    return None


def claude_messages(rev, messages, system, timeout, deadline=None,
                    *, no_content_error=None, default_max_output_tokens=64000):
    """Run one review or committee turn via Claude Code OAuth subscription."""
    why = claude_availability(timeout=min(float(timeout), 15.0), deadline=deadline)
    if why:
        return None, why
    claude_bin = _resolve_cli_bin("claude")
    extra_env = _abs_env_override("CLAUDE_CONFIG_DIR")
    sys_text = str(system or "")
    body = format_messages(messages)
    if len(sys_text.encode("utf-8")) > _MAX_ARGV_SYSTEM_BYTES:
        cli_system = "Follow the <system_instructions> block in the user input."
        body = (f"<system_instructions>\n{sys_text}\n</system_instructions>\n\n"
                f"{body}")
    else:
        cli_system = sys_text
    cmd = [
        claude_bin, "-p",
        "--model", rev["model"],
        "--tools", "",
        "--strict-mcp-config",
        "--setting-sources", "",
        "--no-session-persistence",
        "--system-prompt", cli_system,
        "--output-format", "json",
    ]
    if rev.get("effort"):
        cmd.extend(["--effort", rev["effort"]])
    budget = rev.get("max_output_tokens", default_max_output_tokens)
    if isinstance(budget, int) and not isinstance(budget, bool) and budget > 0:
        extra_env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(budget)
    rc, stdout, stderr, run_err = _run_cli(
        cmd, body, STRIPPED_ENV, timeout, deadline=deadline,
        extra_env=extra_env)
    if run_err:
        return None, run_err
    data = _parse_json_object(stdout, allow_event_list=True)
    if data is None:
        detail = (stderr or stdout or f"exit {rc}").strip()[:200]
        return None, (f"claude CLI returned non-JSON or missing-result output "
                      f"(exit {rc}): {detail}")
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    detail = (usage.get("output_tokens_details")
              if isinstance(usage.get("output_tokens_details"), dict) else {})
    thinking = detail.get("thinking_tokens")
    if thinking is None:
        thinking = usage.get("reasoning_tokens")
    out_tokens = usage.get("output_tokens")
    if data.get("api_error") == "max_output_tokens" and no_content_error is not None:
        return None, no_content_error(
            rev, "", finish_reason="max_output_tokens",
            reasoning_tokens=thinking, completion_tokens=out_tokens)
    if data.get("is_error") or rc != 0:
        msg = data.get("result") or stderr or f"exit {rc}"
        return None, f"claude CLI error: {str(msg).strip()[:300]}"
    text = data.get("result")
    if text is None:
        text = ""
    elif not isinstance(text, str):
        return None, f"claude CLI unexpected result type: {type(text).__name__}"
    stop = data.get("stop_reason")
    terminal = data.get("terminal_reason")
    if stop == "end_turn" or (terminal == "completed" and stop in (None, "stop_sequence")):
        status = "completed"
    else:
        status = stop or terminal
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
    why = codex_availability(timeout=min(float(timeout), 10.0), deadline=deadline)
    if why:
        return None, why
    codex_bin = _resolve_cli_bin("codex")
    home_dir = codex_home_dir()
    extra_env = _abs_env_override("CODEX_HOME")
    store_mode = _codex_credential_store_mode(home_dir)
    cmd = [
        codex_bin, "exec",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
    ]
    for feature in CODEX_DISABLED_FEATURES:
        cmd.extend(["--disable", feature])
    cmd.extend([
        "--sandbox", "read-only",
        "--skip-git-repo-check",
        "-C", "__HANIG_EMPTY_CWD__",
        "--json",
        "-m", rev["model"],
    ])
    if rev.get("effort"):
        cmd.extend(["-c", f"model_reasoning_effort={json.dumps(rev['effort'])}"])
    cmd.extend(["-c", 'forced_login_method="chatgpt"'])
    if store_mode in ("keyring", "auto"):
        cmd.extend(["-c", f"cli_auth_credentials_store={json.dumps(store_mode)}"])
    elif (home_dir / "auth.json").is_file():
        cmd.extend(["-c", 'cli_auth_credentials_store="file"'])
    cmd.append("-")
    body = (f"{system}\n\n"
            f"(Analysis only: do not run shell commands or tools; answer "
            f"directly from the provided text.)\n\n---\n\n"
            f"{format_messages(messages)}")
    rc, stdout, stderr, run_err = _run_cli(
        cmd, body, STRIPPED_ENV, timeout, deadline=deadline,
        extra_env=extra_env)
    if run_err:
        return None, run_err
    thread_id = None
    observed_model = None
    agent_messages = []
    usage = None
    completed = False
    turn_failed_error = None
    stream_error = None
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
        if isinstance(ev.get("model"), str) and ev["model"].strip():
            observed_model = ev["model"].strip()
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
            stream_error = None
            if isinstance(ev.get("usage"), dict):
                usage = ev["usage"]
        elif etype == "turn.failed":
            err_obj = ev.get("error")
            if isinstance(err_obj, dict) and err_obj.get("message"):
                turn_failed_error = str(err_obj["message"])
            else:
                turn_failed_error = str(err_obj or "turn.failed")
        elif etype == "error" and ev.get("message"):
            stream_error = str(ev["message"])
    fatal = turn_failed_error or (stream_error if not completed else None)
    if fatal or rc != 0 or not parsed_any:
        detail = fatal or (stderr or stdout or f"exit {rc}").strip()[:300]
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
        "served_model": observed_model,
        "status": status,
        "response_id": thread_id,
    }, None
