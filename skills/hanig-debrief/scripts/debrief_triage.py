"""Offline mechanical validation of debrief_triage.v1 records. No coverage writes."""
import sys

# Running the validator must not create caches alongside its installed modules.
if __name__ == "__main__":
    sys.dont_write_bytecode = True

import argparse
import hashlib
import json
from pathlib import Path
import re

import debrief_packet as P
import debrief_schema as S


def packet_digest(packet):
    """Packet-only canonical digest, distinct from the filing approval envelope."""
    return hashlib.sha256(json.dumps(packet, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode("utf-8")).hexdigest()


def _fields(value, keys, at):
    S.need(type(value) is dict, at, "expected object")
    for key in keys:
        S.need(key in value, at + "." + key, "missing field")
    S.obj(value, keys, at)


def _reference(value, at):
    S.text(value, at)
    if value.startswith("https://"):
        prefix = "https://github.com/hanig/brood-lord/"
        S.need(value.startswith(prefix) and
               not any(c.isspace() or ord(c) < 32 or c == "\\" for c in value),
               at, "expected public brood-lord HTTPS link")
        path = re.split(r"[?#]", value[len(prefix):], maxsplit=1)[0]
        S.need(bool(path) and all(p not in (".", "..") for p in path.split("/")),
               at, "expected link under brood-lord")
    else:
        path = value
        parts = value.split("/")
        S.need("\x00" not in value and
               all(p not in (".", "..") and (p != "" or i == len(parts) - 1)
                   for i, p in enumerate(parts)),
               at, "expected repository path")
    S.need(re.search(r"%[0-9a-fA-F]{2}", path) is None,
           at, "percent-encoding is not permitted in reference paths")


def _unit_ids(plans):
    S.need(bool(plans), "plans", "expected at least one plan")
    ids = set()
    for index, plan in enumerate(plans):
        at = "plans[%d].units" % index
        S.need(type(plan) is dict and type(plan.get("units")) is list,
               at, "expected list")
        for unit in plan["units"]:
            S.need(type(unit) is dict and "id" in unit, at, "expected unit with id")
            S.text(unit["id"], at + ".id")
            ids.add(unit["id"])
    return ids


def validate_record(record, packet, plans, record_path):
    """Validate parsed values; use validate() for duplicate-safe file loading."""
    P.validate_packet(packet)
    _fields(record, ("schema_version", "packet_id", "packet_digest", "issue",
                   "dispositions"), "triage")
    S.need(record["schema_version"] == S.TRIAGE_SCHEMA,
           "triage.schema_version", "unsupported version")
    S.need(record["packet_id"] == packet["packet_id"],
           "triage.packet_id", "packet id mismatch")
    S.match(record["packet_digest"], r"[0-9a-f]{64}", "triage.packet_digest")
    S.need(record["packet_digest"] == packet_digest(packet),
           "triage.packet_digest", "packet digest mismatch")
    S.need(Path(record_path).name == packet["packet_id"] + ".json",
           "triage.filename", "file-name mismatch")
    issue = record["issue"]
    _fields(issue, ("repository", "number"), "triage.issue")
    S.need(issue["repository"] == S.REPOSITORY,
           "triage.issue.repository", "expected hanig/brood-lord")
    S.need(type(issue["number"]) is int and issue["number"] > 0,
           "triage.issue.number", "expected positive integer")
    dispositions = record["dispositions"]
    S.need(type(dispositions) is dict, "triage.dispositions", "expected object")
    learning_ids = {row["id"] for row in packet["learnings"]}
    for learning_id in dispositions:
        S.need(learning_id in learning_ids, "triage.dispositions",
               "unknown learning id: %s" % learning_id)
    for learning_id in sorted(learning_ids):
        S.need(learning_id in dispositions, "triage.dispositions",
               "missing learning: %s" % learning_id)
    unit_ids = _unit_ids(plans)
    for learning_id, row in dispositions.items():
        at = "triage.dispositions." + learning_id
        S.need(type(row) is dict, at, "expected object")
        disposition = row.get("disposition")
        if disposition == "accept":
            _fields(row, ("disposition", "unit_refs"), at)
            refs = row["unit_refs"]
            S.need(type(refs) is list and bool(refs), at + ".unit_refs",
                   "expected nonempty unit refs")
            for ref in refs:
                S.text(ref, at + ".unit_refs")
                S.need(ref in unit_ids, at + ".unit_refs", "unknown unit ref: " + ref)
        elif disposition == "reject":
            _fields(row, ("disposition", "reason_code", "rationale", "references"), at)
            S.need(type(row["reason_code"]) is str and
                   row["reason_code"] in S.REJECTION_CODES,
                   at + ".reason_code", "unknown reason code")
            S.text(row["rationale"], at + ".rationale")
            refs = row["references"]
            S.need(type(refs) is list, at + ".references", "expected list")
            S.need(row["reason_code"] != "already_addressed" or bool(refs),
                   at + ".references", "already_addressed requires references")
            for ref in refs:
                _reference(ref, at + ".references")
        else:
            raise S.SchemaError(at + ".disposition: unknown disposition")
    return record


def validate(record_path, packet_path, plan_paths):
    return validate_record(S.load_json(record_path), S.load_json(packet_path),
                           [S.load_json(path) for path in plan_paths], record_path)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("validate", help="check a triage record without writing files")
    check.add_argument("--record", required=True)
    check.add_argument("--packet", required=True)
    check.add_argument("--plan", action="append", required=True)
    args = parser.parse_args(argv)
    try:
        validate(args.record, args.packet, args.plan)
    except (S.SchemaError, RecursionError) as exc:
        print("triage validation failed: %s" % exc, file=sys.stderr)
        return 1
    print("triage valid")
    return 0


if __name__ == "__main__":
    sys.exit(main())
