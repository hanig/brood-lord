#!/bin/sh
# Deterministic scheduler entry for one project (cron/launchd, lease-safe).
# Dedicated environments rule: require absolute ORCH_PYTHON (or $2); never bare python3.
set -eu
PROJ="${1:?usage: ORCH_PYTHON=/abs/path/to/orch-venv/bin/python swarm-cron.sh PROJ_DIR [ORCH_PYTHON]}"
ORCH_PYTHON="${ORCH_PYTHON:-${2:-}}"
case "$ORCH_PYTHON" in
  /*) ;;
  *) echo "error: ORCH_PYTHON must be an absolute path to the project's dedicated orchestration python" >&2; exit 2 ;;
esac
[ -x "$ORCH_PYTHON" ] || { echo "error: ORCH_PYTHON is not executable: $ORCH_PYTHON" >&2; exit 2; }
SCRIPT_PATH="$0"
while [ -L "$SCRIPT_PATH" ]; do
  LINK_TARGET=$(readlink "$SCRIPT_PATH")
  case "$LINK_TARGET" in
    /*) SCRIPT_PATH="$LINK_TARGET" ;;
    *)  SCRIPT_PATH="$(dirname "$SCRIPT_PATH")/$LINK_TARGET" ;;
  esac
done
cd "$(dirname "$SCRIPT_PATH")/.."
NONCE="$$"
EXPECTED="ORCH_CHECK_OK:$((NONCE * 7 + 13))"
CHECK_OUT=$("$ORCH_PYTHON" - "$PROJ/plan.json" "$ORCH_PYTHON" "$NONCE" <<'PY'
import json, os, sys
from pathlib import Path
sys.path.insert(0, str(Path("scripts").resolve()))
import swarm_plan_rules as SPR

plan_path, raw_i, nonce = sys.argv[1:4]
ERR, P = (OSError, ValueError, UnicodeError), os.path
def fail(m): sys.stderr.write("error: " + m + "\n"); sys.exit(2)
pd, pn = P.split(raw_i); rd = P.realpath(pd) if P.exists(pd) else P.normpath(pd); ci = P.join(rd, pn)
if not (P.basename(rd).lower() in ("bin", "scripts") and P.isfile(P.join(P.dirname(rd), "pyvenv.cfg"))) and P.islink(ci): ci = P.realpath(ci)
cp = P.realpath(sys.prefix)
def same_dir(a, b):
    try: return a == b or (P.exists(a) and P.exists(b) and P.samefile(a, b))
    except ERR: return a == b
def dir_case_insensitive(d):
    if not d or "\0" in d: return False
    try:
        if not P.isdir(d): return False
        cur = P.realpath(d)
        if sys.platform == "darwin":
            try:
                v = os.pathconf(cur, 11)
                if v in (0, 1): return v == 0
            except ERR: pass
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
                    if not P.islink(swp) and same_dir(e.path, swp): return True
        except ERR: pass
        while cur and cur != P.dirname(cur):
            par, nm = P.split(cur)
            if os.lstat(par).st_dev != dev: break
            sw = nm.swapcase()
            if sw != nm and not P.islink(cur):
                swp = P.join(par, sw)
                if not P.lexists(swp): return False
                if not P.islink(swp) and same_dir(cur, swp): return True
            cur = par
    except ERR: pass
    return False
def is_sh_conda(d):
    try:
        if not P.isdir(P.join(d, "conda-meta")): return False
        cs = (d, P.realpath(d))
        if any(P.basename(P.normpath(c)).lower() in SPR.SHARED_ENV_NAMES for c in cs): return True
        return P.isdir(P.join(d, "condabin")) and not any(len(ps) >= 2 and ps[-2] == "envs" and ps[-1] != "base" for ps in ([x.lower() for x in P.normpath(c).split("/") if x] for c in cs))
    except ERR: return False
def is_hv(p, is_root=False):
    hv = (os.environ.get("HOME") or "").strip()
    if not hv or "\0" in hv or not hv.startswith("/"): return False
    cs, rs, ls = [hv], [], []
    try:
        if P.exists(hv): cs.append(P.realpath(hv))
    except ERR: pass
    for h in cs:
        for nm in (".venv", "venv"):
            x = P.join(h, nm); rs.append(x)
            try:
                if P.lexists(x): rs.append(P.realpath(x))
            except ERR: pass
        xl = P.join(h, ".local"); ls.append(xl)
        try:
            if P.lexists(xl): ls.append(P.realpath(xl))
        except ERR: pass
    if any(SPR._env_path_contains(r, p) for r in rs): return True
    ps, err = SPR._clean_abs_parts(p)
    if err or not ps: return False
    lo = tuple(x.lower() for x in ps)
    for xl in ls:
        lp, lerr = SPR._clean_abs_parts(xl)
        if not lerr and lp and len(ps) >= len(lp) and (ps[:len(lp)] == lp or same_dir(xl, "/" + "/".join(ps[:len(lp)]))):
            if is_root and (len(ps) == len(lp) or (len(ps) == len(lp) + 1 and lo[len(lp)] in ("bin", "scripts", "lib"))): return True
            if not is_root and len(ps) == len(lp) + 2 and lo[len(lp)] in ("bin", "scripts"): return True
    return False
for lb, ca, ir in (("ORCH_PYTHON", raw_i, False), ("resolved ORCH_PYTHON", ci, False), ("sys.prefix", cp, True)):
    r = SPR.shared_env_reason(ca, is_env_root=ir)
    if r or is_hv(ca, ir): fail(f"{lb} refused: {r or 'shared personal env'}")
if is_sh_conda(cp): fail(f"ORCH_PYTHON ({raw_i}) shared conda ({cp})")
def split_ex(p):
    if "\0" in p: return p, ()
    c, m = P.normpath(p), []
    try:
        while c and not P.exists(c):
            par, nm = P.split(c)
            if par == c: break
            m.append(nm); c = par
    except ERR: return p, ()
    return c, tuple(reversed(m))
def overlap(a, b):
    if SPR._env_path_contains(a, b) or SPR._env_path_contains(b, a): return True
    if "\0" in a or "\0" in b: return False
    (ea, ma), (eb, mb) = split_ex(a), split_ex(b)
    if same_dir(ea, eb):
        if not ma or not mb: return True
        if dir_case_insensitive(ea): ma, mb = tuple(x.lower() for x in ma), tuple(x.lower() for x in mb)
        return ma[:len(mb)] == mb or mb[:len(ma)] == ma
    try: return (not ma and any(x.exists() and same_dir(str(x), ea) for x in Path(eb).parents)) or (not mb and any(x.exists() and same_dir(str(x), eb) for x in Path(ea).parents))
    except ERR: return False
if not SPR._interpreter_belongs_to_env(cp, ci): fail(f"ORCH_PYTHON ({raw_i}) mismatched prefix ({cp})")
if not ((P.isfile(P.join(cp, "pyvenv.cfg")) and not same_dir(cp, P.realpath(sys.base_prefix))) or P.isdir(P.join(cp, "conda-meta"))): fail(f"ORCH_PYTHON ({raw_i}) standalone Python ({cp})")
cn, cf = (os.environ.get("CONDA_DEFAULT_ENV") or "").strip().lower(), (os.environ.get("CONDA_PREFIX") or "").strip()
if cn in SPR.SHARED_ENV_NAMES:
    if cf and same_dir(P.realpath(cf) if P.exists(cf) else P.normpath(cf), cp): fail(f"ORCH_PYTHON in shared conda env {cn!r}")
    if not cf and not P.isfile(P.join(cp, "pyvenv.cfg")): fail(f"shared conda env {cn!r} active")
if P.isfile(plan_path):
    try: plan = json.loads(Path(plan_path).read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError) as exc: fail(f"unreadable plan {plan_path}: {exc}")
    if not isinstance(plan, dict): fail(f"plan {plan_path} not JSON object")
    if "environments" in plan:
        envs = plan["environments"]
        if not isinstance(envs, dict) or not envs: fail("plan.json 'environments' must be non-empty object")
        orch_ids, res = [], []
        for eid, sp in sorted(envs.items()):
            if not isinstance(eid, str) or not eid.strip() or eid == "none": fail(f"invalid env key {eid!r}")
            if not isinstance(sp, dict): fail(f"env {eid!r} invalid/missing 'path'")
            unk = sorted(set(sp) - SPR.ENV_KEYS)
            if unk: fail(f"env {eid!r} unknown key(s): {', '.join(unk)}")
            k = sp["kind"] if ("kind" in sp and sp["kind"] is not None) else ("orchestration" if eid == "orchestration" else "model_line")
            if k not in SPR.ENV_KINDS or (eid == "orchestration" and k != "orchestration"): fail(f"env {eid!r} invalid kind={k!r}")
            if k == "orchestration": orch_ids.append(eid)
            sr = sp.get("path")
            if not isinstance(sr, str) or not sr.strip() or any(c.isspace() for c in sr) or "\0" in sr or SPR._looks_unresolved(sr): fail(f"env {eid!r} invalid/missing 'path'")
            pv, lf, bd, ml = sp.get("python"), sp.get("lock_file"), sp.get("build"), sp.get("model_line")
            if not isinstance(pv, str) or not SPR._ENV_PYTHON_RE.fullmatch(pv.strip()): fail(f"env {eid!r} invalid/missing 'python'")
            if not isinstance(lf, str) or not lf.strip() or "\0" in lf or SPR._looks_unresolved(lf) or (lf.strip() == "none" and k != "orchestration"): fail(f"env {eid!r} invalid/missing 'lock_file'")
            if not isinstance(bd, str) or not bd.strip() or "\0" in bd or SPR._looks_unresolved(bd): fail(f"env {eid!r} invalid/missing 'build'")
            if (k == "orchestration" and ml is not None) or (ml is not None and (not isinstance(ml, str) or not ml.strip())): fail(f"env {eid!r} invalid 'model_line'")
            try: rr = P.expanduser(sr); rc = P.realpath(rr)
            except ERR as exc: fail(f"env {eid!r} invalid path: {exc}")
            for ca in (rr, rc):
                sh = SPR.shared_env_reason(ca, is_env_root=True)
                if sh or is_hv(ca, True) or is_sh_conda(ca): fail(f"env {eid!r} in shared env: {sh or ca}")
            res.append((eid, k, sr, rr, rc))
        if not orch_ids: fail("plan.json declares 'environments' with no orchestration environment")
        if len(orch_ids) > 1: fail(f"plan.json declares multiple orchestration environments: {', '.join(sorted(orch_ids))}")
        orch = next(x for x in res if x[0] == orch_ids[0])
        ro, cr = orch[2], orch[4]
        if not same_dir(cp, cr) or not (SPR._interpreter_belongs_to_env(cr, ci) or same_dir(P.dirname(P.dirname(ci)), cr)): fail(f"ORCH_PYTHON ({raw_i}) outside plan.json orchestration environment ({ro})")
        for i, (ea, _, _, _, pa) in enumerate(res):
            for eb, _, _, _, pb in res[i + 1:]:
                if overlap(pa, pb): fail(f"envs {ea!r} ({pa}) and {eb!r} ({pb}) alias/overlap")
print("\nORCH_CHECK_OK:{}\n".format(int(nonce) * 7 + 13))
PY
) || exit 2
case "
$CHECK_OUT
" in
  *"
$EXPECTED
"*) ;;
  *)
    echo "error: ORCH_PYTHON ($ORCH_PYTHON) failed dedicated orchestration Python verification" >&2
    exit 2
    ;;
esac
exec "$ORCH_PYTHON" scripts/swarm.py advance "$PROJ/plan.json" \
  --state-dir "$PROJ/.state" --root "$PROJ/.runs" >> "$PROJ/advance.log" 2>&1
