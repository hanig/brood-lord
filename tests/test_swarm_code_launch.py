"""Code-launch extraction preserves exports and patch delivery."""

import ast
import json
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
import swarm_code_launch as SCL

DEFINITIONS = (
    "_capture_code_launch", "_code_launch_intent_problem", "_paseo_workspace_id",
    "_code_launch_record_payload", "_create_code_worktree", "_register_code_workspace",
    "_write_code_launch_record", "_complete_code_launch", "_write_launch_record",
)
CONSTANTS = ("LAUNCH_RECORD",)


class TestCodeLaunchExtraction(unittest.TestCase):
    def test_identical_reexports(self):
        namespace = {}
        exec("from swarm import " + ", ".join(DEFINITIONS + CONSTANTS), namespace)
        for name in DEFINITIONS + CONSTANTS:
            with self.subTest(name=name):
                self.assertIs(getattr(S, name), getattr(SCL, name))
                self.assertIs(namespace[name], getattr(SCL, name))
                if name in DEFINITIONS:
                    self.assertEqual(getattr(SCL, name).__module__, SCL.__name__)
                    self.assertIs(getattr(SCL, name).__globals__, SCL.__dict__)

    def test_payload_patch_reaches_reexported_writer(self):
        with tempfile.TemporaryDirectory(prefix="launch-writer-") as directory:
            attempt = Path(directory) / "unit" / "attempt"
            facts = {"unit_id": "unit"}
            with mock.patch.object(SCL, "_code_launch_record_payload",
                                   return_value=b"fixture payload\n") as build:
                seal, error = S._write_code_launch_record(attempt, facts)
            self.assertIsNone(error)
            self.assertTrue(seal)
            self.assertEqual(SCL.W.launch_record_path(attempt).read_bytes(),
                             b"fixture payload\n")
            build.assert_called_once_with(facts)

    def test_owner_lookups(self):
        tree = ast.parse(Path(S.__file__).read_text())
        bare = [(n.id, n.lineno) for n in ast.walk(tree)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
                and n.id in DEFINITIONS + CONSTANTS]
        consumers = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
                     and isinstance(n.value, ast.Name) and n.value.id == "SCL"}
        self.assertEqual(consumers, set(DEFINITIONS) - {
            "_code_launch_record_payload", "_write_code_launch_record"})
        self.assertEqual(bare, [], "facade bypasses owner patches")

    def test_patch_owners(self):
        offenders, owner_patches = [], set()
        for path in sorted((ROOT / "tests").glob("test_*.py")):
            tree = ast.parse(path.read_text())
            imports = {(a.name, a.asname or a.name) for n in ast.walk(tree)
                       if isinstance(n, ast.Import) for a in n.names}
            facades = {a for m, a in imports if m == "swarm"}
            owners = {a for m, a in imports if m == "swarm_code_launch"}
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
                    if target in owners | {"swarm_code_launch", "S.SCL", "self.S.SCL"}:
                        owner_patches.add(name)
        self.assertEqual(owner_patches, {
            "_register_code_workspace", "_create_code_worktree",
            "_code_launch_record_payload"})
        self.assertEqual(offenders, [])

    def test_standalone_offline_import(self):
        tree = ast.parse(Path(SCL.__file__).read_text())
        found = {n.name for n in tree.body
                 if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
        self.assertEqual(found, set(DEFINITIONS))
        forbidden = {"swarm", "review", "committee", "resolve_models", "linear_api",
                     "linear_sync", "merge_unit", "urllib", "http", "socket", "ssl"}
        imports = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        imports |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import)
                    for a in n.names}
        self.assertFalse({str(n).split(".")[0] for n in imports} & forbidden)
        # pathlib on Python 3.9 imports urllib.parse for path quoting;
        # urllib.request is the network client, as in the preflight probe.
        blocked = (forbidden - {"urllib"}) | {"urllib.request"}
        # Exclude interpreter startup hooks so the module graph belongs to
        # this probe; sitecustomize may legitimately preload network clients.
        result = subprocess.run([sys.executable, "-S", "-c", (
            "import sys; sys.path.insert(0, sys.argv[1]); import swarm_code_launch; "
            "assert not set(sys.modules).intersection(" + repr(blocked) + ")"
        ), str(SCRIPTS)], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_offline_probe_ignores_startup_customization(self):
        with tempfile.TemporaryDirectory(prefix="launch-startup-") as directory:
            Path(directory, "sitecustomize.py").write_text("import ssl\n")
            with mock.patch.dict(os.environ, {"PYTHONPATH": directory}):
                self.test_standalone_offline_import()


class TestReviewLineageLaunch(unittest.TestCase):
    def test_advance_records_full_chain_before_launch(self):
        from tests.test_attempt_worktrees import TestPerAttemptWorktrees, code_unit, paseo_resolvable
        fixture = TestPerAttemptWorktrees()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        units = [code_unit(fixture.repo, uid) for uid in ("a", "b", "c")]
        for i in (1, 2):
            units[i]["continuation_of"] = units[i - 1]["id"]
        state = {"schema_version": 1, "halted": None, "units": {
            "a": {"state": "DONE"}, "b": {"state": "DONE"}}}
        plan = {"name": "fixture", "units": units}
        state_dir = str(fixture.tmp / "state")
        ok, why = S.acquire_lease(state_dir)
        self.assertTrue(ok, why)
        self.addCleanup(S.release_lease, state_dir)
        with paseo_resolvable():
            report, count, _ = S.advance(plan, state, state_dir,
                                        str(fixture.tmp / "runs"), False, max_new=1)
        self.assertEqual(count, 1, report)
        unit = state["units"]["c"]
        decision = unit["attempt_review_lineages"][Path(unit["attempt_dir"]).name]
        self.assertEqual(decision, {"review_lineage": ["a", "b", "c"], "enrollment": "enrolled"})
        self.assertIn('SWARM_REVIEW_LINEAGE=["a", "b", "c"]', fixture.fake.launches[-1])

    def test_enrollment_shapes_and_history_reach_launch(self):
        from tests.test_attempt_worktrees import TestPerAttemptWorktrees, code_unit, paseo_resolvable
        # Exhaustive 3 shapes x 3 histories x every possible prior ancestor.
        for size in (1, 2, 3):
            for history in ("none", "enrolled", "missing", "legacy"):
                for ancestor in range(size):
                    with self.subTest(size=size, history=history, ancestor=ancestor):
                        fixture = TestPerAttemptWorktrees()
                        fixture.setUp()
                        try:
                            units = [code_unit(fixture.repo, "u" + str(i)) for i in range(size)]
                            for i in range(1, size):
                                units[i]["continuation_of"] = units[i - 1]["id"]
                            unit = units[-1]
                            unit["env"] = ["SWARM_UNIT_ID=" + unit["id"]]
                            chain = S.SPR.review_lineage({"units": units}, unit["id"])
                            state = {"units": {}}
                            if history != "none":
                                prior = {"attempts": ["/synthetic/old"]}
                                if history != "missing":
                                    prior["attempt_review_lineages"] = {"old": {
                                        "review_lineage": chain[:ancestor + 1],
                                        "enrollment": history}}
                                state["units"][chain[ancestor]] = prior
                            attempt = fixture.attempt(unit["id"], "chain")
                            with paseo_resolvable():
                                job, error = S._submit(unit, str(attempt), False, state,
                                                       str(fixture.tmp / "state"), lineage_chain=chain)
                            self.assertIsNone(error)
                            self.assertTrue(job)
                            enrolled = history in ("none", "enrolled")
                            expected = {"review_lineage": chain,
                                        "enrollment": "enrolled" if enrolled else "legacy"}
                            actual = state["units"][unit["id"]]["attempt_review_lineages"]["chain"]
                            self.assertEqual(actual, expected)
                            durable = S.load_state(str(fixture.tmp / "state"))
                            self.assertEqual(durable["units"][unit["id"]]["attempt_review_lineages"]["chain"],
                                             expected)
                            argv = fixture.fake.launches[-1]
                            values = [argv[i + 1] for i, v in enumerate(argv) if v == "--env"]
                            self.assertEqual([v for v in values if v.startswith("SWARM_REVIEW_LINEAGE=")],
                                             ["SWARM_REVIEW_LINEAGE=" + (json.dumps(chain) if enrolled else "legacy")])
                            self.assertEqual(values.count("SWARM_UNIT_ID=" + unit["id"]), 1)
                        finally:
                            fixture.doCleanups()


if __name__ == "__main__":
    unittest.main()
