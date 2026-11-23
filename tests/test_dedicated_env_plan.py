"""Dedicated Python environments: plan validation and survey detection."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SWARM_SCRIPTS = ROOT / "skills/hanig-swarm/scripts"
PROJECT_SCRIPTS = ROOT / "skills/hanig-project/scripts"
for _p in (SWARM_SCRIPTS, PROJECT_SCRIPTS, Path(__file__).resolve().parent):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import survey as SURVEY  # noqa: E402
import swarm as S  # noqa: E402
from scheduler_fixture import cleanup_module_path as tearDownModule, isolated_module_path  # noqa: E402

ORCH_ROOT = "/srv/state/stage/orch-venv"
STAGE_ROOT = "/srv/proj/.venvs/stage"
MAXTOKI_ROOT = "/srv/proj/.venvs/maxtoki"
WB_ENV = "/home/hani/miniforge3/envs/workbook"
_UNSET = object()


def setUpModule():
    isolated_module_path()
    fake_paseo = Path(os.environ["PATH"].split(os.pathsep)[0]) / "paseo"
    fake_paseo.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_paseo.chmod(0o755)


def _run(cmd, env=None):
    return subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=30)


def _env_spec(path, lock="requirements.lock", model_line=None, kind=None, py="3.12"):
    d = {
        "path": str(path),
        "python": py,
        "lock_file": lock,
        "build": f"uv venv --python {py} {path}",
    }
    if model_line is not None:
        d["model_line"] = model_line
    if kind is not None:
        d["kind"] = kind
    return d


def _orch(path=ORCH_ROOT):
    return _env_spec(path, lock="none", kind="orchestration")


def _base_envs():
    return {
        "orchestration": _orch(),
        "stage": _env_spec(STAGE_ROOT, "requirements.stage.lock", "stage"),
        "maxtoki": _env_spec(MAXTOKI_ROOT, "requirements.maxtoki.lock", "maxtoki"),
    }


def _rt(rt_id, ep, env=None, res="direct", **over):
    d = {
        "id": rt_id,
        "resolution": res,
        "entrypoint": ep,
        "probe": f"{ep} -c 'import sys'",
        "verified_by": ("preflight" if res != "direct" else "unverified: homogeneous node check"),
    }
    if env is not None:
        d["environment"] = env
    d.update(over)
    return d


def _slurm(uid="ts", runtime="sp", **over):
    u = {
        "id": uid,
        "kind": "slurm",
        "command": f"{STAGE_ROOT}/bin/python train.py",
        "runtime": runtime,
        "outputs": ["m.json"],
    }
    u.update(over)
    return u


def _code(uid="is", environment="stage", **over):
    u = {
        "id": uid,
        "kind": "code",
        "repo": "/srv/proj",
        "target_branch": "main",
        "prompt": "Implement feature",
        "mode": "full-access",
        "outputs": ["e.md"],
    }
    if environment is not None:
        u["environment"] = environment
    u.update(over)
    return u


class TestDedicatedEnvironmentsValidation(unittest.TestCase):
    def _plan(self, units, envs=_UNSET, **over):
        d = {"project": "p", "units": units}
        if envs is _UNSET:
            d["environments"] = _base_envs()
        elif envs is not None:
            d["environments"] = envs
        d.update(over)
        return d

    def _fail(self, plan, needle=None):
        with self.assertRaises(S.PlanError) as ctx:
            S.validate_plan(plan)
        if needle:
            self.assertIn(needle, str(ctx.exception).lower())

    def _u(self, ep, env=None, res="direct", uid="u", **kw):
        return _slurm(uid, _rt(uid, ep, env, res), **kw)

    def _e1(self, path, key="stage"):
        return {"orchestration": _orch(), key: _env_spec(path, model_line=key)}

    def test_valid_plan_and_cli(self):
        plan = self._plan(
            [
                _slurm("ts", "sp"),
                _slurm("tm", "mp", kind="pipeline", command=f"{MAXTOKI_ROOT}/bin/python train.py"),
                _code("is", "stage", write_scopes=["src/stage/"]),
                _code("im", "maxtoki", write_scopes=["src/maxtoki/"]),
            ],
            project="stage-proj",
            runtimes={
                "sp": _rt("sp", f"{STAGE_ROOT}/bin/python", "stage"),
                "mp": _rt("mp", f"{MAXTOKI_ROOT}/bin/python", "maxtoki"),
            },
        )
        S.validate_plan(plan)
        with tempfile.TemporaryDirectory() as tmp:
            pp = Path(tmp) / "plan.json"
            pp.write_text(json.dumps(plan), encoding="utf-8")
            proc = _run([sys.executable, str(SWARM_SCRIPTS / "swarm.py"), "validate", str(pp)])
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_shared_envs_refused(self):
        hh = "/home/hani"
        lb = "/home/linuxbrew/.linuxbrew"
        ul = "/usr/local"
        ub = "/usr/bin"
        bp = "/bin/python"
        clt = "/Library/Developer/CommandLineTools"
        xc = "/Applications/Xcode.app/Contents/Developer"
        hb = "/opt/homebrew"
        u3 = "/usr/bin/python3"
        sp = f"{STAGE_ROOT}/bin/python"
        p312 = "/opt/python@3.12/bin/python3.12"
        pyv = "/versions/3.12.2/bin/python"
        refused = (
            WB_ENV + bp,
            f"{hh}/miniconda3{bp}3",
            f"{hh}/miniforge3/envs/base{bp}",
            f"{hh}/anaconda3{bp}",
            f"{hh}/envs/workbook{bp}",
            "/srv/conda-prefixes/workbook/bin/python",
            "/opt/miniforge3/condabin/conda",
            "/srv/proj/miniforge3/condabin/conda",
            "/srv/science/dist/condabin/conda",
            "/opt/homebrew/Cellar/pypy3.11/7.3.19/bin/pypy3.11",
            "/srv/tools/env -i python3 scripts/train.py",
            "/srv/tools/env FOO=bar python3 scripts/train.py",
            "/srv/tools/env LC_ALL=C python3 scripts/train.py",
            "/usr/bin/sort -u data/reads.txt",
            "/home/alice/Python Projects/stage/bin/python",
            "/srv/bin/python experiments/stage/bin/python",
            "/usr/bin/(python3)",
            u3,
            f"{u3}.13t",
            f"{u3}.13td",
            f"{u3}.7dm",
            f"{u3}-dbg",
            "/usr/bin/graalpy",
            "/usr/libexec/platform-python",
            f"{ul}{bp}3.12",
            f"{hb}{bp}3.12",
            ul + p312,
            f"{ul}/Cellar/python@3.12/3.12.0{bp}3.12",
            "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3",
            clt + u3,
            f"{clt}/Library/Frameworks/Python3.framework/Versions/3.9/bin/python3.9",
            xc + u3,
            f"{ub}/pip3",
            f"{lb}{bp}3",
            lb + p312,
            f"{hb}/Caskroom/miniforge/base{bp}",
            f"{WB_ENV}/bin/snakemake",
            f"{WB_ENV}/bin/jupyter-lab",
            f"{WB_ENV}/bin/py.test",
            f"/srv/ML_Tools/miniforge3/base{bp}",
            f"{hh}/.pyenv/shims/python3",
            f"{hh}/.pyenv{pyv}",
            f"/opt/pyenv{pyv}",
            f"{hh}/.conda/envs/workbook{bp}",
            f"{WB_ENV}/bin/torchrun",
            f"{hh}/.venv{bp}",
            f"/Users/hani/venv{bp}",
            f"/root/.venv{bp}",
            "python3",
            "sort",
            "coverage",
            "./python3",
            f"'{u3}'",
            f"{u3} -u",
            f"{u3} scripts/train.py",
            f"{sp} scripts/train.py",
            "/srv/tools/bin/env scripts/train.py",
            "/usr/bin/sort input.txt",
            "/usr/bin/sort -- input.txt",
            "/usr/bin/sort --output=/tmp/result",
            "/srv/bin/train --config=/tmp/fit.toml",
            f"{STAGE_ROOT}/bin/train worker",
            "sort input.txt",
            "python3>out",
            f"{u3}>run.log",
            "sort>report",
            "--version",
            "env",
            f"{ub}/env",
            "/srv/ML_Tools/bin/env",
            f"{ub}/p[y]thon3",
            f"{ub}/python[3]",
            f"{ub}/python*",
            f"{sp}/",
            f"{sp} /tmp/train.py",
            "env python3",
            "env 'python3'",
            f"{ub}/env 'python3'",
            f"{ub}/env python3",
            f"{ub}/env -S 'python3 -I'",
            f"env -C {ub} ./python3",
            "env -u LD_PRELOAD python3",
            f"env -u LD_PRELOAD {u3}",
            f"env -u LD_PRELOAD {sp}",
        )
        for ep in refused:
            with self.subTest(ep=ep):
                self._fail(self._plan([_slurm("u", runtime=_rt("b", ep))], None), "refused")

    def test_custom_home_and_case_sensitive_and_spaced_dirs(self):
        # Custom HOME outside hard-coded layouts is refused lexically for .venv/venv and conda base.
        with mock.patch.dict(os.environ, {"HOME": "/cluster/users/alice"}):
            for bad in ("/cluster/users/alice/.venv", "/cluster/users/alice/venv"):
                self._fail(self._plan([self._u(f"{bad}/bin/python", "stage")], self._e1(bad)), "shared home venv")
                self._fail(self._plan([self._u(f"{bad}/bin/python")], None), "shared home venv")
            self._fail(self._plan([self._u("/cluster/users/alice/miniforge3/bin/python")], None), "conda base")
            # Exact-case non-matching names and project subdirs under custom HOME remain accepted.
            for okp in ("/cluster/users/alice/.VENV", "/cluster/users/alice/Venv", "/cluster/users/alice/proj/.venv"):
                S.validate_plan(self._plan([self._u(f"{okp}/bin/python", "stage")], self._e1(okp)))
                S.validate_plan(self._plan([self._u(f"{okp}/bin/python")], None))
        # Case-sensitive HOME comparison for conda base (HOME=/scratch/Alice does not match /scratch/alice/miniforge3).
        with mock.patch.dict(os.environ, {"HOME": "/scratch/Alice"}):
            self._fail(self._plan([self._u("/scratch/Alice/miniforge3/bin/python")], None), "conda base")
            S.validate_plan(self._plan([self._u("/scratch/alice/miniforge3/bin/python")], None))
        for path in ("/home/alice/.VENV", "/home/alice/Venv"):
            S.validate_plan(self._plan([self._u(f"{path}/bin/python", "stage")], self._e1(path)))
        # Whitespace is refused in declared env paths and non-container entrypoints; non-space parentheses in parent dirs are allowed.
        for bad_sp in (
            "/home/alice/Experiment -2/stage",
            "/tmp/bin/Experiment -2/stage",
            "/home/alice/Experiment (2)/stage",
            "/srv/env experiments/stage",
        ):
            self._fail(self._plan([self._u(f"{bad_sp}/bin/python", "stage")], self._e1(bad_sp)), "whitespace")
            self._fail(self._plan([self._u(f"{bad_sp}/bin/python")], None), "whitespace")
        self._fail(self._plan([self._u("/home/alice/Experiment -2/fit", res="conda")], None), "whitespace")
        ok_paren = "/home/alice/Experiment_(2)/stage"
        S.validate_plan(self._plan([self._u(f"{ok_paren}/bin/python", "stage")], self._e1(ok_paren)))
        S.validate_plan(self._plan([self._u(f"{ok_paren}/bin/python")], None))

    def test_resolution_path_and_uv_and_unbound_rules(self):
        # Legacy PATH-resolved non-Python tools (including Ray genome assembler & python3-config) and bare uv validate; bare Python/env on PATH are refused.
        for tool in ("sort", "samtools", "Rscript", "rg", "Ray", "python3-config"):
            S.validate_plan(self._plan([self._u(tool, res="path", command=f"{tool} --version")], None))
            S.validate_plan(self._plan([self._u(tool, "none", res="path", command=f"{tool} --version")]))
            S.validate_plan(self._plan([self._u(tool, "none", res="path", command=f"{tool} --version")], None))
            S.validate_plan(self._plan([self._u(tool, "stage", res="path", command=f"{tool} --version")]))
            # When top-level environments is declared, omitting environment on non-Python slurm/pipeline is refused.
            self._fail(self._plan([self._u(tool, res="path", command=f"{tool} --version")]), "omits 'environment'")
        for bad_path in ("python3", "python", "python.exe", "pip", "pytest", "graalpy", "torchrun", "conda", "env", "sort -u", "./sort", "--version", "(python3)"):
            self._fail(self._plan([self._u(bad_path, res="path")], None), "refused")
            self._fail(self._plan([self._u(bad_path, "none", res="path")]))
        # resolution="uv" with bare "uv" works in legacy plans and when bound; refused when unbound or "none".
        S.validate_plan(self._plan([self._u("uv", res="uv")], None))
        S.validate_plan(self._plan([self._u("uv", "stage", res="uv")]))
        self._fail(self._plan([self._u("uv", res="uv")]), "undeclared")
        self._fail(self._plan([self._u("uv", "none", res="uv")]), "environment='none'")
        # Explicit environment binding on direct slurm requires executable directly inside that env (no graalpy/coverage bypass).
        for outsider in ("/srv/proj/.venvs/other/bin/graalpy", "/srv/proj/.venvs/unregistered/bin/coverage"):
            self._fail(self._plan([self._u(outsider, "stage")]), "does not belong directly")
        # Inside the bound env, graalpy and coverage are accepted.
        for insider in (f"{STAGE_ROOT}/bin/graalpy", f"{STAGE_ROOT}/bin/coverage"):
            S.validate_plan(self._plan([self._u(insider, "stage")]))

    def test_bindings_and_sharing(self):
        st, mk, bp = "stage", "maxtoki", "/bin/python"
        for nm in ("workbook", "base"):
            self._fail(self._plan([_slurm("u", runtime=_rt("c", nm, res="conda"))], None), "shared")
        es = _base_envs()
        es["orchestration"]["path"] = WB_ENV
        self._fail(self._plan([_code()], es), "workbook")
        es_wb = _base_envs()
        es_wb[st]["path"] = "/srv/conda-prefixes/workbook"
        self._fail(self._plan([_code()], es_wb), "workbook")
        self._fail(self._plan([_slurm("u", "m")], runtimes={"m": _rt("m", f"/srv/proj/.venvs/mystery{bp}")}), "undeclared")
        self._fail(self._plan([_code("c", "ghost")]), "not declared")
        self._fail(self._plan([_slurm("u", "m")], runtimes={"m": _rt("m", MAXTOKI_ROOT + bp, st)}), "does not belong directly")
        self._fail(self._plan([_slurm("u", "r")], runtimes={"r": _rt("r", STAGE_ROOT, st)}))
        self._fail(self._plan([_code("c", "orchestration")]), "orchestration")
        self._fail(self._plan([_slurm("u", "o")], runtimes={"o": _rt("o", ORCH_ROOT + bp)}), "orchestration")
        self._fail(self._plan([_code("c", None)]), "environment")
        S.validate_plan(self._plan([_code("c", "none")]))
        S.validate_plan(self._plan([_code("docs", None, runtime="none", prompt="Edit docs")]))
        S.validate_plan(self._plan([_slurm("r", runtime="none")]))
        for badp in (STAGE_ROOT, STAGE_ROOT + "/nested"):
            es = _base_envs()
            es[mk]["path"] = badp
            self._fail(self._plan([_code()], es))
        self._fail(
            self._plan([_code("c1", st, model_line=st, write_scopes=["a/"]), _code("c2", st, model_line=mk, write_scopes=["b/"])]),
            "share environment",
        )
        sh = _rt("s", f"/srv/proj/.venvs/one{bp}")
        self._fail(self._plan([_slurm("u1", "s", model_line=st), _slurm("u2", "s", model_line=mk)], None, runtimes={"s": sh}), "share environment root")
        # Legacy inferred Python root sharing and environment='none' containment refusal (regardless of subdir, model_line, or order).
        for pair in (
            [self._u("/srv/stage/bin/python", uid="a", model_line=st), self._u("/srv/stage/bin/coverage", uid="b", model_line=mk)],
            [self._u("/srv/stage/bin/coverage", "none", uid="b", model_line=mk), self._u("/srv/stage/bin/python", uid="a", model_line=st)],
            [self._u("/srv/stage/bin/python", uid="a", model_line=st), self._u("/srv/stage/bin/coverage", "none", uid="b", model_line=st)],
            [self._u("/srv/stage/bin/python", uid="a"), self._u("/srv/stage/bin/coverage", "none", uid="b")],
            [self._u("/srv/stage/bin/python", uid="a"), self._u("/srv/stage/libexec/coverage", "none", uid="b")],
            [self._u("/srv/fit/bin/torchrun", res="wrapper", uid="a"), self._u("/srv/fit/bin/coverage", "none", uid="b")],
            [self._u("/srv/project/venv/bin/coverage", uid="c1", model_line=st), self._u("/srv/project/venv/bin/coverage", uid="c2", model_line=mk)],
            [self._u("/srv/project/venv/bin/coverage", uid="c1", model_line=st), self._u("/srv/project/venv/bin/coverage", "none", uid="c2", model_line=mk)],
            [self._u("/srv/project/fit-venv/bin/coverage", uid="c1"), self._u("/srv/project/fit-venv/bin/coverage", "none", uid="c2")],
            [self._u("/srv/project/fit-venv/bin/run-fit", res="wrapper", uid="c1", model_line="fit"), self._u("/srv/project/fit-venv/bin/coverage", "none", uid="c2")],
        ):
            self._fail(self._plan(pair, None))
        # Standalone native tools under envs/common or under a *-venv ancestor dir are NOT falsely rejected.
        S.validate_plan(self._plan([self._u("/tmp/project/envs/common/bin/sort", "none", uid="ns")], None))
        S.validate_plan(self._plan([
            self._u("/srv/releases/tool-venv/native-tools-env/bin/sort", uid="a"),
            self._u("/srv/releases/tool-venv/native-tools-env/bin/cut", "none", uid="b"),
        ], None))
        # Unused Python runtime in catalogue does not register its parent as a plan-used root against native units.
        S.validate_plan(self._plan(
            [self._u("/srv/tools/bin/sort", "none", uid="n")],
            None,
            runtimes={"unused": _rt("unused", "/srv/tools/bin/python3")},
        ))
        # Named wrapper and pipeline-engine runtimes inside a declared env's libexec/ match inline behavior when bound on the unit, while unbound nested wrappers are refused.
        wrap_ep = f"{STAGE_ROOT}/libexec/run-stage"
        S.validate_plan(self._plan([_slurm("u", "run", environment=st, command=wrap_ep)], runtimes={"run": _rt("run", wrap_ep, res="wrapper")}))
        nf_ep = f"{STAGE_ROOT}/libexec/nextflow"
        S.validate_plan(self._plan([_slurm("p", "nf", kind="pipeline", environment=st, command=nf_ep)], runtimes={"nf": _rt("nf", nf_ep)}))
        self._fail(self._plan([self._u(f"{STAGE_ROOT}/tools/docs-venv/bin/run-docs", res="wrapper")]), "does not belong directly")
        # Distinct legacy conda prefixes under /srv/common/bin/ (including a prefix named 'env') stay distinct.
        S.validate_plan(self._plan([
            self._u("/srv/common/bin/fit", res="conda", uid="a", model_line=st),
            self._u("/srv/common/bin/eval", res="conda", uid="b", model_line=mk),
            self._u("/srv/common/bin/env", res="conda", uid="c", model_line="other"),
        ], None))
        # Cross-model canary on an isolated container image validates cleanly.
        ctr_r = _rt("img", "/srv/images/native.sif", res="container", probe="apptainer exec /srv/images/native.sif sort --version")
        S.validate_plan(self._plan([
            _slurm("can", ctr_r, environment=st, command=ctr_r["probe"]),
            _slurm("work", dict(ctr_r, verified_by="canary:can"), environment=mk, needs=["can"], command=ctr_r["probe"]),
        ]))
        # A bound external non-Python wrapper does not turn its parent bin dir into a Python root for sibling native tools.
        S.validate_plan(self._plan([
            self._u("/opt/tools/bin/run-stage", st, res="wrapper", uid="w", model_line=st),
            self._u("/opt/tools/bin/sort", "none", uid="s", model_line=st),
            self._u("/opt/tools/bin/cut", "none", uid="c", model_line=mk),
        ]))
        # Independent container images stored under a *-venv directory do not falsely collide.
        S.validate_plan(self._plan([
            self._u("/srv/tool-venv/bin/stage.sif", res="container", uid="c1", model_line=st),
            self._u("/srv/tool-venv/bin/eval.sif", res="container", uid="c2", model_line=mk),
        ], None))
        # Declared conda prefix named .../bin/python3 does not register its grandparent /srv/tools as a Python root.
        S.validate_plan(self._plan(
            [self._u("/srv/tools/bin/python3", st, res="conda", uid="a"), self._u("/srv/tools/bin/rg", "none", uid="b")],
            self._e1("/srv/tools/bin/python3"),
        ))
        # Distinct declared conda prefixes under bin/ named python3 and python3.12 stay distinct.
        S.validate_plan(self._plan(
            [self._u("/srv/bin/python3", st, res="conda", uid="a"), self._u("/srv/bin/python3.12", mk, res="conda", uid="b")],
            {"orchestration": _orch(), st: _env_spec("/srv/bin/python3", model_line=st), mk: _env_spec("/srv/bin/python3.12", model_line=mk)},
        ))
        # Legacy non-Python native tools under dirs named 'envs' or '*-env' with environment='none' are NOT falsely rejected.
        for native in ("/opt/envs/native-tools/bin/rg", "/srv/native-tools-env/bin/sort"):
            units = [self._u(native, "none", uid=x, model_line=x, command=f"{native} --version") for x in (st, mk)]
            S.validate_plan(self._plan(units, None))
        # Distinct legacy conda prefixes under a dir named bin/ stay distinct; offline validation does not guess name-to-prefix mappings.
        S.validate_plan(self._plan([
            self._u("/srv/bin/fit", res="conda", uid="a", model_line=st),
            self._u("/srv/bin/eval", res="conda", uid="b", model_line=mk),
        ], None))
        S.validate_plan(self._plan([
            self._u("fit", res="conda", uid="a", model_line=st),
            self._u("/home/alice/miniforge3/envs/fit", res="conda", uid="b", model_line=mk),
        ], None))
        S.validate_plan(self._plan([
            self._u("/opt/miniforge3/envs/fit", res="conda", uid="a", model_line=st),
            self._u("/home/alice/miniforge3/envs/fit", res="conda", uid="b", model_line=mk),
        ], None))
        # Nested executable inside declared env without explicit environment is refused.
        self._fail(self._plan([self._u(f"{STAGE_ROOT}/tools/docs-venv/bin/coverage")]), "does not belong directly")
        for k, v in (("lock_file", "none"), ("path", "relative/venv"), ("path", "/srv/../other/venv"), ("path", "/srv/$PROJ/venv"), ("python", "latest"), ("build", ""), ("kind", "")):
            es = _base_envs()
            es[st][k] = v
            self._fail(self._plan([_code()], es))
        base = self._plan([_code()], None)
        d0, d1 = S.plan_digest(base), S.plan_digest(dict(base, environments=_base_envs()))
        e2 = _base_envs()
        e2[st]["lock_file"] = "requirements.stage.v2.lock"
        self.assertNotEqual(d0, d1)
        self.assertNotEqual(d1, S.plan_digest(dict(base, environments=e2)))
        self.assertIn("environments", {r[0] for r in S.SCHEMA_PLAN_FIELDS})
        self.assertTrue({"environment", "model_line"} <= {r[0] for r in S.SCHEMA_FIELDS})

    def test_edge_regressions(self):
        st, mk, ub, ul = "stage", "maxtoki", "/usr/bin/", "/usr/local/bin/"
        SP, OP, ha, spj = f"{STAGE_ROOT}/bin/python", f"{ORCH_ROOT}/bin/python", "/home/alice", "/srv/proj"
        envs = self._e1("/srv/model-envs/miniconda-benchmark", "train-env")
        envs["train-env"].pop("model_line")
        S.validate_plan(self._plan([_code("u1", "train-env", model_line=st)], envs))
        self._fail(self._plan([_code("u1", "train-env", model_line=st, write_scopes=["a/"]), _code("u2", "train-env", model_line=mk, write_scopes=["b/"])], envs))
        for t in (ub + "Rscript", ub + "sort", ul + "nextflow"):
            rt = _rt("t", t)
            S.validate_plan(self._plan([_slurm("u", rt, command=t)], None))
            S.validate_plan(self._plan([_slurm("u", rt, command=t, environment="none")]))
            self._fail(self._plan([_slurm("u", rt, command=t)]), "omits 'environment'")
        ce = _base_envs()
        ce[st]["path"] = "/home/owner/miniforge3/envs/stage"
        S.validate_plan(self._plan([_slurm("u", "sc")], ce, runtimes={"sc": _rt("sc", st, st, res="conda")}))
        self._fail(self._plan([_slurm("u1", "fit", model_line=st), _slurm("u2", "fit", model_line=mk)], None, runtimes={"fit": _rt("fit", "fit", res="conda")}))
        self._fail(self._plan([_slurm("u", "n")], runtimes={"n": _rt("n", f"{STAGE_ROOT}/tools/docs-venv/bin/python", st)}))
        self._fail(self._plan([_code("c", None, runtime=_rt("in", SP))]))
        bo = _base_envs()
        bo["orchestration"].update({"kind": "model_line", "lock_file": "requirements.lock"})
        self._fail(self._plan([_code()], bo))
        self._fail(self._plan([_code("c", st)], {st: _env_spec(STAGE_ROOT, model_line=st)}), "no orchestration")
        to = _base_envs()
        to["ops"] = _orch("/srv/state/stage/ops-venv")
        self._fail(self._plan([_code()], to))
        S.validate_plan(self._plan([self._u(f"{spj}/bin/run-stage", st, "wrapper")]))
        for ep in (f"{ORCH_ROOT}/bin/run-stage", "/srv/old-stage/bin/python"):
            self._fail(self._plan([self._u(ep, st, "wrapper")]))
        for ep in (OP, SP, f"{ORCH_ROOT}/bin/run-stage", f"{STAGE_ROOT}/bin/rg"):
            self._fail(self._plan([self._u(ep, environment="none")]))
        cat = {n: _rt(n, ub + n, env="none", model_line=n) for n in ("sort", "cut")}
        cat["nf_cat"] = _rt("nf_cat", ul + "nextflow", st, probe="nf -v")
        S.validate_plan(self._plan(
            [
                _slurm("u1", "sort", environment="none"),
                _slurm("u2", "cut", environment="none"),
                self._u("/opt/homebrew/opt/r/bin/Rscript", "none", uid="u3", model_line="eval"),
                self._u("/usr/lib/git-core/git", "none", uid="u4"),
                self._u("/usr/sbin/ethtool", "none", uid="u5"),
                _slurm("u6", "nf_cat", kind="pipeline"),
                _slurm("u7", _rt("nf2", "/opt/nextflow/bin/nextflow", st, probe="nf -v"), kind="pipeline"),
                self._u("stage/torchrun/2.4", st, "module", "u9"),
                self._u(ul + "uv", st, "uv", "u10"),
                self._u(f"{STAGE_ROOT}/bin/coverage", st, "conda", "u11"),
            ]
            + [
                _slurm(f"n{i}", _rt(f"n{i}", ep), environment="none")
                for i, ep in enumerate((
                    "/Library/Developer/CommandLineTools/usr/bin/clang",
                    "/opt/venv-audit/bin/scan",
                    f"{spj}/.venv/bin/sort",
                    f"{spj}/envs/native-tools/bin/rg",
                ))
            ],
            runtimes=cat,
        ))
        # A slurm unit cannot borrow a pipeline-engine runtime bound to stage.
        self._fail(self._plan([_slurm("u_bad", "nf_cat")], runtimes={"nf_cat": cat["nf_cat"]}), "does not belong directly")
        uv = _rt("uvl", ul + "uv", res="uv")
        S.validate_plan(self._plan([_slurm("u1", uv, model_line=st), _slurm("u2", uv, model_line="eval")], None))
        S.validate_plan(self._plan([_slurm("u1", _rt("c1", "/srv/envs/stage", res="conda"), model_line=st), _slurm("u2", _rt("c2", "/srv/envs/eval", res="conda"), model_line="eval")], None))
        S.validate_plan(self._plan([
            self._u("samtools", res="module", uid="ms"),
            self._u("cuda/12.1", res="module", uid="mc"),
            self._u("compiler/gcc/12.3", res="module", uid="mg"),
            self._u("genomics/scripts/qc/1.0", res="module", uid="mq"),
            self._u("python-imaging/ffmpeg/6.1", res="module", uid="mff"),
            self._u("stage/scripts/python/3.12", res="module", uid="msp", model_line=st),
            self._u("stage/python/3.12", res="module", uid="mp", model_line=st),
            self._u("stage/miniforge3/24.3", res="module", uid="mm", model_line=st),
        ], None))
        self._fail(self._plan([self._u("stage/python/3.12", res="module", uid="m1", model_line=st), self._u("stage/python/3.12", res="module", uid="m2", model_line=mk)], None))
        self._fail(self._plan([self._u("stage/python/3.12", st, "module", "m1", model_line=st), self._u("stage/python/3.12", mk, "module", "m2", model_line=mk)]))
        self._fail(self._plan([self._u("stage/python/3.12", res="module", uid="mu")]))
        # Shared cluster Python/conda Lmod modules (including versioned/ABI/compiler-suffixed tokens and nested shared categories) are refused even when bound.
        for bm in (
            "python3",
            "python/3.12",
            "Python-3.12.2",
            "lang/python/3.12",
            "Core/lang/Python/3.12",
            "Core/lang/Python-3.12.2",
            "Core/lang/Python-v3.12.2",
            "Core/Python-v3.12.2",
            "Core/Python+3.12.2",
            "Core/Python3.12-GCCcore-13.2.0",
            "Core/Python3.13t-GCCcore-14.2.0",
            "Core/compilers/gcc/12/Python-3.12.2",
            "compiler/gcc/12.3/python/3.11",
            "mysite/lang/python/3.12",
            "proj/Core/conda/24.3",
            "anaconda3/2024.02",
            "conda/24.3",
            "torchrun",
        ):
            self._fail(self._plan([self._u(bm, res="module")], None))
            self._fail(self._plan([self._u(bm, st, "module")]))
        # Project-scoped versioned Python module binds cleanly, while absolute paths are refused under resolution="module".
        S.validate_plan(self._plan([self._u("stage/Python-3.12.2", st, "module")]))
        self._fail(self._plan([self._u("stage/Python-3.12.2", "none", "module")]))
        self._fail(self._plan([_code("m", st, runtime=_rt("mod", SP, st, res="module"))]), "not a valid lmod module")
        for mp in ("/tmp/project/miniconda3", "/tmp/p/.venvs/miniconda3"):
            S.validate_plan(self._plan([_code("c", st)], self._e1(mp)))
        S.validate_plan(self._plan([self._u(f"{STAGE_ROOT}/bin/micromamba", st)]))
        for cp in (f"{spj}/conda-prefixes/python3", "/srv/bin/fit", f"{spj}/bin/python3", "/srv/envs/miniconda3"):
            S.validate_plan(self._plan([self._u(cp, st, "conda")], self._e1(cp)))
        for ep, rs in (
            ("fit;staging", "conda"),
            ("/srv/prefixes/fit --no-capture-output", "conda"),
            ("/srv/prefixes/fit --prefix=/tmp/other", "conda"),
            ("/srv/prefixes/fit ", "conda"),
            ("/srv/conda-prefixes/workbook", "conda"),
            ("/srv/prefixes/workbook/bin/python", "conda"),
            ("/srv/conda-prefixes/workbook/bin/coverage", "conda"),
            ("/usr/bin/env", "conda"),
            ("/srv/tools/bin/env", "direct"),
            (f"{WB_ENV}/bin/coverage", "conda"),
            (f"{WB_ENV}/bin/coverage", "direct"),
            (f"{ha}/.venv/bin/rg", "direct"),
            ("fit" + chr(0xD800), "conda"),
            ("cuda/12." + chr(0xD800), "module"),
        ):
            with self.subTest(ep=ep, res=rs):
                self._fail(self._plan([self._u(ep, res=rs)], None))
        for bad_e in (
            self._u(f"{MAXTOKI_ROOT}/bin/coverage", st),
            self._u(f"{STAGE_ROOT}/bin/coverage", "none"),
            self._u(f"{STAGE_ROOT}/bin/ruff", "none"),
            self._u(f"{STAGE_ROOT}/bin/python/", st),
        ):
            self._fail(self._plan([bad_e]))
        for ep in (42, None):
            self._fail(self._plan([_code("c", "none", runtime=_rt("nb", ep, probe="true"))], None))
        for ur in (_rt("unused", ub + "python3", st), _rt("unused", "", probe="true")):
            self._fail(self._plan([_code("c", st)], runtimes={"unused": ur}))
        self._fail(self._plan([_code("c", None)], None, runtimes={"unused": _rt("unused", "python/3.12", res="module", probe="python -V")}))
        for cr, we, pe in (
            (_rt("ci", OP, res="container", probe="py -V"), "none", _UNSET),
            (_rt("uvc", f"{STAGE_ROOT}/bin/uv", probe="uv -V", res="uv"), st, _UNSET),
            (_rt("sp", SP, probe="py -c 1", verified_by="preflight"), st, _UNSET),
            (_rt("sr", ub + "sort", probe="sort -V", verified_by="preflight"), "none", None),
        ):
            can_kw = {"environment": we} if (pe is _UNSET and cr["resolution"] == "container") else {}
            S.validate_plan(self._plan([_slurm("can", cr, command=cr["probe"], **can_kw), _slurm("wk", dict(cr, verified_by="canary:can"), needs=["can"], environment=we)], pe))
        for bm in (f"env {ub}python3", f"env {ORCH_ROOT}/bin/jupyter", f"env {ORCH_ROOT}/bin/snakemake"):
            self._fail(self._plan([self._u(bm, st, "module")]))
        S.validate_plan(self._plan([_code("c", st)], self._e1(f"{ha}/project/conda")))
        for bad_u in (
            self._u("stage/torchrun/2.4", "none", "module"),
            self._u(f"{ha}/proj/prefixes/eval/bin/torchrun", st),
            self._u("/srv/other-venv/bin/torchrun", model_line=st),
            self._u("env -u LD_PRELOAD /srv/other/bin/python", st, model_line=st),
            _slurm("u", _rt("cm", SP, st, model_line=mk), model_line=st),
            self._u(ub + "python3", st, "module", model_line=st),
        ):
            self._fail(self._plan([bad_u]))
        crt = _rt("nf", ul + "nextflow", probe="nf -v", verified_by="preflight")
        self._fail(self._plan([_slurm("can", crt, kind="pipeline", environment=st, command="nf -v"), _slurm("wk", dict(crt, verified_by="canary:can"), kind="pipeline", needs=["can"], environment=mk)]), "canary")
        ce2 = dict(self._e1("/srv/model-prefixes/stage"), py3line=_env_spec("/srv/model-prefixes/python3", model_line="py3line"))
        S.validate_plan(self._plan([_slurm("u1", _rt("cs", st, st, res="conda")), _slurm("u2", _rt("cp", "python3", "py3line", res="conda"))], ce2))
        self._fail(self._plan([self._u("fit", st, "conda")], {"orchestration": _orch("/opt/miniforge3/envs/fit"), st: _env_spec("/srv/one/envs/fit")}), "ambiguous")
        ctr = _rt("ctr", "/srv/images/train.sif", st, res="container")
        self._fail(self._plan([_slurm("u1", ctr, model_line=st), _slurm("u2", ctr, model_line=mk)]), "share environment")
        S.validate_plan(self._plan([_code("c", st)], self._e1("/usr/local/lib/stage/venv")))
        self._fail(self._plan([_code("c", st)], self._e1("/usr/local/lib")))


class TestSurveyPythonEnvDetection(unittest.TestCase):
    def test_survey_python_env(self):
        det = SURVEY.detect_python_env
        CD, CP, VE = "CONDA_DEFAULT_ENV", "CONDA_PREFIX", "VIRTUAL_ENV"
        mf = "/home/hani/miniforge3"
        hbc = "/opt/homebrew/Caskroom/miniforge/base"

        def _det(pr, bp=None, ex=None, **env):
            return det(env=env, executable=ex or f"{pr}/bin/python", prefix=str(pr), base_prefix=str(bp or pr))

        def _key(rec):
            return (rec["kind"], rec["name"], rec["shared_risk"])

        for pr, bp, env, exp in (
            (WB_ENV, None, {CD: "workbook", CP: WB_ENV}, ("conda", "workbook", True)),
            (mf, None, {CD: "base", CP: mf}, ("conda", "base", True)),
            # Active conda env even when prefix != base_prefix is classified as conda, not venv.
            ("/opt/miniforge3/envs/stage", "/opt/miniforge3", {CD: "stage", CP: "/opt/miniforge3/envs/stage", "HOME": "/home/user"}, ("conda", "stage", False)),
            (ORCH_ROOT, "/opt/homebrew/opt/python@3.12", {VE: ORCH_ROOT}, ("venv", "orch-venv", False)),
            (ORCH_ROOT, mf, {CD: "base", CP: mf, VE: ORCH_ROOT}, ("venv", "orch-venv", False)),
            ("/opt/miniforge3/envs/stage-venv", "/opt/miniforge3", {CD: "base", CP: "/opt/miniforge3"}, ("venv", "stage-venv", False)),
            ("/home/alice/.conda/envs/stage", None, {}, ("conda", "stage", False)),
            (f"{hbc}/envs/stage", None, {CD: "stage", CP: f"{hbc}/envs/stage"}, ("conda", "stage", False)),
            (f"{hbc}/envs/miniconda3", None, {}, ("conda", "miniconda3", False)),
            (hbc, None, {}, ("conda", "base", True)),
        ):
            self.assertEqual(_key(_det(pr, bp, **env)), exp, pr)

        # condabin under standard or custom conda roots and Homebrew PyPy are recognized as shared.
        for cbin in (
            "/opt/miniforge3/condabin/conda",
            "/srv/proj/miniforge3/condabin/conda",
            "/srv/science/dist/condabin/conda",
            "/opt/homebrew/Cellar/pypy3.11/7.3.19/bin/pypy3.11",
        ):
            self.assertIsNotNone(SURVEY._shared_python_env_reason(cbin), cbin)

        with tempfile.TemporaryDirectory() as tmp:
            T = Path(tmp)
            for rel, nm in (
                ("miniforge3/projects/stage", "stage"),
                ("envs/stage", "stage"),
                ("envs/miniconda3", "miniconda3"),
                ("Caskroom/miniforge/24.3/envs/miniconda3", "miniconda3"),
                ("miniforge3/envs/fit", "fit"),
            ):
                cp = T / rel
                (cp / "conda-meta").mkdir(parents=True)
                if rel == "envs/stage":
                    (cp / "envs").mkdir()
                if rel == "miniforge3/envs/fit":
                    (cp / "bin").mkdir()
                    (cp / "bin/conda").write_text("#!/bin/sh\n", encoding="utf-8")
                self.assertEqual(_key(_det(cp)), ("conda", nm, False), rel)

            # Unactivated on-disk conda env with prefix != base_prefix is still reported as conda (no pyvenv.cfg).
            uc = T / "miniforge3/envs/unact"
            (uc / "conda-meta").mkdir(parents=True)
            self.assertEqual(_key(_det(uc, T / "miniforge3")), ("conda", "unact", False))

            # Dedicated custom-prefix conda env containing conda package (even with local pkgs/) is NOT guessed to be base.
            cf = T / "models/fit"
            (cf / "conda-meta").mkdir(parents=True)
            (cf / "condabin").mkdir()
            self.assertEqual(_key(_det(cf)), ("conda", "fit", False))
            (cf / "pkgs").mkdir()
            self.assertEqual(_key(_det(cf)), ("conda", "fit", False))

            # Active custom-prefix conda base (via CONDA_DEFAULT_ENV=base + CONDA_PREFIX alias) IS flagged as base.
            dist, cur = T / "science/dist", T / "science/current"
            (dist / "conda-meta").mkdir(parents=True)
            (dist / "condabin").mkdir()
            cur.symlink_to(dist)
            self.assertEqual(_key(_det(dist, **{CD: "base", CP: str(cur)})), ("conda", "base", True))

            # Unactivated $HOME/miniforge3 via symlinked HOME is still recognized as shared conda base.
            home_real = (T / "storage/alice").resolve()
            mf_home = home_real / "miniforge3"
            (mf_home / "conda-meta").mkdir(parents=True)
            home_link = T / "home-link"
            home_link.symlink_to(home_real, target_is_directory=True)
            self.assertEqual(_key(_det(mf_home, HOME=str(home_link))), ("conda", "base", True))

            # Case-sensitive HOME check does not misclassify /scratch/alice/miniforge3 as HOME=/scratch/Alice base.
            with mock.patch.object(SURVEY._P, "samefile", return_value=False):
                cs_h, cs_p = T / "Alice", T / "alice/miniforge3"
                cs_h.mkdir(exist_ok=True)
                (cs_p / "conda-meta").mkdir(parents=True)
                self.assertEqual(_key(_det(cs_p, HOME=str(cs_h))), ("conda", "miniforge3", False))

            # When HOME is unset in live os.environ, detect_python_env falls back to Path.home().
            pw_h = T / "scratch/users/alice"
            pw_mf = pw_h / "miniforge3"
            (pw_mf / "conda-meta").mkdir(parents=True)
            with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(SURVEY.Path, "home", return_value=pw_h):
                self.assertEqual(_key(det(env=None, executable=f"{pw_mf}/bin/python", prefix=str(pw_mf), base_prefix=str(pw_mf))), ("conda", "base", True))

            sc = T / "miniforge3/envs/compiler"
            sc.mkdir(parents=True)
            for ce in ({}, {CP: str(sc), CD: "fit"}):
                rs = _det(sc, **ce)
                self.assertEqual((rs["kind"], rs["shared_risk"]), ("system", True))

            wc, ca = T / "miniforge3/envs/workbook", T / "project-fit"
            (wc / "conda-meta").mkdir(parents=True)
            ca.symlink_to(wc)
            self.assertEqual(_key(_det(ca)), ("conda", "workbook", True))

            cw = T / "conda-prefixes/workbook"
            (cw / "conda-meta").mkdir(parents=True)
            self.assertEqual(_key(_det(cw, **{CP: str(cw), CD: str(cw)})), ("conda", "workbook", True))

            # Case-sensitive Linux HOME venvs differing in case from .venv/venv are not flagged.
            lh = T / "link-home"
            lh.mkdir()
            bv = T / "scratch/default-py"
            bv.mkdir(parents=True)
            (lh / ".venv").symlink_to(bv)
            with mock.patch.object(SURVEY._P, "samefile", return_value=False):
                for pr in ("/home/alice/.VENV", "/home/alice/Venv", f"{T}/scratch/Default-Py"):
                    rec = det(env={"HOME": str(lh)}, executable=f"{pr}/bin/python", prefix=pr, base_prefix="/usr")
                    self.assertFalse(rec["shared_risk"], pr)

        for pr, ex, env in (
            ("/usr", f"{ORCH_ROOT}/bin/python", {VE: ORCH_ROOT}),
            ("/opt/cpython", "/opt/cpython/bin/python3", {CD: "fit", CP: "/srv/proj/envs/fit"}),
            ("/srv/envs/cpython", "/srv/envs/cpython/bin/python3", {}),
            ("/opt/homebrew/opt/python@3.12", "/opt/homebrew/bin/python3.12", {}),
            ("/home/alice/miniforge3/envs/stage/toolchain", None, {}),
        ):
            r = _det(pr, pr, ex, **env)
            self.assertEqual((r["kind"], r["shared_risk"]), ("system", True), pr)

        for bad, home in (
            ("/home/hani/.venv", None),
            ("/root/.venv", None),
            ("/opt/pyenv/versions/3.12.2", None),
            ("/cluster/users/alex/.venv", "/cluster/users/alex"),
        ):
            rec = _det(bad, "/usr", VIRTUAL_ENV=bad, **({} if home is None else {"HOME": home}))
            self.assertTrue(rec["shared_risk"], bad)

    def test_survey_cli_locks(self):
        with tempfile.TemporaryDirectory() as tmp:
            T = Path(tmp)
            wv = T / "envs/workbook"
            wv.mkdir(parents=True)
            for target in (wv, "/home/alice/.venv"):
                vl = T / ("vl-" + Path(str(target)).name.strip("."))
                vl.symlink_to(target)
                self.assertTrue(
                    SURVEY.detect_python_env(env={}, executable=f"{vl}/bin/python", prefix=str(vl), base_prefix="/usr")["shared_risk"]
                )
            repo = T / "proj"
            for d in ("models/maxtoki", ".venv/pkg", "docs", "a/b/c/d/e"):
                (repo / d).mkdir(parents=True)
            for i in range(42):
                (repo / f"docs/d{i:02d}.md").write_text("x\n", encoding="utf-8")
            for i in range(14):
                (repo / f".venv/pkg/requirements.{i}.lock").write_text("x\n", encoding="utf-8")
                (repo / f"sub{i:02d}").mkdir()
                (repo / f"sub{i:02d}/uv.lock").write_text("v=1\n", encoding="utf-8")
            for rel in ("requirements.lock", "models/maxtoki/uv.lock", "a/b/c/d/uv.lock", "a/b/c/d/e/uv.lock"):
                (repo / rel).write_text("x\n", encoding="utf-8")
            with mock.patch.object(SURVEY.os, "walk", side_effect=AssertionError("unbounded os.walk")):
                ro = SURVEY.repo(repo)
            self.assertTrue({"a/b/c/d/uv.lock", "a/b/c/d/e/uv.lock"} <= {d["path"] for d in ro["documents"]})
            proc = _run([sys.executable, str(PROJECT_SCRIPTS / "survey.py"), "--repo", str(repo), "--json"])
            self.assertEqual(proc.returncode, 0, proc.stderr)
            data = json.loads(proc.stdout)
            self.assertEqual(data["schema_version"], 4)
            self.assertIn(data["machine"]["python_env"]["kind"], ("conda", "venv", "system"))
            docs = {d["path"] for d in data["repo"]["documents"]}
            self.assertTrue({"requirements.lock", "models/maxtoki/uv.lock", "sub13/uv.lock", "a/b/c/d/uv.lock", "a/b/c/d/e/uv.lock"} <= docs)
            self.assertFalse(any(".venv/" in p for p in docs))


class TestPlanSurveyDocsAndDeclarations(unittest.TestCase):
    def test_declarations_and_references(self):
        rd = lambda rel: (ROOT / rel).read_text(encoding="utf-8")
        for skill, decl in (
            ("hanig-project", "env.dedicated"),
            ("hanig-project", "interview.orchestration-env"),
            ("hanig-swarm", "env.dedicated"),
        ):
            self.assertIn(f"`{decl}`:", rd(f"skills/{skill}/SKILL.md"))
        uc = rd("skills/hanig-project/references/unit-contract.md")
        si = rd("skills/hanig-project/references/survey-interview.md")
        fe = rd("skills/hanig-swarm/references/field-evidence.md")
        self.assertIn("Dedicated environments per model line", uc)
        self.assertIn("machine.python_env", si)
        self.assertIn("orch-venv", fe)
        self.assertIn("2026-11-22", fe)


if __name__ == "__main__":
    unittest.main()
