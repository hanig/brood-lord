import ast
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills/hanig-debrief/scripts/debrief_triage.py"
sys.path.insert(0, str(SCRIPT.parent))
import debrief_schema as S
import debrief_triage as T


class TestDebriefTriage(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        entry = S.load_catalog()["entries"][0]
        learning = {key: entry[key] for key in
                    ("skill", "what_broke", "mechanism", "proposed_change")}
        learning.update(catalog_entry=entry["id"], mechanism_basis="reported",
                        counts=[], evidence_refs=["E001"])
        self.packet = {"schema_version": S.PACKET_SCHEMA, "catalog_version": 1,
                       "packet_id": "00000000-0000-4000-8000-000000000001",
                       "project": "project A", "learnings":
                       [dict(learning, id="L%02d" % n) for n in range(1, 4)]}
        self.record = {"schema_version": S.TRIAGE_SCHEMA,
                       "packet_id": self.packet["packet_id"],
                       "packet_digest": self.digest(),
                       "issue": {"repository": "hanig/brood-lord", "number": 1},
                       "dispositions": {
                           "L01": {"disposition": "accept", "unit_refs": ["unit-a", "unit-b"]},
                           "L02": self.rejection("insufficient_evidence"),
                           "L03": self.rejection("already_addressed", ["docs/guide.md"])}}
        self.record_path = self.root / "debrief/triage" / (self.packet["packet_id"] + ".json")
        self.record_path.parent.mkdir(parents=True)
        self.packet_path = self.root / "packet.json"
        self.plan_paths = [self.root / "plan-a.json", self.root / "plan-b.json"]
        for path, unit_id in zip(self.plan_paths, ("unit-a", "unit-b")):
            self.write(path, {"name": "synthetic", "units": [{"id": unit_id}]})
        self.save()

    @staticmethod
    def rejection(code, refs=None):
        return {"disposition": "reject", "reason_code": code,
                "rationale": "Synthetic reason.", "references": [] if refs is None else refs}

    @staticmethod
    def write(path, value):
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def digest(self):
        return hashlib.sha256(json.dumps(self.packet, sort_keys=True,
            separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()

    def save(self):
        self.write(self.record_path, self.record)
        self.write(self.packet_path, self.packet)

    def argv(self):
        args = ["validate", "--record", str(self.record_path), "--packet", str(self.packet_path)]
        for path in self.plan_paths:
            args.extend(["--plan", str(path)])
        return args

    def run_cli(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = T.main(self.argv())
        return code, out.getvalue(), err.getvalue()

    def refuses(self, message, save=True):
        if save:
            self.save()
        code, out, err = self.run_cli()
        self.assertEqual(code, 1, (out, err))
        self.assertEqual(out, "")
        self.assertRegex(err, message)

    def test_complete_record_passes_with_multiple_plans(self):
        self.assertEqual(self.run_cli(), (0, "triage valid\n", ""))

    def test_missing_learning(self):
        del self.record["dispositions"]["L02"]
        self.refuses("missing learning: L02")

    def test_duplicate_disposition(self):
        raw = json.dumps(self.record)
        self.record_path.write_text(raw.replace('"L02":',
            '"L02": {"disposition": "accept", "unit_refs": ["unit-a"]}, "L02":'),
            encoding="utf-8")
        self.refuses("L02: duplicate key", save=False)

    def test_unknown_learning(self):
        self.record["dispositions"]["L04"] = self.rejection("out_of_scope")
        self.refuses("unknown learning id: L04")

    def test_accept_without_unit_refs(self):
        row = self.record["dispositions"]["L01"]
        for value in ([], "unit-a", None, [""], [True], [" "]):
            row["unit_refs"] = value
            with self.subTest(value=value):
                self.refuses("unit_refs")
        del row["unit_refs"]
        self.refuses("unit_refs.*missing field")

    def test_unknown_unit_ref(self):
        for ref in ("absent", "UNIT-A", " unit-a", "unit-a "):
            self.record["dispositions"]["L01"]["unit_refs"] = [ref]
            with self.subTest(ref=ref):
                self.refuses("unknown unit ref")

    def test_reject_without_rationale(self):
        row = self.record["dispositions"]["L02"]
        for value in ("", " \n", None, True, "\ud800"):
            row["rationale"] = value
            # JSON escapes surrogates so this remains a well-formed fixture file.
            self.record_path.write_text(json.dumps(self.record), encoding="utf-8")
            with self.subTest(value=value):
                self.refuses("rationale", save=False)
        del row["rationale"]
        self.refuses("rationale.*missing field")

    def test_already_addressed_without_reference(self):
        self.record["dispositions"]["L03"]["references"] = []
        self.refuses("already_addressed requires references")

    def test_rejection_codes_and_optional_references(self):
        for code in S.REJECTION_CODES:
            if code == "already_addressed":
                continue
            self.record["dispositions"]["L02"] = self.rejection(code)
            self.save()
            with self.subTest(code=code):
                self.assertEqual(self.run_cli()[0], 0)
        for code in ("unknown", "INSUFFICIENT_EVIDENCE", "local_modification ", None, []):
            self.record["dispositions"]["L02"]["reason_code"] = code
            with self.subTest(code=code):
                self.refuses("unknown reason code")

    def test_digest_mismatch(self):
        self.record["packet_digest"] = "a" * 64
        self.refuses("packet digest mismatch")

    def test_packet_id_mismatch(self):
        self.record["packet_id"] = "00000000-0000-4000-8000-000000000002"
        self.refuses("packet id mismatch")

    def test_filename_mismatch(self):
        self.record_path = self.record_path.with_name("wrong.json")
        self.refuses("file-name mismatch")

    def test_invalid_packet_even_with_matching_digest(self):
        for key, value in (("project", "private"), ("catalog_version", True),
                           ("schema_version", "debrief_packet.v2"), ("learnings", [])):
            original = self.packet[key]
            self.packet[key] = value
            self.record["packet_digest"] = self.digest()
            with self.subTest(key=key):
                self.refuses("packet")
            self.packet[key] = original

    def test_exact_fields_at_every_record_level(self):
        base = copy.deepcopy(self.record)
        for path in ((), ("issue",), ("dispositions", "L01"), ("dispositions", "L02")):
            node = base
            for key in path:
                node = node[key]
            for key in list(node) + ["unexpected"]:
                self.record = copy.deepcopy(base)
                target = self.record
                for component in path:
                    target = target[component]
                if key == "unexpected":
                    target[key] = "extra"
                else:
                    del target[key]
                with self.subTest(path=path, key=key):
                    self.refuses("triage")

    def test_wrong_types_and_versions(self):
        base = copy.deepcopy(self.record)
        changes = [((), []), (("schema_version",), "debrief_triage.v2"),
                   (("packet_digest",), "A" * 64), (("issue",), []),
                   (("issue", "repository"), "other/repo"),
                   (("dispositions",), []), (("dispositions", "L02"), []),
                   (("dispositions", "L02", "references"), "docs/guide.md"),
                   (("dispositions", "L02", "disposition"), "ACCEPT")]
        changes += [(("issue", "number"), value) for value in (True, 0, -1, 1.0, "1", None)]
        for path, value in changes:
            self.record = copy.deepcopy(base)
            if not path:
                self.record = value
            else:
                node = self.record
                for key in path[:-1]:
                    node = node[key]
                node[path[-1]] = value
            with self.subTest(path=path, value=value):
                self.refuses("triage")

    def test_reference_form(self):
        row = self.record["dispositions"]["L03"]
        valid = ["docs/triage guide.md", "docs/café.md", "README.md",
                 "https://github.com/hanig/brood-lord/issues/1",
                 "https://github.com/hanig/brood-lord/pull/2#discussion",
                 "https://github.com/hanig/brood-lord/commit/" + "a" * 40]
        row["references"] = valid
        self.save()
        self.assertEqual(self.run_cli()[0], 0)
        for ref in ("", " ", None, "/absolute", "../other", "docs/../x", "docs//x",
                    "https://example.invalid/other", "http://github.com/hanig/brood-lord/issues/1",
                    "https://github.com/hanig/brood-lord-else/issues/1",
                    "https://github.com/hanig/brood-lord/../other", "docs/\x00",
                    "https://github.com/hanig/brood-lord/pull/1 "):
            row["references"] = [ref]
            with self.subTest(ref=ref):
                self.refuses("references")

    def test_plan_shape_and_empty_plan(self):
        original = self.plan_paths[0].read_text()
        for plan in ([], {}, {"units": "unit-a"}, {"units": {}}, {"units": ["unit-a"]},
                     {"units": [{}]}, {"units": [{"id": True}]}, {"units": [{"id": " "}]}):
            self.write(self.plan_paths[0], plan)
            with self.subTest(plan=plan):
                self.refuses("plans", save=False)
        self.plan_paths[0].write_text(original)
        self.record["dispositions"]["L01"] = self.rejection("local_modification")
        for path in self.plan_paths:
            self.write(path, {"units": []})
        self.save()
        self.assertEqual(self.run_cli()[0], 0)

    def test_duplicate_keys_in_all_inputs_and_nested_rows(self):
        for path, key in ((self.packet_path, "packet_id"), (self.plan_paths[0], "units"),
                          (self.record_path, "disposition"), (self.record_path, "number")):
            original = path.read_text()
            path.write_text(original.replace('"%s":' % key,
                '"%s": null, "%s":' % (key, key)))
            with self.subTest(path=path.name, key=key):
                self.refuses("duplicate key", save=False)
            path.write_text(original)

    def test_invalid_or_missing_json_is_a_named_refusal(self):
        for raw in ("{", "NaN", "\ufeff{}"):
            self.record_path.write_text(raw, encoding="utf-8")
            with self.subTest(raw=raw):
                self.refuses("JSON", save=False)
        self.record_path.unlink()
        self.refuses("JSON", save=False)

    def test_canonical_digest_ignores_json_layout(self):
        self.packet_path.write_text(json.dumps(dict(reversed(list(self.packet.items()))),
            indent=4), encoding="utf-8")
        self.assertEqual(T.packet_digest(self.packet), self.digest())
        self.assertEqual(self.run_cli()[0], 0)

    def test_cli_writes_nothing_and_coverage_stays_identical(self):
        bundle = self.root / "skills/hanig-debrief"
        scripts = bundle / "scripts"
        scripts.mkdir(parents=True)
        for name in ("debrief_triage.py", "debrief_packet.py", "debrief_schema.py"):
            shutil.copyfile(SCRIPT.parent / name, scripts / name)
        # Install synthetic catalog/coverage and bundle markers, never a real project.
        catalog = copy.deepcopy(S.load_catalog())
        self.write(bundle / "catalog.json", catalog)
        self.write(bundle / "coverage.json", {"schema_version": S.COVERAGE_SCHEMA,
                                             "version": 1, "entries": []})
        for entry in catalog["entries"]:
            marker = self.root / "skills" / entry["skill"] / "SKILL.md"
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text("Synthetic bundle marker.\n")
        def snapshot():
            return {str(p.relative_to(self.root)): p.read_bytes() if p.is_file() else None
                    for p in self.root.rglob("*")}
        before = snapshot()
        env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
        result = subprocess.run([sys.executable, str(scripts / SCRIPT.name)] + self.argv(),
            cwd=self.root, env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual((result.returncode, result.stdout, result.stderr),
                         (0, "triage valid\n", ""))
        self.assertEqual(snapshot(), before)

    def test_no_network_or_subprocess(self):
        forbidden = {"socket", "ssl", "http", "urllib", "ftplib", "smtplib", "asyncio",
                     "subprocess"}
        tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
        imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.add((node.module or "").split(".")[0])
        self.assertFalse(imports & forbidden)
        self.assertLessEqual(imports, {"sys", "argparse", "hashlib", "json", "pathlib",
                                       "re", "debrief_packet", "debrief_schema"})
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = (node.func.id if isinstance(node.func, ast.Name) else
                        node.func.attr if isinstance(node.func, ast.Attribute) else "")
                self.assertNotIn(name, {"eval", "exec", "__import__", "system", "popen",
                                        "import_module", "spawn", "fork", "execv"})


if __name__ == "__main__":
    unittest.main()
