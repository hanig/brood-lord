"""Presence checks, not behavioral proof, for the L1/L2/L4/L6 wiring: declarations
reach SKILL.md bodies, pins and shipped flags are documented, unshipped flags are
not. Deleting each pin in a copy proves its check fails."""

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
REG = "skills/brood-swarm/scripts/declaration_registry.py"  # closed inventories: test_skill_shape
NEW = {"brood-orchestrate": ("review.counterexample-evidence", "review.premise-check",
                             "watch.pr-oracle"),
       "brood-debrief": ("capability.decisions-log", "capability.audit", "capability.attention",
                         "workflow.durability")}
OD, DB, RG = ("skills/brood-%s/SKILL.md" % s for s in ("orchestrate", "debrief", "review-gate"))
HO, RM = "skills/brood-portable-handoff/SKILL.md", "README.md"
RULES = ((OD, "never executes reviewer text"), (OD, "downgrades REVIEW_FAIL"),
         (OD, "pass --require-premise-ack"), (OD, "read-only PR-status oracle"),
         (OD, "Never execute reviewer commands or auto-downgrade severity"),
         (DB, "Drafting is offline; only debrief_file.py reaches the network, "
              "after one named approval of the exact packet."),
         (DB, "never globs unrelated projects"), (DB, "never prints raw transcript text"),
         (DB, "imports no network module"), (DB, "beats prose"), (RG, "(L1):**"),
         (RG, "never changes a verdict"), (RG, "struck in rounds N-2 and N-1"), (HO, "decisions_log.py"), (HO, "debrief_audit.py"),
         (RM, "| `brood-hillclimb` |"), (RM, "All ten are"), (RM, "skills/brood-hillclimb/"),
         (RM, "| `brood-conclave` |"), (RM, "skills/brood-conclave/"),
         (RM, "lib/skill_agent_policy.py   --check"))
UNSHIPPED = tuple((doc, flag) for doc in (OD, DB, RG, RM) for flag in (
    "premise-replay", "--allow-egress", "debrief_attention.py run", "--rerun"))
CLIS = dict({n: "skills/brood-%s/scripts/" % s for n, s in (
    ("watch_pr.py", "orchestrate"), ("hillclimb.py", "hillclimb"), ("review.py", "review-gate"),
    ("conclave.py", "conclave"),
    ("decisions_log.py", "debrief"), ("debrief_audit.py", "debrief"),
    ("debrief_attention.py", "debrief"))}, **{"skill_agent_policy.py": "lib/"})
REVIEW_FLAGS = ("--record-evidence --head --finding --round --method --status --tree --test-ref "
                "--note --require-premise-ack --premise-memo --continue-patching")


def _read(root, relative):
    return (Path(root) / relative).read_text(encoding="utf-8")


def wiring_problems(root):
    problems = []
    for skill, ids in NEW.items():
        data = json.loads(_read(root, "skills/%s/declarations.json" % skill))
        texts = {d["id"]: d["normative_text"] for d in data["declarations"]}
        body = _read(root, "skills/%s/SKILL.md" % skill).split("GENERATED DECLARATIONS")
        for key in ids:
            where = "%s %s: " % (skill, key)
            if key not in data["known_declarations"] or key not in texts:
                problems.append(where + "not registered")
            elif len(body) != 3 or not any(  # exact line: a stale longer bullet is not a match
                    line.split(" (Elaboration: ")[0] == "- **`%s`:** %s" % (key, texts[key])
                    for line in body[1].splitlines()):
                problems.append(where + "not rendered")
    problems += ["%s: missing %r" % r for r in RULES if r[1] not in _read(root, r[0])]
    return problems + ["%s: unshipped %r" % r for r in UNSHIPPED if r[1] in _read(root, r[0])]


def invocations(text, name):
    """(subcommand or None, flags) per fenced line or inline code span naming name."""
    text, fenced, snippets, found = text.replace("\\\n", " "), False, [], []
    for part in re.split(r"^```.*$", text, flags=re.M):
        snippets += part.splitlines() if fenced else re.findall(r"`([^`]+)`", part)
        fenced = not fenced
    for snippet in snippets:
        match = re.search(r"(?:^|[\s/\"])%s\"?\s+(.*)" % re.escape(name), snippet, re.S)
        if match:
            rest = re.split(r"\s(?:\||&&|;|>)\s|#", match.group(1))[0]  # first command only
            sub = rest.split()[0].strip("'\"") if rest.split() and rest[0] not in "-[" else None
            found.append((sub, set(re.findall(r"--[a-z][a-z0-9-]*", rest))))
    return found


def _run(*argv):
    return subprocess.run([sys.executable] + [str(a) for a in argv], stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, universal_newlines=True, timeout=120)


class TestPstackWiring(unittest.TestCase):
    def mutated(self, relative, edit):
        with tempfile.TemporaryDirectory() as temporary:
            for name in {OD, DB, RG, HO, RM} | {
                    "skills/%s/declarations.json" % s for s in NEW}:
                (Path(temporary) / name).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(str(ROOT / name), str(Path(temporary) / name))
            path = Path(temporary) / relative
            text = path.read_text(encoding="utf-8")
            self.assertNotEqual(edit(text), text, relative)
            path.write_text(edit(text), encoding="utf-8")
            return wiring_problems(temporary)

    def test_live_tree_is_wired_and_deleting_any_pin_fails(self):
        self.assertEqual(wiring_problems(ROOT), [])
        for doc, phrase in RULES:
            with self.subTest(doc=doc, rule=phrase):
                self.assertIn("%s: missing %r" % (doc, phrase),
                              self.mutated(doc, lambda t: t.replace(phrase, "")))
        for doc, phrase in UNSHIPPED:
            with self.subTest(doc=doc, unshipped=phrase):
                self.assertIn("%s: unshipped %r" % (doc, phrase),
                              self.mutated(doc, lambda t: t + phrase))
        for skill, ids in NEW.items():
            for key in ids:
                for relative, edit, problem in (
                        ("skills/%s/SKILL.md" % skill, lambda t: t.replace(key + "`:**", "x`:**"),
                         "not rendered"),
                        ("skills/%s/declarations.json" % skill,
                         lambda t: t.replace('"%s"' % key, '"x.y"'), "not registered")):
                    with self.subTest(key=key, file=relative):
                        self.assertIn("%s %s: %s" % (skill, key, problem),
                                      self.mutated(relative, edit))

    def test_documented_flags_exist_in_shipped_parsers(self):
        self.assertEqual(invocations("`x/a.py --b\n--c`\n```\na.py 's|t' --d \\\n --e | b --f\n```",
                                     "a.py"), [(None, {"--b", "--c"}), ("s|t", {"--d", "--e"})])
        review = _run(ROOT / CLIS["review.py"] / "review.py", "--help").stdout
        for doc in (RG, RM):  # every flag spelled in the L1/L2 paragraphs, not a hard-coded list
            found = set(re.findall(r"`(--[a-z][a-z0-9-]*)", "".join(
                p for p in _read(ROOT, doc).split("\n\n") if "(L1" in p or "(L2" in p)))
            self.assertEqual((set(REVIEW_FLAGS.split()) - found, {f for f in found if not re.search(
                r"(?<![\w-])%s(?![\w-])" % f, review)}), (set(), set()), doc)
        for name in sorted(CLIS):
            found = invocations(_read(ROOT, RM), name)
            self.assertTrue(found, name)
            for sub, flags in ((s, f) for subs, f in found for s in (subs or "").split("|")):
                out = _run(ROOT / CLIS[name] / name, *([sub] if sub else []), "--help")
                self.assertEqual((out.returncode, {f for f in flags if not re.search(
                    r"(?<![\w-])%s(?![\w-])" % f, out.stdout)}), (0, set()), (name, sub))

    def test_agent_policy_still_passes_after_write_body(self):
        with tempfile.TemporaryDirectory() as temporary:
            skills = Path(temporary) / "skills"
            shutil.copytree(str(ROOT / "skills"), str(skills), symlinks=True,
                            ignore=shutil.ignore_patterns("__pycache__"))
            for skill in NEW:
                for command in ("write-body", "check-body", "check-references"):
                    out = _run(ROOT / REG, "--skill-dir", skills / skill, command)
                    self.assertEqual(out.returncode, 0, (skill, command, out.stdout))
            policy = (ROOT / "lib/skill_agent_policy.py", "--check", "--skills-root", skills)
            self.assertEqual(_run(*policy).returncode, 0)
            target = skills / "brood-debrief" / "SKILL.md"  # the check must see lost frontmatter
            target.write_text(target.read_text(encoding="utf-8").replace(
                "disable-model-invocation: true\n", "", 1), encoding="utf-8")
            self.assertEqual(_run(*policy).returncode, 1)

    def test_handoff_parses_at_python_37_and_claude_md_fits(self):
        ast.parse(_read(ROOT, "skills/brood-portable-handoff/scripts/handoff.py"),
                  feature_version=(3, 7))
        self.assertLess((ROOT / "CLAUDE.md").stat().st_size, 18000)


if __name__ == "__main__":
    unittest.main()
