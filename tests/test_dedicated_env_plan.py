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
            "/opt/miniforge3/condabin/conda",
            "/srv/proj/miniforge3/condabin/conda",
            u3,
            f"{u3}.13t",
            f"{u3}.13td",
            f"{u3}.7dm",
            f"{u3}-dbg",
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
            f"/srv/ML Tools/miniforge3/base{bp}",
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
            "/usr/bin/sort --output=/tmp/result",
            "/srv/bin/train --config=/tmp/fit.toml",
            "sort input.txt",
            "python3>out",
            f"{u3}>run.log",
            "sort>report",
            "--version",
            "env",
            f"{ub}/env",
            "/srv/ML Tools/bin/env",
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

    def test_case_sensitive_home_venv_and_declared_spaces(self):
        # Case-sensitive Linux paths under HOME differing in case from .venv/venv are accepted.
        for path in ("/home/alice/.VENV", "/home/alice/Venv"):
            S.validate_plan(self._plan([self._u(f"{path}/bin/python", "stage")], self._e1(path)))
        # Declared env paths containing spaces or '-2' are allowed; flags after the prefix are refused.
        spaced = "/home/alice/Experiment -2/stage"
        S.validate_plan(self._plan([self._u(f"{spaced}/bin/python", "stage")], self._e1(spaced)))
        self._fail(self._plan([self._u(f"{spaced}/bin/train --config=/tmp/fit.toml", "stage")], self._e1(spaced)), "refused")
        S.validate_plan(self._plan([self._u(f"{STAGE_ROOT}/bin/train worker", "stage")]))
        S.validate_plan(self._plan(
            [self._u("/srv/env experiments/stage/bin/python", "stage")],
            {"orchestration": _orch("/home/alice/Experiment - A/ops"), "stage": _env_spec("/srv/env experiments/stage", model_line="stage")},
        ))
        S.validate_plan(self._plan(
            [self._u("/srv/bin/python experiments/stage/bin/python", "stage")],
            self._e1("/srv/bin/python experiments/stage"),
        ))

    def test_bindings_and_sharing(self):
        st, mk, bp = "stage", "maxtoki", "/bin/python"
        for nm in ("workbook", "base"):
            self._fail(self._plan([_slurm("u", runtime=_rt("c", nm, res="conda"))], None), "shared")
        es = _base_envs()
        es["orchestration"]["path"] = WB_ENV
        self._fail(self._plan([_code()], es), "workbook")
        self._fail(self._plan([_slurm("u", "m")], runtimes={"m": _rt("m", f"/srv/proj/.venvs/mystery{bp}")}), "undeclared")
        self._fail(self._plan([_code("c", "ghost")]), "not declared")
        self._fail(self._plan([_slurm("u", "m")], runtimes={"m": _rt("m", MAXTOKI_ROOT + bp, st)}), "does not belong directly")
        self._fail(self._plan([_slurm("u", "r")], runtimes={"r": _rt("r", STAGE_ROOT, st)}))
        self._fail(self._plan([_code("c", "orchestration")]), "orchestration")
        self._fail(self._plan([_slurm("u", "o")], runtimes={"o": _rt("o", ORCH_ROOT + bp)}), "orchestration")
        self._fail(self._plan([_code("c", None)]), "environment")
        S.validate_plan(self._plan([_code("c", "none")]))
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
        # Legacy non-python tool inside a venv directory (including plain `venv` and with environment="none") cannot be shared across model lines.
        self._fail(
            self._plan([self._u("/srv/project/venv/bin/coverage", uid="c1", model_line=st), self._u("/srv/project/venv/bin/coverage", uid="c2", model_line=mk)], None),
            "share environment root",
        )
        self._fail(
            self._plan([self._u("/srv/proj/.venvs/one/bin/coverage", "none", uid="c1", model_line=st), self._u("/srv/proj/.venvs/one/bin/coverage", "none", uid="c2", model_line=mk)], None),
            "share environment root",
        )
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
            S.validate_plan(self._plan([_slurm("u", rt, command=t)]))
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
        for ep in (OP, SP, f"{ORCH_ROOT}/bin/run-stage"):
            self._fail(self._plan([self._u(ep, environment="none")]))
        cat = {n: _rt(n, ub + n, env="none", model_line=n) for n in ("sort", "cut")}
        S.validate_plan(self._plan(
            [
                _slurm("u1", "sort", environment="none"),
                _slurm("u2", "cut", environment="none"),
                self._u("/opt/homebrew/opt/r/bin/Rscript", uid="u3", model_line="eval"),
                self._u("/usr/lib/git-core/git", uid="u4"),
                self._u("/usr/sbin/ethtool", uid="u5"),
                _slurm("u6", _rt("nf", ul + "nextflow", st, probe="nf -v"), kind="pipeline"),
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
        uv = _rt("uvl", ul + "uv", res="uv")
        S.validate_plan(self._plan([_slurm("u1", uv, model_line=st), _slurm("u2", uv, model_line="eval")], None))
        S.validate_plan(self._plan([_slurm("u1", _rt("c1", "/srv/envs/stage", res="conda"), model_line=st), _slurm("u2", _rt("c2", "/srv/envs/eval", res="conda"), model_line="eval")], None))
        S.validate_plan(self._plan([
            self._u("samtools", res="module", uid="ms"),
            self._u("cuda/12.1", res="module", uid="mc"),
            self._u("stage/python/3.12", res="module", uid="mp", model_line=st),
            self._u("stage/miniforge3/24.3", res="module", uid="mm", model_line=st),
        ], None))
        self._fail(self._plan([self._u("stage/python/3.12", res="module", uid="m1", model_line=st), self._u("stage/python/3.12", res="module", uid="m2", model_line=mk)], None))
        self._fail(self._plan([self._u("stage/python/3.12", st, "module", "m1", model_line=st), self._u("stage/python/3.12", mk, "module", "m2", model_line=mk)]))
        self._fail(self._plan([self._u("stage/python/3.12", res="module", uid="mu")]))
        # Shared cluster Python/conda Lmod modules are unconditionally refused even when bound to an env.
        for bm in ("python3", "python/3.12", "lang/python/3.12", "anaconda3/2024.02", "conda/24.3", "torchrun"):
            self._fail(self._plan([self._u(bm, res="module")], None))
            self._fail(self._plan([self._u(bm, st, "module")]))
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
            ("/srv/tools/bin/env", "conda"),
            (f"{WB_ENV}/bin/coverage", "conda"),
            (f"{WB_ENV}/bin/coverage", "direct"),
            (f"{ha}/.venv/bin/rg", "direct"),
            ("uv", "uv"),
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
            S.validate_plan(self._plan([_slurm("can", cr, command=cr["probe"]), _slurm("wk", dict(cr, verified_by="canary:can"), needs=["can"], environment=we)], pe))
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
        self._fail(self._plan([_slurm("can", crt, environment=st, command="nf -v"), _slurm("wk", dict(crt, verified_by="canary:can"), needs=["can"], environment=mk)]), "canary")
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
            (ORCH_ROOT, "/opt/homebrew/opt/python@3.12", {VE: ORCH_ROOT}, ("venv", "orch-venv", False)),
            (ORCH_ROOT, mf, {CD: "base", CP: mf, VE: ORCH_ROOT}, ("venv", "orch-venv", False)),
            ("/opt/miniforge3/envs/stage-venv", "/opt/miniforge3", {CD: "base", CP: "/opt/miniforge3"}, ("venv", "stage-venv", False)),
            ("/home/alice/.conda/envs/stage", None, {}, ("conda", "stage", False)),
            (f"{hbc}/envs/stage", None, {CD: "stage", CP: f"{hbc}/envs/stage"}, ("conda", "stage", False)),
            (f"{hbc}/envs/miniconda3", None, {}, ("conda", "miniconda3", False)),
            (hbc, None, {}, ("conda", "base", True)),
        ):
            self.assertEqual(_key(_det(pr, bp, **env)), exp, pr)

        # condabin under standard or custom conda roots is recognized as conda base.
        for cbin in ("/opt/miniforge3/condabin/conda", "/srv/proj/miniforge3/condabin/conda"):
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

            dist, cur = T / "science/dist", T / "science/current"
            (dist / "conda-meta").mkdir(parents=True)
            (dist / "condabin").mkdir()
            cur.symlink_to(dist)
            for ce in ({CD: "base", CP: str(cur)}, {}):
                self.assertEqual(_key(_det(dist, **ce)), ("conda", "base", True))

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
            for d in ("models/maxtoki", ".venv/pkg", "docs", "a/b/c/d"):
                (repo / d).mkdir(parents=True)
            for i in range(42):
                (repo / f"docs/d{i:02d}.md").write_text("x\n", encoding="utf-8")
            for i in range(14):
                (repo / f".venv/pkg/requirements.{i}.lock").write_text("x\n", encoding="utf-8")
                (repo / f"sub{i:02d}").mkdir()
                (repo / f"sub{i:02d}/uv.lock").write_text("v=1\n", encoding="utf-8")
            for rel in ("requirements.lock", "models/maxtoki/uv.lock", "a/b/c/d/uv.lock"):
                (repo / rel).write_text("x\n", encoding="utf-8")
            with mock.patch.object(SURVEY.os, "walk", side_effect=AssertionError("unbounded os.walk")):
                ro = SURVEY.repo(repo)
            self.assertIn("a/b/c/d/uv.lock", {d["path"] for d in ro["documents"]})
            proc = _run([sys.executable, str(PROJECT_SCRIPTS / "survey.py"), "--repo", str(repo), "--json"])
            self.assertEqual(proc.returncode, 0, proc.stderr)
            data = json.loads(proc.stdout)
            self.assertEqual(data["schema_version"], 4)
            self.assertIn(data["machine"]["python_env"]["kind"], ("conda", "venv", "system"))
            docs = {d["path"] for d in data["repo"]["documents"]}
            self.assertTrue({"requirements.lock", "models/maxtoki/uv.lock", "sub13/uv.lock", "a/b/c/d/uv.lock"} <= docs)
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
