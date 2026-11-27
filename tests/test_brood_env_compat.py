#!/usr/bin/env python3
"""BROOD_<X> is primary and HANIG_<X> the transparent fallback (rename PR 1/2).

Rule under test: a BROOD_<X> that is present and non-blank wins; otherwise the
legacy HANIG_<X> value is used exactly as before, so BROOD_REVIEW_NO_LOR=0
overrides HANIG_REVIEW_NO_LOR=1 and a blank BROOD_<X> never masks HANIG_<X>.
"""
import ast
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / "skills"
GATE = SKILLS / "brood-review-gate" / "scripts"
SWARM = SKILLS / "brood-swarm" / "scripts"
ORCH = SKILLS / "brood-orchestrate" / "scripts"
PROJECT = SKILLS / "brood-project" / "scripts"
for _path in (GATE, SWARM):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import review as R  # noqa: E402
import model_family as MF  # noqa: E402
import cli_io as CIO  # noqa: E402
import codex_home as CH  # noqa: E402
import swarm_routing as SR  # noqa: E402


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RSL = load("_brood_compat_review_slurm", GATE / "review_slurm.py")
LOR = load("_brood_compat_review_lor", GATE / "review_lor.py")
SP = load("_brood_compat_skill_paths", ORCH / "skill_paths.py")
OE = load("_brood_compat_orch_env", ORCH / "orch_env.py")
TA = load("_brood_compat_tracker_audit", PROJECT / "tracker_audit.py")

B, H = "BROOD_DEMO_SETTING", "HANIG_DEMO_SETTING"
# (environment, expected value of the HANIG_DEMO_SETTING shim)
PRECEDENCE = (
    ({}, None),
    ({H: "1"}, "1"),
    ({H: ""}, ""),                 # the legacy value is returned unchanged
    ({B: "0", H: "1"}, "0"),       # BROOD_ wins, even when falsy
    ({B: "", H: "1"}, "1"),        # a blank BROOD_ never masks HANIG_
    ({B: "  ", H: "x"}, "x"),
    ({B: "v"}, "v"),
    ({B: " v "}, " v "),           # never normalized before the caller decides
)


class SharedPrecedenceRule(unittest.TestCase):
    def test_every_shim_applies_the_same_rule(self):
        mapping_shims = {
            "review_slurm.setting": lambda env: RSL.setting(env, H),
            "cli_io.setting": lambda env: CIO.setting(env, H),
            "skill_paths.env_setting": lambda env: SP.env_setting(H, environ=env),
        }
        environ_shims = {
            "review._setting": lambda: R._setting(H),
            "model_family._setting": lambda: MF._setting(H),
            "codex_home._setting": lambda: CH._setting(H, None),
        }
        for env, want in PRECEDENCE:
            for name, shim in mapping_shims.items():
                with self.subTest(shim=name, env=env):
                    self.assertEqual(shim(dict(env)), want)
            for name, shim in environ_shims.items():
                with self.subTest(shim=name, env=env), \
                        mock.patch.dict(os.environ, env, clear=True):
                    self.assertEqual(shim(), want)

    def test_legacy_default_is_kept_when_neither_is_set(self):
        self.assertEqual(RSL.setting({}, H, "dflt"), "dflt")
        self.assertEqual(SP.env_setting(H, "dflt", environ={B: " "}), "dflt")
        self.assertEqual(SP.env_setting(H, "dflt", environ={H: ""}), "")

    def test_skill_paths_refuses_a_non_legacy_name(self):
        with self.assertRaises(ValueError):
            SP.env_setting("BROOD_DEMO_SETTING", environ={})


class ReviewGate(unittest.TestCase):
    def test_no_lor_opt_out_precedence(self):
        cases = (({}, False), ({LOR.OPT_OUT: "1"}, True),
                 ({LOR.OPT_OUT_PRIMARY: "0", LOR.OPT_OUT: "1"}, False),
                 ({LOR.OPT_OUT_PRIMARY: "", LOR.OPT_OUT: "1"}, True),
                 ({LOR.OPT_OUT_PRIMARY: "yes", LOR.OPT_OUT: "0"}, True),
                 ({LOR.OPT_OUT_PRIMARY: "1"}, True), ({LOR.OPT_OUT: ""}, False))
        for env, want in cases:
            with self.subTest(env=env):
                self.assertIs(LOR.lor_opted_out(env), want)
        self.assertEqual(LOR.OPT_OUT_PRIMARY, "BROOD_REVIEW_NO_LOR")
        self.assertEqual(LOR.OPT_OUT, "HANIG_REVIEW_NO_LOR")

    def test_review_slurm_mode_stage_and_sbatch_flags(self):
        self.assertEqual(RSL.slurm_mode({"BROOD_REVIEW_SLURM": "off",
                                         "HANIG_REVIEW_SLURM": "force"}), "off")
        self.assertEqual(RSL.slurm_mode({"BROOD_REVIEW_SLURM": "",
                                         "HANIG_REVIEW_SLURM": "off"}), "off")
        self.assertEqual(RSL.slurm_mode({"BROOD_REVIEW_SLURM": "force"}), "force")
        self.assertEqual(RSL.stage_root({"BROOD_REVIEW_SLURM_STAGE_DIR": "/b/stage",
                                         "HANIG_REVIEW_SLURM_STAGE_DIR": "/h/stage"}),
                         Path("/b/stage"))
        env = {"BROOD_REVIEW_SLURM_CPUS": "7", "HANIG_REVIEW_SLURM_CPUS": "3",
               "HANIG_REVIEW_SLURM_MEM": "9G",
               "BROOD_REVIEW_SLURM_ACCOUNT": "acct-b", "HANIG_REVIEW_SLURM_ACCOUNT": "acct-h",
               "BROOD_REVIEW_SLURM_EXTRA_ARGS": "--partition=pb",
               "HANIG_REVIEW_SLURM_EXTRA_ARGS": "--partition=ph"}
        argv = RSL.sbatch_argv("sbatch", "seat", Path("/s"), env, auto_partition="auto_b")
        self.assertIn("--cpus-per-task=7", argv)
        self.assertIn("--mem=9G", argv)
        self.assertIn("--account=acct-b", argv)
        self.assertIn("--partition=pb", argv)
        for absent in ("--partition=ph", "--partition=auto_b", "--account=acct-h"):
            self.assertNotIn(absent, argv)
        self.assertTrue(RSL.explicit_partition({"BROOD_REVIEW_SLURM_PARTITION": "p"}))
        self.assertFalse(RSL.explicit_partition({"BROOD_REVIEW_SLURM_PARTITION": " "}))

    def test_routing_snapshots_off_by_either_name_and_brood_wins(self):
        calls = []
        with tempfile.TemporaryDirectory() as raw, \
                mock.patch.object(MF, "git_toplevel", side_effect=lambda p: calls.append(p)):
            for env, consulted in (({"BROOD_ROUTING_SNAPSHOTS": "off"}, False),
                                   ({"HANIG_ROUTING_SNAPSHOTS": "off"}, False),
                                   ({"BROOD_ROUTING_SNAPSHOTS": "on",
                                     "HANIG_ROUTING_SNAPSHOTS": "off"}, True)):
                calls.clear()
                with self.subTest(env=env), mock.patch.dict(os.environ, env, clear=True):
                    self.assertEqual(MF.find_snapshot(raw), (None, None, None))
                    self.assertEqual(bool(calls), consulted)
        with mock.patch.dict(os.environ, {"BROOD_ROUTING_SNAPSHOTS": "off"}, clear=True), \
                mock.patch.object(R, "load_reviewers", return_value={"pins": 1}):
            self.assertEqual(R.load_effective_reviewers(), {"pins": 1})

    def test_committee_dir_prefers_brood(self):
        with tempfile.TemporaryDirectory() as raw:
            for env, want in (({"BROOD_COMMITTEE_DIR": raw + "/b",
                                "HANIG_COMMITTEE_DIR": raw + "/h"}, raw + "/b"),
                              ({"HANIG_COMMITTEE_DIR": raw + "/h"}, raw + "/h")):
                with self.subTest(env=env), mock.patch.dict(os.environ, env):
                    committee = load("_brood_compat_committee_%d" % len(want),
                                     GATE / "committee.py")
                    self.assertEqual(committee.SESSIONS, Path(want))


class Swarm(unittest.TestCase):
    def test_cli_slurm_mode_and_sbatch_flags(self):
        self.assertEqual(CIO.slurm_mode({"BROOD_SWARM_CLI_SLURM": "off",
                                         "HANIG_SWARM_CLI_SLURM": "on"}), "off")
        self.assertEqual(CIO.slurm_mode({"HANIG_SWARM_CLI_SLURM": "on"}), "on")
        env = {"BROOD_SWARM_CLI_SLURM_TIME": "01:00:00", "HANIG_SWARM_CLI_SLURM_TIME": "02:00:00",
               "BROOD_SWARM_CLI_SLURM_PARTITION": "pb", "HANIG_SWARM_CLI_SLURM_PARTITION": "ph",
               "HANIG_SWARM_CLI_SLURM_QOS": "qh"}
        argv = CIO.sbatch_argv("sbatch", "a1", "/r", 1, "/r/job.sh", env, auto_partition="x_batch")
        self.assertIn("--time=01:00:00", argv)
        self.assertIn("--partition=pb", argv)
        self.assertIn("--qos=qh", argv)
        self.assertNotIn("--partition=ph", argv)
        self.assertNotIn("--partition=x_batch", argv)
        self.assertTrue(CIO._explicit_partition({"BROOD_SWARM_CLI_SLURM_EXTRA_ARGS": "-p q"}))

    def test_codex_homes_prefer_brood(self):
        with tempfile.TemporaryDirectory() as raw:
            env = {"BROOD_SWARM_CODEX_AUTH_HOME": raw + "/auth-b",
                   "HANIG_SWARM_CODEX_AUTH_HOME": raw + "/auth-h",
                   "HANIG_SWARM_CODEX_OPERATOR_HOME": raw + "/op-h"}
            with mock.patch.dict(os.environ, env):
                self.assertEqual(CH.source_home(), Path(raw) / "auth-b")
                self.assertEqual(CH.operator_home(), Path(raw) / "op-h")

    def test_review_gate_dir_prefers_brood(self):
        with tempfile.TemporaryDirectory() as raw:
            for mark in ("brood", "hanig"):
                scripts = Path(raw) / mark / "scripts"
                scripts.mkdir(parents=True)
                (scripts / "model_family.py").write_text(f"MARK = {mark!r}\n")
            for env, want in (({"BROOD_REVIEW_GATE_DIR": raw + "/brood",
                                "HANIG_REVIEW_GATE_DIR": raw + "/hanig"}, "brood"),
                              ({"BROOD_REVIEW_GATE_DIR": " ",
                                "HANIG_REVIEW_GATE_DIR": raw + "/hanig"}, "hanig")):
                with self.subTest(env=env), mock.patch.dict(os.environ, env):
                    self.assertEqual(SR._model_family().MARK, want)


class OrchestrateAndProject(unittest.TestCase):
    def test_orch_python_source_names_the_variable_used(self):
        b, h = "/opt/envb/bin/python", "/opt/envh/bin/python"
        for env, source, chosen in (({"BROOD_ORCH_PYTHON": b, "HANIG_ORCH_PYTHON": h},
                                     "BROOD_ORCH_PYTHON", b),
                                    ({"BROOD_ORCH_PYTHON": "", "HANIG_ORCH_PYTHON": h},
                                     "HANIG_ORCH_PYTHON", h),
                                    ({"HANIG_ORCH_PYTHON": h}, "HANIG_ORCH_PYTHON", h)):
            with self.subTest(env=env):
                declared = OE.declared_orchestration_env(plan={}, env=env)
                self.assertEqual(declared["source"], source)
                self.assertEqual(declared["expected_python"], chosen)

    def test_dependency_roots_merge_brood_before_hanig(self):
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            loaded = base / "installed" / "brood-orchestrate"
            loaded.mkdir(parents=True)
            (loaded / "SKILL.md").write_text("x\n")
            for parent in ("b", "h"):
                sibling = base / parent / "brood-project"
                sibling.mkdir(parents=True)
                (sibling / "SKILL.md").write_text("x\n")
            both = {"BROOD_SKILL_DEP_ROOTS": str(base / "b"),
                    "HANIG_SKILL_DEP_ROOTS": str(base / "h")}
            cases = ((both, "b"), ({"HANIG_SKILL_DEP_ROOTS": str(base / "h")}, "h"),
                     ({"BROOD_SKILL_DEP_ROOTS": str(base / "b")}, "b"))
            for env, want in cases:
                with self.subTest(env=env), mock.patch.dict(os.environ, env, clear=True):
                    found = SP.sibling_skill_root(loaded, "brood-orchestrate", "brood-project")
                    self.assertEqual(found, (base / want / "brood-project").resolve())
            with mock.patch.dict(os.environ, both, clear=True):
                self.assertEqual(SP._configured_roots((), loaded), [base / "b", base / "h"])
            with mock.patch.dict(os.environ, {}, clear=True), \
                    self.assertRaisesRegex(SP.SkillPathError, "BROOD_SKILL_DEP_ROOTS"):
                SP.sibling_skill_root(loaded, "brood-orchestrate", "brood-project")

    def test_whitespace_only_dependency_roots_declare_nothing(self):
        copies = [load(f"_brood_compat_skill_paths_{skill}",
                       ROOT / "skills" / skill / "scripts" / "skill_paths.py")
                  for skill in ("brood-orchestrate", "brood-project", "brood-debrief")]
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            loaded = base / "installed" / "brood-orchestrate"
            loaded.mkdir(parents=True)
            (loaded / "SKILL.md").write_text("x\n")
            legacy = base / "legacy"
            (legacy / "brood-project").mkdir(parents=True)
            (legacy / "brood-project" / "SKILL.md").write_text("x\n")
            blank = (loaded / " ").resolve(strict=False)
            for module in copies:
                for env in ({"BROOD_SKILL_DEP_ROOTS": " ",
                             "HANIG_SKILL_DEP_ROOTS": str(legacy)},
                            {"BROOD_SKILL_DEP_ROOTS": str(legacy),
                             "HANIG_SKILL_DEP_ROOTS": " \t"}):
                    with self.subTest(module=module.__name__, env=env), \
                            mock.patch.dict(os.environ, env, clear=True):
                        roots = module._configured_roots((), loaded)
                        self.assertEqual(roots, [legacy])
                        self.assertNotIn(blank, roots)
                        found = module.sibling_skill_root(
                            loaded, "brood-orchestrate", "brood-project")
                        self.assertEqual(found, (legacy / "brood-project").resolve())
            # an internal space is a real path segment and survives
            spaced = base / "with space"
            with mock.patch.dict(os.environ, {"BROOD_SKILL_DEP_ROOTS": str(spaced)},
                                 clear=True):
                for module in copies:
                    self.assertEqual(module._configured_roots((), loaded), [spaced])

    def test_linear_ops_dir_prefers_brood(self):
        env = {"BROOD_LINEAR_OPS_DIR": "/b/ops", "HANIG_LINEAR_OPS_DIR": "/h/ops"}
        with mock.patch.dict(os.environ, env):
            self.assertEqual(TA.operation_directory("w", "p"), Path("/b/ops/w/p"))
        with mock.patch.dict(os.environ, dict(env, BROOD_LINEAR_OPS_DIR="")):
            self.assertEqual(TA.operation_directory("w", "p"), Path("/h/ops/w/p"))


def legacy_reads_without_brood(source):
    """Direct reads of a HANIG_<X> literal whose scope never names BROOD_<X>.

    A read is ``.get("HANIG_X")``, ``getenv("HANIG_X")``, ``x["HANIG_X"]`` or
    ``"HANIG_X" in x`` (also through a module constant bound to the literal).
    Reads through a shim take the name as an argument and are not reads here.
    """
    tree = ast.parse(source)
    constants = {}
    for node in tree.body:
        if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    constants[target.id] = node.value.value

    def legacy(node):
        value = (node.value if isinstance(node, ast.Constant) else
                 constants.get(node.id) if isinstance(node, ast.Name) else None)
        return value if isinstance(value, str) and value.startswith("HANIG_") else None

    def reads(scope):
        for node in ast.walk(scope):
            if (isinstance(node, ast.Call) and node.args and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("get", "getenv", "pop")):
                name = legacy(node.args[0])
            elif isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load):
                name = legacy(getattr(node.slice, "value", node.slice))
            elif (isinstance(node, ast.Compare) and len(node.ops) == 1
                  and isinstance(node.ops[0], (ast.In, ast.NotIn))):
                name = legacy(node.left)
            else:
                continue
            if name:
                yield node, name

    def names(scope):
        return {n.value for n in ast.walk(scope)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)}

    scopes = [n for n in ast.walk(tree)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    inner = {id(r) for s in scopes for r, _ in reads(s)}
    bad = []
    for scope in scopes:
        present = names(scope) | set(constants.values())
        bad += [(node.lineno, name) for node, name in reads(scope)
                if "BROOD_" + name[len("HANIG_"):] not in present]
    module_names = names(tree)
    bad += [(node.lineno, name) for node, name in reads(tree)
            if id(node) not in inner and "BROOD_" + name[len("HANIG_"):] not in module_names]
    return sorted(set(bad))


class SweepEveryAuthoredScript(unittest.TestCase):
    def test_guard_detects_a_bare_legacy_read(self):
        self.assertEqual(legacy_reads_without_brood(
            'import os\nX = os.environ.get("HANIG_FOO")\n'), [(2, "HANIG_FOO")])
        self.assertEqual(legacy_reads_without_brood(
            'import os\nK = "HANIG_FOO"\ndef f(e):\n    return e.get(K)\n'), [(4, "HANIG_FOO")])
        self.assertEqual(legacy_reads_without_brood(
            'def f(e):\n    return "HANIG_FOO" in e\n'), [(2, "HANIG_FOO")])
        self.assertEqual(legacy_reads_without_brood(
            'def f(e):\n    b = e.get("BROOD_FOO")\n    return b or e.get("HANIG_FOO")\n'), [])

    def test_no_authored_script_reads_a_legacy_name_without_its_brood_twin(self):
        scripts = sorted(SKILLS.glob("brood-*/scripts/*.py")) + sorted((ROOT / "lib").glob("*.py"))
        self.assertGreater(len(scripts), 50)
        offenders = {}
        for path in scripts:
            found = legacy_reads_without_brood(path.read_text(encoding="utf-8"))
            if found:
                offenders[str(path.relative_to(ROOT))] = found
        self.assertEqual(offenders, {})


if __name__ == "__main__":
    unittest.main()
