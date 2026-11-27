"""brood-debrief decisions.tsv trail and durability triage."""
import ast
from datetime import datetime, timezone
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
from unittest import mock
from contextlib import redirect_stderr, redirect_stdout

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills/brood-debrief/scripts"
sys.path.insert(0, str(SCRIPTS))
import debrief_schema as S
import debrief_triage as T
import decisions_log as D

SEED, CASES = 20261124, 400  # seeded sanitize property: 400 strings of <= 24 code points
NOW = datetime(2026, 11, 24, 12, tzinfo=timezone.utc)
# Schema/triage allowlists: pinned in their own test modules.
LOG_IMPORTS = {"argparse", "datetime", "fcntl", "hashlib", "json", "os", "pathlib", "re",
               "stat", "sys", "unicodedata"}


def call(module, argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = module.main(argv)
    return code, out.getvalue(), err.getvalue()


class Base(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()  # TMPDIR must lie outside Git worktrees
        self.log = self.root / "private/trail/decisions.tsv"
        self.repo = self.root / "checkout"
        (self.repo / "tests").mkdir(parents=True)
        (self.repo / "tests/test_lesson.py").write_text("# synthetic\n")

    def add(self, claim="keep", **kw):
        return D.append(self.log, kw.pop("run_id", "r"), "adopt", claim, now=NOW, **kw)


class TestDecisionsLog(Base):
    def test_offline_imports(self):
        bad = {"socket", "ssl", "http", "urllib", "ftplib", "smtplib", "asyncio", "subprocess"}
        for name in ("debrief_schema.py", "debrief_triage.py", "decisions_log.py"):
            tree = ast.walk(ast.parse((SCRIPTS / name).read_text(encoding="utf-8")))
            imports = {(n.module or "").split(".")[0] if isinstance(n, ast.ImportFrom) else
                       a.name.split(".")[0] for n in tree if isinstance(n, (ast.Import,
                       ast.ImportFrom)) for a in getattr(n, "names", [])}
            with self.subTest(name=name):
                self.assertFalse(imports & bad)
        self.assertLessEqual(imports, LOG_IMPORTS)  # decisions_log.py, checked last

    def test_byte_prefix_stable_with_superseding_row(self):
        snaps = []
        for claim in ("first", "second", "third"):
            self.add(claim, evidence_ref="docs/x.md")
            snaps.append(self.log.read_bytes())
        self.assertEqual(self.add("fixed", supersedes=3)["supersedes"], "3")
        snaps.append(self.log.read_bytes())
        for old, new in zip(snaps, snaps[1:]):
            self.assertTrue(new.startswith(old) and len(new) > len(old))
        self.assertEqual(snaps[-1].count(D.HEADER), 1)
        rows = D.load(self.log)
        self.assertEqual([r["prev_sha256"] for r in rows],
                         [D.GENESIS] + [r["row_sha256"] for r in rows[:-1]])
        code, out, _ = call(D, ["show", "--log", str(self.log), "--json"])
        shown = json.loads(out)
        self.assertEqual((shown["entries"], shown["head_sha256"]), (rows, rows[-1]["row_sha256"]))
        self.assertIn("NOT cryptographic tamper-proofing", shown["integrity"])
        self.assertEqual(stat.S_IMODE(self.log.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.log.parent.parent.stat().st_mode), 0o700)
        for bad in (0, 5):
            with self.subTest(bad=bad), self.assertRaises(D.LogError):
                self.add("x", supersedes=bad)
        self.assertEqual(self.log.read_bytes(), snaps[-1])

    def test_refuses_paths_inside_git_worktrees(self):
        repo = self.root / "repo"
        (repo / ".git").mkdir(parents=True)
        linked = self.root / "linked"
        linked.mkdir()
        (linked / ".git").write_text("gitdir: synthetic\n")
        (self.root / "alias").symlink_to(repo, target_is_directory=True)
        for target in (repo / "d.tsv", repo / "deep/new/d.tsv", linked / "d.tsv",
                       self.root / "alias/nested/d.tsv"):
            for command in (["append", "--run-id", "r", "--kind", "k", "--claim", "c"],
                            ["verify"], ["show"]):
                code, _, err = call(D, command + ["--log", str(target)])
                self.assertEqual((code, "inside a Git worktree" in err), (1, True), target)
            self.assertFalse(os.path.lexists(str(target)))
        self.assertFalse((repo / "deep").exists())
        with self.assertRaises(SystemExit) as ctx, redirect_stderr(io.StringIO()):
            D.main(["append", "--run-id", "r", "--kind", "k", "--claim", "c"])
        self.assertEqual(ctx.exception.code, 2)

    def test_round1_regressions(self):
        self.log.parent.mkdir(parents=True)
        self.log.write_bytes(D.HEADER)  # header-only log gains no second header
        self.add()
        self.assertEqual(self.log.read_bytes().count(D.HEADER), 1)
        real, other = os.mkdir, self.root / "race/x.tsv"
        def race(path, mode):  # another append creates the parent first
            real(path, mode)
            real(path, mode)
        with mock.patch.object(D.os, "mkdir", race):
            D.append(other, "r", "k", "c")
        with mock.patch.object(D, "sanitize", str):  # newer-Unicode writer (Cn here)
            self.add("x\u0378")
        self.assertEqual(len(D.load(self.log)) + len(D.load(other)), 3)

    def test_verify_detects_edits_deletions_and_partial_rows(self):
        for claim in ("alpha", "beta", "gamma"):
            self.add(claim)
        good = self.log.read_bytes()
        head = D.load(self.log)[-1]["row_sha256"]
        h, *rows = [line + b"\n" for line in good.split(b"\n")[:-1]]
        for label, data in {"edit first": good.replace(b"alpha", b"alphA"),
                            "edit last": good.replace(b"gamma", b"gammA"),
                            "delete middle": h + rows[0] + rows[2],
                            "reorder": h + rows[1] + rows[0] + rows[2],
                            "partial row": good[:-5]}.items():
            with self.subTest(label=label):
                self.log.write_bytes(data)
                code, _, err = call(D, ["verify", "--log", str(self.log)])
                self.assertEqual((code, err[:15]), (1, "decisions log: "))
                with self.assertRaises(D.LogError):
                    self.add("refused")
                self.assertEqual(self.log.read_bytes(), data)
        self.log.write_bytes(h + rows[0] + rows[1])
        self.assertEqual(call(D, ["verify", "--log", str(self.log)])[0], 0)
        code, _, err = call(D, ["verify", "--log", str(self.log), "--expect-head", head])
        self.assertEqual((code, "expected head not found" in err), (1, True))
        self.log.write_bytes(good)
        self.assertEqual(call(D, ["verify", "--log", str(self.log), "--expect-head", head])[0], 0)

    def test_sanitizes_delimiters_and_control_format_chars(self):
        hostile = "a\tb\nc\rd\u200be\U000E0041f\u2028g\u2029h\ue000i\u0378j\ud800k\x00l"
        row = self.add(hostile, run_id="r\tun", evidence_ref="e\u202ef")
        self.assertEqual((row["claim"], row["run_id"], row["evidence_ref"]),
                         ("a b c d e f g h i j k l", "r un", "e f"))
        self.assertEqual(D.load(self.log)[0]["claim"], row["claim"])
        with self.assertRaises(D.LogError):
            self.add("\t\n\u200b")
        rng = random.Random(SEED)
        pool = ["a", " ", "\t", "\n", "\r", "\x7f", "\x85", "\u200d", "\u2066", "\U000E007F",
                "\u2028", "\u2029", "\ue123", "\u0378", "\udc80"]
        for _ in range(CASES):
            value = "".join(rng.choice(pool) for _ in range(rng.randint(0, 24)))
            clean = D.sanitize(value)
            self.assertFalse(any(unicodedata.category(c) in D.UNSAFE for c in clean))


class TestDurabilityTriageCli(Base):
    def test_schema_and_evaluation(self):
        st = {"holds_6mo": True, "form": "structure", "artifact_path": "tests/test_lesson.py"}
        for value in (st, {"holds_6mo": False, "form": "prose"},
                      {"holds_6mo": True, "form": "prose", "conversion_proposal": "lint"}):
            self.assertIs(S.validate_durability(value, "d"), value)
        cases = [({"holds_6mo": True, "form": "structure"}, "artifact_path: required"),
                 (dict(st, form="prose", conversion_proposal="p"), "forbidden for prose"),
                 ({"holds_6mo": True, "form": "prose"}, "conversion_proposal: required"),
                 ({"holds_6mo": True, "form": "prose", "conversion_proposal": " "}, "nonempty"),
                 ({"holds_6mo": 1, "form": "prose"}, "expected boolean"),
                 (dict(st, form="Structure"), "unknown form"),
                 (dict(st, conversion_proposal="p"), "unknown or missing")]
        cases += [(dict(st, artifact_path=p), "artifact_path") for p in
                  ("", "../x", "a/../b", "/etc/passwd", "./x", "a//b", "a\\..\\b", "a\nb")]
        for value, message in cases:
            with self.subTest(value=value), self.assertRaisesRegex(S.SchemaError, message):
                S.validate_durability(value, "d")
        self.assertTrue(T.evaluate_durability(st, self.repo)["artifact_verified"])
        (self.root / "outside.py").write_text("#\n")
        (self.repo / "escape").symlink_to(self.root / "outside.py")
        got = T.evaluate_durability(dict(st, artifact_path="escape"), self.repo)
        self.assertEqual(got["reason"], "artifact_outside_repo_root")
        with self.assertRaises(S.SchemaError):
            T.evaluate_durability(dict(st, artifact_path="../outside.py"), self.repo)
        with self.assertRaisesRegex(S.SchemaError, "repo_root"):
            T.evaluate_durability(st, self.root / "absent")

    def setUp(self):
        super().setUp()
        e = S.load_catalog()["entries"][0]
        learning = dict(skill=e["skill"], what_broke=e["what_broke"], mechanism=e["mechanism"],
                        proposed_change=e["proposed_change"], catalog_entry=e["id"],
                        mechanism_basis="reported", counts=[], evidence_refs=["E001"])
        self.packet = {"schema_version": S.PACKET_SCHEMA, "catalog_version": 1,
                       "packet_id": "00000000-0000-4000-8000-000000000001",
                       "project": "project A",
                       "learnings": [dict(learning, id="L%02d" % n) for n in (1, 2, 3)]}
        accept = {"disposition": "accept", "unit_refs": ["unit-a"]}
        self.rows = {"L01": dict(accept), "L03": dict(accept),
                     "L02": {"disposition": "reject", "reason_code": "out_of_scope",
                             "rationale": "Synthetic.", "references": []}}
        self.paths = [self.root / n for n in (self.packet["packet_id"] + ".json",
                                              "packet.json", "plan.json")]
        self.paths[2].write_text(json.dumps({"units": [{"id": "unit-a"}]}))

    def run_cli(self, *extra):
        self.paths[0].write_text(json.dumps({
            "schema_version": S.TRIAGE_SCHEMA, "packet_id": self.packet["packet_id"],
            "packet_digest": T.packet_digest(self.packet), "dispositions": self.rows,
            "issue": {"repository": "hanig/brood-lord", "number": 1}}))
        self.paths[1].write_text(json.dumps(self.packet))
        return call(T, ["validate", "--record", str(self.paths[0]), "--packet",
                        str(self.paths[1]), "--plan", str(self.paths[2])] + list(extra))

    def test_legacy_v1_and_v2_records_without_durability_unchanged(self):
        for schema, evidence in ((S.PACKET_V1, "E001"), (S.PACKET_SCHEMA, "E1000")):
            self.packet["schema_version"] = schema
            for row in self.packet["learnings"]:
                row["evidence_refs"] = [evidence]
            self.assertEqual(self.run_cli(), (0, "triage valid\n", ""))
        self.assertEqual(self.run_cli("--repo-root", str(self.repo)), (0, "triage valid\n", ""))

    def test_durability_rows_need_repo_root_and_report_self_attested(self):
        self.rows["L01"]["durability"] = {"holds_6mo": True, "form": "structure",
                                          "artifact_path": "tests/test_lesson.py"}
        self.rows["L02"]["durability"] = {"holds_6mo": False, "form": "prose"}
        self.rows["L03"]["durability"] = {"holds_6mo": True, "form": "prose",
                                          "conversion_proposal": "write a test"}
        code, _, err = self.run_cli()
        self.assertEqual((code, "requires --repo-root" in err), (1, True))
        code, out, err = self.run_cli("--repo-root", str(self.repo))
        head, outcomes = out.split(" {", 1)
        self.assertEqual((code, head), (0, "triage valid\ndurability"))
        outcomes = json.loads("{" + outcomes)
        self.assertEqual({k: v["disposition"] for k, v in outcomes.items()},
                         {"L01": "kept_structure", "L02": "rejected", "L03": "demoted_prose"})
        self.assertEqual({v["holds_6mo_basis"] for v in outcomes.values()}, {"self_attested"})
        self.assertEqual((outcomes["L02"]["reason"], outcomes["L03"]["conversion_proposal"]),
                         ("ephemeral_prose_under_6mo", "write a test"))
        self.assertIn("lint, test, or script", outcomes["L03"]["required_action"])

    def test_accepted_learning_rejected_by_durability_fails(self):
        for durability, reason in (
                ({"holds_6mo": True, "form": "structure", "artifact_path": "tests/gone.py"},
                 "missing_artifact"),
                ({"holds_6mo": False, "form": "prose"}, "ephemeral_prose_under_6mo"),
                ({"holds_6mo": True, "form": "prose"}, "conversion_proposal: required"),
                ({"holds_6mo": True, "form": "structure"}, "artifact_path: required")):
            with self.subTest(reason=reason):
                self.rows["L01"]["durability"] = durability
                code, _, err = self.run_cli("--repo-root", str(self.repo))
                self.assertEqual((code, reason in err), (1, True), err)


if __name__ == "__main__":
    unittest.main()
