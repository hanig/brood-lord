"""Code-protocol extraction preserves exports and patch delivery."""

import ast
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills/hanig-swarm/scripts"
sys.path.insert(0, str(SCRIPTS))
import swarm as S
import swarm_code_protocol as SCP

DEFINITIONS = (
    "_seed_carry_forward", "_code_completion_protocol", "_dispatch_prompt",
    "_code_protocol_problem",
)
CONSTANTS = ("CODE_COMPLETION_PROTOCOL_MARKER",)


class TestCodeProtocolExtraction(unittest.TestCase):
    def test_identical_reexports(self):
        namespace = {}
        exec("from swarm import " + ", ".join(DEFINITIONS + CONSTANTS), namespace)
        for name in DEFINITIONS + CONSTANTS:
            with self.subTest(name=name):
                self.assertIs(getattr(S, name), getattr(SCP, name))
                self.assertIs(namespace[name], getattr(SCP, name))
                if name in DEFINITIONS:
                    self.assertEqual(getattr(SCP, name).__module__, SCP.__name__)
                    self.assertIs(getattr(SCP, name).__globals__, SCP.__dict__)

    def test_builder_patch_reaches_dispatch_prompt(self):
        intent = {"repo": "/fixture"}
        with mock.patch.object(SCP, "_code_completion_protocol",
                               return_value="fixture protocol") as build:
            prompt = S._dispatch_prompt({"kind": "code", "prompt": "work"}, intent)
        self.assertEqual(prompt, "work\n\nfixture protocol")
        build.assert_called_once_with(intent)

    def test_owner_lookups(self):
        tree = ast.parse(Path(S.__file__).read_text())
        bare = [(n.id, n.lineno) for n in ast.walk(tree)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
                and n.id in DEFINITIONS + CONSTANTS]
        consumers = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
                     and isinstance(n.value, ast.Name) and n.value.id == "SCP"}
        self.assertEqual(consumers, {"_dispatch_prompt", "_code_protocol_problem"})
        self.assertEqual(bare, [], "facade bypasses owner patches")

    def test_patch_owners(self):
        offenders, owner_patches = [], set()
        for path in sorted((ROOT / "tests").glob("test_*.py")):
            tree = ast.parse(path.read_text())
            imports = {(a.name, a.asname or a.name) for n in ast.walk(tree)
                       if isinstance(n, ast.Import) for a in n.names}
            facades = {a for m, a in imports if m == "swarm"}
            owners = {a for m, a in imports if m == "swarm_code_protocol"}
            for node in ast.walk(tree):
                target, name = None, None
                if isinstance(node, ast.Call):
                    args = node.args
                    method = getattr(node.func, "attr", None)
                    function = getattr(node.func, "id", None)
                    if (method == "addCleanup" and args
                            and getattr(args[0], "id", None) == "setattr"):
                        args = args[1:]
                    if (len(args) >= 2 and isinstance(args[1], ast.Constant)
                            and (method in {"object", "addCleanup"}
                                 or function == "setattr")):
                        target, name = ast.unparse(args[0]), args[1].value
                    elif (args and isinstance(args[0], ast.Constant)
                          and isinstance(args[0].value, str)):
                        target, _, name = args[0].value.rpartition(".")
                elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store):
                    target, name = ast.unparse(node.value), node.attr
                if name in DEFINITIONS + CONSTANTS:
                    if target in facades | {"swarm", "self.S"}:
                        offenders.append((path.name, node.lineno, target, name))
                    if target in owners | {"swarm_code_protocol", "S.SCP", "self.S.SCP"}:
                        owner_patches.add(name)
        self.assertEqual(owner_patches, {"_code_completion_protocol", "_dispatch_prompt"})
        self.assertEqual(offenders, [])

    def test_standalone_offline_import(self):
        tree = ast.parse(Path(SCP.__file__).read_text())
        found = {n.name for n in tree.body
                 if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
        self.assertEqual(found, set(DEFINITIONS))
        forbidden = {"swarm", "review", "committee", "resolve_models", "linear_api",
                     "linear_sync", "merge_unit", "urllib", "http", "socket", "ssl"}
        imports = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        imports |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import)
                    for a in n.names}
        self.assertFalse({str(n).split(".")[0] for n in imports} & forbidden)
        # Exclude interpreter startup hooks so the module graph belongs to
        # this probe; sitecustomize may legitimately preload network clients.
        result = subprocess.run([sys.executable, "-S", "-c", (
            "import sys; sys.path.insert(0, sys.argv[1]); import swarm_code_protocol; "
            "assert not set(sys.modules).intersection(" + repr(forbidden) + ")"
        ), str(SCRIPTS)], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_offline_probe_ignores_startup_customization(self):
        with tempfile.TemporaryDirectory(prefix="protocol-startup-") as directory:
            Path(directory, "sitecustomize.py").write_text("import ssl\n")
            with mock.patch.dict(os.environ, {"PYTHONPATH": directory}):
                self.test_standalone_offline_import()


class TestRepairLoopProtocol(unittest.TestCase):
    INTENT = {
        "repo": "/fixture/project", "branch": "swarm-fixture",
        "base_commit": "a" * 40, "target_branch": "main",
        "repository_remote": "/fixture/origin.git",
        "judgment_ref": "refs/heads/swarm-fixture",
    }
    STEPS = (
        ("finding reproduction",
         "Reproduce each confirmed finding before acting on it."),
        ("in-envelope repair",
         "Repair the reproduced findings that fall inside the task's supported envelope and do not contradict the task."),
        ("repair round discipline",
         "Mutation-check each repair, commit all intended edits, and keep the worktree clean. If no findings reproduce, no code repair is required; record not-reproduced dispositions and gate the next round under the same round limit. Only after a REVIEW_FAIL, once all reproduced in-envelope findings (if any) are repaired, gate the next round with --dispositions FILE, up to the round limit the task states. A passing round never requires another. Before stopping for an orchestrator decision on one remaining finding, first repair and mutation-check every other reproduced in-envelope finding that does not contradict the task, and commit those repairs; only then preserve and stop."),
        ("exhausted or decision-bound review",
         "Only when a reproduced failure remains after the last permitted round, "
         "or a remaining finding needs an orchestrator decision (out of envelope, "
         "or contradicting the task), do NOT open a pull request: follow the recovery "
         "procedure below without moving the anchored attempt ref, then STOP AND "
         "REPORT where it was preserved."),
        ("passing review permission",
         "Only exit 0 (REVIEW_PASS) permits opening a pull request"),
    )

    def test_dispatched_repair_sequence_in_order(self):
        prompt = SCP._dispatch_prompt({"kind": "code", "prompt": "work"}, self.INTENT)
        self.assertIsNone(SCP._code_protocol_problem(prompt, self.INTENT))
        positions = []
        for name, sentence in self.STEPS:
            with self.subTest(step=name):
                self.assertEqual(prompt.count(sentence), 1)
                positions.append(prompt.index(sentence))
        self.assertEqual(positions, sorted(positions))
        self.assertNotIn("If a reproduced failure remains, do NOT open a pull request", prompt)
        self.assertIn("unavailable, partial, incomplete, and failing reviews are not a pass", prompt)

    def test_next_round_is_conditional_and_decision_stop_follows_repairs(self):
        prompt = S._dispatch_prompt({"kind": "code", "prompt": "work"}, self.INTENT)
        conditional = ("Only after a REVIEW_FAIL, once all reproduced in-envelope "
                       "findings (if any) are repaired, gate the next round")
        self.assertIn(conditional, prompt)
        self.assertIn("A passing round never requires another.", prompt)
        self.assertIn("commit those repairs; only then preserve and stop.", prompt)
        # Exercise the consuming guard even when the producer also regresses.
        for old in (conditional, "A passing round never requires another.",
                    "commit those repairs; only then preserve and stop."):
            with self.subTest(removed=old):
                protocol = SCP._code_completion_protocol(self.INTENT).replace(old, "")
                with mock.patch.object(SCP, "_code_completion_protocol", return_value=protocol):
                    mutated = S._dispatch_prompt({"kind": "code"}, self.INTENT)
                    self.assertEqual(SCP._code_protocol_problem(mutated, self.INTENT),
                                     "missing repair round discipline")

    def test_pass_head_and_false_findings_have_explicit_routes(self):
        prompt = S._dispatch_prompt({"kind": "code"}, self.INTENT)
        for phrase in (
                "After REVIEW_PASS, keep the reviewed head unchanged",
                "If that head changes, do not open a pull request; preserve and stop",
                "If no findings reproduce, no code repair is required",
                "record not-reproduced dispositions and gate the next round under the same round limit"):
            self.assertIn(phrase, prompt)
            protocol = SCP._code_completion_protocol(self.INTENT).replace(phrase, "")
            with mock.patch.object(SCP, "_code_completion_protocol", return_value=protocol):
                mutated = S._dispatch_prompt({"kind": "code"}, self.INTENT)
                self.assertIsNotNone(SCP._code_protocol_problem(mutated, self.INTENT))
        self.assertLess(prompt.index("For REVIEW_FAIL, follow this repair loop:"),
                        prompt.index("Repair the reproduced findings"))

    def test_false_only_failure_explicitly_permits_next_round(self):
        prompt = S._dispatch_prompt({"kind": "code"}, self.INTENT)
        self.assertIn("all reproduced in-envelope findings (if any) are repaired", prompt)
        self.assertIn("record not-reproduced dispositions and gate the next round", prompt)

    def test_required_phrase_guard_rejects_task_contradicting_repair(self):
        protocol = SCP._code_completion_protocol(self.INTENT)
        unqualified = protocol.replace(
            "supported envelope and do not contradict the task.",
            "supported envelope.")
        self.assertNotEqual(unqualified, protocol)
        with mock.patch.object(SCP, "_code_completion_protocol",
                               return_value=unqualified):
            prompt = SCP._dispatch_prompt({"kind": "code"}, self.INTENT)
            self.assertEqual(SCP._code_protocol_problem(prompt, self.INTENT),
                             "missing in-envelope repair")

    def test_required_phrase_guard_rejects_each_missing_step(self):
        protocol = SCP._code_completion_protocol(self.INTENT)
        for name, sentence in self.STEPS:
            with self.subTest(step=name):
                self.assertEqual(protocol.count(sentence), 1)
                # Patch the producer too: otherwise the suffix check alone
                # would reject the prompt without exercising the phrase table.
                with mock.patch.object(SCP, "_code_completion_protocol",
                                       return_value=protocol.replace(sentence, "")):
                    prompt = SCP._dispatch_prompt({"kind": "code"}, self.INTENT)
                    self.assertEqual(SCP._code_protocol_problem(prompt, self.INTENT),
                                     "missing " + name)


if __name__ == "__main__":
    unittest.main()
