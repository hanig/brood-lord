"""Synthetic private records exercise the public boundary through rendering."""
import ast
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills/hanig-debrief/scripts/debrief_packet.py"
sys.path.insert(0, str(SCRIPT.parent))
import debrief_packet as P
import debrief_schema as S


def dossier():
  return {"schema_version": S.DOSSIER_SCHEMA, "evidence": {"E001": {
    "source_root": "fixture root", "locator": "records/event.json", "sha256": "a" * 64,
    "location": {"line_range": [1, 2]}, "unit_id": None, "attempt_id": "attempt one",
    "adapter": {"name": "fixture", "version": "one"}, "status": "ok"}},
    "cohorts": {"C001": {"population": "attempts", "members": ["a", "b"], "qualifying": ["a"]}},
    "candidates": [{"catalog_entry": "DB001", "status": "selected", "basis": "reported",
      "rationale": "Private assertion.", "evidence_refs": ["E001"], "counts": [{
        "measure": "affected", "population": "attempts", "scope": S.COUNT_SCOPE,
        "numerator": 1, "denominator": 2, "cohort_ref": "C001"}], "reproduction": None,
      "coverage_disposition": "unknown"}],
    "omissions": [{"source_root": "fixture root", "locator": ".", "reason": "budget"}],
    "provenance": {"hanig-swarm": "private marker contents"}}


class TestDebriefPacket(unittest.TestCase):
  def setUp(self):
    temp = tempfile.TemporaryDirectory()
    self.addCleanup(temp.cleanup)
    self.root = Path(temp.name)
    self.catalog = S.load_catalog()
    self.dossier = dossier()
    self.packet = P.compile_packet(self.dossier, self.catalog)

  def reject_changes(self, original, validator, changes):
    for path, replacement in changes:
      value = copy.deepcopy(original)
      node = value
      for key in path[:-1]:
        node = node[key]
      node[path[-1]] = replacement
      with self.subTest(path=path, value=replacement), self.assertRaises(S.SchemaError):
        validator(value)

  def test_valid_round_trip_and_independent_containers(self):
    before = copy.deepcopy(self.dossier)
    self.assertEqual(P.validate_dossier(self.dossier), self.dossier)
    self.assertEqual(P.validate_packet(self.packet, self.catalog), self.packet)
    self.assertEqual(P.recount(self.dossier, self.packet), self.packet)
    title, body = P.render(self.packet)
    self.assertEqual(title, S.TITLE_PREFIX + self.packet["packet_id"])
    self.assertEqual(body, S.PREAMBLE + "\n\n```json\n" + json.dumps(
      self.packet, sort_keys=True, indent=2, ensure_ascii=False) + "\n```")
    self.assertEqual(P.parse_body(body), self.packet)
    self.assertNotEqual(P.compile_packet(self.dossier, self.catalog)["packet_id"],
              self.packet["packet_id"])
    self.packet["learnings"][0]["counts"][0]["numerator"] = 0
    self.packet["learnings"][0]["evidence_refs"].clear()
    self.assertEqual(self.dossier, before)

  def test_public_boundary_fixtures(self):
    sentinels = ["SYNTHETIC_PROJECT_SECRET", "/synthetic/private/fixture",
          "Synthetic scientific result: marker Q increased.",
          "sk-SYNTHETIC-NOT-A-CREDENTIAL-93841"]
    for sentinel in sentinels:
      d = dossier()
      e = d["evidence"]["E001"]
      for key in ("source_root", "unit_id", "attempt_id"):
        e[key] = sentinel
      e["locator"] = "relative/" + sentinel
      e["location"] = {"json_pointer": "/" + sentinel}
      e["adapter"] = {"name": sentinel, "version": sentinel}
      d["cohorts"]["C001"].update(members=[sentinel + "a", sentinel + "b"],
                    qualifying=[sentinel + "a"])
      c = d["candidates"][0]
      c.update(rationale=sentinel, basis="reproduced", reproduction={
        "receipt_ref": "E001", "tested_version": sentinel,
        "probe": sentinel, "observed": sentinel})
      d["omissions"] = [{"source_root": sentinel, "locator": "relative/" + sentinel,
               "reason": sentinel}]
      d["provenance"]["hanig-swarm"] = sentinel
      packet = P.compile_packet(d, self.catalog)
      title, body = P.render(packet)
      with self.subTest(sentinel=sentinel):
        self.assertNotIn(sentinel, title + body)
        self.assertEqual(packet["learnings"][0]["mechanism"],
                self.catalog["entries"][0]["mechanism"])
        self.assertEqual(P.recount(d, packet), packet)

  def test_only_selected_and_sequential_learning_ids(self):
    for status in ("covered", "unrepresentable", "rejected"):
      candidate = copy.deepcopy(self.dossier["candidates"][0])
      candidate.update(status=status, catalog_entry=None, counts=[], evidence_refs=[])
      self.dossier["candidates"].insert(0, candidate)
    self.dossier["candidates"].append(copy.deepcopy(self.dossier["candidates"][-1]))
    packet = P.compile_packet(self.dossier, self.catalog)
    self.assertEqual([x["id"] for x in packet["learnings"]], ["L01", "L02"])
    self.dossier["candidates"] = []
    self.assertEqual(P.validate_dossier(self.dossier), self.dossier)
    with self.assertRaises(S.SchemaError):
      P.compile_packet(self.dossier, self.catalog)

  def test_unknown_and_missing_fields_at_every_object(self):
    def object_paths(value, path=()):
      if type(value) is dict:
        yield path
        for key, child in value.items():
          yield from object_paths(child, path + (key,))
      elif type(value) is list:
        for index, child in enumerate(value):
          yield from object_paths(child, path + (index,))
    for original, validate in ((self.dossier, P.validate_dossier),
                 (self.packet, P.validate_packet)):
      for path in object_paths(original):
        # Provenance is a map of existing skills, not an exact-key record.
        for extra in (True, False):
          value = copy.deepcopy(original)
          node = value
          for key in path:
            node = node[key]
          if extra:
            node["unexpected"] = "synthetic"
          elif path not in (("provenance",), ("evidence",), ("cohorts",)):
            del node[next(iter(node))]
          else:
            continue
          with self.subTest(path=path, extra=extra), self.assertRaises(S.SchemaError):
            validate(value)

  def test_duplicate_keys_through_strict_loaders(self):
    for value, loader in ((self.dossier, P.load_dossier), (self.packet, P.load_packet)):
      raw = json.dumps(value)
      for key in ("schema_version", "numerator"):
        duplicate = raw.replace('"%s":' % key, '"%s": null, "%s":' % (key, key))
        path = self.root / "duplicate.json"
        path.write_text(duplicate, encoding="utf-8")
        with self.subTest(key=key), self.assertRaisesRegex(S.SchemaError, "duplicate key"):
          loader(path)
      path.write_text(raw, encoding="utf-8")
      self.assertEqual(loader(path), value)

  def test_integer_types_and_bounds(self):
    for original, prefix, validate in ((self.dossier, ("candidates", 0), P.validate_dossier),
                     (self.packet, ("learnings", 0), P.validate_packet)):
      changes = [(prefix + ("counts", 0, key), value)
           for key in ("numerator", "denominator")
           for value in (True, False, 1.0, "1", None, -1, S.MAX_DENOMINATOR + 1)]
      changes += [(prefix + ("counts", 0, "numerator"), 3),
            (prefix + ("counts", 0, "denominator"), 0)]
      self.reject_changes(original, validate, changes)
    self.reject_changes(self.packet, P.validate_packet, [(('catalog_version',), True)])
    self.reject_changes(self.dossier, P.validate_dossier,
              [(('evidence', 'E001', 'location', 'line_range'), [True, 2])])
    for numerator, denominator in ((0, 1), (1, 1), (0, S.MAX_DENOMINATOR),
                   (S.MAX_DENOMINATOR, S.MAX_DENOMINATOR)):
      self.packet["learnings"][0]["counts"][0].update(numerator=numerator, denominator=denominator)
      P.validate_packet(self.packet)

  def test_catalog_whole_tuple_exactness(self):
    learning = ("learnings", 0)
    changes = [(learning + ("catalog_entry",), "DB999")]
    for key in ("skill", "what_broke", "mechanism", "proposed_change"):
      value = self.packet["learnings"][0][key]
      changes.extend((learning + (key,), variant) for variant in
             (value.upper(), " " + value, value + " ", value.replace("a", "а")))
    self.reject_changes(self.packet, P.validate_packet, changes)
    mixed = copy.deepcopy(self.packet)
    mixed["learnings"][0]["mechanism"] = self.catalog["entries"][1]["mechanism"]
    with self.assertRaises(S.SchemaError):
      P.validate_packet(mixed)
    self.reject_changes(self.dossier, P.validate_dossier,
              [(("candidates", 0, "catalog_entry"), "DB999")])

  def test_count_pairs_and_duplicate_measures(self):
    for original, prefix, validate in ((self.dossier, ("candidates", 0), P.validate_dossier),
                     (self.packet, ("learnings", 0), P.validate_packet)):
      count = copy.deepcopy(original[prefix[0]][0]["counts"][0])
      self.reject_changes(original, validate, [
        (prefix + ("counts",), [count, count]),
        (prefix + ("counts", 0, "population"), "units"),
        (prefix + ("counts", 0, "measure"), "unpermitted"),
        (prefix + ("counts", 0, "scope"), "all")])
    self.dossier["candidates"][0]["counts"] = []
    packet = P.compile_packet(self.dossier, self.catalog)
    self.assertEqual(packet["learnings"][0]["counts"], [])

  def test_private_reference_and_location_refusals(self):
    c = ("candidates", 0)
    e = ("evidence", "E001")
    h = ("cohorts", "C001")
    self.reject_changes(self.dossier, P.validate_dossier, [
      (("schema_version",), "debrief_dossier.v2"), (c + ("status",), "SELECTED"),
      (c + ("basis",), "approved"), (c + ("coverage_disposition",), "maybe"),
      (c + ("catalog_entry",), None), (c + ("evidence_refs",), []),
      (c + ("evidence_refs",), ["E999"]), (c + ("evidence_refs",), "E001"),
      (c + ("counts", 0, "cohort_ref"), "C999"),
      (h + ("population",), "units"), (h + ("members",), ["a", "a"]),
      (h + ("members",), ["b", "a"]), (h + ("qualifying",), ["c"]),
      (e + ("status",), "OK"), (e + ("sha256",), "A" * 64),
      (e + ("locator",), "/synthetic/absolute"), (e + ("locator",), "x/../y"),
      (e + ("locator",), "C:\\synthetic"), (e + ("locator",), "x\\..\\y"),
      (e + ("location",), {"line_range": [2, 1]}),
      (e + ("location",), {"line_range": [0, 1]}),
      (e + ("location",), {"json_pointer": "/bad~2escape"}),
      (e + ("location",), {"json_pointer": "no slash"}),
      (c + ("rationale",), " "), (c + ("rationale",), "\ud800"),
      (("provenance", "hanig-swarm"), "\udfff")])
    for pointer in ("", "/", "/a~0b~1c", "/a/b"):
      self.dossier["evidence"]["E001"]["location"] = {"json_pointer": pointer}
      P.validate_dossier(self.dossier)

  def test_reproduced_requires_complete_resolving_receipt(self):
    c = self.dossier["candidates"][0]
    c["basis"] = "reproduced"
    reproduction = {"receipt_ref": "E001", "tested_version": "local synthetic modification",
            "probe": "synthetic check", "observed": "synthetic result"}
    for value in (None, {}, dict(reproduction, receipt_ref="E999")):
      c["reproduction"] = value
      with self.subTest(value=value), self.assertRaises(S.SchemaError):
        P.validate_dossier(self.dossier)
    for key in reproduction:
      c["reproduction"] = dict(reproduction)
      del c["reproduction"][key]
      with self.subTest(missing=key), self.assertRaises(S.SchemaError):
        P.validate_dossier(self.dossier)
    c["reproduction"] = reproduction
    packet = P.compile_packet(self.dossier, self.catalog)
    self.assertEqual(packet["learnings"][0]["mechanism_basis"], "reproduced")
    for basis in ("reported", "inferred"):
      c["basis"] = basis
      with self.assertRaises(S.SchemaError):
        P.validate_dossier(self.dossier)

  def test_packet_ids_lists_and_enums(self):
    l = ("learnings", 0)
    self.reject_changes(self.packet, P.validate_packet, [
      (("schema_version",), "debrief_packet.v2"), (("project",), "private project"),
      (("catalog_version",), 2), (("packet_id",), "00000000-0000-1000-8000-000000000000"),
      (("packet_id",), self.packet["packet_id"].upper()),
      (("learnings",), []), (("learnings",), {}),
      (l + ("id",), "L02"), (l + ("mechanism_basis",), "verified"),
      (l + ("evidence_refs",), []), (l + ("evidence_refs",), ["E000"]),
      (l + ("counts", 0, "cohort_ref"), "C000")])
    self.dossier["candidates"] *= S.MAX_LEARNINGS
    packet = P.compile_packet(self.dossier, self.catalog)
    self.assertEqual(packet["learnings"][-1]["id"], "L50")
    self.dossier["candidates"].append(copy.deepcopy(self.dossier["candidates"][0]))
    with self.assertRaises(S.SchemaError):
      P.compile_packet(self.dossier, self.catalog)

  def test_digest_binds_all_public_fields_and_envelope(self):
    title, _ = P.render(self.packet)
    digest = P.approval_digest(self.packet, title)
    envelope = {"packet": self.packet, "title": title, "repository": S.REPOSITORY,
          "label": S.LABEL, "renderer_version": S.RENDERER_VERSION}
    self.assertEqual(digest, hashlib.sha256(json.dumps(envelope, sort_keys=True,
      separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest())
    def leaves(value, path=()):
      if type(value) is dict:
        for key, child in value.items():
          yield from leaves(child, path + (key,))
      elif type(value) is list:
        for i, child in enumerate(value):
          yield from leaves(child, path + (i,))
      else:
        yield path, value
    for path, value in leaves(self.packet):
      changed = copy.deepcopy(self.packet)
      node = changed
      for key in path[:-1]:
        node = node[key]
      node[path[-1]] = value + 1 if type(value) is int else value + "changed"
      with self.subTest(path=path):
        self.assertNotEqual(P.approval_digest(changed, title), digest)
    self.assertNotEqual(P.approval_digest(self.packet, title + "changed"), digest)
    for name, value in (("REPOSITORY", "fixture/other"), ("LABEL", "other"), ("RENDERER_VERSION", 2)):
      with self.subTest(name=name), patch.object(S, name, value):
        self.assertNotEqual(P.approval_digest(self.packet, title), digest)

  def test_parse_body_serialization_differences_only(self):
    _, body = P.render(self.packet)
    reordered = dict(reversed(list(self.packet.items())))
    variants = [body.replace("\n", "\r\n"), S.PREAMBLE + "\n\n```json\n" +
          json.dumps(reordered, indent=4) + "\n```"]
    for variant in variants:
      self.assertEqual(P.parse_body(variant), self.packet)
    for variant in (body + "trailing", body + "\n", body + "\n```json\n{}\n```",
            body + "\n" + body, "prefix" + body, body.replace("```json", "```JSON"),
            body.replace(S.PREAMBLE, "Other preamble"), body.replace("\n", "\r"),
            body.replace('"numerator":', '"numerator": 0, "numerator":'),
            body.replace('"numerator": 1', '"numerator": NaN')):
      with self.subTest(body=variant), self.assertRaises(S.SchemaError):
        P.parse_body(variant)

  def test_recount_rejects_both_directions(self):
    for numerator, denominator in ((0, 2), (2, 2), (1, 1), (1, 3)):
      packet = copy.deepcopy(self.packet)
      packet["learnings"][0]["counts"][0].update(numerator=numerator, denominator=denominator)
      P.validate_packet(packet)
      with self.subTest(n=numerator, d=denominator), self.assertRaisesRegex(
          S.SchemaError, "cohort count mismatch"):
        P.recount(self.dossier, packet)
    for key, value in (("numerator", 0), ("denominator", 3)):
      bad = copy.deepcopy(self.dossier)
      bad["candidates"][0]["counts"][0][key] = value
      with self.assertRaisesRegex(S.SchemaError, "cohort count mismatch"):
        P.validate_dossier(bad)

  def test_no_network_or_subprocess_imports(self):
    forbidden = {"socket", "ssl", "http", "urllib", "ftplib", "smtplib", "asyncio", "subprocess"}
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
      if isinstance(node, ast.Import):
        names = [alias.name for alias in node.names]
      elif isinstance(node, ast.ImportFrom):
        names = [node.module or ""]
      else:
        continue
      self.assertFalse(forbidden.intersection(name.split(".")[0] for name in names))


if __name__ == "__main__":
  unittest.main()
