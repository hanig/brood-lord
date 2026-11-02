"""Approval and safe retry within one trusted-writer, same-host work directory.

approve() freezes a public packet into private filing.json, whose approval and
transaction state share one atomic write. No dossier is read. file() returns
that record; only status FILED denotes verified delivery. Refusals raise
SchemaError; transport errors retain a stopped state without exception text.

The injected transport is bound by its operator to S.REPOSITORY. It implements
create(title, body, label), get(number) returning title/body/labels/state, and
find(packet_id) returning numbers across BOTH open and closed issues. Neither
FILED nor CONFLICT is automatically reopened. Operator reconciliation must
retain this directory; copying it cannot provide global exactly-once delivery.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import tempfile

from debrief_packet import approval_digest, parse_body, render, validate_packet
import debrief_schema as S


RECORD = "filing.json"
RECORD_SCHEMA = "debrief_filing.v1"
APPROVAL_SCHEMA = "debrief_approval.v1"


@contextmanager
def _locked(workdir, create=False):
    root = Path(workdir)
    if create:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Lock the directory inode, never an atomically replaced record inode.
    fd = os.open(str(root), os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield root
    finally:
        os.close(fd)


def _write(root, record):
    fd, name = tempfile.mkstemp(prefix=".filing-", dir=str(root))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(record, stream, sort_keys=True, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, str(root / RECORD))
        directory = os.open(str(root), os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _binding(packet):
    title, _ = render(packet)
    return {
        "schema_version": APPROVAL_SCHEMA,
        "packet_schema_version": packet["schema_version"],
        "catalog_version": packet["catalog_version"],
        "renderer_version": S.RENDERER_VERSION,
        "packet_id": packet["packet_id"],
        "repository": S.REPOSITORY, "label": S.LABEL, "title": title,
        "digest": approval_digest(packet, title),
    }


def _number(value):
    return type(value) is int and value > 0


def _read(root):
    record = S.load_json(root / RECORD)
    S.obj(record, ("schema_version", "packet", "approval", "status", "number"),
          "filing")
    S.need(record["schema_version"] == RECORD_SCHEMA, "filing", "unknown schema")
    packet = validate_packet(record["packet"])
    binding = _binding(packet)
    approval = record["approval"]
    S.obj(approval, tuple(binding) + ("approver", "approved_at"), "approval")
    S.text(approval["approver"], "approval.approver")
    S.match(approval["approved_at"], r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z",
            "approval.approved_at")
    for key, value in binding.items():
        S.need(type(approval[key]) is type(value) and approval[key] == value,
               "approval." + key, "stale approval")
    status, number = record["status"], record["number"]
    S.need(type(status) is str and status in S.FILING_TRANSITIONS,
           "filing.status", "unknown state")
    S.need(number is None or _number(number), "filing.number", "invalid issue number")
    if status in ("DRAFT", "APPROVED"):
        S.need(number is None, "filing.number", "unexpected issue number")
    if status in ("READBACK_FAILED", "FILED"):
        S.need(_number(number), "filing.number", "missing issue number")
    return record


def approve(workdir, packet, digest, approver):
    """Freeze exactly the displayed packet/digest; never reset a possible POST."""
    S.text(approver, "approval.approver")
    validate_packet(packet)
    # Snapshot containers before binding or persisting them.
    packet = json.loads(json.dumps(packet, ensure_ascii=False))
    binding = _binding(packet)
    S.need(type(digest) is str and digest == binding["digest"],
           "approval.digest", "digest does not match rendered packet")
    with _locked(workdir, create=True) as root:
        if (root / RECORD).exists():
            old = _read(root)
            S.need(old["status"] in ("DRAFT", "APPROVED"), "filing.status",
                   "cannot reapprove after possible delivery")
        binding.update(approver=approver, approved_at=datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"))
        record = {"schema_version": RECORD_SCHEMA, "packet": packet,
                  "approval": binding, "status": "APPROVED", "number": None}
        _write(root, record)
        return record


def _save(root, record, status, number=None):
    previous = record["status"]
    S.need(status == previous or status in S.FILING_TRANSITIONS[previous],
           "filing.status", "invalid transition")
    record["status"] = status
    if number is not None:
        record["number"] = number
    _write(root, record)
    return record


def _readback(root, record, transport, mismatch):
    try:
        issue = transport.get(record["number"])
    except Exception:
        return _save(root, record, "READBACK_FAILED")
    try:
        S.need(type(issue) is dict, "issue", "expected object")
        S.need(issue["title"] == record["approval"]["title"], "issue", "title mismatch")
        S.need(issue["labels"] == [record["approval"]["label"]], "issue", "label mismatch")
        S.need(issue["state"] in ("open", "closed"), "issue", "invalid state")
        S.need(parse_body(issue["body"]) == record["packet"], "issue", "packet mismatch")
    except (S.SchemaError, KeyError, TypeError, RecursionError):
        return _save(root, record, mismatch)
    return _save(root, record, "FILED")


def _reconcile(root, record, transport):
    try:
        matches = transport.find(record["packet"]["packet_id"])
    except Exception:
        return _save(root, record, "DELIVERY_UNCERTAIN")
    if type(matches) is not list or any(not _number(n) for n in matches):
        return _save(root, record, "CONFLICT")
    if not matches:
        return _save(root, record, "DELIVERY_UNCERTAIN")
    if len(matches) != 1 or (record["number"] is not None and
                            record["number"] != matches[0]):
        return _save(root, record, "CONFLICT")
    _save(root, record, record["status"], matches[0])
    return _readback(root, record, transport, "CONFLICT")


def file(workdir, transport):
    """Run one locked transition; uncertainty only permits read-only recovery."""
    with _locked(workdir) as root:
        record = _read(root)  # All approval checks precede ANY transport call.
        status = record["status"]
        S.need(status != "DRAFT", "filing.status", "approval required")
        if status in ("FILED", "CONFLICT"):
            return record
        if status in ("POST_IN_FLIGHT", "DELIVERY_UNCERTAIN"):
            return _reconcile(root, record, transport)
        if status == "READBACK_FAILED":
            return _readback(root, record, transport, "CONFLICT")
        title, body = render(record["packet"])
        _save(root, record, "POST_IN_FLIGHT")
        try:
            number = transport.create(title, body, record["approval"]["label"])
        except Exception:
            return _save(root, record, "DELIVERY_UNCERTAIN")
        if not _number(number):
            return _save(root, record, "DELIVERY_UNCERTAIN")
        _save(root, record, "POST_IN_FLIGHT", number)
        return _readback(root, record, transport, "READBACK_FAILED")
