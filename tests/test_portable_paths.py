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
AUTHORED = ("brood-wandb-logging", "brood-conclave", "brood-debrief", "brood-hillclimb", "brood-orchestrate", "brood-portable-handoff", "brood-project",
            "brood-review-gate", "brood-swarm", "brood-verified-workflow")


# A bare `docs/...` path in installed skill prose resolves against nothing: the
# installer copies only the skill directory (issue 188).  Cite repository docs
# by REPO_BLOB URL, or ship the file inside the skill.  The exemptions name
# files in the checkout the skill operates on, read or written at run time.
# An optional leading `./` names the same unshipped path; `../docs/` is not
# matched.  The exemption and shipped-file checks use the path after `./`.
DOCS_PATH = re.compile(r"(?<![\w./-])(?:\./)?(?P<path>docs/[A-Za-z0-9_.*/-]*[A-Za-z0-9_*/-])")
REPO_BLOB = re.compile(r"https://github\.com/hanig/brood-lord/blob/main/([^\s)>\]`\"']+)")
# Sentence punctuation that ends prose, not a repository path.
URL_TRAILING_PUNCTUATION = ".,;:!?"
OPERATED_CHECKOUT_PATHS = {
    "brood-orchestrate": {"docs/orchestrator-mandate.md", "docs/d5-cutover-runbook.md",
                          "docs/deferred-edge-cases.md", "docs/*.md", "docs/README.md"},
    "brood-review-gate": {"docs/orchestrator-mandate.md", "docs/**"},
}


def installed_markdown(skill_dir):
    return [skill_dir / "SKILL.md", *sorted((skill_dir / "references").glob("*.md"))]


def unshipped_docs_citations(skill_dir, exempt=frozenset()):
    problems = []
    for doc in installed_markdown(skill_dir):
        if not doc.is_file():
            continue
        for match in DOCS_PATH.finditer(doc.read_text(encoding="utf-8")):
            path = match.group("path")
            if path not in exempt and not (skill_dir / path).is_file():
                problems.append(f"{doc.relative_to(skill_dir)}: {match.group(0)}")
    return problems


def repository_doc_links(text):
    return [target.rstrip(URL_TRAILING_PUNCTUATION) for target in REPO_BLOB.findall(text)]


def tracked_files(repo):
    listed = subprocess.run(["git", "-C", str(repo), "ls-files", "-z"],
                            capture_output=True, check=True).stdout
    return {os.fsdecode(entry) for entry in listed.split(b"\0") if entry}


def untracked_repository_links(skill_dir, tracked):
    problems = []
    for doc in installed_markdown(skill_dir):
        if not doc.is_file():
            continue
        for target in repository_doc_links(doc.read_text(encoding="utf-8")):
            if target not in tracked:
                problems.append(f"{doc.relative_to(skill_dir)}: {target}")
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
        project = (SKILLS / "brood-project" / "SKILL.md").read_text()
        self.assertNotIn("../brood-swarm/scripts", project)

    def test_debrief_resolver_uses_loaded_copy_from_another_project(self):
        loaded = self.prefix / "brood-debrief"
        self.env["HANIG_DEBRIEF_DIR"] = str(loaded)
        result = subprocess.run(
            ["sh", "-c", 'exec "$1" "$HANIG_DEBRIEF_DIR/scripts/skill_paths.py" '
             'self "$HANIG_DEBRIEF_DIR" brood-debrief', "resolver", sys.executable],
            cwd=str(self.project), env=self.env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), str(loaded.resolve()))

    def test_contract_handoff_survey_and_validation_use_copy_not_checkout(self):
        contract = self.invoke("brood-verified-workflow/scripts/contract.py", "init",
                               "run", "--command", "true", "--output", "out.txt")
        self.assertEqual(contract.returncode, 0, contract.stderr)
        self.assertTrue((self.project / "run" / "contract.json").is_file())
        self.assertFalse((self.prefix / "brood-verified-workflow" / "run").exists())

        run_dir = self.project / "handoff run"
        run_dir.mkdir()
        handoff = self.invoke("brood-portable-handoff/scripts/handoff.py", "capture",
                              str(run_dir), "--out", "handoff.json")
        self.assertEqual(handoff.returncode, 0, handoff.stderr)
        self.assertTrue((self.project / "handoff.json").is_file())

        survey = self.invoke("brood-project/scripts/survey.py", "--repo", ".",
                             "--out", ".swarm/survey.json")
        self.assertEqual(survey.returncode, 0, survey.stderr)
        self.assertTrue((self.project / ".swarm" / "survey.json").is_file())

        plan = {"name": "portable", "units": [{"id": "check", "kind": "slurm",
                "runtime": "none", "command": "true", "outputs": ["out.txt"]}]}
        (self.project / "plan.json").write_text(json.dumps(plan))
        valid = self.invoke("brood-swarm/scripts/swarm.py", "validate", "plan.json")
        self.assertEqual(valid.returncode, 0, valid.stderr + valid.stdout)

    def test_review_configuration_and_project_sibling_are_portable(self):
        review = self.invoke("brood-review-gate/scripts/review.py", "--list", "--no-probe")
        # Loading a valid copied config is distinct from having provider keys.
        # An empty HOME/environment honestly reports REVIEW_UNAVAILABLE (2).
        self.assertEqual(review.returncode, 2, review.stderr + review.stdout)
        config = json.loads((self.prefix / "brood-review-gate" / "reviewers.json").read_text())
        expected = [reviewer for reviewer in config["reviewers"]
                    if config["default_profile"] in reviewer.get("profiles", [])]
        self.assertTrue(expected)
        for reviewer in expected:
            self.assertIn(reviewer["name"], review.stdout)
        self.assertIn("No reviewer can run", review.stdout)
        self.assertNotIn("REVIEW_ERROR", review.stdout + review.stderr)

        resolver = self.prefix / "brood-project" / "scripts" / "skill_paths.py"
        found = subprocess.run([sys.executable, str(resolver), "sibling",
                                str(self.prefix / "brood-project"), "brood-project",
                                "brood-swarm"], env=self.env, text=True, capture_output=True)
        self.assertEqual(found.returncode, 0, found.stderr)
        self.assertEqual(Path(found.stdout.strip()),
                         (self.prefix / "brood-swarm").resolve())

        shutil.rmtree(self.prefix / "brood-swarm")
        missing = subprocess.run([sys.executable, str(resolver), "sibling",
                                  str(self.prefix / "brood-project"), "brood-project",
                                  "brood-swarm"], env=self.env, text=True, capture_output=True)
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("missing declared installed dependency 'brood-swarm'",
                      missing.stderr)

    def test_linked_loaded_skill_keeps_logical_parent_and_uses_known_root(self):
        linked_parent = self.root / "link store"
        linked_parent.mkdir()
        linked_project = linked_parent / "brood-project"
        linked_project.symlink_to(self.prefix / "brood-project", target_is_directory=True)
        separate = self.root / "separate known parent"
        separate.mkdir()
        shutil.copytree(SKILLS / "brood-swarm", separate / "brood-swarm")
        resolver = self.prefix / "brood-project" / "scripts" / "skill_paths.py"
        found = subprocess.run([sys.executable, str(resolver), "sibling",
                                str(linked_project), "brood-project", "brood-swarm",
                                "--root", str(separate)], env=self.env,
                               text=True, capture_output=True)
        self.assertEqual(found.returncode, 0, found.stderr)
        self.assertEqual(Path(found.stdout.strip()),
                         (separate / "brood-swarm").resolve())

        mixed_env = dict(self.env, HANIG_SKILL_DEP_ROOTS=str(separate))
        report = subprocess.run([sys.executable,
                                 str(linked_project / "scripts" / "report.py"),
                                 ".", "--json"], cwd=self.project, env=mixed_env,
                                text=True, capture_output=True)
        self.assertEqual(report.returncode, 0, report.stderr)

    def test_project_report_resolves_its_sibling_without_source_layout(self):
        report = self.invoke("brood-project/scripts/report.py", ".", "--json")
        self.assertEqual(report.returncode, 0, report.stderr)

    def test_missing_relative_root_reports_loaded_anchor_and_absolute_escape(self):
        shutil.rmtree(self.prefix / "brood-swarm")
        links = self.root / "logical link store"
        links.mkdir()
        for name in ("brood-project", "brood-orchestrate"):
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
                             "sibling", str(loaded), name, "brood-swarm", *option],
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
        shutil.move(str(self.prefix / "brood-swarm"), str(deps / "brood-swarm"))
        # ../deps is valid from the project cwd, but not from the loaded skill.
        self.assertEqual((self.project / "../deps").resolve(), deps.resolve())
        consumers = (("brood-project", "report.py", [".", "--json"]),
                     ("brood-orchestrate", "merge_unit.py", ["--help"]))
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
                if name == "brood-project":
                    self.assertIsInstance(json.loads(migrated.stdout), dict)
                else:
                    self.assertIn("usage:", migrated.stdout)

    def test_both_consumers_use_declared_roots_from_unrelated_cwd(self):
        consumers = (("brood-project", "report.py", [".", "--json"]),
                     ("brood-orchestrate", "merge_unit.py", ["--help"]))
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
                shutil.copytree(self.prefix / "brood-swarm", deps / "brood-swarm")
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
                                 "sibling", str(loaded), name, "brood-swarm", *option],
                                cwd=self.project, env=resolver_env,
                                text=True, capture_output=True)
                            self.assertEqual(found.returncode, 0, found.stderr)
                            self.assertEqual(Path(found.stdout.strip()),
                                             (deps / "brood-swarm").resolve())
                        result = subprocess.run(
                            [sys.executable, str(loaded / "scripts" / program), *args],
                            cwd=self.project, env=env, text=True, capture_output=True)
                        self.assertEqual(result.returncode, 0,
                                         result.stderr + result.stdout)
                        if name == "brood-project":
                            self.assertIsInstance(json.loads(result.stdout), dict)
                        else:
                            self.assertIn("usage:", result.stdout)


    def test_installed_markdown_cites_no_unshipped_repository_docs(self):
        for name in AUTHORED:
            with self.subTest(skill=name):
                self.assertEqual(unshipped_docs_citations(
                    self.prefix / name, OPERATED_CHECKOUT_PATHS.get(name, set())), [])
        gate = (self.prefix / "brood-review-gate" / "SKILL.md").read_text()
        compat = "https://github.com/hanig/brood-lord/blob/main/docs/agent-compatibility.md"
        self.assertTrue(compat in gate, "brood-review-gate must cite " + compat)

    def test_repository_doc_links_name_tracked_files(self):
        tracked = tracked_files(REPO)
        for name in AUTHORED:
            with self.subTest(skill=name):
                self.assertEqual(untracked_repository_links(SKILLS / name, tracked), [])

    def test_repository_doc_link_parser_drops_trailing_sentence_punctuation(self):
        blob = "https://github.com/hanig/brood-lord/blob/main/"
        text = (f"See {blob}docs/agent-compatibility.md. Then {blob}docs/a.md, "
                f"{blob}docs/b.md; {blob}docs/c.md: and ({blob}docs/d.md).\n")
        self.assertEqual(repository_doc_links(text), [
            "docs/agent-compatibility.md", "docs/a.md", "docs/b.md", "docs/c.md", "docs/d.md"])

    def test_repository_doc_link_check_requires_a_tracked_file(self):
        skill = self.root / "hanig-example"
        skill.mkdir()
        # Present on disk but absent from the tracked set: must not satisfy it.
        (skill / "docs").mkdir()
        (skill / "docs" / "local-only.md").write_text("x\n")
        blob = "https://github.com/hanig/brood-lord/blob/main/"
        (skill / "SKILL.md").write_text(
            f"Read {blob}docs/tracked.md.\nAlso {blob}docs/local-only.md.\n")
        self.assertEqual(untracked_repository_links(skill, {"docs/tracked.md"}),
                         ["SKILL.md: docs/local-only.md"])

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

    def test_unshipped_docs_citation_detector_rejects_a_dot_slash_prefix(self):
        skill = self.root / "hanig-example"
        (skill / "docs").mkdir(parents=True)
        (skill / "docs" / "shipped.md").write_text("x\n")
        (skill / "SKILL.md").write_text(
            "Report it unreviewed (`./docs/agent-compatibility.md`).\n"
            "See [notes](./docs/shipped.md) and ./docs/orchestrator-mandate.md.\n"
            "Outside the skill: ../docs/elsewhere.md.\n")
        self.assertEqual(
            unshipped_docs_citations(skill, {"docs/orchestrator-mandate.md"}),
            ["SKILL.md: ./docs/agent-compatibility.md"])


if __name__ == "__main__":
    unittest.main()
