"""Fail-closed zero-tool Lor subagent transport (SKILL.md, "Lor sessions").

Limits: no client-side max_output_tokens; `--tools pass` is the smallest tool
list Lor accepts; a start that times out returns no id to stop.
Every returned id gets `lor session stop`. A failed stop fails closed unless
the subagent already finished cleanly (STOP or length): a `--tools pass`
subagent has no tool that can spawn background work, so after a clean finish
the stop is best-effort and its failure never discards the verdict.
Python 3.8+, stdlib only. Must never import review.py.
"""

import importlib.util
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import subprocess
import tempfile
import time

_spec = importlib.util.spec_from_file_location(
    "_hanig_review_gate_lor_subs", Path(__file__).resolve().parent / "review_subscriptions.py")
_subs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_subs)

OPT_OUT = "HANIG_REVIEW_NO_LOR"
LOR_SECRET_ENV = ("LOR_SESSION_TOKEN", "LOR_ROUTER_TOKEN", "LOR_SESSION_REFRESH_PROOF")
LOR_ROUTABLE_PROVIDERS = ("openai", "openrouter")
LOR_EFFORTS = ("minimal", "low", "medium", "high", "xhigh", "max")
STOP, LENGTH = "FINISH_REASON_STOP", ("FINISH_REASON_LENGTH", "FINISH_REASON_MAX_TOKENS")
NO_TOOLS = "(Analysis only. Do NOT call any tool, including `pass`: answer in plain text.)"
_SID = re.compile(r'"session_id"\s*:\s*"([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})"')


def lor_in_session(environ=None):
    env = os.environ if environ is None else environ
    return (bool((env.get("LOR_SESSION_ID") or "").strip())
            and (env.get(OPT_OUT) or "").strip().lower() not in ("1", "true", "yes", "on"))


def _lor_bin(env):
    path = _subs._sanitized_path(env.get("PATH", ""))
    found = path and shutil.which("lor", path=path)
    return os.path.abspath(found) if found else None


def lor_availability(environ=None):
    env = os.environ if environ is None else environ
    if not (env.get("LOR_SESSION_ID") or "").strip():
        return "LOR_SESSION_ID not set: the lor provider runs only inside a Lor session"
    return None if _lor_bin(env) else "lor CLI not found on PATH"


def build_lor_child_env(environ=None):
    """review_subscriptions' strip rules, except every LOR_* the CLI needs."""
    explicit = set(_subs.STRIPPED_ENV)
    env = {k: v for k, v in (os.environ if environ is None else environ).items()
           if k.startswith("LOR_") or not _subs._should_strip_env_key(k, explicit)}
    if "PATH" in env:
        env["PATH"] = _subs._sanitized_path(env["PATH"])
    return env


def lor_model_id(rev):
    if rev.get("provider") == "lor":
        return rev.get("model")
    value = rev.get("lor_model")
    return value if isinstance(value, str) and value.strip() else None


def _get(obj, *keys):
    for key in keys:
        obj = obj.get(key) if isinstance(obj, dict) else None
    return obj


def parse_lor_events(events, session_id=None):
    """Fold watch envelopes or inspect records up to the first finish. Text is
    the LAST assistant message: earlier prose never rescues an empty end."""
    out = dict.fromkeys(("in_tokens", "out_tokens", "reasoning_tokens", "served_model",
                         "finish_reason", "response_id", "error"), None)
    out.update(text="", finished=False)
    for item in events:
        ev = _get(item, "recorded", "event") or _get(item, "event")
        if not isinstance(ev, dict):
            continue
        if _get(ev, "message", "role") == "MESSAGE_ROLE_ASSISTANT":
            parts = _get(ev, "message", "content")
            out["text"] = "".join(p["text"] for p in parts if isinstance(_get(p, "text"), str)) \
                if isinstance(parts, list) else ""
            try:
                served = json.loads(ev.get("provider_metadata_json") or "{}").get("providerModelId")
            except (ValueError, AttributeError):
                served = None
            out["served_model"] = served if isinstance(served, str) and served else None
            out["response_id"] = ev.get("model_response_id") or out["response_id"]
        for key, src in (("in_tokens", "input_tokens"), ("out_tokens", "output_tokens"),
                         ("reasoning_tokens", "reasoning_tokens")):
            value = _get(ev, "metrics", "usage", src)
            if isinstance(value, (str, int)) and not isinstance(value, bool) and str(value).isdigit():
                out[key] = (out[key] or 0) + int(value)
        if ev.get("error"):
            out["error"] = str(_get(ev, "error", "message") or ev["error"])
        if isinstance(ev.get("finish"), dict):
            out["finished"], out["finish_reason"] = True, ev["finish"].get("reason")
            break
    reason = out["finish_reason"]
    out["status"] = ("failed" if out["error"] else "completed" if reason == STOP
                     else str(reason).lower() if out["finished"] and reason not in LENGTH
                     else "incomplete")
    out["response_id"] = out["response_id"] or session_id
    return out


def _reap(proc):
    """Kill the group while its unreaped leader still reserves the pgid."""
    if proc is not None:
        if proc.returncode is None:
            _subs._kill_process_group(proc)
        try:
            proc.wait(timeout=5)
        except Exception:
            pass


def _run(cmd, stdin_text, env, cwd, seconds):
    """(returncode, stdout, stderr, error); kills the group on any exit and
    keeps output printed before a timeout (a session id still to stop)."""
    proc, out, err, error = None, b"", b"", None
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, cwd=cwd, env=env, start_new_session=True)
        out, err = proc.communicate(stdin_text.encode("utf-8"), timeout=seconds)
    except subprocess.TimeoutExpired:
        error = f"lor {cmd[2]} timed out after {seconds:.1f}s"
        _reap(proc)
        try:
            out, err = proc.communicate(timeout=5)
        except Exception:
            pass
    except OSError as exc:
        return None, "", "", f"lor {cmd[2]} failed: {exc}"
    finally:
        _reap(proc)
    rc = None if error else proc.returncode
    return rc, (out or b"").decode("utf-8", "replace"), (err or b"").decode("utf-8", "replace"), error


def _watch(lor, sid, env, cwd, end):
    """Committed events until the first finish ('' error), EOF, or deadline."""
    events, buf, proc, sel = [], b"", None, selectors.DefaultSelector()
    try:
        proc = subprocess.Popen([lor, "session", "watch", "--session-id", sid, "--committed-only"],
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, cwd=cwd, env=env, start_new_session=True)
        sel.register(proc.stdout, selectors.EVENT_READ)  # no FD_SETSIZE cap, unlike select()
        while time.time() < end:
            if not sel.select(max(0, min(end - time.time(), 1))):
                continue
            chunk = os.read(proc.stdout.fileno(), 65536)
            *lines, buf = (buf + chunk).split(b"\n") if chunk else (buf, b"")  # EOF: keep the tail
            if len(buf) > 1 << 26:
                return events, "watch line exceeded 64MB"
            for line in lines:
                try:
                    events.append(json.loads(line))
                except ValueError:
                    continue
                if parse_lor_events(events[-1:])["finished"]:
                    return events, ""
            if not chunk:
                return events, "watch ended before the subagent finished"
        return events, "timeout"
    except OSError as exc:
        return events, f"lor session watch failed: {exc}"
    finally:
        sel.close()
        _reap(proc)
        if proc is not None:
            proc.stdout.close()


def lor_messages(rev, messages, system, timeout, deadline=None, *,
                 no_content_error=None, format_messages=None):
    """One review or committee turn as a zero-tool Lor subagent; never an API."""
    if not lor_in_session():
        return None, f"lor provider disabled: needs LOR_SESSION_ID set and {OPT_OUT} unset"
    why = lor_availability() or (None if lor_model_id(rev) else "seat has no lor model")
    seconds = min(float(timeout), float(deadline) - time.time()) if deadline else float(timeout)
    if why or seconds <= 0:
        return None, why or "review deadline exceeded before lor launch"
    end, env = time.time() + seconds, build_lor_child_env()
    lor = _lor_bin(env)
    body = f"{system or ''}\n\n{NO_TOOLS}\n\n---\n\n{(format_messages or _subs.format_messages)(messages)}"
    cmd = [lor, "session", "start", "--canonical-model-id", lor_model_id(rev)]
    cmd += ["--effort", str(rev["effort"])] if rev.get("effort") else []
    with tempfile.TemporaryDirectory(prefix="hanig-review-lor-") as tmp:
        cmd += ["--tools", "pass", "--working-directory", tmp,
                "--title", f"hanig-review {rev.get('name')}"[:80], "--task", "@-"]
        rc, out, err, run_err = _run(cmd, body, env, tmp, seconds)
        sid = (_SID.findall(out) or [None])[0]
        # Untruncated: review.py redacts first, so no secret prefix survives a cut.
        start_err = (run_err or rc != 0) and f"lor session start failed: {run_err or (err or out).strip()}"
        if not sid:
            return None, start_err or f"lor session start returned no session id: {out.strip()}"
        events, watch_err = [], start_err
        try:
            if not start_err:
                events, watch_err = _watch(lor, sid, env, tmp, end)
        finally:  # any returned id is stopped, whatever happened after start
            rc, _o, err, stop_err = _run([lor, "session", "stop", "--session-id", sid],
                                         "", env, tmp, 15.0)
    stopped = "" if rc == 0 else f"; lor session stop failed for {sid}: {stop_err or err.strip()}"
    got = parse_lor_events(events, sid)
    reason = got["finish_reason"]
    if watch_err == "timeout":
        return None, f"lor subagent {sid} timed out after {seconds:.1f}s{stopped}"
    clean = not (start_err or watch_err or got["error"]) and (reason == STOP or reason in LENGTH)
    if not clean:  # may still be running: fail closed and name any failed stop
        return None, f"lor subagent {sid} failed: {got['error'] or watch_err or reason}{stopped}"
    # Clean finish: a failed post-finish stop is best-effort (module docstring).
    empty = (no_content_error or (lambda *a, **k: None if got["text"].strip() else "no content"))(
        rev, got["text"], finish_reason="length" if reason in LENGTH else None,
        reasoning_tokens=got["reasoning_tokens"], completion_tokens=got["out_tokens"],
        status=got["status"])
    if empty:
        return None, empty
    return {k: got[k] for k in ("text", "in_tokens", "out_tokens", "reasoning_tokens",
                                "served_model", "status", "response_id")}, None
