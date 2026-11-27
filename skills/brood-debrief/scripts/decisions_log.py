"""Append-only, hash-chained debrief decision trail (decisions.tsv) outside Git worktrees."""
import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import unicodedata

SCHEMA = "hanig_debrief_decisions.v1"
COLUMNS = ("schema", "seq", "timestamp_utc", "run_id", "decision_kind", "claim",
           "evidence_ref", "supersedes", "prev_sha256", "row_sha256")
HEADER = ("\t".join(COLUMNS) + "\n").encode("utf-8")
GENESIS = hashlib.sha256(HEADER).hexdigest()
UNSAFE = frozenset(("Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"))
INTEGRITY = ("SHA-256 chain: detects accidental or partial-row edits and deleted or "
             "reordered rows inside the chain; NOT cryptographic tamper-proofing "
             "against a same-UID writer. Deleted trailing rows need --expect-head.")
STAMP = r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z"


class LogError(ValueError):
    pass


def sanitize(value):
    """Replace tabs, newlines and every Cc/Cf/Cs/Co/Cn/Zl/Zp code point with a space."""
    return "".join(" " if unicodedata.category(c) in UNSAFE else c for c in str(value))


def checked_path(raw):
    """Refuse a log path whose lexical or resolved ancestry holds a .git entry."""
    lexical, resolved = Path(raw).absolute(), Path(raw).resolve()
    for parent in (lexical, resolved) + tuple(lexical.parents) + tuple(resolved.parents):
        if os.path.lexists(str(parent / ".git")):
            raise LogError("log path is inside a Git worktree: %s" % parent)
    return lexical


def _digest(cells):
    return hashlib.sha256("\t".join(cells).encode("utf-8")).hexdigest()


def parse(data):
    """Return verified rows as dicts, or raise LogError naming the first defect."""
    if data == b"":
        return []
    if not data.startswith(HEADER):
        raise LogError("line 1: not a %s header" % SCHEMA)
    if not data.endswith(b"\n"):
        raise LogError("partial final row (no trailing newline)")
    try:
        lines = data[len(HEADER):].decode("utf-8").split("\n")[:-1]
    except UnicodeDecodeError as exc:
        raise LogError("rows are not valid UTF-8") from exc
    rows, prev = [], GENESIS
    for index, line in enumerate(lines, 1):
        cells = line.split("\t")
        row = dict(zip(COLUMNS, cells))
        sup = row.get("supersedes", "")
        problem = (
            len(cells) != len(COLUMNS) and "wrong cell count" or
            row["schema"] != SCHEMA and "unknown schema" or
            row["seq"] != str(index) and "sequence gap or reorder" or
            not re.fullmatch(STAMP, row["timestamp_utc"]) and "invalid timestamp" or
            # Cn is writer-version dependent: a newer Unicode may assign it.
            any(unicodedata.category(c) in UNSAFE - {"Cn"} for c in line.replace("\t", ""))
            and "unsanitized character" or
            not all(row[k].strip() for k in ("run_id", "decision_kind", "claim"))
            and "empty required cell" or
            sup and not (re.fullmatch(r"[1-9][0-9]*", sup) and int(sup) < index)
            and "supersedes must name an earlier row" or
            row["prev_sha256"] != prev and "prev_sha256 breaks the chain" or
            row["row_sha256"] != _digest(cells[:-1]) and "row_sha256 mismatch")
        if problem:
            raise LogError("row %d: %s" % (index, problem))
        prev = row["row_sha256"]
        rows.append(row)
    return rows


def _open(path, flags):
    return os.open(str(path), flags | os.O_NOFOLLOW, 0o600)


def _read(fd, lock):
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise LogError("log must be a regular file owned by this user")
    fcntl.flock(fd, lock)
    os.lseek(fd, 0, os.SEEK_SET)
    chunks = [b""]
    while chunks[-1] or len(chunks) == 1:
        chunks.append(os.read(fd, 1 << 16))
    return parse(b"".join(chunks))


def load(raw):
    fd = _open(checked_path(raw), os.O_RDONLY)
    try:
        return _read(fd, fcntl.LOCK_SH)
    finally:
        os.close(fd)


def append(raw, run_id, decision_kind, claim, evidence_ref="", supersedes=None, now=None):
    """Append one sanitized row with O_APPEND; never rewrite earlier bytes."""
    path = checked_path(raw)
    cells = [sanitize(v) for v in (run_id, decision_kind, claim, evidence_ref)]
    if not all(v.strip() for v in cells[:3]):
        raise LogError("run_id, kind and claim must be nonempty after sanitizing")
    for parent in reversed(path.parents):
        if not os.path.lexists(str(parent)):
            try:
                os.mkdir(str(parent), 0o700)
                os.chmod(str(parent), 0o700)
            except FileExistsError:  # a concurrent append created it
                pass
    flags = os.O_RDWR | os.O_APPEND
    try:
        fd = _open(path, flags | os.O_CREAT | os.O_EXCL)
        os.fchmod(fd, 0o600)
    except FileExistsError:
        fd = _open(path, flags)
    try:
        rows = _read(fd, fcntl.LOCK_EX)
        seq = len(rows) + 1
        if supersedes is not None and not (type(supersedes) is int and 0 < supersedes < seq):
            raise LogError("--supersedes must name an existing earlier row")
        stamp = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
        body = [SCHEMA, str(seq), stamp] + cells + [
            "" if supersedes is None else str(supersedes),
            rows[-1]["row_sha256"] if rows else GENESIS]
        body.append(_digest(body))
        data = (b"" if os.fstat(fd).st_size else HEADER) + ("\t".join(body) + "\n").encode()
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
        return dict(zip(COLUMNS, body))
    finally:
        os.close(fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__ + " " + INTEGRITY)
    sub = parser.add_subparsers(dest="command", required=True)
    add = sub.add_parser("append", description=INTEGRITY)
    add.add_argument("--run-id", required=True)
    add.add_argument("--kind", required=True)
    add.add_argument("--claim", required=True)
    add.add_argument("--evidence-ref", default="")
    add.add_argument("--supersedes", type=int)
    check = sub.add_parser("verify", description=INTEGRITY)
    check.add_argument("--expect-head", help="row_sha256 recorded earlier; must be present")
    show = sub.add_parser("show", description=INTEGRITY)
    for command in (add, check, show):
        command.add_argument("--log", required=True, help="path outside every Git worktree")
        command.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "append":
            row = append(args.log, args.run_id, args.kind, args.claim, args.evidence_ref,
                         args.supersedes)
            result = {"appended": row, "integrity": INTEGRITY}
            text = "appended seq %s row_sha256 %s" % (row["seq"], row["row_sha256"])
        else:
            rows = load(args.log)
            head = rows[-1]["row_sha256"] if rows else GENESIS
            if getattr(args, "expect_head", None) not in (None, GENESIS) and \
                    args.expect_head not in {r["row_sha256"] for r in rows}:
                raise LogError("expected head not found (rows deleted or wrong log)")
            result = {"ok": True, "rows": len(rows), "head_sha256": head,
                      "integrity": INTEGRITY}
            text = "chain ok: %d rows, head %s" % (len(rows), head)
            if args.command == "show":
                result["entries"] = rows
                text = "\n".join("\t".join(r[k] for k in COLUMNS[1:8]) for r in rows)
    except (LogError, OSError) as exc:
        print("decisions log: %s" % exc, file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, ensure_ascii=True) if args.json else text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
