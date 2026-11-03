import json
from pathlib import Path
import re


CATALOG_SCHEMA = "debrief_catalog.v1"
DOSSIER_V1 = "debrief_dossier.v1"
DOSSIER_SCHEMA = "debrief_dossier.v2"
PACKET_V1 = "debrief_packet.v1"
PACKET_SCHEMA = "debrief_packet.v2"
TRIAGE_SCHEMA = "debrief_triage.v1"
COVERAGE_SCHEMA = "debrief_coverage.v1"
CATALOG_VERSION = 1
COVERAGE_VERSION = 1
RENDERER_VERSION = 1
REPOSITORY = "hanig/brood-lord"
LABEL = "debrief"
PROJECT = "project A"
TITLE_PREFIX = "hanig-debrief: "
PREAMBLE = "Evidence references are held privately by the source project's owner."
POPULATIONS = ("units", "attempts", "evidence_documents", "helper_scripts", "state_entries")
MECHANISM_BASES = ("reported", "inferred", "reproduced")
EVIDENCE_STATUSES = ("ok", "skipped", "unsupported", "unstable")
CANDIDATE_STATUSES = ("selected", "covered", "unrepresentable", "rejected")
COVERAGE_DISPOSITIONS = ("covered", "extends", "conflicts", "not_found_in_snapshot", "unknown")
REJECTION_CODES = ("already_addressed", "local_modification", "unsupported_mechanism",
         "insufficient_evidence", "out_of_scope")
COUNT_SCOPE = "inspected_set"
MAX_LEARNINGS = 50
MAX_DENOMINATOR = 1000000
PER_FILE_BYTES = 8 * 1024 * 1024
PER_ROOT_FILES = 10000
TOTAL_BYTES = 256 * 1024 * 1024
LEARNING_ID = r"L(?:0[1-9]|[1-4][0-9]|50)"
EVIDENCE_ID_V1 = r"E(?!000)[0-9]{3}"
COHORT_ID_V1 = r"C(?!000)[0-9]{3}"
EVIDENCE_ID = r"E(?:00[1-9]|0[1-9][0-9]|[1-9][0-9]{2,5})"
COHORT_ID = r"C(?:00[1-9]|0[1-9][0-9]|[1-9][0-9]{2,5})"
MAX_IDENTIFIERS = 999999
CATALOG_ID = r"DB[0-9]{3}"
COVERAGE_ID = r"CV(?:00[1-9]|0[1-9][0-9]|[1-9][0-9]{2,})"
COMMIT_SHA = r"(?:[0-9a-f]{40}|[0-9a-f]{64})"
FILING_TRANSITIONS = {
  "DRAFT": ("APPROVED",),
  "APPROVED": ("DRAFT", "POST_IN_FLIGHT"),
  "POST_IN_FLIGHT": ("FILED", "DELIVERY_UNCERTAIN", "READBACK_FAILED", "CONFLICT"),
  "DELIVERY_UNCERTAIN": ("FILED", "READBACK_FAILED", "CONFLICT"),
  "READBACK_FAILED": ("FILED", "CONFLICT"),
  "CONFLICT": (),
  "FILED": (),
}
SKILL_DIR = Path(__file__).resolve().parents[1]


class SchemaError(ValueError):
  pass

def need(ok, at, reason):
  if not ok:
    raise SchemaError(f"{at}: {reason}")

def _pairs(pairs):
  value = {}
  for key, item in pairs:
    need(key not in value, key, "duplicate key")
    value[key] = item
  return value

def _constant(_value):
  raise SchemaError("JSON: non-finite number")

def load_json(path):
  try:
    return json.loads(Path(path).read_text(encoding="utf-8"),
             object_pairs_hook=_pairs, parse_constant=_constant)
  except SchemaError:
    raise
  except (OSError, UnicodeError, ValueError, RecursionError) as exc:
    raise SchemaError("JSON: unreadable or invalid document") from exc

def obj(value, keys, at):
  need(type(value) is dict, at, "expected object")
  need(set(value) == set(keys), at, "unknown or missing fields")

def text(value, at):
  need(type(value) is str and bool(value) and not value.isspace(),
      at, "expected nonempty string")
  need(not any(0xD800 <= ord(c) <= 0xDFFF for c in value), at,
      "unpaired Unicode surrogate")

def match(value, pattern, at):
  text(value, at)
  need(re.fullmatch(pattern, value) is not None, at, "invalid literal")

def _header(value, schema, version, at):
  obj(value, ("schema_version", "version", "entries"), at)
  need(type(value["schema_version"]) is str and value["schema_version"] == schema,
      at + ".schema_version", "unsupported version")
  need(type(value["version"]) is int, at + ".version", "expected integer")
  need(value["version"] == version, at + ".version", "unsupported version")
  need(type(value["entries"]) is list, at + ".entries", "expected list")

def _entries(value, schema, version, label, keys, pattern):
  _header(value, schema, version, label)
  ids = set()
  for index, entry in enumerate(value["entries"]):
    at = f"{label}.entries[{index}]"
    obj(entry, keys, at)
    match(entry["id"], pattern, at + ".id")
    need(entry["id"] not in ids, at + ".id", "duplicate entry id")
    ids.add(entry["id"])
    yield entry, at

def validate_catalog(value, skills_root=None):
  root = Path(skills_root) if skills_root is not None else SKILL_DIR.parent
  for entry, at in _entries(value, CATALOG_SCHEMA, CATALOG_VERSION, "catalog",
      ("id", "skill", "what_broke", "mechanism", "proposed_change", "measures"), CATALOG_ID):
    text(entry["skill"], at + ".skill")
    try:
      present = any(p.name == entry["skill"] and p.is_dir() and (p / "SKILL.md").is_file()
          for p in root.iterdir())
    except OSError as exc:
      raise SchemaError("catalog: unreadable skills root") from exc
    need(present, at + ".skill", "no such skill bundle")
    for key in ("what_broke", "mechanism", "proposed_change"):
      text(entry[key], at + "." + key)
    need(type(entry["measures"]) is list and bool(entry["measures"]),
        at + ".measures", "expected nonempty list")
    pairs = set()
    for measure in entry["measures"]:
      place = at + ".measures"
      obj(measure, ("measure", "population"), place)
      match(measure["measure"], r"[a-z][a-z0-9_]*", place + ".measure")
      need(type(measure["population"]) is str and measure["population"] in POPULATIONS,
          place + ".population", "unknown population")
      pair = (measure["measure"], measure["population"])
      need(pair not in pairs, place, "duplicate measure/population pair")
      pairs.add(pair)
  need(bool(value["entries"]), "catalog.entries", "expected nonempty list")
  return value

def _reference(value, at):
  text(value, at)
  need(not any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in value),
      at, "control character in reference")
  if re.fullmatch(r"https://github\.com/hanig/brood-lord/pull/[1-9][0-9]*(?:/[A-Za-z0-9_-]+)*/?(?:\?[^\\\s#]*)?(?:#[^\\\s]*)?", value):
    return
  parts = value.split("/")
  need("\x00" not in value and all(part not in ("", ".", "..") for part in parts),
      at, "expected public brood-lord PR URL or repository path")

def validate_coverage(value, catalog):
  catalog_ids = {entry["id"] for entry in catalog["entries"]}
  for entry, at in _entries(value, COVERAGE_SCHEMA, COVERAGE_VERSION, "coverage",
      ("id", "catalog_entry", "applicability", "references", "note"), COVERAGE_ID):
    match(entry["catalog_entry"], CATALOG_ID, at + ".catalog_entry")
    need(entry["catalog_entry"] in catalog_ids, at + ".catalog_entry", "unknown entry")
    applies = entry["applicability"]
    need(type(applies) is dict, at + ".applicability", "expected object")
    if set(applies) == {"fixed_in"}:
      match(applies["fixed_in"], COMMIT_SHA, at + ".applicability.fixed_in")
    else:
      obj(applies, ("always",), at + ".applicability")
      need(applies["always"] is True, at + ".applicability.always", "expected true")
    need(type(entry["references"]) is list and bool(entry["references"]),
        at + ".references", "expected nonempty list")
    for reference in entry["references"]:
      _reference(reference, at + ".references")
    text(entry["note"], at + ".note")
  return value

def load_catalog(path=None, skills_root=None):
  return validate_catalog(load_json(SKILL_DIR / "catalog.json" if path is None else path), skills_root)

def load_coverage(path=None, catalog=None):
  if catalog is None:
    catalog = load_catalog()
  return validate_coverage(load_json(SKILL_DIR / "coverage.json" if path is None else path), catalog)


def identifier_grammar(schema, kind):
  """Select one exact grammar by artifact header, without normalizing IDs."""
  versions = {"dossier": (DOSSIER_V1, DOSSIER_SCHEMA),
              "packet": (PACKET_V1, PACKET_SCHEMA)}
  old, current = versions[kind]
  need(type(schema) is str and schema in (old, current),
       kind + ".schema_version", "unsupported version")
  if schema == old:
    return EVIDENCE_ID_V1, COHORT_ID_V1, 999
  return EVIDENCE_ID, COHORT_ID, MAX_IDENTIFIERS
