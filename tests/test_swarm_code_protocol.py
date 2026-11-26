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
    "_code_protocol_problem", "attempt_context_values", "render_attempt_context",
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
        trees = [ast.parse(Path(m.__file__).read_text()) for m in (S, S.SV)]
        bare = [(n.id, n.lineno) for tree in trees for n in ast.walk(tree)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
                and n.id in DEFINITIONS + CONSTANTS]
        consumers = {n.attr for tree in trees for n in ast.walk(tree)
                     if isinstance(n, ast.Attribute)
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


class TestOrchestratorOwnedGateProtocol(unittest.TestCase):
    """The author is not its own reviewer; the gate runs where the orchestrator runs."""
    INTENT = {
        "repo": "/fixture/project", "branch": "swarm-fixture",
        "base_commit": "a" * 40, "target_branch": "main",
        "repository_remote": "/fixture/origin.git",
        "judgment_ref": "refs/heads/swarm-fixture",
        "provider": "p", "model": "author",
    }
    STEPS = (
        ("author is not its reviewer",
         "The author is not its own reviewer; the gate runs where the orchestrator runs."),
        ("worker review prohibition",
         "NEVER run review.py or committee.py on your own diff"),
        ("worker pull-request prohibition", "NEVER open a pull request yourself."),
        ("orchestrator review gate",
         "The orchestrator runs hanig-review-gate's review.py from its own Lor "
         "session on your pushed head"),
        ("repair continuation",
         "dispatches a repair continuation with the findings on REVIEW_FAIL."),
        ("scoped tests", "run its scoped tests"),
        ("mutation checks", "mutation-check every fix"),
        ("evidence hand-off", "write $SWARM_UNIT_DIR/evidence.md"),
        ("pushed head evidence", "the exact pushed HEAD SHA"),
        ("drafted pull request", "a drafted pull-request title and body"),
        ("test output hand-off", "write $SWARM_UNIT_DIR/test-output.txt"),
        ("finding reproduction",
         "Reproduce each confirmed finding before acting on it."),
        ("in-envelope repair",
         "Repair the reproduced findings that fall inside the task's supported envelope and do not contradict the task."),
        ("repair dispositions",
         "record a disposition (reproduced or not-reproduced, with its reproduction) "
         "for every finding in evidence.md, and STOP again."),
        ("decision-bound repair",
         "first repair and mutation-check every other reproduced in-envelope finding "
         "and push those repairs; then follow the recovery procedure below without "
         "moving the anchored attempt ref, and STOP AND REPORT where it was preserved."),
    )
    # Worker-side gate instructions this design retired. None may come back.
    RETIRED = (
        "run this repository's review gate",
        "Before opening a pull request",
        "Open a pull request",
        "After opening the pull request",
        "permits opening a pull request",
        "After REVIEW_PASS, keep the reviewed head unchanged",
        "gate the next round",
        "Pass --author",
        "--kind implementation",
        "--dispositions FILE",
    )

    def prompt(self, intent=None):
        return SCP._dispatch_prompt({"kind": "code", "prompt": "work"}, intent or self.INTENT)

    def test_dispatched_hand_off_sequence_in_order(self):
        prompt = self.prompt()
        self.assertIsNone(SCP._code_protocol_problem(prompt, self.INTENT))
        positions = []
        for name, sentence in self.STEPS:
            with self.subTest(step=name):
                self.assertEqual(prompt.count(sentence), 1)
                positions.append(prompt.index(sentence))
        self.assertEqual(positions, sorted(positions))

    def test_workers_never_run_the_gate_or_open_the_pull_request(self):
        for remote in (self.INTENT["repository_remote"], None):
            intent = dict(self.INTENT, repository_remote=remote)
            prompt = self.prompt(intent)
            with self.subTest(remote=remote):
                self.assertIsNone(SCP._code_protocol_problem(prompt, intent))
                self.assertIn("NEVER run review.py or committee.py on your own diff, "
                              "in any form, and NEVER open a pull request yourself.", prompt)
                for retired in self.RETIRED:
                    self.assertNotIn(retired, prompt)
                # The only review.py/committee.py mentions forbid the worker,
                # describe the orchestrator's run, or hand it the author flags.
                for line in prompt.splitlines():
                    if "review.py" in line or "committee.py" in line:
                        self.assertTrue(
                            line.startswith("ORCHESTRATOR-OWNED REVIEW GATE: "), line)
        self.assertIn("The orchestrator, not you, opens the pull request", self.prompt())
        self.assertIn("Record --author p/author in evidence.md as the author flags the "
                      "orchestrator passes on every review.py or committee.py run",
                      self.prompt())

    def test_restoring_a_worker_review_instruction_is_detected(self):
        protocol = SCP._code_completion_protocol(self.INTENT)
        legacy = protocol.replace(
            "NEVER run review.py or committee.py on your own diff",
            "Before opening a pull request, run this repository's review gate")
        self.assertNotEqual(legacy, protocol)
        with mock.patch.object(SCP, "_code_completion_protocol", return_value=legacy):
            prompt = SCP._dispatch_prompt({"kind": "code"}, self.INTENT)
            self.assertEqual(SCP._code_protocol_problem(prompt, self.INTENT),
                             "missing worker review prohibition")

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
