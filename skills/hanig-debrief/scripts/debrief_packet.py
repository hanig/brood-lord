"""Strict packet construction. Load JSON with load_dossier/load_packet to reject duplicate keys."""
import hashlib
import json
import re
import uuid

import debrief_schema as S


def _enum(v, choices, at):
  S.need(type(v) is str and v in choices, at, "unknown literal")


def _list(v, at):
  S.need(type(v) is list, at, "expected list")


def _integer(v, at):
  S.need(type(v) is int, at, "expected integer")


def _string(v, at):
  S.need(type(v) is str, at, "expected string")
  S.need(not any(0xD800 <= ord(c) <= 0xDFFF for c in v), at,
    "unpaired Unicode surrogate")


def _catalog(catalog):
  catalog = S.load_catalog() if catalog is None else S.validate_catalog(catalog)
  return catalog, {entry["id"]: entry for entry in catalog["entries"]}


def _refs(refs, at, evidence=None, nonempty=False):
  _list(refs, at)
  S.need(not nonempty or bool(refs), at, "expected evidence references")
  for ref in refs:
    S.match(ref, S.EVIDENCE_ID, at)
    if evidence is not None:
      S.need(ref in evidence, at, "dangling evidence reference")


def _counts(counts, entry, at, cohorts=None):
  _list(counts, at)
  measures = set()
  for count in counts:
    S.obj(count, ("measure", "population", "scope", "numerator",
      "denominator", "cohort_ref"), at)
    S.need(entry is not None, at, "count requires catalog entry")
    S.match(count["measure"], r"[a-z][a-z0-9_]*", at + ".measure")
    _enum(count["population"], S.POPULATIONS, at + ".population")
    S.need(any(count["measure"] == pair["measure"] and
      count["population"] == pair["population"]
      for pair in entry["measures"]), at, "unpermitted count pair")
    S.need(count["measure"] not in measures, at, "duplicate measure")
    measures.add(count["measure"])
    _enum(count["scope"], (S.COUNT_SCOPE,), at + ".scope")
    for key in ("numerator", "denominator"):
      _integer(count[key], at + "." + key)
    S.need(0 <= count["numerator"] <= count["denominator"] <= S.MAX_DENOMINATOR
      and count["denominator"] >= 1, at, "invalid count bounds")
    S.match(count["cohort_ref"], S.COHORT_ID, at + ".cohort_ref")
    if cohorts is not None:
      S.need(count["cohort_ref"] in cohorts, at, "dangling cohort reference")
      group = cohorts[count["cohort_ref"]]
      S.need(count["population"] == group["population"], at,
        "cohort population mismatch")
      S.need(count["numerator"] == len(group["qualifying"]) and
        count["denominator"] == len(group["members"]), at,
        "cohort count mismatch")


def _locator(v, at):
  S.text(v, at)
  S.need(not v.startswith(("/", "\\")) and "\x00" not in v and
    re.match(r"[A-Za-z]:", v) is None and
    ".." not in re.split(r"[/\\]", v), at,
    "expected relative locator without parent traversal")


def validate_dossier(v, catalog=None):
  _, entries = _catalog(catalog)
  S.obj(v, ("schema_version", "evidence", "cohorts", "candidates",
    "omissions", "provenance"), "dossier")
  _enum(v["schema_version"], (S.DOSSIER_SCHEMA,), "dossier.schema_version")
  evidence, cohorts = v["evidence"], v["cohorts"]
  S.need(type(evidence) is dict, "evidence", "expected object")
  for ref, rec in evidence.items():
    S.match(ref, S.EVIDENCE_ID, "evidence")
    S.obj(rec, ("source_root", "locator", "sha256", "location", "unit_id",
      "attempt_id", "adapter", "status"), ref)
    S.text(rec["source_root"], ref + ".source_root")
    _locator(rec["locator"], ref + ".locator")
    S.match(rec["sha256"], r"[0-9a-f]{64}", ref + ".sha256")
    for key in ("unit_id", "attempt_id"):
      if rec[key] is not None:
        S.text(rec[key], ref + "." + key)
    S.obj(rec["adapter"], ("name", "version"), ref + ".adapter")
    for key in ("name", "version"):
      S.text(rec["adapter"][key], ref + ".adapter." + key)
    _enum(rec["status"], S.EVIDENCE_STATUSES, ref + ".status")
    loc = rec["location"]
    S.need(type(loc) is dict, ref + ".location", "expected object")
    if set(loc) == {"line_range"}:
      lines = loc["line_range"]
      _list(lines, ref + ".line_range")
      S.need(len(lines) == 2, ref, "expected start and end")
      for line in lines:
        _integer(line, ref + ".line_range")
      S.need(1 <= lines[0] <= lines[1], ref, "invalid line range")
    else:
      S.obj(loc, ("json_pointer",), ref + ".location")
      pointer = loc["json_pointer"]
      _string(pointer, ref + ".json_pointer")
      S.need(re.fullmatch(r"(?:/(?:[^~]|~[01])*)*", pointer) is not None,
        ref, "invalid JSON pointer")
  S.need(type(cohorts) is dict, "cohorts", "expected object")
  for ref, group in cohorts.items():
    S.match(ref, S.COHORT_ID, "cohorts")
    S.obj(group, ("population", "members", "qualifying"), ref)
    _enum(group["population"], S.POPULATIONS, ref + ".population")
    for key in ("members", "qualifying"):
      _list(group[key], ref + "." + key)
      for member in group[key]:
        S.text(member, ref + "." + key)
      S.need(group[key] == sorted(set(group[key])), ref,
        "expected sorted distinct ids")
    S.need(set(group["qualifying"]) <= set(group["members"]), ref,
      "qualifying must be subset of members")
  _list(v["candidates"], "candidates")
  for cand in v["candidates"]:
    S.obj(cand, ("catalog_entry", "status", "basis", "rationale", "evidence_refs",
      "counts", "reproduction", "coverage_disposition"), "candidate")
    _enum(cand["status"], S.CANDIDATE_STATUSES, "candidate.status")
    _enum(cand["basis"], S.MECHANISM_BASES, "candidate.basis")
    _enum(cand["coverage_disposition"], S.COVERAGE_DISPOSITIONS,
      "candidate.coverage_disposition")
    S.text(cand["rationale"], "candidate.rationale")
    entry_id = cand["catalog_entry"]
    if entry_id is not None:
      S.match(entry_id, S.CATALOG_ID, "candidate.catalog_entry")
      S.need(entry_id in entries, "candidate.catalog_entry", "unknown catalog entry")
    selected = cand["status"] == "selected"
    S.need(not selected or entry_id is not None, "candidate", "selected requires catalog entry")
    _refs(cand["evidence_refs"], "candidate.evidence_refs", evidence, selected)
    _counts(cand["counts"], entries.get(entry_id), "candidate.counts", cohorts)
    repro = cand["reproduction"]
    if cand["basis"] == "reproduced":
      S.obj(repro, ("receipt_ref", "tested_version", "probe", "observed"),
        "candidate.reproduction")
      _refs([repro["receipt_ref"]], "reproduction.receipt_ref", evidence)
      for key in ("tested_version", "probe", "observed"):
        S.text(repro[key], "reproduction." + key)
    else:
      S.need(repro is None, "candidate.reproduction", "must be null unless reproduced")
  _list(v["omissions"], "omissions")
  for omission in v["omissions"]:
    S.obj(omission, ("source_root", "locator", "reason"), "omission")
    for key in ("source_root", "reason"):
      S.text(omission[key], "omission." + key)
    _locator(omission["locator"], "omission.locator")
  S.need(type(v["provenance"]) is dict, "provenance", "expected object")
  for skill, marker in v["provenance"].items():
    S.text(skill, "provenance.skill")
    _string(marker, "provenance.marker")
  return v


def validate_packet(v, catalog=None):
  catalog, entries = _catalog(catalog)
  S.obj(v, ("schema_version", "catalog_version", "packet_id", "project", "learnings"),
    "packet")
  _enum(v["schema_version"], (S.PACKET_SCHEMA,), "packet.schema_version")
  _integer(v["catalog_version"], "packet.catalog_version")
  S.need(v["catalog_version"] == catalog["version"], "packet", "catalog version mismatch")
  S.match(v["packet_id"], r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
    "packet.packet_id")
  _enum(v["project"], (S.PROJECT,), "packet.project")
  _list(v["learnings"], "packet.learnings")
  S.need(1 <= len(v["learnings"]) <= S.MAX_LEARNINGS, "packet", "invalid learning count")
  for index, lesson in enumerate(v["learnings"], 1):
    S.obj(lesson, ("id", "catalog_entry", "skill", "what_broke", "mechanism",
      "proposed_change", "mechanism_basis", "counts", "evidence_refs"), "learning")
    _enum(lesson["id"], ("L%02d" % index,), "learning.id")
    S.match(lesson["catalog_entry"], S.CATALOG_ID, "learning.catalog_entry")
    S.need(lesson["catalog_entry"] in entries, "learning", "unknown catalog entry")
    entry = entries[lesson["catalog_entry"]]
    for key in ("skill", "what_broke", "mechanism", "proposed_change"):
      _string(lesson[key], "learning." + key)
      S.need(lesson[key] == entry[key], "learning." + key, "catalog tuple mismatch")
    _enum(lesson["mechanism_basis"], S.MECHANISM_BASES, "learning.mechanism_basis")
    _refs(lesson["evidence_refs"], "learning.evidence_refs", nonempty=True)
    _counts(lesson["counts"], entry, "learning.counts")
  return v


def compile_packet(dossier, catalog):
  catalog, entries = _catalog(catalog)
  validate_dossier(dossier, catalog)
  learnings = []
  for cand in dossier["candidates"]:
    if cand["status"] != "selected":
      continue
    entry = entries[cand["catalog_entry"]]
    learnings.append({
      "id": "L%02d" % (len(learnings) + 1), "catalog_entry": entry["id"],
      "skill": entry["skill"], "what_broke": entry["what_broke"],
      "mechanism": entry["mechanism"], "proposed_change": entry["proposed_change"],
      "mechanism_basis": cand["basis"],
      "evidence_refs": ["E%03d" % int(ref[1:]) for ref in cand["evidence_refs"]],
      "counts": [{"measure": pair["measure"], "population": pair["population"],
      "scope": S.COUNT_SCOPE, "numerator": count["numerator"],
      "denominator": count["denominator"],
      "cohort_ref": "C%03d" % int(count["cohort_ref"][1:])}
      for count in cand["counts"] for pair in entry["measures"]
      if pair["measure"] == count["measure"] and
      pair["population"] == count["population"]]})
  return validate_packet({"schema_version": S.PACKET_SCHEMA, "catalog_version": catalog["version"],
    "packet_id": str(uuid.uuid4()), "project": S.PROJECT,
    "learnings": learnings}, catalog)


def load_dossier(path, catalog=None):
  return validate_dossier(S.load_json(path), catalog)


def load_packet(path, catalog=None):
  return validate_packet(S.load_json(path), catalog)


def render(packet, catalog=None):
  validate_packet(packet, catalog)
  return (S.TITLE_PREFIX + packet["packet_id"], S.PREAMBLE + "\n\n```json\n" +
    json.dumps(packet, sort_keys=True, indent=2, ensure_ascii=False) + "\n```")


def approval_digest(packet, title):
  envelope = {"packet": packet, "title": title, "repository": S.REPOSITORY,
    "label": S.LABEL, "renderer_version": S.RENDERER_VERSION}
  return hashlib.sha256(json.dumps(envelope, sort_keys=True, separators=(",", ":"),
    ensure_ascii=False).encode("utf-8")).hexdigest()


def parse_body(body, catalog=None):
  _string(body, "body")
  body = body.replace("\r\n", "\n")
  prefix, suffix = S.PREAMBLE + "\n\n```json\n", "\n```"
  S.need(body.startswith(prefix) and body.endswith(suffix), "body", "invalid framing")
  raw = body[len(prefix):-len(suffix)]
  try:
    packet = json.loads(raw,
      object_pairs_hook=S._pairs, parse_constant=S._constant)
  except (ValueError, RecursionError) as exc:
    raise S.SchemaError("body: invalid JSON") from exc
  # Keep raw string tokens; only inter-token JSON whitespace is insignificant.
  compact = re.sub(r'("(?:[^"\\]|\\.)*")|[ \t\r\n]+',
    lambda match: match[1] or "", raw)
  S.need(compact == json.dumps(packet, separators=(",", ":"), ensure_ascii=False),
    "body", "noncanonical scalar encoding")
  return validate_packet(packet, catalog)


def recount(dossier, packet, catalog=None):
  validate_dossier(dossier, catalog)
  validate_packet(packet, catalog)
  _, entries = _catalog(catalog)
  for lesson in packet["learnings"]:
    _refs(lesson["evidence_refs"], "learning.evidence_refs", dossier["evidence"], True)
    _counts(lesson["counts"], entries[lesson["catalog_entry"]],
      "learning.counts", dossier["cohorts"])
  return packet
