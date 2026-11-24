"""Dedicated orchestration environment startup checks, cron guards, templates, and migration."""
import ast
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SS = ROOT / "skills/hanig-swarm/scripts"
OS = ROOT / "skills/hanig-orchestrate/scripts"
for _p in (SS, OS, Path(__file__).resolve().parent):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import orch_env as OE  # noqa: E402
from scheduler_fixture import cleanup_module_path as tearDownModule, isolated_module_path  # noqa: E402

ORCH_ROOT = "/srv/state/stage/orch-venv"
STAGE_ROOT = "/srv/proj/.venvs/stage"
WB_ENV = "/home/hani/miniforge3/envs/workbook"
_TD = tempfile.TemporaryDirectory
_SIO = io.StringIO


def setUpModule():
    isolated_module_path()
    p = Path(os.environ["PATH"].split(os.pathsep)[0]) / "paseo"
    _w(p, "#!/bin/sh\nexit 0\n")
    p.chmod(0o755)


def _w(p, s):
    p.write_text(s, encoding="utf-8")


def _run(cmd, env=None):
    return subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=30)


def _e(path, lock="requirements.lock", model_line=None, kind=None, py="3.12"):
    d = {"path": str(path), "python": py, "lock_file": lock, "build": f"uv venv --python {py} {path}"}
    if model_line is not None:
        d["model_line"] = model_line
    if kind is not None:
        d["kind"] = kind
    return d


def _orch(path=ORCH_ROOT):
    return _e(path, "none", kind="orchestration")


class TestOrchestrationEnvCheckAndHelpers(unittest.TestCase):
    def _venv(self, root, name="orch-venv"):
        v = Path(root) / name
        b = v / "bin"
        b.mkdir(parents=True)
        _w(v / "pyvenv.cfg", f"home = {Path(sys.executable).parent}\n")
        py, py3 = b / "python", b / "python3"
        py.symlink_to(sys.executable)
        py3.symlink_to(sys.executable)
        return v, py, py3

    def _wplan(self, path, envs):
        _w(path, json.dumps({"project": "p", "environments": envs, "units": []}))

    def _ok(self, py, run=None, env=None, **kw):
        return OE.check_session_python(python_path=str(py), running_python=str(run or py), env=({} if env is None else env), **kw)["ok"]

    def _cron(self, script, proj, env):
        return _run(["/bin/sh", str(script), str(proj)], env=env)

    def _oe(self, p, **more):
        envs = {"orchestration": _orch(p)}
        for k, v in more.items():
            envs[k] = v if isinstance(v, dict) else _e(v, model_line=k)
        return {"environments": envs}

    def _mkx(self, p, body="#!/bin/sh\n"):
        p.parent.mkdir(parents=True, exist_ok=True)
        _w(p, body)
        p.chmod(0o755)
        return p

    def test_orch_startup_check(self):
        ae, at, af, ok, oe = self.assertEqual, self.assertTrue, self.assertFalse, self._ok, self._oe
        H, CD, CP = "HANIG_ORCH_PYTHON", "CONDA_DEFAULT_ENV", "CONDA_PREFIX"
        rep = OE.check_session_python(python_path=f"{WB_ENV}/bin/python3", running_python="/usr/bin/python3", env={CD: "workbook", CP: WB_ENV})
        wt = "\n".join(rep["warnings"]).lower()
        ae((rep["ok"], "undeclared" in wt, "workbook" in wt), (False, True, True))
        proc = _run([sys.executable, str(OS / "orch_env.py"), "check", "--strict", "--json"], env={k: v for k, v in os.environ.items() if k != H})
        ae((proc.returncode, "WARNING:" in proc.stderr, json.loads(proc.stdout)["ok"]), (1, True, False))
        with _TD() as tmp:
            T = Path(tmp)
            v, py, py3 = self._venv(tmp)
            pp = T / "plan.json"
            self._wplan(pp, {"orchestration": _orch(v)})
            at(ok(py3, py, {"PATH": str(v / "bin")}, plan_path=pp))
            p_ok = _run([str(py), str(OS / "orch_env.py"), "check", "--plan", str(pp), "--strict"], env={"PATH": str(v / "bin"), H: str(py), CD: "workbook", CP: WB_ENV})
            ae((p_ok.returncode, p_ok.stderr, "orch_env OK:" in p_ok.stdout), (0, "", True))
            _, npy, npy3 = self._venv(v / "sub", "other-venv")
            af(ok(npy3, npy, plan_path=pp))
            cl = T / "envs/workbook"
            cl.parent.mkdir()
            cl.symlink_to(v)
            af(ok(py3, py, plan=oe(cl)))
            with mock.patch.object(sys, "executable", str(py)), mock.patch.object(sys, "prefix", "/usr"):
                af(OE.check_session_python(plan_path=pp, python_path=str(py3), running_python=None, env={})["ok"])
                at(OE._live_mismatch(OE.declared_orchestration_env(plan_path=pp, env={}), str(py)))
                bp = _SIO()
                at(OE.warn_if_declared_mismatch(plan_path=pp, env={}, running_python=None, stream=bp))
                self.assertIn("sys.prefix", bp.getvalue())
            mf = T / "miniforge3"
            po = mf / "envs/project-orch"
            (po / "conda-meta").mkdir(parents=True)
            self._mkx(po / "bin/conda")
            at(ok(self._mkx(po / "bin/python"), env={CD: "base", CP: str(mf)}, plan=oe(po)))
            for bh in (v / "bin/pythno", v / "Scripts/python3"):
                af(ok(py3, py, {H: str(bh)}, plan_path=pp))
                af(ok(bh, py, plan_path=pp))
            tl = T / "tools"
            tl.symlink_to(v / "bin")
            af(ok(tl / "python3", py, plan_path=pp))
            old = os.getcwd()
            try:
                os.chdir(tmp)
                at(OE.check_session_python(plan_path=pp, running_python=str(py), env={"PATH": "orch-venv/bin"})["ok"])
            finally:
                os.chdir(old)
            sd, ad = T / "shared-envs", T / "env-alias"
            sd.mkdir()
            ad.symlink_to(sd)
            af(ok(py3, py, plan=oe(v, a=sd / "fit", b=ad / "fit")))
            uv = v.parent / v.name.upper()
            if uv.exists() and os.path.samefile(str(v), str(uv)):
                at(ok(py3, py, plan=oe(uv)))
                af(ok(py3, py, plan=oe(v, m=uv / "new-model")))
                es = T / "EnvStore"
                es.mkdir()
                af(ok(py3, py, plan=oe(v, a=es / "fit", b=T / "envstore/fit")))
                af(ok(py3, py, plan=oe(v, a=es / "NewFit", b=T / "envstore/newfit")))
            else:
                es = T / "EnvStore"
                es.mkdir()
                at(ok(py3, py, plan=oe(v, a=es / "NewFit", b=es / "newfit")))
            # Case-variant symlink (tEST -> Test) on a case-sensitive filesystem does NOT trick _dir_case_insensitive into false overlap.
            td_cs = T / "Test"
            td_cs.mkdir()
            if not (T / "tEST").exists():
                (T / "tEST").symlink_to(td_cs)
            with mock.patch.object(OE.sys, "platform", "linux"), mock.patch.object(OE.P, "islink", return_value=True):
                af(OE._dir_case_insensitive(str(td_cs)))
                af(OE._overlap(str(td_cs / "NewFit"), str(td_cs / "newfit")))
            # Non-orchestration shared symlink (miniforge3/envs/workbook -> wb-store) is caught via its raw declared path.
            wbs = T / "wb-store"
            (wbs / "conda-meta").mkdir(parents=True)
            mfw = T / "miniforge3/envs/workbook"
            mfw.parent.mkdir(parents=True)
            mfw.symlink_to(wbs)
            af(ok(py3, py, plan=oe(v, stage=mfw)))
            pc = self._mkx(v / "bin/python-copy")
            eb = T / "ext-tools"
            eb.mkdir()
            (eb / "python3").symlink_to(pc)
            af(ok(eb / "python3", py, plan_path=pp))

    def test_orch_discovery_and_edges(self):
        ae, at, af, ok, oe = self.assertEqual, self.assertTrue, self.assertFalse, self._ok, self._oe
        wm, H, O, E, W, PJ = OE.warn_if_declared_mismatch, "HANIG_ORCH_PYTHON", "orchestration", "environments", "WARNING: orch_env:", "plan.json"
        w1 = lambda **kw: wm(stream=_SIO(), **kw)
        # Custom-named orchestration entry works; key 'orchestration' with kind='model_line' or multiple orchestration entries are rejected.
        d = OE.declared_orchestration_env(plan={E: {"stage": _e("/srv/p/m", model_line="stage"), "ops": _orch("/srv/p/ops")}}, env={})
        ae((d["env_id"], d["plan_env_path"], d["plan_error"], d["other_env_errors"]), ("ops", "/srv/p/ops", None, []))
        d_bad_k = OE.declared_orchestration_env(plan={E: {O: _e("/srv/p/m", kind="model_line"), "ops": _orch("/srv/p/ops")}}, env={})
        at(any("invalid kind" in e for e in d_bad_k["other_env_errors"]))
        d_dup = OE.declared_orchestration_env(plan={E: {O: _orch("/srv/p/o1"), "ops": _orch("/srv/p/o2")}}, env={})
        at("multiple orchestration" in (d_dup["plan_error"] or ""))
        for bad in ("/root/.venv", "/usr/local/lib", "/usr/libexec", "/usr/sbin/fit", "/home/alice/.local", "/srv/team/venvs/workbook", "/opt/miniforge3/condabin"):
            af(ok(f"{bad}/bin/python3", f"{bad}/bin/python", plan=oe(bad)), bad)
        # Exact-case POSIX system prefixes preserve /USR/bin/fit on case-sensitive hosts; deeper $HOME/.local/state/.../orch-venv is allowed.
        with mock.patch.object(OE.P, "samefile", return_value=False):
            self.assertIsNone(OE.shared_env_reason("/USR/bin/fit", is_env_root=True))
            self.assertIsNone(OE.shared_env_reason("/home/alice/.local/state/stage/orch-venv", is_env_root=True))
            self.assertIsNotNone(OE.shared_env_reason("/home/alice/.local", is_env_root=True))
            self.assertIsNotNone(OE.shared_env_reason("/.venv", is_env_root=True, home="/"))
            self.assertIsNotNone(OE.shared_env_reason("/miniforge3", is_env_root=True, home="/"))
            self.assertIsNone(OE.shared_env_reason("/opt/miniforge3/envs/common/bin/fit", is_env_root=True))
        # Whitespace and missing required env fields (python/lock_file/build) are refused.
        for bad_spec in (
            _orch("/srv/with space/orch-venv"),
            {"path": ORCH_ROOT, "lock_file": "none", "build": "uv venv"},
            {"path": ORCH_ROOT, "python": "latest", "lock_file": "none", "build": "uv venv"},
            {"path": ORCH_ROOT, "python": "3.12", "lock_file": "none"},
        ):
            af(ok(f"{ORCH_ROOT}/bin/python", plan={E: {O: bad_spec}}))
        af(ok(f"{ORCH_ROOT}/bin/python", plan={E: {O: _orch(), "stage": _e(STAGE_ROOT, lock="none", model_line="stage")}}))
        sd0, cp = "/srv/science/dist", oe("/srv/science/dist")
        for ce in ({"CONDA_DEFAULT_ENV": "base", "CONDA_PREFIX": sd0}, {"CONDA_DEFAULT_ENV": "base"}):
            af(ok(f"{sd0}/bin/python3", f"{sd0}/bin/python", ce, plan=cp))
            at(w1(plan=cp, env=ce, running_python=f"{sd0}/bin/python"))
        af(ok(f"{ORCH_ROOT}/bin/python", env={H: f"{ORCH_ROOT}/bin/python"}, plan={E: {"stage": _e(STAGE_ROOT, model_line="stage")}}))
        for bp in ({E: []}, []):
            at(w1(plan=bp, env={}))
        buf = _SIO()
        ae((wm(env={}, stream=buf), buf.getvalue()), ([], ""))
        with _TD() as tmp:
            T = Path(tmp)
            va, pya, _ = self._venv(tmp, "orch-a")
            _, pyb, _ = self._venv(tmp, "orch-b")
            af(ok(pyb, pya, {H: str(pya)}))
            pa = oe(va)
            af(ok(pya, env={H: str(pyb)}, plan=pa))
            b2 = _SIO()
            at(wm(plan=pa, env={H: str(pyb)}, running_python=str(pyb), stream=b2))
            self.assertIn(W, b2.getvalue())
            sd = T / "state"
            sd.mkdir()
            _w(sd / "swarm-state.json", '{"units": {}}\n')
            wcmd = [sys.executable, str(OS / "wait_event.py"), "--state-dir", str(sd), "--once"]
            pe = _run(wcmd, env=dict(os.environ, **{H: str(pya)}))
            ae((pe.returncode, W in pe.stderr), (3, True))
            self._wplan(T / PJ, pa[E])
            clean = {k: v for k, v in os.environ.items() if k != H}
            pp = _run(wcmd, env=clean)
            ae((pp.returncode, W in pp.stderr), (3, True))
            p2 = T / "plan2.json"
            self._wplan(p2, {O: _orch(tmp + "/orch-b")})
            at(w1(env={}, running_python=str(pya), candidate_plan_paths=[T / PJ, p2]))
            coord = T / "coord"
            for nm, vd in (("A", va), ("B", T / "orch-b")):
                (coord / nm / "state").mkdir(parents=True)
                _w(coord / nm / "state/swarm-state.json", '{"units": {}}\n')
                self._wplan(coord / nm / PJ, {O: _orch(vd)})
            for bd, rc in (
                ("C", json.dumps({E: {O: _orch("~bad\x00x")}})),
                ("D", json.dumps({E: {O: _orch("~" + chr(0xD800) + "/v")}})),
                ("E", "{"),
                ("F", '{"environments":{"orchestration":{"path":null}}}'),
                ("G", '{"environments":[]}'),
            ):
                (coord / bd).mkdir()
                _w(coord / bd / PJ, rc)
            od, er = T / "reports", T / "empty-repo"
            er.mkdir()
            ph = _run([str(pya), str(OS / "hourly_facts.py"), "--state-root", str(coord), "--output-dir", str(od), "--repo", str(er)], env=clean)
            ae((ph.returncode, W in ph.stderr, "could not read plan" in ph.stderr, "invalid/missing" in ph.stderr, "not an object" in ph.stderr), (0, True, True, True, True))
            locked, dang = T / "locked", T / "dang-plan.json"
            locked.mkdir()
            _w(locked / PJ, "{}")
            locked.chmod(0)
            dang.symlink_to(T / "missing.json")
            at(w1(candidate_plan_paths=[dang], env={}))
            try:
                if os.geteuid() != 0:
                    at(w1(candidate_plan_paths=[locked / PJ], env={}))
            finally:
                locked.chmod(0o700)
            for rel, extra in (("custom-base", "condabin"), ("conda-store/workbook", None)):
                c = T / rel
                (c / "conda-meta").mkdir(parents=True)
                if extra:
                    (c / extra).mkdir()
                cpy = self._mkx(c / "bin/python")
                af(ok(cpy, env={H: str(cpy)}))
            hv, hpy, _ = self._venv(T / "custom-home", ".venv")
            af(ok(hpy, env={"HOME": str(hv.parent), H: str(hpy)}))
            # Symlinked $HOME/.venv and $HOME/.local are caught; case-sensitive Linux siblings are not flagged.
            bv, bpy, _ = self._venv(T / "scratch", "default-py")
            lh = T / "link-home"
            lh.mkdir()
            (lh / ".venv").symlink_to(bv)
            af(ok(bpy, env={"HOME": str(lh), H: str(bpy)}))
            lv, lpy, _ = self._venv(T / "scratch", "local-target")
            (lh / ".local").symlink_to(lv)
            af(ok(lpy, env={"HOME": str(lh), H: str(lpy)}))
            with mock.patch.object(OE.P, "samefile", return_value=False):
                self.assertIsNone(OE.shared_env_reason(str(T / "scratch/Default-Py/bin/python"), home=str(lh)))
            for bad_s in (None, {"path": None}):
                mp = {E: {O: _orch(va), "stage": bad_s}}
                af(ok(pya, plan=mp))
                at(w1(plan=mp, env={}, running_python=str(pya)))

    def test_no_bare_python3(self):
        hits = [
            (p.name, n.lineno, n.elts[0].value)
            for p in sorted(OS.glob("*.py"))
            for n in ast.walk(ast.parse(p.read_text(encoding="utf-8")))
            if isinstance(n, ast.List)
            and n.elts
            and isinstance(n.elts[0], ast.Constant)
            and isinstance(n.elts[0].value, str)
            and re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", n.elts[0].value)
        ]
        self.assertEqual(hits, [])

    def test_swarm_cron_guard(self):
        ae, ane, cron, O, S_, PJ = self.assertEqual, self.assertNotEqual, SS / "swarm-cron.sh", "orchestration", "stage", "plan.json"
        with _TD() as tmp:
            T = Path(tmp)
            proj = T / "proj"
            proj.mkdir()
            clean = {k: v for k, v in os.environ.items() if k != "ORCH_PYTHON"}
            rc = lambda py=None, c=cron, pr=proj, **kw: self._cron(c, pr, (dict(clean, ORCH_PYTHON=str(py), **kw) if py is not None else dict(clean, **kw)))
            c2 = lambda p, nd="", lb="": ae((p.returncode, nd in p.stderr.lower()), (2, True), lb)
            ok2 = lambda p: ane(p.returncode, 2)
            for lb, val, nd in (("missing", None, "orch_python"), ("relative", "python3", "absolute")):
                c2(rc(val), nd, lb)
            if os.path.exists("/usr/bin/python3"):
                c2(rc("/usr/bin/python3"), "shared")
            vd, pyd, _ = self._venv(tmp, "orch-declared")
            _, pyo, _ = self._venv(tmp, "orch-other")
            oo = {O: _orch(vd)}
            self._wplan(proj / PJ, oo)
            c2(rc(pyo), "orchestration environment")
            ok2(rc(pyd))
            self.assertTrue((proj / "advance.log").exists())
            cl = T / "swarm-cron-link.sh"
            cl.symlink_to(cron)
            ok2(rc(pyd, c=cl))
            bd = T / "banner"
            bd.mkdir()
            _w(bd / "sitecustomize.py", "print('env banner: ', end='')\n")
            ok2(rc(pyd, PYTHONPATH=str(bd)))
            sv, _, _ = self._venv(tmp, "stage-real")
            sa, ma = T / "stage-alias", T / "max-alias"
            sa.symlink_to(vd)
            ma.symlink_to(sv)
            als = [dict(oo, stage=_e(sa, model_line=S_)), dict(oo, stage=_e(sv, model_line=S_), maxtoki=_e(ma, model_line="maxtoki"))]
            uv = vd.parent / vd.name.upper()
            if uv.exists() and os.path.samefile(str(vd), str(uv)):
                als += [
                    dict(oo, stage=_e(uv, model_line=S_)),
                    dict(oo, stage=_e(uv / "new-model", model_line=S_)),
                    dict(oo, stage=_e(T / "EnvCase/NewFit", model_line=S_), maxtoki=_e(T / "envcase/newfit", model_line="maxtoki")),
                ]
                (T / "EnvCase").mkdir(exist_ok=True)
            for bad in als:
                self.assertFalse(self._ok(pyd, plan={"environments": bad}))
                self._wplan(proj / PJ, bad)
                c2(rc(pyd), "alias")
            wr, wl = T / "miniforge3/envs/workbook", T / "stage-to-wb"
            wr.mkdir(parents=True)
            wl.symlink_to(wr)
            we = dict(oo, stage=_e(wl, model_line=S_))
            self.assertFalse(self._ok(pyd, plan={"environments": we}))
            self._wplan(proj / PJ, we)
            c2(rc(pyd))
            # Malformed env entries (missing python/lock_file/build, multiple orchestration envs, $HOME/.local) are refused.
            for bad_envs, nd in (
                ({O: _orch(vd), "ops": _orch(sv)}, "multiple orchestration"),
                ({O: {"path": str(vd), "python": "3.12", "lock_file": "none"}}, "build"),
                ({O: _orch(vd), S_: _e(sv, lock="none", model_line=S_)}, "lock_file"),
                ({O: _orch(vd), S_: None}, "invalid/missing"),
                ({S_: _e(vd, model_line=S_)}, "no orchestration"),
            ):
                self._wplan(proj / PJ, bad_envs)
                c2(rc(pyd), nd)
            hl_home = T / "hl-home"
            hl_home.mkdir()
            (hl_home / ".local").symlink_to(vd)
            self._wplan(proj / PJ, oo)
            c2(rc(pyd, HOME=str(hl_home)), "refused")
            self._wplan(proj / PJ, oo)
            c2(rc(pyd, CONDA_DEFAULT_ENV="base", CONDA_PREFIX=str(vd)), "shared conda")
            np = T / "home/alice/proj"
            dv, py_d, _ = self._venv(np, ".venv")
            self._wplan(np / PJ, {O: _orch(dv)})
            ok2(rc(py_d, pr=np))
            _w(proj / PJ, json.dumps({"project": "leg", "units": []}))
            ok2(rc(pyd))
            ec, epy, _ = self._venv(T / "envs", "ops")
            (ec / "conda-meta").mkdir()
            self._mkx(ec / "bin/conda")
            self.assertIsNone(OE._shared_conda_dir(str(ec)))
            self._wplan(proj / PJ, {O: _orch(ec)})
            ok2(rc(epy))
            br = T / "broken-venv"
            (br / "bin").mkdir(parents=True)
            (br / "bin/python").symlink_to(sys.executable)
            c2(rc(br / "bin/python"))
            c2(rc(self._mkx(T / "fake-orch-python", "#!/bin/sh\nprintf 'ARGS:%s\\n' \"$*\"\n")))


class TestSkillDocsTemplatesAndMigration(unittest.TestCase):
    def test_templates_preamble_migration_bodies_and_changelog(self):
        rd = lambda r: (ROOT / r).read_text(encoding="utf-8")
        OR, ai = "skills/hanig-orchestrate/references/", self.assertIn
        lp = rd(OR + "operating-loop.md")
        pr = rd("skills/hanig-swarm/references/protocol-details.md")
        pm = rd(OR + "dispatch-preamble.md")
        cl = rd("CHANGELOG.md")
        self.assertNotIn("<string>/python3</string>", lp)
        self.assertNotRegex(lp, r"(?m)^python3\s+")
        self.assertNotRegex(lp, r"\s/python3\s")
        for s in (
            'ORCH_PYTHON="/home/USER/.local/state/PROJECT/orch-venv/bin/python"',
            "\\%",
            "swarm-cron.sh",
            "hourly_facts.py",
            "Migrating a running project",
            "uv venv",
            "mktemp -d",
            "validate",
            "status",
            "set -eu",
            "crontab.bak",
            "crontab.err",
            "no crontab for",
            "LaunchAgents.bak",
            "plan.json.bak",
            "scripts.bak",
            "--accept-plan-change",
            'lock_file="none"',
        ):
            ai(s, lp)
        for s in ('ORCH_PYTHON=\\"$HOME/.local/state/PROJ/orch-venv/bin/python\\"', "/orch-venv/bin/python"):
            ai(s, pr)
        for s in ("{{UNIT_ENV_NAME}}", "{{UNIT_ENV_PATH}}", "{{UNIT_ENV_PYTHON}}", "{{UNIT_ENV_LOCK}}", "{{UNIT_ENV_BUILD}}", "new model line", "lock file"):
            ai(s, pm)
        for sk, dec in (
            ("hanig-project", "env.dedicated"),
            ("hanig-project", "interview.orchestration-env"),
            ("hanig-swarm", "env.dedicated"),
            ("hanig-orchestrate", "env.dedicated"),
        ):
            ai(f"`{dec}`:", rd(f"skills/{sk}/SKILL.md"))
        for s in ("2026-11-22", "workbook", "orch-venv"):
            ai(s, cl)


if __name__ == "__main__":
    unittest.main()
