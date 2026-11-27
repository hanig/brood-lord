"""Contract tests for lib/skill_agent_policy.py on synthetic skill trees and
on the live repository, which unit p2-policy-apply brought into policy."""

import ast
import hashlib
import itertools
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from lib import skill_agent_policy as sap  # noqa: E402

SCRIPT = ROOT / "lib" / "skill_agent_policy.py"
YAML = b"policy:\n  allow_implicit_invocation: false\n"
LISTED = "brood-swarm"
UNLISTED = "brood-review-gate"


def skill_md(name, flag_line=None):
    lines = ["---", "name: %s" % name, "description: >-",
             "  Synthetic skill.", "  disable-model-invocation: true"]
    if flag_line is not None:
        lines.append(flag_line)
    return "\n".join(lines + ["---", "# body", "disable-model-invocation: true", ""])


class TreeCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(self.tmp), ignore_errors=True)
        self.root = self.tmp / "skills"
        self.root.mkdir()

    def make(self, name, flag_line=None, yaml=None, text=None):
        path = self.root / name
        path.mkdir()
        (path / "SKILL.md").write_text(text if text is not None else skill_md(name, flag_line))
        if yaml is not None:
            (path / "agents").mkdir()
            (path / "agents" / "openai.yaml").write_bytes(yaml)
        return path

    def valid_tree(self):
        for name in sap.EXPLICIT_ONLY_SKILLS:
            self.make(name, "disable-model-invocation: true", YAML)
        self.make(UNLISTED)

    def codes(self, report):
        return sorted((e["code"], e["skill"]) for e in report["errors"])

    def cli(self, *args):
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--skills-root", str(self.root)] + list(args),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
            timeout=60)

    def snapshot(self, base):
        out = {}
        for dirpath, dirnames, filenames in os.walk(str(base)):
            for entry in dirnames + filenames:
                full = os.path.join(dirpath, entry)
                st = os.lstat(full)
                digest = ""
                if stat.S_ISREG(st.st_mode):
                    with open(full, "rb") as handle:
                        digest = hashlib.sha256(handle.read()).hexdigest()
                out[os.path.relpath(full, str(base))] = (st.st_mode, st.st_mtime_ns, digest)
        return out


class TestConstants(unittest.TestCase):
    def test_explicit_only_list_and_canonical_bytes(self):
        self.assertEqual(sap.EXPLICIT_ONLY_SKILLS, (
            "brood-debrief", "brood-hillclimb", "brood-orchestrate", "brood-portable-handoff",
            "brood-project", "brood-swarm"))
        self.assertEqual(sap.OPENAI_YAML_BYTES, YAML)

    def test_parses_under_python_38_grammar(self):
        ast.parse(SCRIPT.read_text(), filename=str(SCRIPT), feature_version=(3, 8))


class TestParseFlag(unittest.TestCase):
    def test_flag_classification_table(self):
        table = [
            ("disable-model-invocation: true", sap.FLAG_TRUE),
            (None, sap.FLAG_ABSENT),
            ('disable-model-invocation: "true"', sap.FLAG_NONCANONICAL),
            ("disable-model-invocation: 'true'", sap.FLAG_NONCANONICAL),
            ("disable-model-invocation: false", sap.FLAG_NONCANONICAL),
            ("disable-model-invocation: True", sap.FLAG_NONCANONICAL),
            ("disable-model-invocation: yes", sap.FLAG_NONCANONICAL),
            ("disable-model-invocation:", sap.FLAG_NONCANONICAL),
            ("disable-model-invocation: true # c", sap.FLAG_NONCANONICAL),
            ("disable-model-invocation: true\ndisable-model-invocation: true",
             sap.FLAG_NONCANONICAL),
        ]
        for line, expected in table:
            with self.subTest(line=line):
                self.assertEqual(sap.parse_flag(skill_md("x", line))["state"], expected)

    def test_malformed_frontmatter_table(self):
        for text in ["", "name: x\n", "---\nname: x\n", "---\r\nname: x\r\n---\r\n",
                     " ---\nname: x\n---\n"]:
            with self.subTest(text=text):
                self.assertEqual(sap.parse_flag(text)["state"], sap.FLAG_MALFORMED)


class TestCheck(TreeCase):
    def test_valid_tree_exits_zero(self):
        self.valid_tree()
        self.assertEqual(self.codes(sap.run(self.root)), [])
        result = self.cli("--check", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["ok"])

    def test_absent_listed_skill_is_not_required(self):
        self.make(LISTED, "disable-model-invocation: true", YAML)
        self.assertEqual(self.codes(sap.run(self.root)), [])

    def test_each_drift_class_exits_one(self):
        table = [
            ("missing yaml", LISTED, "disable-model-invocation: true", None,
             "missing_openai_yaml"),
            ("missing yaml unlisted", UNLISTED, "disable-model-invocation: true", None,
             "missing_openai_yaml"),
            ("orphan yaml", UNLISTED, None, YAML, "orphan_openai_yaml"),
            ("byte drift", LISTED, "disable-model-invocation: true", YAML + b"\n",
             "openai_yaml_drift"),
            ("byte drift crlf", LISTED, "disable-model-invocation: true",
             YAML.replace(b"\n", b"\r\n"), "openai_yaml_drift"),
            ("double quoted", LISTED, 'disable-model-invocation: "true"', YAML,
             "noncanonical_flag"),
            ("single quoted", UNLISTED, "disable-model-invocation: 'true'", None,
             "noncanonical_flag"),
            ("false", UNLISTED, "disable-model-invocation: false", None,
             "noncanonical_flag"),
            ("listed missing flag", LISTED, None, None, "explicit_only_missing_flag"),
        ]
        for label, name, flag, yaml, code in table:
            with self.subTest(label):
                shutil.rmtree(str(self.root))
                self.root.mkdir()
                self.valid_tree()
                shutil.rmtree(str(self.root / name))
                self.make(name, flag, yaml)
                report = sap.run(self.root)
                self.assertIn((code, name), self.codes(report))
                self.assertFalse(report["ok"])
                result = self.cli("--check", "--json")
                self.assertEqual(result.returncode, 1)
                errors = json.loads(result.stdout)["errors"]
                self.assertIn(code, [e["code"] for e in errors])
                for err in errors:
                    self.assertEqual(sorted(err), ["code", "detail", "path", "skill"])
                text = self.cli("--check")
                self.assertEqual(text.returncode, 1)
                self.assertIn("ERROR %s %s" % (code, name), text.stderr)

    def test_malformed_frontmatter_and_unsafe_paths_fail_closed(self):
        self.make(LISTED, text="no frontmatter\n")
        skill = self.make("brood-a", "disable-model-invocation: true")
        (skill / "agents").write_text("not a dir")
        self.make("brood-b")
        (self.root / "brood-b" / "SKILL.md").write_bytes(b"---\nname: \xff\n---\n")
        codes = self.codes(sap.run(self.root))
        self.assertIn(("malformed_frontmatter", LISTED), codes)
        self.assertIn(("unsafe_openai_yaml", "brood-a"), codes)
        self.assertIn(("malformed_frontmatter", "brood-b"), codes)

    def test_symlinked_yaml_is_unsafe_and_not_written_through(self):
        outside = self.tmp / "outside.yaml"
        outside.write_bytes(b"keep\n")
        skill = self.make(LISTED, "disable-model-invocation: true")
        (skill / "agents").mkdir()
        os.symlink(str(outside), str(skill / "agents" / "openai.yaml"))
        report = sap.run(self.root, write=True)
        self.assertIn(("unsafe_openai_yaml", LISTED), self.codes(report))
        self.assertEqual(outside.read_bytes(), b"keep\n")

    def test_missing_skills_root_fails_closed(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--skills-root", str(self.tmp / "nope"), "--check"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
            timeout=60)
        self.assertEqual(result.returncode, 1)
        self.assertIn("skills_root_invalid", result.stderr)

    def test_exhaustive_single_skill_matrix(self):
        # Exhaustive enumeration: 2 names x 7 flag lines x 3 yaml states = 42 trees.
        flags = [None, "disable-model-invocation: true", 'disable-model-invocation: "true"',
                 "disable-model-invocation: 'true'", "disable-model-invocation: false",
                 "disable-model-invocation: TRUE", "disable-model-invocation:  true"]
        yamls = [None, YAML, b"policy:\n  allow_implicit_invocation: true\n"]
        count = 0
        for name, flag, yaml in itertools.product((LISTED, UNLISTED), flags, yamls):
            with self.subTest(name=name, flag=flag, yaml=yaml):
                shutil.rmtree(str(self.root))
                self.root.mkdir()
                self.make(name, flag, yaml)
                flagged = flag == "disable-model-invocation: true"
                expected_ok = (yaml == YAML) if flagged else (
                    yaml is None and flag is None and name != LISTED)
                self.assertEqual(sap.run(self.root)["ok"], expected_ok)
                count += 1
        self.assertEqual(count, 42)


class TestWrite(TreeCase):
    def test_write_generates_only_for_flagged_skills_and_is_idempotent(self):
        for name in sap.EXPLICIT_ONLY_SKILLS:
            self.make(name, "disable-model-invocation: true")
        self.make("brood-debrief-x", "disable-model-invocation: true", YAML + b"# drift\n")
        self.make(UNLISTED)
        result = self.cli("--write", "--json")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(sorted(json.loads(result.stdout)["written"]),
                         sorted(sap.EXPLICIT_ONLY_SKILLS + ("brood-debrief-x",)))
        for name in sap.EXPLICIT_ONLY_SKILLS + ("brood-debrief-x",):
            self.assertEqual((self.root / name / "agents" / "openai.yaml").read_bytes(), YAML)
        self.assertFalse(os.path.lexists(str(self.root / UNLISTED / "agents")))
        before = self.snapshot(self.root)
        again = self.cli("--write", "--json")
        self.assertEqual(again.returncode, 0)
        self.assertEqual(json.loads(again.stdout)["written"], [])
        self.assertEqual(self.snapshot(self.root), before)
        self.assertEqual(self.cli("--check").returncode, 0)

    def test_write_does_not_repair_orphans_or_noncanonical_flags(self):
        self.make(UNLISTED, None, b"stale\n")
        self.make("brood-q", 'disable-model-invocation: "true"')
        report = sap.run(self.root, write=True)
        self.assertEqual(report["written"], [])
        self.assertEqual((self.root / UNLISTED / "agents" / "openai.yaml").read_bytes(),
                         b"stale\n")
        self.assertFalse(os.path.lexists(str(self.root / "brood-q" / "agents")))
        self.assertEqual(self.codes(report), [("noncanonical_flag", "brood-q"),
                                              ("orphan_openai_yaml", UNLISTED)])
        self.assertEqual(self.cli("--write").returncode, 1)

    def test_vendored_bundles_are_never_read_or_mutated(self):
        self.valid_tree()
        for name in sap.EXPLICIT_ONLY_SKILLS:
            os.unlink(str(self.root / name / "agents" / "openai.yaml"))
        vendored = []
        for name in ("paseo", "agent-bus", "swarm"):
            path = self.make(name, "disable-model-invocation: true", b"orphan\n")
            vendored.append(path)
        self.make("start-a-sprint", None, b"interface: {}\n")
        vendored.append(self.root / "start-a-sprint")
        before = {p.name: self.snapshot(p) for p in vendored}
        locked = vendored[0]
        os.chmod(str(locked), 0)
        self.addCleanup(os.chmod, str(locked), 0o755)
        if os.access(str(locked), os.R_OK):
            self.skipTest("running with privileges that bypass directory modes")
        result = self.cli("--write", "--json")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual([s["name"] for s in report["skills"]],
                         sorted(sap.EXPLICIT_ONLY_SKILLS + (UNLISTED,)))
        self.assertEqual(self.cli("--check").returncode, 0)
        os.chmod(str(locked), 0o755)
        self.assertEqual({p.name: self.snapshot(p) for p in vendored}, before)


class TestLiveRepository(unittest.TestCase):
    IMPLICIT = ("brood-conclave", "brood-review-gate", "brood-verified-workflow",
                "brood-wandb-logging")

    def test_explicit_only_list_is_pinned_and_check_exits_zero(self):
        self.assertEqual(sap.EXPLICIT_ONLY_SKILLS, (
            "brood-debrief", "brood-hillclimb", "brood-orchestrate", "brood-portable-handoff",
            "brood-project", "brood-swarm"))
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--check", "--json"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
            timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(Path(report["skills_root"]), ROOT / "skills")
        states = {s["name"]: (s["flag"], s["openai_yaml"]) for s in report["skills"]}
        self.assertEqual(sorted(states), sorted(sap.EXPLICIT_ONLY_SKILLS + self.IMPLICIT))
        for name in sap.EXPLICIT_ONLY_SKILLS:
            self.assertEqual(states[name], (sap.FLAG_TRUE, "ok"), name)
            self.assertEqual((ROOT / "skills" / name / "agents" / "openai.yaml")
                             .read_bytes(), YAML)
        for name in self.IMPLICIT:
            self.assertEqual(states[name], (sap.FLAG_ABSENT, "absent"), name)
            self.assertFalse(os.path.lexists(str(ROOT / "skills" / name / "agents")))


if __name__ == "__main__":
    unittest.main()
