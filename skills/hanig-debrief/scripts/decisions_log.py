"""Append-only debrief decision trail (decisions.tsv), kept outside Git worktrees.

Each row carries prev_sha256 and row_sha256, chaining it to the row before it.
The chain detects accidental or partial-row edits, deleted or reordered rows
inside the chain, and torn appends. It is NOT cryptographic tamper-proofing:
a writer with the same UID can rewrite the whole chain consistently. Deleting
whole trailing rows is detected only against a head recorded elsewhere
(verify --expect-head).
"""
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
UNSAFE_CATEGORIES = frozenset(("Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"))
INTEGRITY = ("SHA-256 chain: detects accidental or partial-row edits and deleted or "
             "reordered rows inside the chain; NOT cryptographic tamper-proofing "
             "against a same-UID writer, who can rewrite the whole chain. Deleted "
             "trailing rows are detected only with --expect-head.")
SEQ = re.compile(r"[1-9][0-9]*")
TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
DIGEST = re.compile(r"[0-9a-f]{64}")


class LogError(ValueError):
    pass


def sanitize(value):
    """Replace delimiters and every control/format/unassigned code point with a space."""
    return "".join(" " if unicodedata.category(c) in UNSAFE_CATEGORIES else c
                   for c in str(value))


def _unsafe(value):
    return any(unicodedata.category(c) in UNSAFE_CATEGORIES for c in value)


def _chain(path):
    return (path,) + tuple(path.parents)


def checked_path(raw):
    """Refuse any log path whose lexical or resolved ancestry holds a .git entry."""
    if raw is None or str(raw) == "":
        raise LogError("an explicit --log PATH outside any Git worktree is required")
    lexical = Path(raw).absolute()
    resolved = Path(raw).resolve()
    for parent in _chain(lexical) + _chain(resolved):
        if os.path.lexists(str(parent / ".git")):
            raise LogError("log path is inside a Git worktree: %s" % parent)
    return lexical


def _row_digest(cells):
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
        at = "row %d" % index
        cells = line.split("\t")
        if len(cells) != len(COLUMNS):
            raise LogError("%s: expected %d cells" % (at, len(COLUMNS)))
        row = dict(zip(COLUMNS, cells))
        if row["schema"] != SCHEMA:
            raise LogError(at + ": unknown schema")
        if SEQ.fullmatch(row["seq"]) is None or int(row["seq"]) != index:
            raise LogError(at + ": sequence gap or reorder")
        if TIMESTAMP.fullmatch(row["timestamp_utc"]) is None:
            raise LogError(at + ": invalid timestamp")
        if any(_unsafe(cell) for cell in cells):
            raise LogError(at + ": unsanitized character")
        for key in ("run_id", "decision_kind", "claim"):
            if not row[key].strip():
                raise LogError(at + ": empty " + key)
        sup = row["supersedes"]
        if sup and (SEQ.fullmatch(sup) is None or int(sup) >= index):
            raise LogError(at + ": supersedes must name an earlier row")
        if row["prev_sha256"] != prev:
            raise LogError(at + ": prev_sha256 breaks the chain (edited or deleted row)")
        if DIGEST.fullmatch(row["row_sha256"]) is None or \
                row["row_sha256"] != _row_digest(cells[:-1]):
            raise LogError(at + ": row_sha256 mismatch (edited row)")
        prev = row["row_sha256"]
        rows.append(row)
    return rows


def _open_existing(path):
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise LogError("cannot open log: %s" % exc.strerror) from exc
    return fd


def _read_all(fd):
    os.lseek(fd, 0, os.SEEK_SET)
    chunks = []
    while True:
        chunk = os.read(fd, 1 << 16)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _regular(fd):
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise LogError("log must be a regular file owned by this user")


def load(raw):
    path = checked_path(raw)
    fd = _open_existing(path)
    try:
        _regular(fd)
        fcntl.flock(fd, fcntl.LOCK_SH)
        return parse(_read_all(fd))
    finally:
        os.close(fd)


def _make_parents(path):
    for parent in reversed(tuple(path.parents)):
        if not os.path.lexists(str(parent)):
            os.mkdir(str(parent), 0o700)
            os.chmod(str(parent), 0o700)
        elif not parent.is_dir():
            raise LogError("log parent is not a directory: %s" % parent)


def append(raw, run_id, decision_kind, claim, evidence_ref="", supersedes=None, now=None):
    """Append one sanitized row with O_APPEND; never rewrite earlier bytes."""
    path = checked_path(raw)
    cells = [sanitize(v) for v in (run_id, decision_kind, claim, evidence_ref)]
    for name, value in zip(("run_id", "decision_kind", "claim"), cells):
        if not value.strip():
            raise LogError(name + " must be nonempty after sanitizing")
    _make_parents(path)
    flags = os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW
    try:
        fd = os.open(str(path), flags | os.O_CREAT | os.O_EXCL, 0o600)
        os.fchmod(fd, 0o600)
    except FileExistsError:
        fd = _open_existing_rw(path, flags)
    except OSError as exc:
        raise LogError("cannot create log: %s" % exc.strerror) from exc
    try:
        _regular(fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        rows = parse(_read_all(fd))
        seq = len(rows) + 1
        if supersedes is not None and not (type(supersedes) is int and 1 <= supersedes < seq):
            raise LogError("--supersedes must name an existing earlier row")
        stamp = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
        prev = rows[-1]["row_sha256"] if rows else GENESIS
        body = [SCHEMA, str(seq), stamp] + cells + [
            "" if supersedes is None else str(supersedes), prev]
        digest = _row_digest(body)
        data = ("\t".join(body + [digest]) + "\n").encode("utf-8")
        if not rows:
            data = HEADER + data
        _write_all(fd, data)
        os.fsync(fd)
        return dict(zip(COLUMNS, body + [digest]))
    finally:
        os.close(fd)


def _open_existing_rw(path, flags):
    try:
        return os.open(str(path), flags)
    except OSError as exc:
        raise LogError("cannot open log: %s" % exc.strerror) from exc


def _write_all(fd, data):
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


def _superseded(rows):
    out = {}
    for row in rows:
        if row["supersedes"]:
            out.setdefault(row["supersedes"], []).append(int(row["seq"]))
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    add = sub.add_parser("append", help="append one decision row")
    add.add_argument("--run-id", required=True)
    add.add_argument("--kind", required=True, dest="decision_kind")
    add.add_argument("--claim", required=True)
    add.add_argument("--evidence-ref", default="")
    add.add_argument("--supersedes", type=int)
    check = sub.add_parser("verify", help="check the hash chain", description=INTEGRITY)
    check.add_argument("--expect-head", help="row_sha256 recorded earlier; must be present")
    show = sub.add_parser("show", help="print verified rows", description=INTEGRITY)
    for command in (add, check, show):
        command.add_argument("--log", required=True,
                             help="decisions.tsv path outside every Git worktree")
        command.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "append":
            row = append(args.log, args.run_id, args.decision_kind, args.claim,
                         args.evidence_ref, args.supersedes)
            result = {"appended": row, "integrity": INTEGRITY}
            text = "appended seq %s row_sha256 %s" % (row["seq"], row["row_sha256"])
        else:
            rows = load(args.log)
            if args.command == "verify" and args.expect_head is not None and \
                    args.expect_head not in {r["row_sha256"] for r in rows}:
                raise LogError("expected head not found (rows deleted or wrong log)")
            head = rows[-1] if rows else None
            result = {"ok": True, "rows": len(rows), "integrity": INTEGRITY,
                      "head_seq": int(head["seq"]) if head else 0,
                      "head_sha256": head["row_sha256"] if head else GENESIS}
            text = "chain ok: %d rows, head %s" % (len(rows), result["head_sha256"])
            if args.command == "show":
                later = _superseded(rows)
                result["entries"] = [dict(r, superseded_by=later.get(r["seq"], []))
                                     for r in rows]
                text = "\n".join(
                    "%s %s %s %s: %s%s%s" % (r["seq"], r["timestamp_utc"], r["run_id"],
                        r["decision_kind"], r["claim"],
                        " [evidence %s]" % r["evidence_ref"] if r["evidence_ref"] else "",
                        " (supersedes %s)" % r["supersedes"] if r["supersedes"] else "")
                    for r in rows) or "no rows"
    except (LogError, OSError) as exc:
        print("decisions log: %s" % exc, file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, ensure_ascii=True) if args.json else text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
