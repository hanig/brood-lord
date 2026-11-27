"""Read-only audit of decisions.tsv claims (command:, file:, commit:, test:) against
Claude/Codex transcripts whose header cwd is realpath(--workspace). Prints ids only."""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

import decisions_log as D

SCHEMA = "hanig_debrief_audit.v1"
STATUSES = ("supported", "contradicted", "unverifiable")
PREFIX = {"command": "command_ran", "file": "file_edited", "commit": "commit_exists",
          "test": "test_ran"}
KINDS = tuple(PREFIX.values()) + ("prose",)
TOKEN = re.compile(r";\s*(?=(?:command|file|commit|test):)")
HEADER_BYTES, CLAUDE_LINES, CHUNK = 1 << 16, 32, 4096
MIN_NEEDLE, MAX_MATCHES = 4, 20
EXEC_TOOLS = {"Bash", "exec_command", "shell", "local_shell", "exec"}
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
EXIT_RE = re.compile(r"^Process exited with code (-?[0-9]{1,4})$", re.M)
PATCH_RE = re.compile(r"^\*\*\* (?:(?:Add|Update|Delete) File|Move to): (.+)$", re.M)


def _json(raw):
    try:
        return raw if isinstance(raw, dict) else json.loads(raw)
    except (TypeError, ValueError, RecursionError):
        return None


def _open_regular(path):
    try:
        if not stat.S_ISREG(os.lstat(str(path)).st_mode):
            return None
        fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    if stat.S_ISREG(os.fstat(fd).st_mode):
        return fd
    os.close(fd)


def header_lines(path, max_lines):
    fd, buf, total = _open_regular(path), b"", 0
    if fd is None:
        return
    try:
        while max_lines and total < HEADER_BYTES:
            chunk = os.read(fd, min(CHUNK, HEADER_BYTES - total))
            total += len(chunk)
            buf += chunk or b"\n"
            while b"\n" in buf and max_lines:
                line, buf = buf.split(b"\n", 1)
                max_lines -= 1
                yield line
            if not chunk:
                return
    finally:
        os.close(fd)


def _cwd_is(value, ws, cache):
    if not (isinstance(value, str) and os.path.isabs(value)):
        return False
    if value not in cache:
        cache[value] = os.path.realpath(value) == ws
    return cache[value]


def claude_verified(path, ws, cache):
    with closing(header_lines(path, CLAUDE_LINES)) as lines:
        for raw in lines:
            obj = _json(raw)
            if isinstance(obj, dict) and "cwd" in obj:
                return _cwd_is(obj["cwd"], ws, cache)
    return False


def codex_verified(path, ws, cache):
    with closing(header_lines(path, 1)) as lines:
        obj = _json(next(lines, None))
    payload = obj.get("payload") if isinstance(obj, dict) else None
    return isinstance(payload, dict) and _cwd_is(payload.get("cwd"), ws, cache)


def slug(path):
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def session_id(kind, path):
    stem = Path(path).stem
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", stem):
        stem = "sha256-" + hashlib.sha256(stem.encode("utf-8", "replace")).hexdigest()[:16]
    return kind + ":" + stem


def discover(home, ws, spelled):
    cache, found, candidates = {}, [], []
    for name in sorted({slug(ws), slug(spelled)}):
        folder = Path(home, ".claude", "projects", name)
        candidates += [("claude", p) for p in sorted(folder.glob("*.jsonl"))]
    for top, dirs, files in os.walk(str(Path(home, ".codex", "sessions"))):
        dirs.sort()
        candidates += [("codex", Path(top, n)) for n in sorted(files)
                       if n.startswith("rollout-") and n.endswith(".jsonl")]
    for kind, path in candidates:
        if (claude_verified if kind == "claude" else codex_verified)(path, ws, cache):
            found.append((kind, path))
    return found, len(candidates) - len(found)


def _claude_events(obj, ws, cache):
    if "cwd" in obj and not _cwd_is(obj["cwd"], ws, cache):
        return
    msg = obj.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    for item in content if isinstance(content, list) else ():
        item = item if isinstance(item, dict) else {}
        kind, name = item.get("type"), item.get("name")
        args = item.get("input") if isinstance(item.get("input"), dict) else {}
        if kind == "tool_use" and name in EXEC_TOOLS:
            yield "exec", item.get("id"), args.get("command")
        elif kind == "tool_use" and name in EDIT_TOOLS:
            yield "edit", item.get("id"), args.get("file_path") or args.get("notebook_path")
        elif kind == "tool_result" and isinstance(item.get("is_error"), bool):
            yield "result", item.get("tool_use_id"), not item["is_error"]


def _exit_code(out):
    if isinstance(out, str):
        head, sep, _ = out.partition("\nOutput:")
        match = sep and EXIT_RE.search(head)
        if match:
            return int(match.group(1))
        obj = _json(out) if out.startswith("{") else None
        out = obj.get("metadata") if isinstance(obj, dict) else None
    code = out.get("exit_code") if isinstance(out, dict) else None
    return code if type(code) is int else None


def _codex_events(obj):
    p = obj.get("payload")
    p = p if isinstance(p, dict) else {}
    kind, name, cid, patch = p.get("type"), p.get("name"), p.get("call_id"), None
    if kind in ("function_call", "custom_tool_call"):
        args = _json(p.get("arguments")) if kind == "function_call" else {"cmd": p.get("input")}
        args = args if isinstance(args, dict) else {}
        if name in EXEC_TOOLS:
            cmd = args.get("cmd", args.get("command"))
            if isinstance(cmd, list) and all(isinstance(c, str) for c in cmd):
                cmd = " ".join(cmd)
            yield "exec", cid, cmd
        elif name == "apply_patch":
            patch = args.get("input", args.get("cmd"))
    elif kind in ("function_call_output", "custom_tool_call_output", "exec_command_end"):
        code = _exit_code(p if kind == "exec_command_end" else p.get("output"))
        yield "result", cid, None if code is None else code == 0
    for path in PATCH_RE.findall(patch) if isinstance(patch, str) else ():
        yield "edit", cid, path.strip()


def _norm(path, ws):
    return os.path.realpath(os.path.join(ws, path))


def scan(kind, path, ws, cache):
    calls, execs, edits, bad, fd = {}, [], [], set(), _open_regular(path)
    with os.fdopen(fd, "rb") if fd is not None else open(os.devnull, "rb") as handle:
        for line, raw in enumerate(handle, 1):
            obj = _json(raw)
            if not isinstance(obj, dict):
                continue
            events = _claude_events(obj, ws, cache) if kind == "claude" else _codex_events(obj)
            for event, cid, value in events:
                if event == "exec" and isinstance(value, str):
                    execs.append({"cmd": value, "line": line, "ok": []})
                    if isinstance(cid, str):
                        calls[cid] = execs[-1]
                elif event == "edit" and isinstance(value, str) and value:
                    edits.append((_norm(value, ws), line, cid))
                elif event == "result" and value is False and isinstance(cid, str):
                    bad.add(cid)
                if event == "result" and value is not None and cid in calls:
                    calls[cid]["ok"].append(value)
    return execs, [(p, n) for p, n, c in edits if c not in bad]


def cat_file_e(ws, obj):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_NO_LAZY_FETCH"] = "1"
    try:
        return subprocess.run(["git", "-C", ws, "cat-file", "-e", obj], env=env,
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=30).returncode
    except (OSError, subprocess.TimeoutExpired):
        return None


def claims(row):
    out = []
    for token in TOKEN.split(row["evidence_ref"]):
        prefix, sep, value = token.strip().partition(":")
        if sep and prefix in PREFIX and value.strip():
            out.append((PREFIX[prefix], value.strip()))
        elif token.strip():
            out.append(("prose", ""))
    return out or [("prose", "")]


def verify(kind, value, sessions, ws):
    if kind == "file_edited":
        hits = [(s, n) for s, _, edits in sessions for p, n in edits if p == _norm(value, ws)]
        return ("supported" if hits else "unverifiable"), hits
    if kind in ("command_ran", "test_ran") and len(value) >= MIN_NEEDLE:
        hits = [(s, r) for s, execs, _ in sessions for r in execs if value in r["cmd"]]
        lines = [(s, r["line"]) for s, r in hits if kind == "command_ran" or True in r["ok"]]
        if lines:
            return "supported", lines
        if kind == "test_ran" and hits and all(r["ok"] for _, r in hits):
            return "contradicted", [(s, r["line"]) for s, r in hits]
    if kind == "commit_exists" and re.fullmatch(r"[0-9a-f]{7,64}", value):
        if cat_file_e(ws, value + "^{commit}") == 0:
            return "supported", []
        if len(value) in (40, 64) and cat_file_e(ws, value) in (0, 1):  # absent, not a commit
            return "contradicted", []
    return "unverifiable", []


def audit(rows, ws, home, spelled):
    found, rejected = discover(home, ws, spelled)
    cache = {}
    sessions = [(session_id(k, p),) + scan(k, p, ws, cache) for k, p in found]
    out = [{"seq": row["seq"], "claims": [
        {"kind": kind, "status": status,
         "matches": [{"session": s, "line": n} for s, n in hits[:MAX_MATCHES]]}
        for kind, value in claims(row)
        for status, hits in [verify(kind, value, sessions, ws)]]} for row in rows]
    return {"schema": SCHEMA, "workspace": ws, "sessions": [s[0] for s in sessions],
            "rejected_candidates": rejected, "rows": out,
            "decisions_head_sha256": rows[-1]["row_sha256"] if rows else D.GENESIS}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--decisions", required=True)
    parser.add_argument("--workspace", required=True)
    args = parser.parse_args(argv)
    ws = os.path.realpath(args.workspace)
    try:
        if not os.path.isdir(ws):
            raise D.LogError("workspace is not a directory")
        report = audit(D.load(args.decisions), ws, Path.home(), os.path.abspath(args.workspace))
    except (D.LogError, OSError) as exc:
        print("debrief audit: %s" % exc, file=sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
