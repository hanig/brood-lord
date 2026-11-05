"""Workspace extraction preserves exports and patch delivery."""

import ast
import itertools
import json
import shutil
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
import swarm_workspaces as SW

DEFINITIONS = (
    "_paseo_json", "_paseo_error", "_paseo_workspace_records",
    "_paseo_workspace_for_path", "_paseo_path_ownership_problem",
    "_registered_attempt_workspace", "_recover_code_launch",
    "_report_retained_worktrees", "_report_would_archive_code_worktree",
    "_worktree_cleanup_failed", "_cleanup_codex_home", "_archive_code_worktree",
    "_preserve_and_archive_code_worktree", "reconcile_orphan",
)
CONSTANTS = ("WORKTREE_ARCHIVE_MAX_ATTEMPTS", "WORKTREE_CLEANUP_SUMMARY_PREFIX")


class TestWorkspacesExtraction(unittest.TestCase):
    def test_identical_reexports(self):
        namespace = {}
        exec("from swarm import " + ", ".join(DEFINITIONS + CONSTANTS), namespace)
        for name in DEFINITIONS + CONSTANTS:
            with self.subTest(name=name):
                self.assertIs(getattr(S, name), getattr(SW, name))
                self.assertIs(namespace[name], getattr(SW, name))
                if name in DEFINITIONS:
                    self.assertEqual(getattr(SW, name).__module__, SW.__name__)
                    self.assertIs(getattr(SW, name).__globals__, SW.__dict__)

    def test_registry_patch_reaches_reexported_lookup(self):
        with tempfile.TemporaryDirectory(prefix="workspace-lookup-") as directory:
            records = [{"cwd": directory, "workspaceId": "fixture-workspace"}]
            with mock.patch.object(SW, "_paseo_workspace_records",
                                   return_value=records) as registry:
                workspace = S._paseo_workspace_for_path(directory)
            self.assertEqual(workspace, "fixture-workspace")
            registry.assert_called_once_with()

    def test_owner_lookups(self):
        tree = ast.parse(Path(S.__file__).read_text())
        bare = [(n.id, n.lineno) for n in ast.walk(tree)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
                and n.id in DEFINITIONS + CONSTANTS]
        consumers = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
                     and isinstance(n.value, ast.Name) and n.value.id == "SW"}
        self.assertEqual(consumers, {
            "_paseo_json", "_paseo_error", "_paseo_path_ownership_problem",
            "_recover_code_launch", "_report_retained_worktrees",
            "_report_would_archive_code_worktree", "_cleanup_codex_home",
            "_archive_code_worktree", "reconcile_orphan"})
        self.assertEqual(bare, [], "facade bypasses owner patches")

    def test_patch_owners(self):
        offenders, owner_patches = [], set()
        for path in sorted((ROOT / "tests").glob("test_*.py")):
            tree = ast.parse(path.read_text())
            imports = {(a.name, a.asname or a.name) for n in ast.walk(tree)
                       if isinstance(n, ast.Import) for a in n.names}
            facades = {a for m, a in imports if m == "swarm"}
            owners = {a for m, a in imports if m == "swarm_workspaces"}
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
                    if target in owners | {"swarm_workspaces", "S.SW", "self.S.SW"}:
                        owner_patches.add(name)
        self.assertEqual(owner_patches, {
            "_paseo_workspace_records", "_archive_code_worktree",
            "_preserve_and_archive_code_worktree", "_recover_code_launch",
            "reconcile_orphan"})
        self.assertEqual(offenders, [])

    def test_standalone_offline_import(self):
        tree = ast.parse(Path(SW.__file__).read_text())
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
            "import sys; sys.path.insert(0, sys.argv[1]); import swarm_workspaces; "
            "assert not set(sys.modules).intersection(" + repr(blocked) + ")"
        ), str(SCRIPTS)], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_offline_probe_ignores_startup_customization(self):
        with tempfile.TemporaryDirectory(prefix="workspace-startup-") as directory:
            Path(directory, "sitecustomize.py").write_text("import ssl\n")
            with mock.patch.dict(os.environ, {"PYTHONPATH": directory}):
                self.test_standalone_offline_import()



class TestCleanupLiveness(unittest.TestCase):
    def code_unit(self, root):
        from tests import test_attempt_worktrees as fixtures
        repo = fixtures.repo_at(root / "repo")
        fixtures.git(repo, "branch", "-M", "main")
        fixtures.git(repo, "init", "-q", "--bare", str(root / "origin.git"))
        fixtures.git(repo, "remote", "add", "origin", str(root / "origin.git"))
        fixtures.git(repo, "push", "-q", "origin", "main")
        return fixtures.code_unit(repo)

    # Finite exhaustive bound: 5 states x 6 results x 3 pending values x
    # 2 call sites x 2 ownership modes = 360 cases. No randomness.
    def test_both_advance_sites_require_fresh_settlement(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            unit = self.code_unit(root)
            unit["max_attempts"] = 2
            attempt = root / "runs" / "attempt"
            attempt.mkdir(parents=True)
            error, launch = S._capture_code_launch(str(attempt), unit)
            self.assertIsNone(error)
            state_dir = root / "state"
            self.assertTrue(S.acquire_lease(str(state_dir))[0])
            self.addCleanup(S.release_lease, str(state_dir))
            for target, result, pending, site, owner in itertools.product(
                    sorted(S.WORKTREE_CLEANUP_STATES),
                    ("running", "idle", "done", "error", "timeout", "unknown"),
                    (False, True, "unacknowledged"),
                    ("pre", "post"), ("coordinator", "paseo")):
                with self.subTest(state=target, result=result, pending=pending,
                                  site=site, owner=owner):
                    self.cleanup_case(root, unit, attempt, launch,
                                      target, result, pending, site, owner)

    def cleanup_case(self, root, unit, attempt, launch,
                     target, result, pending, site, owner):
        class ReachedCleanup(BaseException):
            pass
        tree = root / "checkout"
        tree.mkdir(exist_ok=True)
        (tree / "work.txt").write_text("must survive")
        meta = {"path": str(tree), "workspace_owner": owner,
                "workspace_id": "fixture-workspace"}
        us = {"state": target if site == "pre" else "SUBMITTED",
              "job_id": "agent", "attempt_dir": str(attempt),
              "attempts": [str(attempt)], "gpu_hours": 0,
              "incomplete_since": 0,
              "attempt_cleanup_agents": {"attempt": "agent"},
              "attempt_launch_intents": {"attempt": launch["intent"]},
              "attempt_workspaces": {"attempt": meta},
              "continuations": ([{"agent_id": "agent",
                                 "sent": True, "status": "sent"}]
                                if pending else [])}
        state = {"schema_version": 1, "units": {"code": us}}
        state_dir = root / "state"
        if pending == "unacknowledged":
            us["continuations"] = []
            with mock.patch.object(S.U, "run", return_value=(127, "", "send timed out")), \
                 mock.patch.object(S.SE, "_receipt_reason", return_value=S.U.REASON_NO_OUTPUTS):
                for retry in (False, True):
                    if retry:
                        us["continuations"][0].update(
                            sent=None, status="pending", settled_since_send=True)
                    self.assertFalse(S.maybe_continue(
                        str(state_dir), "code", dict(unit, continuation={"max": 1}),
                        us, [], state))
            self.assertFalse(us["continuations"][0]["sent"])
        events = []
        def service(argv, **kwargs):
            if argv[:2] == ["paseo", "inspect"]:
                self.assertEqual(argv, ["paseo", "inspect", "--json", "agent"])
                self.assertEqual(kwargs["timeout"], 60)
                events.append("inspect")
                if result == "timeout":
                    return 127, "", "timed out after 60s"
                return 0, json.dumps({"status": result}), ""
            if (argv[:3] == ["paseo", "workspace", "archive"]
                    or (argv[0] == "git" and "remove" in argv)):
                events.append("remove")
                self.assertEqual(events[-2], "inspect")
                shutil.rmtree(tree)
                return 0, "", ""
            self.fail("unexpected service call: " + repr(argv))
        original = SW._archive_code_worktree
        def cleanup(*args):
            if site == "post":
                self.assertEqual(us["state"], target)
            original(*args)
            raise ReachedCleanup()
        rc = {"FAILED": S.FAILED, "FAILED_EVIDENCE": S.INCOMPLETE,
              "PREEMPTED": S.PREEMPTED}.get(target, S.DONE)
        head = "a" * 40 if rc == S.DONE else None
        def produced_head(*args):
            self.assertEqual(site, "post",
                             "pre-pass cleanup must precede judgment")
            return head
        merge = ({"merged_as": "b" * 40, "pr": "fixture"}, None)
        with mock.patch.object(S.U, "run", side_effect=service), \
             mock.patch.object(SW, "_archive_code_worktree", side_effect=cleanup), \
             mock.patch.object(S.SS, "trusted_launch_facts", return_value={"fixture": True}), \
             mock.patch.object(S.W, "validate_pinned_head", return_value=None), \
             mock.patch.multiple(S.SE,
                 _check=mock.Mock(return_value=(rc, "", "")),
                 trusted_produced_head=mock.Mock(side_effect=produced_head),
                 _receipt_reason=mock.Mock(return_value=None),
                 admit_merge=mock.Mock(return_value=(
                     merge if target == "DONE" else (None, "not merged")))):
            with self.assertRaises(ReachedCleanup):
                observed = S.advance({"name": "fixture", "units": [unit]}, state,
                                     str(state_dir), str(root / "runs"), False, max_new=0)
                self.fail(repr(observed))
        self.assertEqual(events.count("inspect"), 1)
        removed = result in ("idle", "done") and not pending
        self.assertEqual("remove" in events, removed)
        self.assertEqual(tree.exists(), not removed)
        durable = S.load_state(str(state_dir))["units"]["code"]
        saved = durable["attempt_workspaces"]["attempt"]
        if not removed:
            self.assertIn("cleanup_liveness", saved)
            self.assertEqual(saved["cleanup_attempts"], 0)
            self.assertEqual((tree / "work.txt").read_text(),
                             "must survive")
            shutil.rmtree(tree)
        self.assertIn("attempt", durable["attempt_recovery_snapshots"])

    def test_unknown_historical_agent_never_uses_current_agent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            unit = self.code_unit(root)
            error, launch = S._capture_code_launch(str(root / "old"), unit)
            self.assertIsNone(error)
            tree = root / "workspace"
            tree.mkdir()
            (tree / "work").write_text("old work")
            meta = {"path": str(tree), "workspace_owner": "coordinator"}
            state = {"units": {"code": {
                "attempt_dir": str(root / "new"), "job_id": "new-agent",
                "attempt_launch_intents": {"old": launch["intent"]},
                "attempt_workspaces": {"old": meta}}}}
            with mock.patch.object(S.U, "run") as run:
                SW._archive_code_worktree(state, unit, str(root / "old"), [],
                                          str(root / "state"))
            run.assert_not_called()
            self.assertTrue(tree.exists())
            self.assertIn("unknown", meta["cleanup_liveness"])

if __name__ == "__main__":
    unittest.main()
