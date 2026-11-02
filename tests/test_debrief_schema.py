"""Synthetic schema fixtures."""
import ast
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills/hanig-debrief/scripts/debrief_schema.py"
sys.path.insert(0, str(SCRIPT.parent))
import debrief_schema as S


class TestDebriefSchema(unittest.TestCase):
  def setUp(self):
    temp = tempfile.TemporaryDirectory()
    self.addCleanup(temp.cleanup)
    self.root = Path(temp.name)
    bundle = self.root / "fixture-skill"
    bundle.mkdir()
    (bundle / "SKILL.md").write_text("fixture", encoding="utf-8")
    self.cat = {"schema_version": S.CATALOG_SCHEMA, "version": 1, "entries": [{
      "id": "DB001", "skill": "fixture-skill", "what_broke": "Observation.",
      "mechanism": "Records reported an observation.", "proposed_change": "Check it.",
      "measures": [{"measure": "observed", "population": "units"}]}]}
    self.cov = {"schema_version": S.COVERAGE_SCHEMA, "version": 1, "entries": [{
      "id": "CV001", "catalog_entry": "DB001", "applicability": {"always": True},
      "references": ["README.md", "https://github.com/hanig/brood-lord/pull/1"],
      "note": "Fixture coverage."}]}

  def load(self, value, coverage=False):
    path = self.root / "input.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return (S.load_coverage(path, self.cat) if coverage else
        S.load_catalog(path, self.root))

  def reject(self, value, error, coverage=False):
    with self.assertRaisesRegex(S.SchemaError, error):
      self.load(value, coverage)

  def test_shipped_data(self):
    catalog = S.load_catalog()
    self.assertEqual(len(catalog["entries"]), 16)
    self.assertEqual(S.load_coverage(catalog=catalog), {
      "schema_version": "debrief_coverage.v1", "version": 1, "entries": []})

  def test_valid_coverage_variants(self):
    for applicability in ({"always": True}, {"fixed_in": "a" * 40}, {"fixed_in": "b" * 64}):
      self.cov["entries"][0]["applicability"] = applicability
      self.assertEqual(self.load(self.cov, True), self.cov)

  def test_duplicate_keys_rejected(self):
    for value, coverage, key in ((self.cat, False, "version"),
                  (self.cat, False, "measure"),
                  (self.cov, True, "always")):
      raw = json.dumps(value).replace('"%s":' % key, '"%s": null, "%s":' % (key, key))
      path = self.root / "duplicate.json"
      path.write_text(raw, encoding="utf-8")
      with self.subTest(key=key), self.assertRaisesRegex(S.SchemaError, key + ": duplicate key"):
        if coverage:
          S.load_coverage(path, self.cat)
        else:
          S.load_catalog(path, self.root)

  def test_skill_must_exist(self):
    self.cat["entries"][0]["skill"] = "missing-skill"
    self.reject(self.cat, "skill: no such skill bundle")

  def test_bundle_requires_marker(self):
    (self.root / "fixture-skill/SKILL.md").unlink()
    self.reject(self.cat, "skill: no such skill bundle")

  def test_invalid_catalog_fields(self):
    entry = ("entries", 0)
    cases = [((), "extra", 1, "catalog: unknown"),
        ((), "version", True, "version: expected integer"),
        ((), "version", 2, "version: unsupported"),
        ((), "schema_version", "debrief_catalog.v2", "schema_version: unsupported"),
        ((), "entries", {}, "entries: expected list"),
        (entry, "extra", 1, "unknown or missing"),
        (entry, "what_broke", 7, "what_broke: expected"),
        (entry, "measures", [{"measure": "observed"}], "measures: unknown"),
        (entry, "measures", "observed", "measures: expected")]
    self.check_changes(self.cat, cases)

  def check_changes(self, original, cases, coverage=False):
    for path, key, value, error in cases:
      candidate = copy.deepcopy(original)
      node = candidate
      for part in path:
        node = node[part]
      node[key] = value
      with self.subTest(path=path, key=key, value=value):
        self.reject(candidate, error, coverage)

  def test_values_are_not_normalized(self):
    for path, key, literal, variant in [
      ((), "schema_version", S.CATALOG_SCHEMA, "debrief_catal\u043eg.v1"),
      (("entries", 0), "skill", "fixture-skill", "f\u0131xture-skill"),
      (("entries", 0), "id", "DB001", "DB\uff1001"),
      (("entries", 0, "measures", 0), "population", "units", "un\u0131ts"),
    ]:
      for value in (" " + literal, literal + " ", literal.swapcase(), variant):
        self.check_changes(self.cat, [(path, key, value, key)])

  def test_duplicate_ids_and_pairs(self):
    for value, coverage in ((self.cat, False), (self.cov, True)):
      value["entries"].append(copy.deepcopy(value["entries"][0]))
      self.reject(value, "id: duplicate entry id", coverage)
    self.cat["entries"].pop()
    pairs = self.cat["entries"][0]["measures"]
    pairs.append(copy.deepcopy(pairs[0]))
    self.reject(self.cat, "duplicate measure/population pair")

  def test_invalid_coverage_fields(self):
    entry = ("entries", 0)
    cases = [((), "version", True, "version: expected integer"),
        ((), "version", 2, "version: unsupported"),
        ((), "schema_version", "debrief_coverage.v2", "schema_version: unsupported"),
        ((), "extra", 1, "unknown or missing"),
        (entry, "extra", 1, "unknown or missing"),
        (entry, "catalog_entry", "DB999", "catalog_entry: unknown"),
        (entry, "applicability", {"always": 1}, "always: expected true"),
        (entry, "applicability", {"always": True, "fixed_in": "a" * 40}, "unknown or missing"),
        (entry, "applicability", {"fixed_in": "a" * 39}, "fixed_in: invalid"),
        (entry, "references", [], "references: expected"),
        (entry, "references", ["../private"], "references: expected"),
        (entry, "references", ["https://example.invalid/pull/1"], "references: expected")]
    self.check_changes(self.cov, cases, True)

  def test_schema_imports_are_offline(self):
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    imports = set()
    for node in ast.walk(tree):
      if isinstance(node, ast.Import):
        imports.update(alias.name.split(".")[0] for alias in node.names)
      elif isinstance(node, ast.ImportFrom):
        imports.add((node.module or "").split(".")[0])
    forbidden = {"socket", "ssl", "http", "urllib", "ftplib", "smtplib", "asyncio", "subprocess"}
    self.assertFalse(imports & forbidden)
    self.assertLessEqual(imports, {"json", "pathlib", "re"})


if __name__ == "__main__":
  unittest.main()
