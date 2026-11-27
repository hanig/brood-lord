#!/usr/bin/env python3
"""Installed-snapshot path contracts for the authored hanig skills.

These tests intentionally execute copied artifacts after their staging source
has gone away, from a project cwd containing spaces and with an empty HOME.
They exercise path resolution only; no model, scheduler, or network call runs.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent))
from scheduler_fixture import closed_bin  # noqa: E402


REPO = Path(__file__).resolve().parents[1]
SKILLS = REPO / "skills"
AUTHORED = ("hanig-wandb-logging", "hanig-debrief", "hanig-hillclimb", "hanig-orchestrate", "hanig-portable-handoff", "hanig-project",
            "hanig-review-gate", "hanig-swarm", "hanig-verified-workflow")


# A bare `docs/...` path in installed skill prose resolves against nothing: the
# installer copies only the skill directory (issue 188).  Cite repository docs
# by REPO_BLOB URL, or ship the file inside the skill.  The exemptions name
# files in the checkout the skill operates on, read or written at run time.
DOCS_PATH = re.compile(r"(?<![\w./-])docs/[A-Za-z0-9_.*/-]*[A-Za-z0-9_*/-]")
REPO_BLOB = re.compile(r"https://github\.com/hanig/brood-lord/blob/main/([^\s)>\]`\"']+)")
OPERATED_CHECKOUT_PATHS = {
    "hanig-orchestrate": {"docs/orchestrator-mandate.md", "docs/d5-cutover-runbook.md",
                          "docs/deferred-edge-cases.md", "docs/*.md", "docs/README.md"},
    "hanig-review-gate": {"docs/orchestrator-mandate.md", "docs/**"},
}


def installed_markdown(skill_dir):
    return [skill_dir / "SKILL.md", *sorted((skill_dir / "references").glob("*.md"))]


def unshipped_docs_citations(skill_dir, exempt=frozenset()):
    problems = []
    for doc in installed_markdown(skill_dir):
        if not doc.is_file():
            continue
        for match in DOCS_PATH.finditer(doc.read_text(encoding="utf-8")):
            cited = match.group(0)
            if cited not in exempt and not (skill_dir / cited).is_file():
                problems.append(f"{doc.relative_to(skill_dir)}: {cited}")
    return problems


class InstalledSnapshot(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "empty home"
        self.home.mkdir()
        self.project = self.root / "project cwd with spaces"
        self.project.mkdir()
        source = self.root / "source checkout" / "skills"
        prefix = self.root / "custom prefix with spaces"
        source.mkdir(parents=True)
        prefix.mkdir()
        for name in AUTHORED:
            shutil.copytree(SKILLS / name, source / name)
            shutil.copytree(source / name, prefix / name)
        # The copied installation is all that remains.  An accidentally
        # checkout-relative helper would therefore fail rather than masking
        # the portability defect with this test repository.
        shutil.rmtree(source.parent)
        self.prefix = prefix
        # Portability must not depend on coordinator credentials or config.
        self.env = {"HOME": str(self.home), "PATH": closed_bin(self.root / "bin")}

    def tearDown(self):
        self.temp.cleanup()

    def invoke(self, relative, *args, cwd=None):
        return subprocess.run([sys.executable, str(self.prefix / relative), *args],
                              cwd=str(cwd or self.project), env=self.env,
                              text=True, capture_output=True)

    def test_authored_markdown_has_no_claude_path_or_cwd_script_path(self):
        for name in AUTHORED:
            text = (SKILLS / name / "SKILL.md").read_text()
            self.assertNotIn("~/.claude/skills", text, name)
            self.assertNotIn("python3 scripts/", text, name)
        project = (SKILLS / "hanig-project" / "SKILL.md").read_text()
        self.assertNotIn("../hanig-swarm/scripts", project)

    def test_debrief_resolver_uses_loaded_copy_from_another_project(self):
        loaded = self.prefix / "hanig-debrief"
        self.env["HANIG_DEBRIEF_DIR"] = str(loaded)
        result = subprocess.run(
            ["sh", "-c", 'exec "$1" "$HANIG_DEBRIEF_DIR/scripts/skill_paths.py" '
             'self "$HANIG_DEBRIEF_DIR" hanig-debrief', "resolver", sys.executable],
            cwd=str(self.project), env=self.env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), str(loaded.resolve()))

    def test_contract_handoff_survey_and_validation_use_copy_not_checkout(self):
        contract = self.invoke("hanig-verified-workflow/scripts/contract.py", "init",
                               "run", "--command", "true", "--output", "out.txt")
        self.assertEqual(contract.returncode, 0, contract.stderr)
        self.assertTrue((self.project / "run" / "contract.json").is_file())
        self.assertFalse((self.prefix / "hanig-verified-workflow" / "run").exists())

        run_dir = self.project / "handoff run"
        run_dir.mkdir()
        handoff = self.invoke("hanig-portable-handoff/scripts/handoff.py", "capture",
                              str(run_dir), "--out", "handoff.json")
        self.assertEqual(handoff.returncode, 0, handoff.stderr)
        self.assertTrue((self.project / "handoff.json").is_file())

        survey = self.invoke("hanig-project/scripts/survey.py", "--repo", ".",
                             "--out", ".swarm/survey.json")
        self.assertEqual(survey.returncode, 0, survey.stderr)
        self.assertTrue((self.project / ".swarm" / "survey.json").is_file())

        plan = {"name": "portable", "units": [{"id": "check", "kind": "slurm",
                "runtime": "none", "command": "true", "outputs": ["out.txt"]}]}
        (self.project / "plan.json").write_text(json.dumps(plan))
        valid = self.invoke("hanig-swarm/scripts/swarm.py", "validate", "plan.json")
        self.assertEqual(valid.returncode, 0, valid.stderr + valid.stdout)

    def test_review_configuration_and_project_sibling_are_portable(self):
        review = self.invoke("hanig-review-gate/scripts/review.py", "--list", "--no-probe")
        # Loading a valid copied config is distinct from having provider keys.
        # An empty HOME/environment honestly reports REVIEW_UNAVAILABLE (2).
        self.assertEqual(review.returncode, 2, review.stderr + review.stdout)
        config = json.loads((self.prefix / "hanig-review-gate" / "reviewers.json").read_text())
        expected = [reviewer for reviewer in config["reviewers"]
                    if config["default_profile"] in reviewer.get("profiles", [])]
        self.assertTrue(expected)
        for reviewer in expected:
            self.assertIn(reviewer["name"], review.stdout)
        self.assertIn("No reviewer can run", review.stdout)
        self.assertNotIn("REVIEW_ERROR", review.stdout + review.stderr)

        resolver = self.prefix / "hanig-project" / "scripts" / "skill_paths.py"
        found = subprocess.run([sys.executable, str(resolver), "sibling",
                                str(self.prefix / "hanig-project"), "hanig-project",
                                "hanig-swarm"], env=self.env, text=True, capture_output=True)
        self.assertEqual(found.returncode, 0, found.stderr)
        self.assertEqual(Path(found.stdout.strip()),
                         (self.prefix / "hanig-swarm").resolve())

        shutil.rmtree(self.prefix / "hanig-swarm")
        missing = subprocess.run([sys.executable, str(resolver), "sibling",
                                  str(self.prefix / "hanig-project"), "hanig-project",
                                  "hanig-swarm"], env=self.env, text=True, capture_output=True)
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("missing declared installed dependency 'hanig-swarm'",
                      missing.stderr)

    def test_linked_loaded_skill_keeps_logical_parent_and_uses_known_root(self):
        linked_parent = self.root / "link store"
        linked_parent.mkdir()
        linked_project = linked_parent / "hanig-project"
        linked_project.symlink_to(self.prefix / "hanig-project", target_is_directory=True)
        separate = self.root / "separate known parent"
        separate.mkdir()
        shutil.copytree(SKILLS / "hanig-swarm", separate / "hanig-swarm")
        resolver = self.prefix / "hanig-project" / "scripts" / "skill_paths.py"
        found = subprocess.run([sys.executable, str(resolver), "sibling",
                                str(linked_project), "hanig-project", "hanig-swarm",
                                "--root", str(separate)], env=self.env,
                               text=True, capture_output=True)
        self.assertEqual(found.returncode, 0, found.stderr)
        self.assertEqual(Path(found.stdout.strip()),
                         (separate / "hanig-swarm").resolve())

        mixed_env = dict(self.env, HANIG_SKILL_DEP_ROOTS=str(separate))
        report = subprocess.run([sys.executable,
                                 str(linked_project / "scripts" / "report.py"),
                                 ".", "--json"], cwd=self.project, env=mixed_env,
                                text=True, capture_output=True)
        self.assertEqual(report.returncode, 0, report.stderr)

    def test_project_report_resolves_its_sibling_without_source_layout(self):
        report = self.invoke("hanig-project/scripts/report.py", ".", "--json")
        self.assertEqual(report.returncode, 0, report.stderr)

    def test_missing_relative_root_reports_loaded_anchor_and_absolute_escape(self):
        shutil.rmtree(self.prefix / "hanig-swarm")
        links = self.root / "logical link store"
        links.mkdir()
        for name in ("hanig-project", "hanig-orchestrate"):
            linked = links / name
            linked.symlink_to(self.prefix / name, target_is_directory=True)
            for loaded in (self.prefix / name, linked):
                for option in ([], ["--root", "../missing deps"]):
                    with self.subTest(skill=name, loaded=str(loaded), option=option):
                        env = dict(self.env)
                        if not option:
                            env["HANIG_SKILL_DEP_ROOTS"] = "../missing deps"
                        result = subprocess.run(
                            [sys.executable, str(loaded / "scripts/skill_paths.py"),
                             "sibling", str(loaded), name, "hanig-swarm", *option],
                            cwd=self.project, env=env, text=True, capture_output=True)
                        self.assertNotEqual(result.returncode, 0)
                        self.assertIn("missing declared installed dependency", result.stderr)
                        self.assertIn(
                            "Relative dependency roots are anchored to the loaded "
                            "skill directory " + str(loaded), result.stderr)
                        self.assertIn("use an absolute path", result.stderr)

    def test_cwd_relative_consumers_migrate_to_absolute_dependency_root(self):
        deps = self.root / "deps"
        deps.mkdir()
        shutil.move(str(self.prefix / "hanig-swarm"), str(deps / "hanig-swarm"))
        # ../deps is valid from the project cwd, but not from the loaded skill.
        self.assertEqual((self.project / "../deps").resolve(), deps.resolve())
        consumers = (("hanig-project", "report.py", [".", "--json"]),
                     ("hanig-orchestrate", "merge_unit.py", ["--help"]))
        for name, program, args in consumers:
            loaded = self.prefix / name
            env = dict(self.env, HANIG_SKILL_DEP_ROOTS="../deps")
            env["HANIG_" + name[6:].upper() + "_DIR"] = str(loaded)
            with self.subTest(skill=name):
                command = [sys.executable, str(loaded / "scripts" / program), *args]
                legacy = subprocess.run(command, cwd=self.project, env=env,
                                        text=True, capture_output=True)
                self.assertNotEqual(legacy.returncode, 0)
                self.assertIn("use an absolute path", legacy.stderr)
                env["HANIG_SKILL_DEP_ROOTS"] = str(deps)
                migrated = subprocess.run(command, cwd=self.project, env=env,
                                          text=True, capture_output=True)
                self.assertEqual(migrated.returncode, 0,
                                 migrated.stderr + migrated.stdout)
                if name == "hanig-project":
                    self.assertIsInstance(json.loads(migrated.stdout), dict)
                else:
                    self.assertIn("usage:", migrated.stdout)

    def test_both_consumers_use_declared_roots_from_unrelated_cwd(self):
        consumers = (("hanig-project", "report.py", [".", "--json"]),
                     ("hanig-orchestrate", "merge_unit.py", ["--help"]))
        for name, program, args in consumers:
            for mode in ("copy", "link"):
                parent = self.root / (name + " " + mode + " store")
                parent.mkdir()
                loaded = parent / name
                if mode == "link":
                    # No sibling beside the physical target: falling back to
                    # a checkout-relative import cannot accidentally succeed.
                    target = self.root / (name + " isolated source") / name
                    shutil.copytree(self.prefix / name, target)
                    loaded.symlink_to(target, target_is_directory=True)
                else:
                    shutil.copytree(self.prefix / name, loaded)
                deps = parent / "deps"
                shutil.copytree(self.prefix / "hanig-swarm", deps / "hanig-swarm")
                for dep_root in (str(deps), "../deps"):
                    with self.subTest(skill=name, mode=mode, root=dep_root):
                        env = dict(self.env, HANIG_SKILL_DEP_ROOTS=dep_root)
                        env["HANIG_" + name[6:].upper() + "_DIR"] = str(loaded)
                        for option in ([], ["--root", dep_root]):
                            resolver_env = dict(env)
                            if option:
                                resolver_env.pop("HANIG_SKILL_DEP_ROOTS")
                            found = subprocess.run(
                                [sys.executable, str(loaded / "scripts/skill_paths.py"),
                                 "sibling", str(loaded), name, "hanig-swarm", *option],
                                cwd=self.project, env=resolver_env,
                                text=True, capture_output=True)
                            self.assertEqual(found.returncode, 0, found.stderr)
                            self.assertEqual(Path(found.stdout.strip()),
                                             (deps / "hanig-swarm").resolve())
                        result = subprocess.run(
                            [sys.executable, str(loaded / "scripts" / program), *args],
                            cwd=self.project, env=env, text=True, capture_output=True)
                        self.assertEqual(result.returncode, 0,
                                         result.stderr + result.stdout)
                        if name == "hanig-project":
                            self.assertIsInstance(json.loads(result.stdout), dict)
                        else:
                            self.assertIn("usage:", result.stdout)


    def test_installed_markdown_cites_no_unshipped_repository_docs(self):
        for name in AUTHORED:
            with self.subTest(skill=name):
                self.assertEqual(unshipped_docs_citations(
                    self.prefix / name, OPERATED_CHECKOUT_PATHS.get(name, set())), [])
        gate = (self.prefix / "hanig-review-gate" / "SKILL.md").read_text()
        compat = "https://github.com/hanig/brood-lord/blob/main/docs/agent-compatibility.md"
        self.assertTrue(compat in gate, "hanig-review-gate must cite " + compat)

    def test_repository_doc_links_name_tracked_files(self):
        for name in AUTHORED:
            for doc in installed_markdown(SKILLS / name):
                for target in REPO_BLOB.findall(doc.read_text(encoding="utf-8")):
                    with self.subTest(doc=str(doc.relative_to(REPO)), target=target):
                        self.assertTrue((REPO / target).is_file())

    def test_unshipped_docs_citation_detector_rejects_the_issue_188_form(self):
        skill = self.root / "hanig-example"
        (skill / "references").mkdir(parents=True)
        (skill / "docs").mkdir()
        (skill / "docs" / "shipped.md").write_text("x\n")
        (skill / "SKILL.md").write_text(
            "Report it unreviewed (`docs/agent-compatibility.md`).\n"
            "See docs/shipped.md and docs/orchestrator-mandate.md.\n"
            "Linked: https://github.com/hanig/brood-lord/blob/main/docs/x.md\n")
        (skill / "references" / "more.md").write_text("Read docs/plan.md.\n")
        self.assertEqual(
            unshipped_docs_citations(skill, {"docs/orchestrator-mandate.md"}),
            ["SKILL.md: docs/agent-compatibility.md", "references/more.md: docs/plan.md"])


if __name__ == "__main__":
    unittest.main()
