import ast, copy, hashlib, json, sys, subprocess, tempfile, unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills/hanig-debrief/scripts/debrief_packet.py"
sys.path.insert(0, str(SCRIPT.parent))
import debrief_packet as P
import debrief_schema as S


def dossier():
  return {"schema_version": S.DOSSIER_SCHEMA, "evidence": {"E001": {
    "source_root": "root", "locator": "event.json", "sha256": "a" * 64,
    "location": {"line_range": [1, 2]}, "unit_id": None, "attempt_id": "attempt",
    "adapter": {"name": "fixture", "version": "one"}, "status": "ok"}},
    "cohorts": {"C001": {"population": "attempts", "members": ["a", "b"], "qualifying": ["a"]}},
    "candidates": [{"catalog_entry": "DB001", "status": "selected", "basis": "reported",
    "rationale": "private", "evidence_refs": ["E001"], "counts": [{
    "measure": "affected", "population": "attempts", "scope": S.COUNT_SCOPE,
    "numerator": 1, "denominator": 2, "cohort_ref": "C001"}], "reproduction": None,
    "coverage_disposition": "unknown"}],
    "omissions": [{"source_root": "root", "locator": ".", "reason": "budget"}],
    "provenance": {"hanig-swarm": "marker"}}


class TestDebriefPacket(unittest.TestCase):
  def setUp(self):
    temp = tempfile.TemporaryDirectory()
    self.addCleanup(temp.cleanup)
    self.root = Path(temp.name)
    self.c = S.load_catalog()
    self.d = dossier()
    self.p = P.compile_packet(self.d, self.c)

  def refuses(self):
    return self.assertRaises(S.SchemaError)

  def bad(self, base, validator, changes):
    for path, new in changes:
      v = copy.deepcopy(base)
      node = v
      for key in path[:-1]:
        node = node[key]
      node[path[-1]] = new
      with self.subTest(path=path, v=new), self.refuses():
        validator(v)

  def test_nonselected_duplicate_measures(self):
    for status in ('rejected', 'covered', 'unrepresentable'):
      d = dossier()
      candidate = d['candidates'][0]
      candidate['status'] = status
      candidate['counts'] *= 2
      with self.subTest(status=status):
        self.assertEqual(P.validate_dossier(d), d)
        # Recount/type guards remain active even for nonselected evidence.
        candidate['counts'][1] = dict(candidate['counts'][1], numerator=True)
        with self.refuses():
          P.validate_dossier(d)

  def test_selected_duplicate_measures_refused(self):
    self.d['candidates'][0]['counts'] *= 2
    with self.assertRaisesRegex(S.SchemaError, 'duplicate measure'):
      P.validate_dossier(self.d)
    self.p['learnings'][0]['counts'] *= 2
    with self.assertRaisesRegex(S.SchemaError, 'duplicate measure'):
      P.validate_packet(self.p)

  def test_round_trip_and_fresh_containers(self):
    old = copy.deepcopy(self.d)
    self.assertEqual(P.validate_dossier(self.d), self.d)
    self.assertEqual(P.validate_packet(self.p, self.c), self.p)
    self.assertEqual(P.recount(self.d, self.p), self.p)
    title, body = P.render(self.p)
    self.assertEqual(title, S.TITLE_PREFIX + self.p["packet_id"])
    self.assertEqual(body, S.PREAMBLE + "\n\n```json\n" + json.dumps(
      self.p, sort_keys=True, indent=2, ensure_ascii=False) + "\n```")
    self.assertEqual(P.parse_body(body), self.p)
    self.assertNotEqual(P.compile_packet(self.d, self.c)["packet_id"],
      self.p["packet_id"])
    self.p["learnings"][0]["counts"][0]["numerator"] = 0
    self.p["learnings"][0]["evidence_refs"].clear()
    self.assertEqual(self.d, old)

  def test_public_boundary_fixtures(self):
    sentinels = ["PRIVATE_PROJECT", "/private/fixture",
      "Synthetic scientific claim.",
      "sk-FAKE-TEST-KEY"]
    for secret in sentinels:
      d = dossier()
      e = d["evidence"]["E001"]
      for key in ("source_root", "unit_id", "attempt_id"):
        e[key] = secret
      e["locator"] = "relative/" + secret
      e["location"] = {"json_pointer": "/" + secret}
      e["adapter"] = {"name": secret, "version": secret}
      d["cohorts"]["C001"].update(members=[secret + "a", secret + "b"],
        qualifying=[secret + "a"])
      c = d["candidates"][0]
      c.update(rationale=secret, basis="reproduced", reproduction={
        "receipt_ref": "E001", "tested_version": secret,
        "probe": secret, "observed": secret})
      d["omissions"] = [{"source_root": secret, "locator": "relative/" + secret,
        "reason": secret}]
      d["provenance"]["hanig-swarm"] = secret
      pkt = P.compile_packet(d, self.c)
      title, body = P.render(pkt)
      with self.subTest(secret=secret):
        self.assertNotIn(secret, title + body)
        self.assertEqual(pkt["learnings"][0]["mechanism"],
          self.c["entries"][0]["mechanism"])
        self.assertEqual(P.recount(d, pkt), pkt)

  def test_selected_and_sequential_ids(self):
    for status in ("covered", "unrepresentable", "rejected"):
      cand = copy.deepcopy(self.d["candidates"][0])
      cand.update(status=status, catalog_entry=None, counts=[], evidence_refs=[])
      self.d["candidates"].insert(0, cand)
    self.d["candidates"].append(copy.deepcopy(self.d["candidates"][-1]))
    pkt = P.compile_packet(self.d, self.c)
    self.assertEqual([x["id"] for x in pkt["learnings"]], ["L01", "L02"])
    self.d["candidates"] = []
    self.assertEqual(P.validate_dossier(self.d), self.d)
    with self.refuses():
      P.compile_packet(self.d, self.c)

  def test_unknown_and_missing_fields(self):
    def object_paths(v, path=()):
      if type(v) is dict:
        yield path
        for key, child in v.items():
          yield from object_paths(child, path + (key,))
      elif type(v) is list:
        for index, child in enumerate(v):
          yield from object_paths(child, path + (index,))
    for base, check in ((self.d, P.validate_dossier),
      (self.p, P.validate_packet)):
      for path in object_paths(base):
        for extra in (True, False):
          v = copy.deepcopy(base)
          node = v
          for key in path:
            node = node[key]
          if extra:
            node[None if path == ("provenance",) else "unexpected"] = "synthetic"
          elif path not in (("provenance",), ("evidence",), ("cohorts",)):
            del node[next(iter(node))]
          else:
            continue
          with self.subTest(path=path, extra=extra), self.refuses():
            check(v)

  def test_duplicate_keys_in_loaders(self):
    for v, loader in ((self.d, P.load_dossier), (self.p, P.load_packet)):
      raw = json.dumps(v)
      for key in ("schema_version", "numerator"):
        duplicate = raw.replace('"%s":' % key, '"%s": null, "%s":' % (key, key))
        path = self.root / "duplicate.json"
        path.write_text(duplicate, encoding="utf-8")
        with self.subTest(key=key), self.assertRaisesRegex(S.SchemaError, "duplicate key"):
          loader(path)
      path.write_text(raw, encoding="utf-8")
      self.assertEqual(loader(path), v)

  def test_integer_types_and_bounds(self):
    for base, pre, check in ((self.d, ("candidates", 0), P.validate_dossier),
      (self.p, ("learnings", 0), P.validate_packet)):
      changes = [(pre + ("counts", 0, key), v)
        for key in ("numerator", "denominator")
        for v in (True, False, 1.0, "1", None, -1, S.MAX_DENOMINATOR + 1)]
      changes += [(pre + ("counts", 0, "numerator"), 3),
        (pre + ("counts", 0, "denominator"), 0)]
      self.bad(base, check, changes)
    self.bad(self.p, P.validate_packet, [(('catalog_version',), True)])
    self.bad(self.d, P.validate_dossier,
      [(('evidence', 'E001', 'location', 'line_range'), [True, 2])])
    for num, den in ((0, 1), (1, 1), (0, S.MAX_DENOMINATOR),
      (S.MAX_DENOMINATOR, S.MAX_DENOMINATOR)):
      self.p["learnings"][0]["counts"][0].update(numerator=num, denominator=den)
      P.validate_packet(self.p)

  def test_catalog_whole_tuple_exactness(self):
    lesson = ("learnings", 0)
    changes = [(lesson + ("catalog_entry",), "DB999")]
    for key in ("skill", "what_broke", "mechanism", "proposed_change"):
      v = self.p["learnings"][0][key]
      changes.extend((lesson + (key,), alt) for alt in
        (v.upper(), " " + v, v + " ", v.replace("a", "а")))
    self.bad(self.p, P.validate_packet, changes)
    mixed = copy.deepcopy(self.p)
    mixed["learnings"][0]["mechanism"] = self.c["entries"][1]["mechanism"]
    with self.refuses():
      P.validate_packet(mixed)
    self.bad(self.d, P.validate_dossier,
      [(("candidates", 0, "catalog_entry"), "DB999")])

  def test_count_pairs_and_duplicate_measures(self):
    for base, pre, check in ((self.d, ("candidates", 0), P.validate_dossier),
      (self.p, ("learnings", 0), P.validate_packet)):
      cnt = copy.deepcopy(base[pre[0]][0]["counts"][0])
      self.bad(base, check, [
        (pre + ("counts",), [cnt, cnt]),
        (pre + ("counts", 0, "population"), "units"),
        (pre + ("counts", 0, "measure"), "unpermitted"),
        (pre + ("counts", 0, "scope"), "all")])
    self.d["candidates"][0]["counts"] = []
    pkt = P.compile_packet(self.d, self.c)
    self.assertEqual(pkt["learnings"][0]["counts"], [])

  def test_private_reference_refusals(self):
    c = ("candidates", 0)
    e = ("evidence", "E001")
    h = ("cohorts", "C001")
    self.bad(self.d, P.validate_dossier, [
      (("schema_version",), "debrief_dossier.v3"), (c + ("status",), "SELECTED"),
      (c + ("basis",), "approved"), (c + ("coverage_disposition",), "maybe"),
      (c + ("catalog_entry",), None), (c + ("evidence_refs",), [None]),
      (c + ("evidence_refs",), ["E999"]), (c + ("evidence_refs",), "E001"),
      (c + ("counts", 0, "cohort_ref"), "C999"),
      (h + ("population",), "units"), (h + ("members",), ["a", "a"]),
      (h + ("members",), ["b", "a"]), (h + ("qualifying",), ["c"]),
      (e + ("status",), "OK"), (e + ("sha256",), "A" * 64),
      (e + ("locator",), "/synthetic/absolute"), (e + ("locator",), "x/../y"),
      (e + ("locator",), "//synthetic"), (e + ("locator",), "x/../y/."),
      (e + ("location",), {"line_range": [2, 1]}),
      (e + ("location",), {"line_range": [0, 1]}),
      (e + ("location",), {"json_pointer": "/bad~2escape"}),
      (e + ("location",), {"json_pointer": "no slash"}),
      (c + ("rationale",), " "), (c + ("rationale",), "\ud800"),
      (("provenance", "hanig-swarm"), "\udfff")])
    for pointer in ("", "/", "/a~0b~1c", "/a/b"):
      self.d["evidence"]["E001"]["location"] = {"json_pointer": pointer}
      P.validate_dossier(self.d)

  def test_reproduced_requires_receipt(self):
    c = self.d["candidates"][0]
    c["basis"] = "reproduced"
    repro = {"receipt_ref": "E001", "tested_version": "local fixture",
      "probe": "probe", "observed": "result"}
    for v in (None, {}, dict(repro, receipt_ref="E999")):
      c["reproduction"] = v
      with self.subTest(v=v), self.refuses():
        P.validate_dossier(self.d)
    for key in repro:
      c["reproduction"] = dict(repro)
      del c["reproduction"][key]
      with self.subTest(missing=key), self.refuses():
        P.validate_dossier(self.d)
    c["reproduction"] = repro
    pkt = P.compile_packet(self.d, self.c)
    self.assertEqual(pkt["learnings"][0]["mechanism_basis"], "reproduced")
    for basis in ("reported", "inferred"):
      c["basis"] = basis
      with self.refuses():
        P.validate_dossier(self.d)

  def test_packet_grammar(self):
    l = ("learnings", 0)
    self.bad(self.p, P.validate_packet, [
      (("schema_version",), "debrief_packet.v3"), (("project",), "private"),
      (("catalog_version",), 2), (("packet_id",), "00000000-0000-1000-8000-000000000000"),
      (("packet_id",), self.p["packet_id"].upper()),
      (("learnings",), []), (("learnings",), {}),
      (l + ("id",), "L02"), (l + ("mechanism_basis",), "verified"),
      (l + ("evidence_refs",), []), (l + ("evidence_refs",), ["E000"]),
      (l + ("counts", 0, "cohort_ref"), "C000")])
    self.d["candidates"] *= S.MAX_LEARNINGS
    pkt = P.compile_packet(self.d, self.c)
    self.assertEqual(pkt["learnings"][-1]["id"], "L50")
    self.d["candidates"].append(copy.deepcopy(self.d["candidates"][0]))
    with self.refuses():
      P.compile_packet(self.d, self.c)

  def test_digest_binds_all_public_fields_and_envelope(self):
    title, _ = P.render(self.p)
    digest = P.approval_digest(self.p, title)
    envelope = {"packet": self.p, "title": title, "repository": S.REPOSITORY,
      "label": S.LABEL, "renderer_version": S.RENDERER_VERSION}
    self.assertEqual(digest, hashlib.sha256(json.dumps(envelope, sort_keys=True,
      separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest())
    def leaves(v, path=()):
      if type(v) is dict:
        for key, child in v.items():
          yield from leaves(child, path + (key,))
      elif type(v) is list:
        for i, child in enumerate(v):
          yield from leaves(child, path + (i,))
      else:
        yield path, v
    for path, v in leaves(self.p):
      changed = copy.deepcopy(self.p)
      node = changed
      for key in path[:-1]:
        node = node[key]
      node[path[-1]] = v + 1 if type(v) is int else v + "changed"
      with self.subTest(path=path):
        self.assertNotEqual(P.approval_digest(changed, title), digest)
    self.assertNotEqual(P.approval_digest(self.p, title + "changed"), digest)
    for name, v in (("REPOSITORY", "fixture/other"), ("LABEL", "other"), ("RENDERER_VERSION", 2)):
      with self.subTest(name=name), patch.object(S, name, v):
        self.assertNotEqual(P.approval_digest(self.p, title), digest)

  def test_parse_body_serialization_differences_only(self):
    _, body = P.render(self.p)
    reordered = dict(reversed(list(self.p.items())))
    variants = [body.replace("\n", "\r\n"), S.PREAMBLE + "\n\n```json\n" +
      json.dumps(reordered, indent=4) + "\n```"]
    for alt in variants:
      self.assertEqual(P.parse_body(alt), self.p)
    for alt in (body + "trailing", body + "\n", body + "\n```json\n{}\n```",
      body + "\n" + body, "prefix" + body, body.replace("```json", "```JSON"),
      body.replace(S.PREAMBLE, "Other preamble"), body.replace("\n", "\r"),
      body.replace('"numerator":', '"numerator": 0, "numerator":'),
      body.replace('"numerator": 1', '"numerator": NaN')):
      with self.subTest(body=alt), self.refuses():
        P.parse_body(alt)

  def test_recount_rejects_both_directions(self):
    for num, den in ((0, 2), (2, 2), (1, 1), (1, 3)):
      pkt = copy.deepcopy(self.p)
      pkt["learnings"][0]["counts"][0].update(numerator=num, denominator=den)
      P.validate_packet(pkt)
      with self.subTest(n=num, d=den), self.assertRaisesRegex(
        S.SchemaError, "cohort count mismatch"):
        P.recount(self.d, pkt)
    for key, v in (("numerator", 0), ("denominator", 3)):
      bad = copy.deepcopy(self.d)
      bad["candidates"][0]["counts"][0][key] = v
      with self.assertRaisesRegex(S.SchemaError, "cohort count mismatch"):
        P.validate_dossier(bad)

  def test_private_provenance_is_not_an_install_inventory(self):
    for marker in ("v1", "", " \n"):
      self.d["provenance"] = {"absent-skill": marker}
      pkt = P.compile_packet(self.d, self.c)
      self.assertNotIn("absent-skill", P.render(pkt)[1])

  def test_count_labels_come_from_catalog(self):
    cnt = self.d["candidates"][0]["counts"][0]
    pair = self.c["entries"][0]["measures"][0]
    for key in ("measure", "population"):
      cnt[key] = "".join(list(pair[key]))
      self.assertIsNot(cnt[key], pair[key])
    public = P.compile_packet(self.d, self.c)["learnings"][0]["counts"][0]
    for key in ("measure", "population"):
      self.assertIs(public[key], pair[key])

  def test_parser_refuses_alternate_scalar_encodings(self):
    self.p["learnings"][0]["counts"][0]["numerator"] = 0
    _, body = P.render(self.p)
    for old, new in (("debrief_packet.v2", r"debrief_packet.v\u0032"),
      ("project A", r"project\u0020A"),
      ('"id":', r'"\u0069d":'),
      ('"numerator": 0', '"numerator": -0')):
      with self.subTest(new=new), self.refuses():
        P.parse_body(body.replace(old, new))
    self.c["entries"][0]["mechanism"] = 'Quoted "text", backslash \\, newline\n, café /'
    pkt = P.compile_packet(self.d, self.c)
    _, body = P.render(pkt, self.c)
    self.assertEqual(P.parse_body(body, self.c), pkt)
    for changed in (body.replace("é", r"\u00e9"), body.replace(" /", r" \/")):
      with self.refuses():
        P.parse_body(changed, self.c)

  def test_posix_locators_and_private_string_ids(self):
    for name in ("x:report", "C:\\synthetic", "x\\..\\y"):
      path = self.root / name
      path.write_text("a\nb\n")
      self.d["evidence"]["E001"].update(locator=name, sha256=hashlib.sha256(path.read_bytes()).hexdigest())
      P.compile_packet(self.d, self.c)
    for member in ("", " "):
      self.d["cohorts"]["C001"].update(members=[member, "b"], qualifying=[member])
      pkt = P.compile_packet(self.d, self.c)
      self.assertEqual(P.recount(self.d, pkt), pkt)

  def test_private_candidate_need_not_be_publishable(self):
    self.d["candidates"][0]["evidence_refs"] = []
    for null in (False, True):
      if null:
        self.d["candidates"][0].update(catalog_entry=None, counts=[])
      self.assertEqual(P.validate_dossier(self.d), self.d)
      with self.refuses():
        P.compile_packet(self.d, self.c)

  def test_invalid_pointer_refusal_is_bounded(self):
    code = "from tests.test_debrief_packet import dossier,P,S; import unittest\nd=dossier()\nd['evidence']['E001']['location']={'json_pointer':'/'*32+'~'}\nwith unittest.TestCase().assertRaises(S.SchemaError): P.validate_dossier(d)"
    subprocess.run([sys.executable, "-c", code], cwd=ROOT, check=True,
                   capture_output=True, timeout=2)

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
