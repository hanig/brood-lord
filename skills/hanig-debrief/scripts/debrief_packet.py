"""Private dossier validation and allow-list construction of public packets.

Decode files with load_dossier/load_packet (or debrief_schema.load_json) so
that duplicate keys are rejected before Python dictionaries discard them.
No project files are read by validation or compilation.
"""
import hashlib
import json
import re
import uuid

import debrief_schema as S


def _enum(value, choices, at):
  S.need(type(value) is str and value in choices, at, "unknown literal")


def _list(value, at):
  S.need(type(value) is list, at, "expected list")


def _integer(value, at):
  S.need(type(value) is int, at, "expected integer")


def _string(value, at):
  S.need(type(value) is str, at, "expected string")
  S.need(not any(0xD800 <= ord(c) <= 0xDFFF for c in value), at,
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
      cohort = cohorts[count["cohort_ref"]]
      S.need(count["population"] == cohort["population"], at,
         "cohort population mismatch")
      S.need(count["numerator"] == len(cohort["qualifying"]) and
         count["denominator"] == len(cohort["members"]), at,
         "cohort count mismatch")


def _locator(value, at):
  S.text(value, at)
  S.need(not value.startswith(("/", "\\")) and "\x00" not in value and
     re.match(r"[A-Za-z]:", value) is None and
     ".." not in re.split(r"[/\\]", value), at,
     "expected relative locator without parent traversal")


def validate_dossier(value, catalog=None):
  """Validate a decoded dossier, including all private count/reference links."""
  _, entries = _catalog(catalog)
  S.obj(value, ("schema_version", "evidence", "cohorts", "candidates",
         "omissions", "provenance"), "dossier")
  _enum(value["schema_version"], (S.DOSSIER_SCHEMA,), "dossier.schema_version")
  evidence, cohorts = value["evidence"], value["cohorts"]
  S.need(type(evidence) is dict, "evidence", "expected object")
  for ref, record in evidence.items():
    S.match(ref, S.EVIDENCE_ID, "evidence")
    S.obj(record, ("source_root", "locator", "sha256", "location", "unit_id",
           "attempt_id", "adapter", "status"), ref)
    S.text(record["source_root"], ref + ".source_root")
    _locator(record["locator"], ref + ".locator")
    S.match(record["sha256"], r"[0-9a-f]{64}", ref + ".sha256")
    for key in ("unit_id", "attempt_id"):
      if record[key] is not None:
        S.text(record[key], ref + "." + key)
    S.obj(record["adapter"], ("name", "version"), ref + ".adapter")
    for key in ("name", "version"):
      S.text(record["adapter"][key], ref + ".adapter." + key)
    _enum(record["status"], S.EVIDENCE_STATUSES, ref + ".status")
    location = record["location"]
    S.need(type(location) is dict, ref + ".location", "expected object")
    if set(location) == {"line_range"}:
      lines = location["line_range"]
      _list(lines, ref + ".line_range")
      S.need(len(lines) == 2, ref, "expected start and end")
      for line in lines:
        _integer(line, ref + ".line_range")
      S.need(1 <= lines[0] <= lines[1], ref, "invalid line range")
    else:
      S.obj(location, ("json_pointer",), ref + ".location")
      pointer = location["json_pointer"]
      _string(pointer, ref + ".json_pointer")
      S.need(re.fullmatch(r"(?:/(?:[^~]|~[01])*)*", pointer) is not None,
         ref, "invalid JSON pointer")
  S.need(type(cohorts) is dict, "cohorts", "expected object")
  for ref, cohort in cohorts.items():
    S.match(ref, S.COHORT_ID, "cohorts")
    S.obj(cohort, ("population", "members", "qualifying"), ref)
    _enum(cohort["population"], S.POPULATIONS, ref + ".population")
    for key in ("members", "qualifying"):
      _list(cohort[key], ref + "." + key)
      for member in cohort[key]:
        S.text(member, ref + "." + key)
      S.need(cohort[key] == sorted(set(cohort[key])), ref,
         "expected sorted distinct ids")
    S.need(set(cohort["qualifying"]) <= set(cohort["members"]), ref,
       "qualifying must be subset of members")
  _list(value["candidates"], "candidates")
  for candidate in value["candidates"]:
    S.obj(candidate, ("catalog_entry", "status", "basis", "rationale", "evidence_refs",
             "counts", "reproduction", "coverage_disposition"), "candidate")
    _enum(candidate["status"], S.CANDIDATE_STATUSES, "candidate.status")
    _enum(candidate["basis"], S.MECHANISM_BASES, "candidate.basis")
    _enum(candidate["coverage_disposition"], S.COVERAGE_DISPOSITIONS,
       "candidate.coverage_disposition")
    S.text(candidate["rationale"], "candidate.rationale")
    entry_id = candidate["catalog_entry"]
    if entry_id is not None:
      S.match(entry_id, S.CATALOG_ID, "candidate.catalog_entry")
      S.need(entry_id in entries, "candidate.catalog_entry", "unknown catalog entry")
    selected = candidate["status"] == "selected"
    S.need(not selected or entry_id is not None, "candidate", "selected requires catalog entry")
    _refs(candidate["evidence_refs"], "candidate.evidence_refs", evidence, selected)
    _counts(candidate["counts"], entries.get(entry_id), "candidate.counts", cohorts)
    reproduction = candidate["reproduction"]
    if candidate["basis"] == "reproduced":
      S.obj(reproduction, ("receipt_ref", "tested_version", "probe", "observed"),
         "candidate.reproduction")
      _refs([reproduction["receipt_ref"]], "reproduction.receipt_ref", evidence)
      for key in ("tested_version", "probe", "observed"):
        S.text(reproduction[key], "reproduction." + key)
    else:
      S.need(reproduction is None, "candidate.reproduction", "must be null unless reproduced")
  _list(value["omissions"], "omissions")
  for omission in value["omissions"]:
    S.obj(omission, ("source_root", "locator", "reason"), "omission")
    for key in ("source_root", "reason"):
      S.text(omission[key], "omission." + key)
    _locator(omission["locator"], "omission.locator")
  S.need(type(value["provenance"]) is dict, "provenance", "expected object")
  skills = {p.name for p in S.SKILL_DIR.parent.iterdir()
       if p.is_dir() and (p / "SKILL.md").is_file()}
  for skill, marker in value["provenance"].items():
    _enum(skill, skills, "provenance.skill")
    _string(marker, "provenance.marker")
  return value


def validate_packet(value, catalog=None):
  """Accept only the public grammar and exact whole catalog tuples."""
  catalog, entries = _catalog(catalog)
  S.obj(value, ("schema_version", "catalog_version", "packet_id", "project", "learnings"),
     "packet")
  _enum(value["schema_version"], (S.PACKET_SCHEMA,), "packet.schema_version")
  _integer(value["catalog_version"], "packet.catalog_version")
  S.need(value["catalog_version"] == catalog["version"], "packet", "catalog version mismatch")
  S.match(value["packet_id"], r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
      "packet.packet_id")
  _enum(value["project"], (S.PROJECT,), "packet.project")
  _list(value["learnings"], "packet.learnings")
  S.need(1 <= len(value["learnings"]) <= S.MAX_LEARNINGS, "packet", "invalid learning count")
  for index, learning in enumerate(value["learnings"], 1):
    S.obj(learning, ("id", "catalog_entry", "skill", "what_broke", "mechanism",
            "proposed_change", "mechanism_basis", "counts", "evidence_refs"), "learning")
    _enum(learning["id"], ("L%02d" % index,), "learning.id")
    S.match(learning["catalog_entry"], S.CATALOG_ID, "learning.catalog_entry")
    S.need(learning["catalog_entry"] in entries, "learning", "unknown catalog entry")
    entry = entries[learning["catalog_entry"]]
    for key in ("skill", "what_broke", "mechanism", "proposed_change"):
      _string(learning[key], "learning." + key)
      S.need(learning[key] == entry[key], "learning." + key, "catalog tuple mismatch")
    _enum(learning["mechanism_basis"], S.MECHANISM_BASES, "learning.mechanism_basis")
    _refs(learning["evidence_refs"], "learning.evidence_refs", nonempty=True)
    _counts(learning["counts"], entry, "learning.counts")
  return value


def compile_packet(dossier, catalog):
  """Build fresh public containers; private prose never participates."""
  catalog, entries = _catalog(catalog)
  validate_dossier(dossier, catalog)
  learnings = []
  for candidate in dossier["candidates"]:
    if candidate["status"] != "selected":
      continue
    entry = entries[candidate["catalog_entry"]]
    learnings.append({
      "id": "L%02d" % (len(learnings) + 1), "catalog_entry": entry["id"],
      "skill": entry["skill"], "what_broke": entry["what_broke"],
      "mechanism": entry["mechanism"], "proposed_change": entry["proposed_change"],
      "mechanism_basis": candidate["basis"],
      "evidence_refs": ["E%03d" % int(ref[1:]) for ref in candidate["evidence_refs"]],
      "counts": [{"measure": count["measure"], "population": count["population"],
            "scope": S.COUNT_SCOPE, "numerator": count["numerator"],
            "denominator": count["denominator"],
            "cohort_ref": "C%03d" % int(count["cohort_ref"][1:])}
           for count in candidate["counts"]]})
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
  """Bind the exact approval envelope; validation is a separate operation."""
  envelope = {"packet": packet, "title": title, "repository": S.REPOSITORY,
        "label": S.LABEL, "renderer_version": S.RENDERER_VERSION}
  return hashlib.sha256(json.dumps(envelope, sort_keys=True, separators=(",", ":"),
                  ensure_ascii=False).encode("utf-8")).hexdigest()


def parse_body(body, catalog=None):
  """Permit only CRLF/LF and JSON whitespace/key ordering differences."""
  _string(body, "body")
  body = body.replace("\r\n", "\n")
  prefix, suffix = S.PREAMBLE + "\n\n```json\n", "\n```"
  S.need(body.startswith(prefix) and body.endswith(suffix), "body", "invalid framing")
  try:
    packet = json.loads(body[len(prefix):-len(suffix)],
              object_pairs_hook=S._pairs, parse_constant=S._constant)
  except (ValueError, RecursionError) as exc:
    raise S.SchemaError("body: invalid JSON") from exc
  return validate_packet(packet, catalog)


def recount(dossier, packet, catalog=None):
  """Recompute every published count from its private cohort, independently."""
  validate_dossier(dossier, catalog)
  validate_packet(packet, catalog)
  _, entries = _catalog(catalog)
  for learning in packet["learnings"]:
    _refs(learning["evidence_refs"], "learning.evidence_refs", dossier["evidence"], True)
    _counts(learning["counts"], entries[learning["catalog_entry"]],
        "learning.counts", dossier["cohorts"])
  return packet
