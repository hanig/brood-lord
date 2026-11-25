"""Wiring of the L1, L2, L4 and L6 capabilities into skill prose and README.

These are presence checks, not behavioral proof: they show that registered
declarations reach the generated SKILL.md bodies, that pinned rules and
documented flags are present, and that unimplemented flags are absent. They
cannot show that an agent obeys any rule. Each pinned rule is also deleted
from a temporary copy to prove its check fails.
"""

import ast
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = "skills/hanig-swarm/scripts/declaration_registry.py"
SHAPE = "tests/test_skill_shape.py"
BEGIN = "<!-- BEGIN GENERATED DECLARATIONS: declarations.json -->"
END = "<!-- END GENERATED DECLARATIONS -->"

NEW_DECLARATIONS = {
    "hanig-orchestrate": ("ORCHESTRATE_DECLARATIONS", (
        "review.counterexample-evidence", "review.premise-check", "watch.pr-oracle")),
    "hanig-debrief": ("DEBRIEF_DECLARATIONS", (
        "capability.decisions-log", "capability.audit", "capability.attention",
        "workflow.durability")),
}

OD = "skills/hanig-orchestrate/SKILL.md"
DB = "skills/hanig-debrief/SKILL.md"
RG = "skills/hanig-review-gate/SKILL.md"
HO = "skills/hanig-portable-handoff/SKILL.md"
RM = "README.md"
RULES = (
    (OD, "never executes reviewer text, and a record never closes a finding, "
         "changes a verdict or severity, or downgrades REVIEW_FAIL"),
    (OD, "pass --require-premise-ack"),
    (OD, "unless --premise-memo FILE"),
    (OD, "or --continue-patching REASON"),
    (OD, "Use scripts/watch_pr.py --repo OWNER/REPO --pr N as the read-only PR-status oracle"),
    (OD, "ci_zero_step_flake (exit 14)"),
    (OD, "It makes no mutating gh call and never checks out, reruns or edits a PR or workflow"),
    (OD, "Never execute reviewer commands or auto-downgrade severity"),
    (DB, "Drafting is offline; only debrief_file.py reaches the network, "
         "after one named approval of the exact packet."),
    (DB, "scripts/decisions_log.py keeps an append-only, SHA-256 hash-chained decisions.tsv"),
    (DB, "It is not tamper-proofing against a same-UID writer."),
    (DB, "it never globs unrelated projects"),
    (DB, "never raw transcript text"),
    (DB, "scripts/debrief_attention.py is offline"),
    (DB, "It imports no network module and calls no model or subprocess"),
    (DB, "structure (a lint, test or script at a repository-relative artifact_path) "
         "is preferred over prose"),
    (DB, "holds_6mo is self-attested, never verified."),
    (RG, "**Counterexample evidence (L1):**"),
    (RG, "`scripts/review_evidence.py` binds each record"),
    (RG, "**Premise check (L2):**"),
    (RG, "With `--require-premise-ack` on round N >= 2"),
    (RG, "`--premise-memo FILE`"),
    (RG, "`--continue-patching REASON`"),
    (HO, "use `hanig-debrief`: `scripts/decisions_log.py`"),
    (HO, "`scripts/debrief_audit.py` checks its claims"),
    (RM, "## The nine skills we wrote"),
    (RM, "| `hanig-hillclimb` |"),
    (RM, "All nine are prefixed `hanig-`"),
    (RM, "skills/hanig-hillclimb/          hillclimb.py"),
    (RM, "`watch_pr.py` is the read-only PR-status oracle"),
    (RM, "`--record-evidence` (L1"),
    (RM, "`--require-premise-ack` (L2"),
    (RM, "### hanig-debrief"),
    (RM, "### hanig-hillclimb"),
    (RM, "python3 lib/skill_agent_policy.py --check"),
)

# Flags and subcommands that are not shipped and must not be documented.
UNSHIPPED = (
    (RG, "premise-replay"), (RM, "premise-replay"), (OD, "premise-replay"),
    (DB, "--allow-egress"), (RM, "--allow-egress"),
    (DB, "debrief_attention.py run"), (RM, "debrief_attention.py run"),
    (OD, "watch_pr.py --rerun"), (RM, "--rerun"),
)

CLIS = {
    "lib/skill_agent_policy.py": "lib/skill_agent_policy.py",
    "watch_pr.py": "skills/hanig-orchestrate/scripts/watch_pr.py",
    "hillclimb.py": "skills/hanig-hillclimb/scripts/hillclimb.py",
    "decisions_log.py": "skills/hanig-debrief/scripts/decisions_log.py",
    "debrief_audit.py": "skills/hanig-debrief/scripts/debrief_audit.py",
    "debrief_attention.py": "skills/hanig-debrief/scripts/debrief_attention.py",
}
REVIEW_FLAGS = ("--record-evidence", "--finding", "--method", "--status", "--tree",
                "--test-ref", "--note", "--head", "--require-premise-ack",
                "--premise-memo", "--continue-patching")


def _read(root, relative):
    return (Path(root) / relative).read_text(encoding="utf-8")


def _closed(root):
    """CLOSED_DECLARATIONS from the registry source, without importing it."""
    for node in ast.walk(ast.parse(_read(root, REGISTRY))):
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and getattr(node.targets[0], "id", None) == "CLOSED_DECLARATIONS"):
            return {ast.literal_eval(k): tuple(ast.literal_eval(v.args[0]).split())
                    for k, v in zip(node.value.keys, node.value.values)}
    return {}


def _inventory(root, name):
    for node in ast.parse(_read(root, SHAPE)).body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and getattr(node.targets[0], "id", None) == name):
            return tuple(ast.literal_eval(node.value))
    return ()


def wiring_problems(root):
    problems = []
    closed = _closed(root)
    for skill, (constant, ids) in NEW_DECLARATIONS.items():
        registry = json.loads(_read(root, "skills/%s/declarations.json" % skill))
        texts = {d["id"]: d["normative_text"] for d in registry["declarations"]}
        text = _read(root, "skills/%s/SKILL.md" % skill)
        body = text[text.find(BEGIN):text.find(END)] if BEGIN in text and END in text else ""
        for declaration in ids:
            where = "%s %s" % (skill, declaration)
            if declaration not in closed.get(skill, ()):
                problems.append(where + ": not in CLOSED_DECLARATIONS")
            if declaration not in _inventory(root, constant):
                problems.append(where + ": not in " + constant)
            if declaration not in registry["known_declarations"] or declaration not in texts:
                problems.append(where + ": not registered in declarations.json")
            elif "- **`%s`:** %s" % (declaration, texts[declaration]) not in body:
                problems.append(where + ": not rendered in the generated body")
    for relative, phrase in RULES:
        if phrase not in _read(root, relative):
            problems.append("%s: missing pinned rule %r" % (relative, phrase))
    for relative, phrase in UNSHIPPED:
        if phrase in _read(root, relative):
            problems.append("%s: documents unshipped %r" % (relative, phrase))
    return problems


def readme_invocations(text, name):
    """(subcommand or None, flags) for each fenced README line invoking name."""
    text = text.replace("\\\n", " ")
    fenced, found = False, []
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        match = re.search(r"(?:^|[\s/\"])%s\"?\s+(.*)" % re.escape(name), line) if fenced else None
        if match:
            rest = match.group(1).split("#")[0]
            words = rest.split()
            sub = words[0] if words and not words[0].startswith(("-", "[")) else None
            found.append((sub, sorted(set(re.findall(r"--[a-z][a-z0-9-]*", rest)))))
    return found


def _help(script, *sub):
    result = subprocess.run([sys.executable, str(ROOT / script)] + list(sub) + ["--help"],
                            cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            universal_newlines=True, timeout=60)
    return result.returncode, result.stdout


def _copy(root, relatives):
    for relative in relatives:
        target = Path(root) / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(str(ROOT / relative), str(target))


class TestPstackWiring(unittest.TestCase):
    def test_live_tree_has_every_wired_rule(self):
        self.assertEqual(wiring_problems(ROOT), [])

    def _mutated(self, relative, edit):
        with tempfile.TemporaryDirectory() as temporary:
            files = {REGISTRY, SHAPE, OD, DB, RG, HO, RM,
                     "skills/hanig-orchestrate/declarations.json",
                     "skills/hanig-debrief/declarations.json"}
            _copy(temporary, files)
            path = Path(temporary) / relative
            original = path.read_text(encoding="utf-8")
            changed = edit(original)
            self.assertNotEqual(changed, original, relative)
            path.write_text(changed, encoding="utf-8")
            return wiring_problems(temporary)

    def test_deleting_any_pinned_rule_fails(self):
        for relative, phrase in RULES:
            with self.subTest(file=relative, rule=phrase):
                problems = self._mutated(relative, lambda t: t.replace(phrase, ""))
                self.assertIn("%s: missing pinned rule %r" % (relative, phrase), problems)

    def test_documenting_an_unshipped_flag_fails(self):
        for relative, phrase in UNSHIPPED:
            with self.subTest(file=relative, phrase=phrase):
                problems = self._mutated(relative, lambda t: t + "\n" + phrase + "\n")
                self.assertIn("%s: documents unshipped %r" % (relative, phrase), problems)

    def test_unregistering_or_unrendering_a_declaration_fails(self):
        for skill, (constant, ids) in NEW_DECLARATIONS.items():
            for declaration in ids:
                where = "%s %s" % (skill, declaration)
                cases = (
                    (REGISTRY, lambda t: re.sub(r"(?<=\s)%s(?=\s)" % re.escape(declaration), "", t),
                     where + ": not in CLOSED_DECLARATIONS"),
                    (SHAPE, lambda t: re.sub(r'"%s",?' % re.escape(declaration), "", t, count=1),
                     where + ": not in " + constant),
                    ("skills/%s/SKILL.md" % skill,
                     lambda t: t.replace("- **`%s`:**" % declaration, "- **`x`:**"),
                     where + ": not rendered in the generated body"),
                    ("skills/%s/declarations.json" % skill,
                     lambda t: t.replace('"%s"' % declaration, '"retired.%s"'
                                         % declaration.replace(".", "-")),
                     where + ": not registered in declarations.json"),
                )
                for relative, edit, expected in cases:
                    with self.subTest(declaration=declaration, file=relative):
                        self.assertIn(expected, self._mutated(relative, edit))

    def test_readme_flags_exist_in_shipped_parsers(self):
        readme = _read(ROOT, RM)
        for name, script in sorted(CLIS.items()):
            invocations = readme_invocations(readme, name)
            with self.subTest(cli=name):
                self.assertTrue(invocations, "README has no fenced invocation of " + name)
            code, top = _help(script)
            self.assertEqual(code, 0, name)
            for sub, flags in invocations:
                code, text = _help(script, sub) if sub is not None else (0, top)
                with self.subTest(cli=name, subcommand=sub):
                    self.assertEqual(code, 0, (name, sub))
                    for flag in flags:
                        self.assertRegex(text, r"(?<![\w-])%s(?![\w-])" % re.escape(flag),
                                         (name, sub, flag))

    def test_invocation_reader_sees_a_bogus_flag(self):
        text = "```sh\npython3 scripts/watch_pr.py --repo R \\\n  --pr 1 --bogus\n```\n"
        self.assertEqual(readme_invocations(text, "watch_pr.py"),
                         [(None, ["--bogus", "--pr", "--repo"])])
        code, top = _help(CLIS["watch_pr.py"])
        self.assertEqual(code, 0)
        self.assertNotRegex(top, r"(?<![\w-])--bogus(?![\w-])")

    def test_documented_review_flags_and_evidence_values_are_shipped(self):
        code, text = _help("skills/hanig-review-gate/scripts/review.py")
        self.assertEqual(code, 0)
        sys.path.insert(0, str(ROOT / "skills/hanig-review-gate/scripts"))
        try:
            import review_evidence
        finally:
            sys.path.remove(str(ROOT / "skills/hanig-review-gate/scripts"))
        for relative in (RG, RM):
            doc = _read(ROOT, relative)
            for flag in REVIEW_FLAGS:
                with self.subTest(document=relative, flag=flag):
                    self.assertIn(flag, doc)
                    self.assertRegex(text, r"(?<![\w-])%s(?![\w-])" % re.escape(flag))
            for values in (review_evidence.METHODS, review_evidence.STATUSES):
                self.assertIn("|".join(values), doc.replace("\n", ""), relative)

    def test_agent_policy_still_passes_after_write_body(self):
        with tempfile.TemporaryDirectory() as temporary:
            skills = Path(temporary) / "skills"
            shutil.copytree(str(ROOT / "skills"), str(skills), symlinks=True,
                            ignore=shutil.ignore_patterns("__pycache__"))
            for skill in NEW_DECLARATIONS:
                for command in ("write-body", "check-body", "check-references"):
                    result = subprocess.run(
                        [sys.executable, str(ROOT / REGISTRY), "--skill-dir",
                         str(skills / skill), command], stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT, universal_newlines=True, timeout=120)
                    self.assertEqual(result.returncode, 0, (skill, command, result.stdout))
                self.assertEqual((skills / skill / "SKILL.md").read_bytes(),
                                 (ROOT / "skills" / skill / "SKILL.md").read_bytes(), skill)

            def check():
                return subprocess.run(
                    [sys.executable, str(ROOT / "lib/skill_agent_policy.py"), "--check",
                     "--skills-root", str(skills)], stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, universal_newlines=True, timeout=120)
            result = check()
            self.assertEqual(result.returncode, 0, result.stdout)
            # The consumer must notice lost frontmatter, or the pass above proves nothing.
            target = skills / "hanig-debrief" / "SKILL.md"
            target.write_text(target.read_text(encoding="utf-8").replace(
                "disable-model-invocation: true\n", "", 1), encoding="utf-8")
            self.assertEqual(check().returncode, 1)

    def test_handoff_parses_at_python_37(self):
        source = _read(ROOT, "skills/hanig-portable-handoff/scripts/handoff.py")
        ast.parse(source, feature_version=(3, 7))
        with self.assertRaises(SyntaxError):
            ast.parse("if (x := 1):\n    pass\n", feature_version=(3, 7))

    def test_claude_md_stays_under_budget(self):
        self.assertLess((ROOT / "CLAUDE.md").stat().st_size, 18000)


if __name__ == "__main__":
    unittest.main()
