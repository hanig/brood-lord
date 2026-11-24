#!/usr/bin/env python3
"""Check orchestration Python interpreter against declared dedicated environments."""
import argparse, json, os, re, shutil, stat, sys
from pathlib import Path
sys.dont_write_bytecode = True

_ERR = (OSError, ValueError, UnicodeError)
_ENV_KINDS = ("model_line", "orchestration")
_ENV_KEYS = frozenset({"path", "python", "lock_file", "build", "model_line", "kind", "notes", "description"})
_ENV_PYTHON_RE = re.compile(r"^\d+\.\d+(?:\.\d+)?$")
_PLACEHOLDER_RE = re.compile(r"(?:\$\{?[A-Za-z_][A-Za-z0-9_]*\}?|<[A-Za-z0-9_.-]+>|\{\{[A-Za-z0-9_.-]+\}\})")
_PYTHON_EXE_RE = re.compile(
    r"^(?:platform-python(?:\d+(?:\.\d+)*)?(?:[tdmu]+|-dbg|-debug)?|pythonw?(?:\d+(?:\.\d+)*)?(?:[tdmu]+|-dbg|-debug)?|"
    r"pypy(?:\d+(?:\.\d+)*)?|graalpy(?:\d+(?:\.\d+)*)?|jython(?:\d+(?:\.\d+)*)?|micropython(?:\d+(?:\.\d+)*)?|pyston(?:\d+(?:\.\d+)*)?|"
    r"pip(?:x|\d+(?:\.\d+)*)?|ipython(?:\d+)?|ptpython(?:\d+)?|bpython(?:\d+)?|py\.?test(?:-?\d+(?:\.\d+)*)?|"
    r"nosetests(?:\d+)?|jupyter(?:-[a-z0-9._-]+)?|torchrun|accelerate|deepspeed|snakemake|poetry|pdm|hatch|"
    r"flask|uvicorn|gunicorn|celery|wandb|mlflow|dask(?:-[a-z0-9._-]+)?|conda|mamba|micromamba)(?:\.exe)?$", re.I)
_BAD_SHELL_CHARS = ("'", '"', "`", "$", ";", "|", "&", "<", ">", "\\", "*", "?", "[", "]", "{", "}", "(", ")", "\n", "\r", "\0")
_CONDA_ROOT_RE = re.compile(r"^(?:\.conda|micromamba|(?:miniconda|miniforge|anaconda|mambaforge)(?:\d+)?)$", re.I)
SHARED_ENV_NAMES = frozenset({"base", "workbook", "default", "shared", "personal", "common", "global", "root"})
_pt = lambda *xs: tuple(tuple(x.split("/")) for x in xs)
_SYS_PREFS = _pt("usr/bin", "usr/local/bin", "usr/sbin", "usr/libexec", "bin", "sbin", "opt/homebrew/bin", "home/linuxbrew/.linuxbrew/bin",
                 "Library/Frameworks/Python.framework", "Library/Developer/CommandLineTools", "Applications/Xcode.app", "System/Library")
_ROOT_EXACT = frozenset(_pt("usr", "usr/local", "usr/bin", "usr/local/bin", "usr/sbin", "usr/libexec", "usr/lib", "usr/local/lib", "usr/share",
                            "usr/local/share", "bin", "sbin", "opt/homebrew", "opt/homebrew/bin", "home/linuxbrew/.linuxbrew", "home/linuxbrew/.linuxbrew/bin"))
_ROOT_PREFS = _SYS_PREFS
_HOME_LAYOUTS = ((("home",), 2), (("Users",), 2), (("mnt", "weka", "home"), 4), (("root",), 1),
                 (("System", "Volumes", "Data", "home"), 5), (("System", "Volumes", "Data", "Users"), 5))
_HB = _pt("opt/homebrew/opt", "opt/homebrew/Cellar", "usr/local/opt", "usr/local/Cellar", "home/linuxbrew/.linuxbrew/opt", "home/linuxbrew/.linuxbrew/Cellar")

P = os.path
def _same_dir(a, b):
    if not a or not b: return False
    if a == b: return True
    try: return P.exists(a) and P.exists(b) and P.samefile(a, b)
    except _ERR: return False

def _has_prefix(ps, lo, pr):
    if len(ps) < len(pr): return False
    if ps[:len(pr)] == pr: return True
    return lo[:len(pr)] == tuple(x.lower() for x in pr) and _same_dir("/" + "/".join(ps[:len(pr)]), "/" + "/".join(pr))

def _is_caskroom(ps, lo, i):
    if i <= 0: return False
    if ps[i - 1] == "Caskroom": return True
    return lo[i - 1] == "caskroom" and _same_dir("/" + "/".join(ps[:i]), "/" + "/".join(ps[:i - 1] + ("Caskroom",)))

def _dir_case_insensitive(d):
    if not d or "\0" in d: return False
    try:
        if not P.isdir(d): return False
        cur = P.realpath(d)
        if sys.platform == "darwin":
            try:
                v = os.pathconf(cur, 11)
                if v in (0, 1): return v == 0
            except _ERR: pass
        dev = os.lstat(cur).st_dev
        try:
            with os.scandir(cur) as it:
                for idx, e in enumerate(it):
                    if idx >= 64: break
                    if e.is_symlink(): continue
                    sw = e.name.swapcase()
                    if sw == e.name: continue
                    swp = P.join(cur, sw)
                    if not P.lexists(swp): return False
                    if not P.islink(swp) and _same_dir(e.path, swp): return True
        except _ERR: pass
        while cur and cur != P.dirname(cur):
            par, nm = P.split(cur)
            if os.lstat(par).st_dev != dev: break
            sw = nm.swapcase()
            if sw != nm and not P.islink(cur):
                swp = P.join(par, sw)
                if not P.lexists(swp): return False
                if not P.islink(swp) and _same_dir(cur, swp): return True
            cur = par
    except _ERR: pass
    return False

def _is_exec(p):
    try: return bool(p) and P.isfile(p) and os.access(p, os.X_OK)
    except _ERR: return False

def _safe_expand(raw):
    if not isinstance(raw, str) or "\0" in raw: return raw
    try: return P.expanduser(raw)
    except _ERR: return raw

def _looks_unresolved(s):
    return isinstance(s, str) and bool(_PLACEHOLDER_RE.search(s))

def _is_conda_base(d):
    if not d or "\0" in d: return False
    try:
        if not (P.isdir(P.join(d, "conda-meta")) and P.isdir(P.join(d, "condabin"))): return False
        return not any(len(ps) >= 2 and ps[-2] == "envs" and ps[-1] != "base" for ps in ([x.lower() for x in P.normpath(c).split("/") if x] for c in (d, P.realpath(d))))
    except _ERR: return False

def _shared_conda_dir(d):
    if not d or "\0" in d: return None
    try:
        if not P.isdir(P.join(d, "conda-meta")): return None
        if _is_conda_base(d): return f"({d}) is conda base"
        for c in (d, P.realpath(d)):
            lf = P.basename(P.normpath(c)).lower()
            if lf in SHARED_ENV_NAMES: return f"({d}) is shared conda env {lf!r}"
    except _ERR: pass
    return None

def _home_dirs(home):
    if not home or not isinstance(home, str) or not home.startswith("/") or "\0" in home: return []
    hs = [home]
    try:
        if P.exists(home):
            rh = P.realpath(home)
            if rh and rh.startswith("/") and rh != home: hs.append(rh)
    except _ERR: pass
    return hs

def _home_venv_roots(home):
    out = []
    for h in _home_dirs(home):
        for nm in (".venv", "venv"):
            hv = P.join(h, nm); out.append((nm, hv))
            try:
                if P.lexists(hv): out.append((nm, P.realpath(hv)))
            except _ERR: pass
    return out

def _home_local_roots(home):
    out = []
    for h in _home_dirs(home):
        hl = P.join(h, ".local"); out.append(hl)
        try:
            if P.lexists(hl): out.append(P.realpath(hl))
        except _ERR: pass
    return out

def _abs_parts(p):
    if not isinstance(p, str) or not p.strip(): return None
    t = p.strip()
    if t != p or any(c in t for c in ("\0", "\n", "\r")) or not t.startswith("/"): return None
    ps = [x for x in t.split("/") if x]
    return None if (not ps or any(x in (".", "..") for x in ps)) else tuple(ps)

def _canon_root(p):
    if not isinstance(p, str) or not p.strip(): return None
    r = p.strip()
    if "\0" in r: return r
    try: r = _safe_expand(r); return P.realpath(r if P.isabs(r) else P.abspath(r))
    except _ERR: return P.normpath(r)

def _is_venv_bin(p):
    try: d = P.dirname(p); return P.basename(d).lower() in ("bin", "scripts") and P.isfile(P.join(P.dirname(d), "pyvenv.cfg"))
    except _ERR: return False

def _canon_interp(p):
    if not isinstance(p, str) or not p.strip(): return None
    r = p.strip()
    if "\0" in r: return r
    try:
        r = _safe_expand(r)
        if not P.isabs(r):
            if os.sep not in r: return r
            r = P.abspath(r)
        rv, (par, nm) = _is_venv_bin(r), P.split(r)
        c = P.join(P.realpath(par) if P.exists(par) else P.normpath(par), nm)
        if _is_venv_bin(c): return c if rv else r
        if P.islink(c): t = P.realpath(c); return c if _is_venv_bin(t) else t
        return c
    except _ERR: return r

def _is_std_conda_loc(ps, lo, i, home=None):
    for h in _home_dirs(home):
        hp = tuple(x for x in h.split("/") if x)
        if i == len(hp) and (ps[:len(hp)] == hp or (hp and _same_dir(h, "/" + "/".join(ps[:len(hp)])))):
            return True
    for h, d in _HOME_LAYOUTS:
        hl = tuple(x.lower() for x in h)
        if i == d and lo[:len(h)] == hl and (ps[:len(h)] == h or _same_dir("/" + "/".join(ps[:d]), "/" + "/".join(h + ps[len(h):d]))):
            return True
    return ((i == 1 and (_has_prefix(ps, lo, ("opt",)) or _has_prefix(ps, lo, ("usr",))))
            or (i == 2 and _has_prefix(ps, lo, ("usr", "local")))
            or _is_caskroom(ps, lo, i))

def _resolve_home(em, env_was_none=False):
    hm = (em.get("HOME") or "").strip() or None
    if hm is None and env_was_none:
        try: hm = str(Path.home())
        except _ERR: hm = None
    return hm

def shared_env_reason(path, is_env_root=False, home=None):
    if not isinstance(path, str) or not path.strip(): return None
    try: path.encode("utf-8")
    except UnicodeEncodeError: return f"{path!r} invalid UTF-8 surrogate"
    if any(c.isspace() for c in path): return f"{path!r} contains whitespace"
    t = path
    if any(c in t for c in _BAD_SHELL_CHARS): return f"{t!r} shell quotes/metachars"
    if not is_env_root:
        if t in ("env", "/usr/bin/env", "/bin/env", "/usr/local/bin/env", "/opt/homebrew/bin/env"): return f"{t!r} uses 'env' wrapper"
        if not t.startswith("/"):
            return f"{t!r} is a flag" if t.startswith("-") else f"{t!r} must be an absolute path"
        if t.endswith("/"): return f"{t!r} ends with '/'"
    elif not t.startswith("/"): return f"{t!r} must be absolute"
    ps = _abs_parts(t)
    if ps is None: return f"{t!r} not clean absolute"
    if not is_env_root and ps[-1] == "env": return f"{t!r} uses 'env' wrapper"
    lo = tuple(x.lower() for x in ps)
    for hb in _HB:
        if _has_prefix(ps, lo, hb) and len(lo) > len(hb):
            fm = lo[len(hb)]
            if fm in ("python", "pypy", "pypy3", "graalpy", "micropython") or fm.startswith(("python@", "python3", "pypy@", "pypy3", "graalpy@", "micropython@")):
                return f"{t!r} in shared Homebrew Python"
    if is_env_root:
        if any(len(ps) == len(rp) and _has_prefix(ps, lo, rp) for rp in _ROOT_EXACT) or any(_has_prefix(ps, lo, pr) for pr in _ROOT_PREFS):
            return f"{t!r} in shared system dir"
        if lo[-1] in SHARED_ENV_NAMES: return f"{t!r} in shared personal env {ps[-1]!r}"
    else:
        if any(_has_prefix(ps, lo, pr) for pr in _SYS_PREFS): return f"{t!r} in shared system dir"
        if len(lo) >= 3 and lo[-2] in ("bin", "scripts") and lo[-3] in SHARED_ENV_NAMES: return f"{t!r} in shared personal env {ps[-3]!r}"
    for i, s in enumerate(lo):
        if s in (".pyenv", "pyenv") and i + 1 < len(lo) and lo[i + 1] in ("shims", "versions"): return f"{t!r} in shared pyenv"
    def _is_user_local_parts(hl_len, p_match):
        if not p_match or len(ps) <= hl_len or lo[hl_len] != ".local": return False
        if ps[hl_len] != ".local" and not _same_dir("/" + "/".join(ps[:hl_len + 1]), "/" + "/".join(ps[:hl_len] + (".local",))): return False
        if is_env_root:
            return len(ps) == hl_len + 1 or (len(ps) == hl_len + 2 and lo[hl_len + 1] in ("bin", "scripts", "lib"))
        return len(ps) == hl_len + 3 and lo[hl_len + 1] in ("bin", "scripts")
    for h, d in _HOME_LAYOUTS:
        hl = tuple(x.lower() for x in h)
        if len(ps) > d and lo[:len(h)] == hl and lo[d] in (".venv", "venv"):
            if (ps[:len(h)] == h and ps[d] in (".venv", "venv")) or _same_dir("/" + "/".join(ps[:d + 1]), "/" + "/".join(h + ps[len(h):d] + (lo[d],))):
                return f"{t!r} in shared home venv {ps[d]!r}"
        if len(ps) > d and lo[:len(h)] == hl and _is_user_local_parts(d, ps[:len(h)] == h or _same_dir("/" + "/".join(ps[:d]), "/" + "/".join(h + ps[len(h):d]))):
            return f"{t!r} in shared user-site (~/.local)"
    for nm, hv in _home_venv_roots(home):
        hp = _abs_parts(hv)
        if hp and len(ps) >= len(hp) and (ps[:len(hp)] == hp or _same_dir(hv, "/" + "/".join(ps[:len(hp)]))): return f"{t!r} in shared home venv {nm!r}"
    for hl_dir in _home_local_roots(home):
        lp = _abs_parts(hl_dir)
        if lp and len(ps) >= len(lp) and (ps[:len(lp)] == lp or _same_dir(hl_dir, "/" + "/".join(ps[:len(lp)]))):
            if is_env_root and (len(ps) == len(lp) or (len(ps) == len(lp) + 1 and lo[len(lp)] in ("bin", "scripts", "lib"))):
                return f"{t!r} in shared user-site (~/.local)"
            if not is_env_root and len(ps) == len(lp) + 2 and lo[len(lp)] in ("bin", "scripts"):
                return f"{t!r} in shared user-site (~/.local)"
    if any(lo[i] == "condabin" and (i == len(lo) - 1 or lo[i + 1] in ("conda", "mamba", "micromamba")) for i in range(len(lo))):
        return f"{t!r} in conda base condabin"
    for i in range(len(lo) if is_env_root else len(lo) - 1):
        if i > 0 and lo[i - 1] == "envs": continue
        s = lo[i]
        if (_CONDA_ROOT_RE.fullmatch(s) or s == "conda") and (_is_std_conda_loc(ps, lo, i, home=home) or (i + 1 < len(lo) and lo[i + 1] in ("base", "envs", "condabin"))):
            r = lo[i + 1:]; bo = 1 if (r and (r[0] == "base" or (_is_caskroom(ps, lo, i) and r[0] not in ("bin", "scripts", "condabin", "envs")))) else 0; r = r[bo:]
            if not r or r[0] in ("bin", "scripts", "condabin"): return f"{t!r} in conda base {ps[i]!r}"
            if r[0] == "envs" and len(r) >= 2:
                if r[1] in SHARED_ENV_NAMES and (len(r) == 2 or (len(r) == 4 and r[2] in ("bin", "scripts") and (not is_env_root or r[1] in ("workbook", "base")))):
                    return f"{t!r} in shared conda env {ps[i + 2 + bo]!r}"
                if len(r) == 2 or r[2] in ("bin", "scripts"): break
    for i, s in enumerate(lo):
        if s in ("envs", "virtualenvs", ".virtualenvs") and i + 1 < len(lo) and lo[i + 1] in SHARED_ENV_NAMES:
            if (is_env_root and i + 1 == len(lo) - 1) or (not is_env_root and (s != "envs" or (len(lo) == i + 4 and lo[i + 2] in ("bin", "scripts")))):
                return f"{t!r} in shared personal env {ps[i + 1]!r}"
    return None

def _belongs(er, tp):
    if not er or not tp: return False
    ce, ct = _canon_root(er), _canon_interp(tp); ep, tp2 = _abs_parts(ce), _abs_parts(ct)
    return bool(ep and tp2 and len(tp2) >= 3 and tp2[-2].lower() in ("bin", "scripts") and ((len(tp2) == len(ep) + 2 and tp2[:len(ep)] == ep) or _same_dir(ce, P.dirname(P.dirname(ct)))))

def _validate_env_spec(eid, sp):
    errs = []
    if not isinstance(eid, str) or not eid.strip() or eid == "none": return None, [f"invalid env key {eid!r}"]
    if not isinstance(sp, dict): return None, [f"plan env {eid!r} invalid/missing 'path'"]
    unk = sorted(set(sp) - _ENV_KEYS)
    if unk: errs.append(f"plan env {eid!r} unknown key(s): {', '.join(unk)}")
    k = sp["kind"] if ("kind" in sp and sp["kind"] is not None) else ("orchestration" if eid == "orchestration" else "model_line")
    if k not in _ENV_KINDS or (eid == "orchestration" and k != "orchestration"): errs.append(f"plan env {eid!r} invalid kind={k!r}")
    rp = sp.get("path")
    if not isinstance(rp, str) or not rp.strip() or any(c.isspace() for c in rp) or "\0" in rp or _looks_unresolved(rp):
        errs.append(f"plan env {eid!r} invalid/missing 'path'")
    else:
        ex = _safe_expand(rp)
        if _abs_parts(ex) is None: errs.append(f"plan env {eid!r} invalid/missing 'path'")
    pv, lf, bd, ml = sp.get("python"), sp.get("lock_file"), sp.get("build"), sp.get("model_line")
    if not isinstance(pv, str) or not _ENV_PYTHON_RE.fullmatch(pv.strip()): errs.append(f"plan env {eid!r} invalid/missing 'python'")
    if not isinstance(lf, str) or not lf.strip() or "\0" in lf or _looks_unresolved(lf) or (lf.strip() == "none" and k != "orchestration"):
        errs.append(f"plan env {eid!r} invalid/missing 'lock_file'")
    if not isinstance(bd, str) or not bd.strip() or "\0" in bd or _looks_unresolved(bd):
        errs.append(f"plan env {eid!r} invalid/missing 'build'")
    if (k == "orchestration" and ml is not None) or (ml is not None and (not isinstance(ml, str) or not ml.strip())):
        errs.append(f"plan env {eid!r} invalid 'model_line'")
    return k, errs

def declared_orchestration_env(plan_path=None, plan=None, env=None):
    em, po = (os.environ if env is None else env), plan
    o = dict.fromkeys(("source", "env_id", "env_path", "raw_plan_env_path", "plan_env_path", "env_var_python", "expected_python", "plan_error"))
    o["declared"], o["other_env_roots"], o["other_env_errors"] = False, [], []
    if po is None and plan_path is not None:
        try:
            ld = json.loads(Path(plan_path).read_text(encoding="utf-8"))
            if isinstance(ld, dict): po = ld
            else: o["plan_error"] = f"plan {plan_path} is not a JSON object"
        except (OSError, ValueError, RecursionError) as exc: o["plan_error"] = f"could not read plan {plan_path}: {exc}"
    if isinstance(po, dict):
        pe = po.get("environments")
        if isinstance(pe, dict):
            orch_ids, specs = [], {}
            for eid, sp in sorted(pe.items()):
                k, errs = _validate_env_spec(eid, sp)
                o["other_env_errors"].extend(errs)
                if k == "orchestration": orch_ids.append(eid)
                if isinstance(sp, dict) and isinstance(sp.get("path"), str) and sp["path"].strip() and not any(c.isspace() for c in sp["path"]) and "\0" not in sp["path"]:
                    specs[eid] = (k, sp)
            if not orch_ids: o["plan_error"] = "plan declares 'environments' with no orchestration environment"
            elif len(orch_ids) > 1: o["plan_error"] = f"plan declares multiple orchestration environments: {', '.join(sorted(orch_ids))}"
            sid = orch_ids[0] if len(orch_ids) == 1 else None
            for eid, (k, sp) in sorted(specs.items()):
                if eid == sid: continue
                raw_o = _safe_expand(sp["path"])
                o["other_env_roots"].append((eid, raw_o, _canon_root(raw_o)))
            if sid is not None and sid in specs:
                raw = _safe_expand(specs[sid][1]["path"]); norm = _canon_root(raw)
                o.update(declared=True, source="plan", env_id=sid, env_path=norm, raw_plan_env_path=raw, plan_env_path=norm, expected_python=P.join(norm, "bin", "python"))
        elif "environments" in po: o["plan_error"] = "plan 'environments' is not an object"
    elif po is not None: o["plan_error"] = "plan is not a JSON object"
    ep = (em.get("HANIG_ORCH_PYTHON") or "").strip()
    if ep:
        cpy = _canon_interp(ep); o.update(declared=True, env_var_python=cpy)
        if o["source"] == "plan": o["source"] = "plan+HANIG_ORCH_PYTHON"
        else:
            o["source"], o["expected_python"] = "HANIG_ORCH_PYTHON", cpy
            if cpy and cpy.startswith("/") and P.basename(P.dirname(cpy)).lower() in ("bin", "scripts"): o["env_path"] = P.dirname(P.dirname(cpy))
    return o

def _matches(d, cand):
    c = _canon_interp(cand) if cand else None
    if not c or not c.startswith("/") or "\0" in c: return False
    if d["plan_env_path"] or d["env_path"]: return _belongs(d["plan_env_path"] or d["env_path"], c)
    ev = d["env_var_python"]
    return bool(ev and "\0" not in ev and P.basename(c) == P.basename(ev) and _same_dir(P.dirname(c), P.dirname(ev)))

def _split_ex(p):
    if not p or "\0" in p: return p, ()
    cur, ms = P.normpath(p), []
    try:
        while cur and not P.exists(cur):
            par, nm = P.split(cur)
            if par == cur: break
            ms.append(nm); cur = par
    except _ERR: return p, ()
    return cur, tuple(reversed(ms))

def _overlap(a, b):
    pa, pb = _abs_parts(a), _abs_parts(b)
    if pa and pb and (pa[:len(pb)] == pb or pb[:len(pa)] == pa): return True
    if not a or not b or "\0" in a or "\0" in b: return False
    (ea, ma), (eb, mb) = _split_ex(a), _split_ex(b)
    if _same_dir(ea, eb):
        if not ma or not mb: return True
        if _dir_case_insensitive(ea): ma, mb = tuple(x.lower() for x in ma), tuple(x.lower() for x in mb)
        return ma[:len(mb)] == mb or mb[:len(ma)] == ma
    try: return (not ma and any(x.exists() and _same_dir(str(x), ea) for x in Path(eb).parents)) or (not mb and any(x.exists() and _same_dir(str(x), eb) for x in Path(ea).parents))
    except _ERR: return False

def _conflicts(d, em, env_was_none=False):
    hm, w = _resolve_home(em, env_was_none), list(d.get("other_env_errors") or ())
    roots = list(d.get("other_env_roots") or ())
    if d["plan_env_path"]: roots.insert(0, (d["env_id"] or "orchestration", d["raw_plan_env_path"], d["plan_env_path"]))
    for i, (ea, raw_a, ra) in enumerate(roots):
        sr = shared_env_reason(raw_a, True, hm) or shared_env_reason(ra, True, hm) or _shared_conda_dir(ra)
        if sr: w.append(f"env {ea!r} in shared env: {sr}")
        for eb, _, rb in roots[i + 1:]:
            if _overlap(ra, rb): w.append(f"envs {ea!r} ({ra}) and {eb!r} ({rb}) alias/overlap")
    if d["declared"]:
        for v in (d["raw_plan_env_path"], d["plan_env_path"] or d["env_path"]):
            r = shared_env_reason(v, True, hm) or _shared_conda_dir(v)
            if r: w.append(f"orchestration env not dedicated: {r}"); break
        er, ev = shared_env_reason(em.get("HANIG_ORCH_PYTHON"), False, hm), d["env_var_python"]
        if er: w.append(f"HANIG_ORCH_PYTHON not dedicated: {er}")
        if d["plan_env_path"] and ev and not _belongs(d["plan_env_path"], ev): w.append(f"HANIG_ORCH_PYTHON ({ev}) outside {d['plan_env_path']}")
        if ev and not _is_exec(ev): w.append(f"HANIG_ORCH_PYTHON ({ev}) missing/non-executable")
    return w

def _conda_warn(d, em, rpy, spy=None, req_s=False, env_was_none=False):
    cd, cp = (em.get("CONDA_DEFAULT_ENV") or "").strip() or None, (em.get("CONDA_PREFIX") or "").strip() or None
    if not (cd and cd.lower() in SHARED_ENV_NAMES): return None
    hm, dr, cc = _resolve_home(em, env_was_none), _canon_root(d["plan_env_path"] or d["env_path"]), (_canon_root(cp) if cp else None)
    try: hc = bool(dr and "\0" not in dr and P.isfile(P.join(dr, "pyvenv.cfg")))
    except _ERR: hc = False
    inc = (_same_dir(dr, cc) or _belongs(cc, rpy) or (spy is not None and _belongs(cc, spy))) if cc else not hc
    rok = d["declared"] and _matches(d, rpy) and not shared_env_reason(rpy, home=hm)
    sok = (not req_s) or (bool(spy) and _matches(d, spy) and not shared_env_reason(spy, home=hm))
    return f"shared conda env {cd!r} active (CONDA_PREFIX={cp or 'unset'})" if (inc or not (rok and sok)) else None

def _live_mismatch(d, running_python):
    if not d["declared"] or (running_python is not None and _canon_interp(running_python) != _canon_interp(sys.executable)): return None
    lv, ex = _canon_root(sys.prefix), _canon_root(d["plan_env_path"] or d["env_path"])
    if ex and not _same_dir(lv, ex): return f"sys.prefix ({lv}) disagrees with orchestration ({ex})"
    try:
        if lv and "\0" not in lv and P.isdir(lv):
            sc = _shared_conda_dir(lv)
            if sc: return f"sys.prefix {sc}"
            if not ((P.isfile(P.join(lv, "pyvenv.cfg")) and not _same_dir(lv, _canon_root(sys.base_prefix))) or P.isdir(P.join(lv, "conda-meta"))):
                return f"sys.prefix ({lv}) is standalone Python"
    except _ERR: pass
    return None

def _check_interp(d, em, py, lb, hm, live_py=None, req_exec=False, skip_dup=None):
    w, dl, el = [], d["plan_env_path"] or d["env_path"] or d["expected_python"], d["expected_python"] or d["env_path"]
    r = shared_env_reason(py, home=hm)
    if r and py != skip_dup: w.append(f"{lb} in shared env ({r}); use {el}" if not req_exec else f"{lb} in shared env: {r}")
    if d["declared"] and not _matches(d, py): w.append(f"{lb} ({py}) outside {dl} (use {el})")
    elif live_py is not _NO_LIVE:
        pm = _live_mismatch(d, live_py)
        if pm: w.append(pm)
    if req_exec and py.startswith("/") and py != skip_dup and not _is_exec(py): w.append(f"{lb} ({py}) missing/non-executable")
    return w
_NO_LIVE = object()

def check_session_python(plan_path=None, plan=None, python_path=None, running_python=None, env=None):
    env_was_none = env is None
    em = os.environ if env_was_none else env
    hm, d = _resolve_home(em, env_was_none), declared_orchestration_env(plan_path=plan_path, plan=plan, env=em)
    w = ([d["plan_error"]] if d["plan_error"] else []) + _conflicts(d, em, env_was_none)
    rs = shutil.which("python3", path=em.get("PATH")) if python_path is None else python_path
    spy, rpy = (_canon_interp(rs) if rs else None), _canon_interp(running_python or sys.executable)
    if not d["declared"]: w.append(f"session python3 ({spy or 'missing'}) in undeclared env")
    w += ["session PATH has no python3"] if spy is None else _check_interp(d, em, spy, "session python3", hm, _NO_LIVE, True)
    if rpy: w += _check_interp(d, em, rpy, "running interpreter", hm, running_python, True, spy)
    cw = _conda_warn(d, em, rpy, spy, True, env_was_none)
    if cw: w.append(cw)
    gv = lambda k: (em.get(k) or "").strip() or None
    return {"ok": not w, "declared": d["declared"], "declared_source": d["source"], "declared_env_id": d["env_id"],
            "declared_env_path": d["plan_env_path"] or d["env_path"], "declared_python": d["expected_python"], "session_python3": spy,
            "running_python": rpy, "conda_default_env": gv("CONDA_DEFAULT_ENV"), "conda_prefix": gv("CONDA_PREFIX"), "virtual_env": gv("VIRTUAL_ENV"), "warnings": w}

def _discover_plans(paths):
    seen, found, errs = set(), [], []
    for raw in paths or ():
        if raw is None: continue
        p = Path(raw)
        try: os.lstat(str(p))
        except (FileNotFoundError, NotADirectoryError): continue
        except _ERR as exc: errs.append(f"could not inspect plan {p}: {exc}"); continue
        try:
            if not stat.S_ISREG(os.stat(str(p)).st_mode): continue
            k = P.realpath(str(p))
            if k in seen: continue
            seen.add(k)
        except _ERR as exc: errs.append(f"could not inspect plan {p}: {exc}"); continue
        try: data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError) as exc: errs.append(f"could not read plan {p}: {exc}"); continue
        if not isinstance(data, dict): errs.append(f"plan {p} is not a JSON object")
        elif "environments" in data:
            if isinstance(data["environments"], dict): found.append(data)
            else: errs.append(f"plan {p} 'environments' is not an object")
    return found, errs

def _safe(s): return s.encode("utf-8", "backslashreplace").decode("utf-8")

def warn_if_declared_mismatch(plan=None, plan_path=None, env=None, running_python=None, stream=None, candidate_plan_paths=None):
    env_was_none = env is None
    em = os.environ if env_was_none else env
    hm, errs = _resolve_home(em, env_was_none), []
    if plan is not None or plan_path is not None: decls = [declared_orchestration_env(plan_path=plan_path, plan=plan, env=em)]
    else: ps, errs = _discover_plans(candidate_plan_paths); decls = [declared_orchestration_env(plan=p, env=em) for p in ps] or [declared_orchestration_env(env=em)]
    out, rpy, warns, seen = (sys.stderr if stream is None else stream), _canon_interp(running_python or sys.executable), [], set()
    def emit(m):
        ln = _safe("WARNING: orch_env: " + m)
        if ln not in seen: seen.add(ln); warns.append(ln); print(ln, file=out)
    for e in errs: emit(e)
    for d in decls:
        if not d["declared"] and not d["plan_error"] and not d["other_env_errors"]: continue
        rw = ([d["plan_error"]] if d["plan_error"] else []) + _conflicts(d, em, env_was_none) + _check_interp(d, em, rpy, "running interpreter", hm, running_python)
        cw = _conda_warn(d, em, rpy, env_was_none=env_was_none)
        if cw: rw.append(cw)
        for m in rw: emit(m)
    return warns

def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__); c = p.add_subparsers(dest="cmd", required=True).add_parser("check")
    c.add_argument("--plan"); c.add_argument("--python"); c.add_argument("--strict", action="store_true"); c.add_argument("--json", action="store_true")
    a = p.parse_args(argv); r = check_session_python(plan_path=a.plan, python_path=a.python)
    for w in r["warnings"]: print(_safe("WARNING: " + w), file=sys.stderr)
    print(json.dumps(r, indent=2, sort_keys=True) if a.json else f"orch_env {'OK' if r['ok'] else 'WARN'}: running={r['running_python'] or 'unknown'} session_python3={r['session_python3'] or 'missing'} declared={r['declared_env_path'] or r['declared_python'] or 'undeclared'}")
    if a.plan and any("could not read plan" in w or "is not a JSON object" in w for w in r["warnings"]): return 2
    return 1 if (a.strict and not r["ok"]) else 0

if __name__ == "__main__": sys.exit(main())
