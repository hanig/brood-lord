"""decisions.tsv trail and durability triage for hanig-debrief (offline, stdlib only)."""
import ast
import copy
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import random
import stat
import sys
import tempfile
import unicodedata
import unittest
from contextlib import redirect_stderr, redirect_stdout

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills/hanig-debrief/scripts"
sys.path.insert(0, str(SCRIPTS))
import debrief_schema as S
import debrief_triage as T
import decisions_log as D

SANITIZE_SEED = 20261124
SANITIZE_CASES = 400
FORBIDDEN = {"socket", "ssl", "http", "urllib", "ftplib", "smtplib", "asyncio", "subprocess"}
NOW = datetime(2026, 11, 24, 12, 0, 0, tzinfo=timezone.utc)


def call(module, argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = module.main(argv)
    return code, out.getvalue(), err.getvalue()


class TempRoot(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        for parent in (self.root,) + tuple(self.root.parents):
            self.assertFalse(os.path.lexists(str(parent / ".git")),
                             "TMPDIR must lie outside every Git worktree")


class TestOfflineImports(unittest.TestCase):
    def test_no_network_or_subprocess_imports_or_calls(self):
        allowed = {"debrief_schema.py": {"json", "pathlib", "re"},
                   "debrief_triage.py": {"sys", "argparse", "hashlib", "json", "pathlib",
                                         "re", "debrief_packet", "debrief_schema"},
                   "decisions_log.py": {"argparse", "datetime", "fcntl", "hashlib", "json",
                                        "os", "pathlib", "re", "stat", "sys",
                                        "unicodedata"}}
        for name in sorted(allowed):
            tree = ast.parse((SCRIPTS / name).read_text(encoding="utf-8"))
            imports = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    imports.add((node.module or "").split(".")[0])
                elif isinstance(node, ast.Call):
                    func = node.func
                    called = (func.id if isinstance(func, ast.Name) else
                              func.attr if isinstance(func, ast.Attribute) else "")
                    self.assertNotIn(called, {"system", "popen", "spawn", "fork", "execv",
                                              "__import__", "import_module", "eval", "exec"})
            with self.subTest(name=name):
                self.assertTrue(imports)
                self.assertFalse(imports & FORBIDDEN)
                self.assertLessEqual(imports, allowed[name])


class TestDecisionsLog(TempRoot):
    def setUp(self):
        super().setUp()
        self.log = self.root / "private" / "trail" / "decisions.tsv"

    def add(self, claim="keep structure", **kw):
        return D.append(self.log, kw.pop("run_id", "run-1"), kw.pop("kind", "adopt"),
                        claim, now=NOW, **kw)

    def test_appends_are_byte_prefix_stable_and_correction_supersedes(self):
        snapshots = []
        for claim in ("first", "second", "third"):
            self.add(claim, evidence_ref="docs/x.md")
            snapshots.append(self.log.read_bytes())
        row = self.add("third, corrected", supersedes=3)
        snapshots.append(self.log.read_bytes())
        for older, newer in zip(snapshots, snapshots[1:]):
            self.assertTrue(newer.startswith(older))
            self.assertGreater(len(newer), len(older))
        self.assertTrue(snapshots[0].startswith(D.HEADER))
        self.assertEqual(snapshots[-1].count(D.HEADER), 1)
        self.assertEqual((row["seq"], row["supersedes"]), ("4", "3"))
        rows = D.load(self.log)
        self.assertEqual([r["claim"] for r in rows],
                         ["first", "second", "third", "third, corrected"])
        self.assertEqual(rows[0]["prev_sha256"], D.GENESIS)
        for prev, cur in zip(rows, rows[1:]):
            self.assertEqual(cur["prev_sha256"], prev["row_sha256"])
        code, out, err = call(D, ["show", "--log", str(self.log), "--json"])
        self.assertEqual((code, err), (0, ""))
        shown = json.loads(out)
        self.assertEqual(shown["entries"][2]["superseded_by"], [4])
        self.assertIn("NOT cryptographic", shown["integrity"])

    def test_private_modes_on_creation(self):
        old = os.umask(0o022)
        try:
            self.add()
        finally:
            os.umask(old)
        self.assertEqual(stat.S_IMODE(self.log.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.log.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.log.parent.parent.stat().st_mode), 0o700)

    def test_refuses_paths_inside_git_worktrees(self):
        repo = self.root / "repo"
        (repo / ".git").mkdir(parents=True)
        linked = self.root / "linked"
        linked.mkdir()
        (linked / ".git").write_text("gitdir: synthetic\n")
        alias = self.root / "alias"
        alias.symlink_to(repo, target_is_directory=True)
        targets = [repo / "decisions.tsv", repo / "deep/new/decisions.tsv",
                   linked / "decisions.tsv", alias / "nested/decisions.tsv"]
        for target in targets:
            for command in (["append", "--run-id", "r", "--kind", "k", "--claim", "c"],
                            ["verify"], ["show"]):
                with self.subTest(target=str(target), command=command[0]):
                    code, _, err = call(D, command + ["--log", str(target)])
                    self.assertEqual(code, 1)
                    self.assertIn("inside a Git worktree", err)
            self.assertFalse(os.path.lexists(str(target)))
        self.assertFalse((repo / "deep").exists())
        with self.assertRaises(SystemExit) as ctx, redirect_stderr(io.StringIO()):
            D.main(["append", "--run-id", "r", "--kind", "k", "--claim", "c"])
        self.assertEqual(ctx.exception.code, 2)
        with self.assertRaises(D.LogError):
            D.checked_path(None)

    def test_refuses_symlinked_log_file(self):
        real = self.root / "elsewhere.tsv"
        self.add()
        self.log.rename(real)
        self.log.symlink_to(real)
        before = real.read_bytes()
        with self.assertRaises(D.LogError):
            self.add("again")
        self.assertEqual(real.read_bytes(), before)

    def test_verify_detects_edits_deletions_reorders_and_partial_rows(self):
        for claim in ("alpha", "beta", "gamma"):
            self.add(claim)
        good = self.log.read_bytes()
        head = D.load(self.log)[-1]["row_sha256"]
        lines = good.split(b"\n")
        header, rows = lines[0] + b"\n", [line + b"\n" for line in lines[1:-1]]
        tampered = {
            "edit first claim": good.replace(b"alpha", b"alphA"),
            "edit last claim": good.replace(b"gamma", b"gammA"),
            "edit timestamp": good.replace(b"2026-11-24T12:00:00Z", b"2026-11-24T12:00:01Z", 1),
            "delete middle": header + rows[0] + rows[2],
            "delete first": header + rows[1] + rows[2],
            "reorder": header + rows[1] + rows[0] + rows[2],
            "partial row": good[:-5],
            "torn header": good[3:],
            "extra cell": good[:-1] + b"\tx\n",
            "control char": good.replace(b"beta", b"be\x1bta"),
        }
        for label, data in tampered.items():
            with self.subTest(label=label):
                self.log.write_bytes(data)
                code, _, err = call(D, ["verify", "--log", str(self.log)])
                self.assertEqual(code, 1, label)
                self.assertTrue(err.startswith("decisions log: "))
                with self.assertRaises(D.LogError):
                    self.add("refused")
                self.assertEqual(self.log.read_bytes(), data)
        self.log.write_bytes(header + rows[0] + rows[1])
        self.assertEqual(call(D, ["verify", "--log", str(self.log)])[0], 0)
        code, _, err = call(D, ["verify", "--log", str(self.log), "--expect-head", head])
        self.assertEqual(code, 1)
        self.assertIn("expected head not found", err)
        self.log.write_bytes(good)
        code, out, _ = call(D, ["verify", "--log", str(self.log), "--expect-head", head,
                                "--json"])
        self.assertEqual(code, 0)
        result = json.loads(out)
        self.assertEqual((result["rows"], result["head_sha256"]), (3, head))
        self.assertIn("same-UID", result["integrity"])

    def test_sanitizes_delimiters_and_control_format_chars(self):
        hostile = "a\tb\nc\rd​e\U000E0041f g hi͸j\ud800k\x00l"
        row = self.add(hostile, run_id="r\tun", kind="k\nind", evidence_ref="e‮f")
        self.assertEqual(row["claim"], "a b c d e f g h i j k l")
        self.assertEqual((row["run_id"], row["decision_kind"], row["evidence_ref"]),
                         ("r un", "k ind", "e f"))
        self.assertEqual(self.log.read_bytes().count(b"\n"), 2)
        self.assertEqual(D.load(self.log)[0]["claim"], row["claim"])
        with self.assertRaises(D.LogError):
            self.add("\t\n​")

    def test_sanitize_property_seeded(self):
        # Seeded generator (SANITIZE_SEED, SANITIZE_CASES strings of <= 24 code points).
        rng = random.Random(SANITIZE_SEED)
        pool = ["a", "Z", " ", "\t", "\n", "\r", "\x7f", "\x85", "‍", "⁦",
                "\U000E007F", " ", " ", "", "\U0010FFFD", "͸",
                "\udc80", "é", "中", "\U0001F600"]
        for _ in range(SANITIZE_CASES):
            value = "".join(rng.choice(pool) for _ in range(rng.randint(0, 24)))
            clean = D.sanitize(value)
            self.assertEqual(len(clean), len(value))
            self.assertFalse(any(unicodedata.category(c) in D.UNSAFE_CATEGORIES
                                 for c in clean))
            self.assertNotIn("\t", clean)
            clean.encode("utf-8")
        self.add("x" + value)
        self.assertEqual(len(D.load(self.log)), 1)

    def test_supersedes_must_name_an_earlier_row(self):
        self.add()
        before = self.log.read_bytes()
        for bad in (0, 2, 5, -1):
            with self.subTest(bad=bad), self.assertRaises(D.LogError):
                self.add("fix", supersedes=bad)
        self.assertEqual(self.log.read_bytes(), before)

    def test_cli_append_and_help_document_integrity_limits(self):
        code, out, err = call(D, ["append", "--log", str(self.log), "--run-id", "r1",
                                  "--kind", "adopt", "--claim", "c", "--json"])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out)["appended"]["seq"], "1")
        self.assertEqual(call(D, ["show", "--log", str(self.log)])[1].count("adopt: c"), 1)
        self.assertIn("NOT cryptographic tamper-proofing", D.__doc__)
        help_out = io.StringIO()
        with redirect_stdout(help_out), self.assertRaises(SystemExit):
            D.main(["verify", "--help"])
        self.assertIn("same-UID", help_out.getvalue())
        self.assertNotEqual(D.SCHEMA, "decision.tsv")
        self.assertEqual(self.log.read_bytes().split(b"\n")[0].decode().split("\t"),
                         list(D.COLUMNS))


class TestDurability(TempRoot):
    def setUp(self):
        super().setUp()
        self.repo = self.root / "checkout"
        (self.repo / "tests").mkdir(parents=True)
        (self.repo / "tests/test_lesson.py").write_text("# synthetic\n")

    def test_schema_accepts_each_branch(self):
        for value in ({"holds_6mo": True, "form": "structure",
                       "artifact_path": "tests/test_lesson.py"},
                      {"holds_6mo": False, "form": "structure", "artifact_path": "a/b"},
                      {"holds_6mo": False, "form": "prose"},
                      {"holds_6mo": False, "form": "prose", "conversion_proposal": "lint"},
                      {"holds_6mo": True, "form": "prose", "conversion_proposal": "lint"}):
            with self.subTest(value=value):
                self.assertIs(S.validate_durability(value, "d"), value)

    def test_schema_refusals(self):
        cases = [
            ({"holds_6mo": True, "form": "structure"}, "artifact_path: required"),
            ({"holds_6mo": True, "form": "structure", "artifact_path": ""}, "nonempty"),
            ({"holds_6mo": True, "form": "prose", "artifact_path": "x",
              "conversion_proposal": "p"}, "forbidden for prose"),
            ({"holds_6mo": True, "form": "prose"}, "conversion_proposal: required"),
            ({"holds_6mo": True, "form": "prose", "conversion_proposal": " "}, "nonempty"),
            ({"holds_6mo": 1, "form": "prose"}, "expected boolean"),
            ({"holds_6mo": True, "form": "Structure", "artifact_path": "x"}, "unknown form"),
            ({"form": "prose"}, "missing form or holds_6mo"),
            ({"holds_6mo": False, "form": "prose", "extra": 1}, "unknown or missing"),
            ({"holds_6mo": True, "form": "structure", "artifact_path": "x",
              "conversion_proposal": "p"}, "unknown or missing"),
            ([], "expected object"),
        ]
        for path in ("../x", "a/../b", "/etc/passwd", "./x", "a//b", "a/", "a\\..\\b",
                     "a\nb"):
            cases.append(({"holds_6mo": True, "form": "structure", "artifact_path": path},
                          "artifact_path"))
        for value, message in cases:
            with self.subTest(value=value):
                with self.assertRaisesRegex(S.SchemaError, message):
                    S.validate_durability(value, "d")

    def test_evaluation_branches(self):
        structure = {"holds_6mo": True, "form": "structure",
                     "artifact_path": "tests/test_lesson.py"}
        kept = T.evaluate_durability(structure, self.repo)
        self.assertEqual((kept["disposition"], kept["artifact_verified"],
                          kept["holds_6mo_basis"]), ("kept_structure", True, "self_attested"))
        missing = T.evaluate_durability(dict(structure, artifact_path="tests/gone.py"),
                                        self.repo)
        self.assertEqual((missing["disposition"], missing["reason"]),
                         ("rejected", "missing_artifact"))
        (self.root / "outside.py").write_text("# outside\n")
        (self.repo / "escape").symlink_to(self.root / "outside.py")
        escaped = T.evaluate_durability(dict(structure, artifact_path="escape"), self.repo)
        self.assertEqual(escaped["reason"], "artifact_outside_repo_root")
        with self.assertRaises(S.SchemaError):
            T.evaluate_durability(dict(structure, artifact_path="../outside.py"), self.repo)
        drop = T.evaluate_durability({"holds_6mo": False, "form": "prose"}, self.repo)
        self.assertEqual((drop["disposition"], drop["reason"], drop["holds_6mo_basis"]),
                         ("rejected", "ephemeral_prose_under_6mo", "self_attested"))
        demote = T.evaluate_durability({"holds_6mo": True, "form": "prose",
                                        "conversion_proposal": "add a lint"}, self.repo)
        self.assertEqual((demote["disposition"], demote["conversion_proposal"]),
                         ("demoted_prose", "add a lint"))
        self.assertIn("lint, test, or script", demote["required_action"])
        with self.assertRaisesRegex(S.SchemaError, "conversion_proposal"):
            T.evaluate_durability({"holds_6mo": True, "form": "prose"}, self.repo)
        with self.assertRaisesRegex(S.SchemaError, "repo_root"):
            T.evaluate_durability(structure, self.root / "absent")


class TestDurabilityTriageCli(TempRoot):
    def setUp(self):
        super().setUp()
        self.repo = self.root / "checkout"
        (self.repo / "tests").mkdir(parents=True)
        (self.repo / "tests/test_lesson.py").write_text("# synthetic\n")
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
                       "packet_id": self.packet["packet_id"], "packet_digest": "",
                       "issue": {"repository": "hanig/brood-lord", "number": 1},
                       "dispositions": {
                           "L01": {"disposition": "accept", "unit_refs": ["unit-a"]},
                           "L02": {"disposition": "reject", "reason_code": "out_of_scope",
                                   "rationale": "Synthetic.", "references": []},
                           "L03": {"disposition": "accept", "unit_refs": ["unit-a"]}}}
        self.record_path = self.root / (self.packet["packet_id"] + ".json")
        self.packet_path = self.root / "packet.json"
        self.plan_path = self.root / "plan.json"
        self.plan_path.write_text(json.dumps({"units": [{"id": "unit-a"}]}))

    def save(self):
        self.record["packet_digest"] = hashlib.sha256(json.dumps(
            self.packet, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False).encode("utf-8")).hexdigest()
        self.record_path.write_text(json.dumps(self.record))
        self.packet_path.write_text(json.dumps(self.packet))

    def run_cli(self, *extra):
        return call(T, ["validate", "--record", str(self.record_path), "--packet",
                        str(self.packet_path), "--plan", str(self.plan_path)] + list(extra))

    def test_legacy_v1_and_v2_records_without_durability_unchanged(self):
        for schema, evidence in ((S.PACKET_V1, "E001"), (S.PACKET_SCHEMA, "E1000")):
            self.packet["schema_version"] = schema
            for row in self.packet["learnings"]:
                row["evidence_refs"] = [evidence]
            self.save()
            for extra in ((), ("--repo-root", str(self.repo))):
                with self.subTest(schema=schema, extra=extra):
                    self.assertEqual(self.run_cli(*extra), (0, "triage valid\n", ""))
            self.assertEqual(self.run_cli("--json"),
                             (0, '{"durability": {}, "valid": true}\n', ""))

    def test_durability_rows_need_repo_root_and_report_self_attested(self):
        dispositions = self.record["dispositions"]
        dispositions["L01"]["durability"] = {"holds_6mo": True, "form": "structure",
                                             "artifact_path": "tests/test_lesson.py"}
        dispositions["L02"]["durability"] = {"holds_6mo": False, "form": "prose"}
        dispositions["L03"]["durability"] = {"holds_6mo": True, "form": "prose",
                                             "conversion_proposal": "write a test"}
        self.save()
        code, _, err = self.run_cli()
        self.assertEqual(code, 1)
        self.assertIn("requires --repo-root", err)
        code, out, err = self.run_cli("--repo-root", str(self.repo), "--json")
        self.assertEqual((code, err), (0, ""))
        outcomes = json.loads(out)["durability"]
        self.assertEqual({k: v["disposition"] for k, v in outcomes.items()},
                         {"L01": "kept_structure", "L02": "rejected", "L03": "demoted_prose"})
        self.assertEqual({v["holds_6mo_basis"] for v in outcomes.values()}, {"self_attested"})
        code, out, _ = self.run_cli("--repo-root", str(self.repo))
        self.assertEqual(code, 0)
        self.assertIn("L02 durability: rejected ephemeral_prose_under_6mo", out)

    def test_accepted_learning_rejected_by_durability_fails(self):
        cases = ({"holds_6mo": True, "form": "structure", "artifact_path": "tests/gone.py"},
                 {"holds_6mo": False, "form": "prose"})
        for durability, reason in zip(cases, ("missing_artifact",
                                              "ephemeral_prose_under_6mo")):
            with self.subTest(reason=reason):
                record = copy.deepcopy(self.record)
                self.record["dispositions"]["L01"]["durability"] = durability
                self.save()
                code, _, err = self.run_cli("--repo-root", str(self.repo))
                self.assertEqual(code, 1)
                self.assertIn("L01: " + reason, err)
                self.record = record

    def test_invalid_durability_on_record_fails_validation(self):
        self.record["dispositions"]["L01"]["durability"] = {"holds_6mo": True,
                                                            "form": "structure"}
        self.save()
        with self.assertRaisesRegex(S.SchemaError, "L01.durability.artifact_path"):
            T.validate(self.record_path, self.packet_path, [self.plan_path])
        code, _, err = self.run_cli("--repo-root", str(self.repo))
        self.assertEqual(code, 1)
        self.assertIn("artifact_path: required", err)


if __name__ == "__main__":
    unittest.main()
