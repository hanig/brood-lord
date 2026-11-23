"""Plan declarations, scheduler survey rules and promotion destinations.

Python 3.8+, standard library only; dependencies never point to the facade.
"""

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import shlex

import unit as U
import converge as CV
import swarm_evidence as SE
from swarm_types import PlanError


def declared_seed(u):
    """Validate optional code provenance without rewriting any deciding byte."""
    if "seed" not in u:
        return None
    seed = u["seed"]
    prefix = f"unit {u.get('id', '?')!r}: seed"
    if not isinstance(seed, dict):
        raise PlanError(prefix + " must be a JSON object")
    if u.get("kind") != "code":
        raise PlanError(prefix + " is supported only for kind=code")
    if set(seed) - {"ref", "base", "head", "evidence"}:
        raise PlanError(prefix + " has unknown fields; use ref, base, head, evidence")
    ref = seed.get("ref")
    if not isinstance(ref, str) or not ref.startswith("refs/heads/"):
        raise PlanError(prefix + ".ref must be a full refs/heads/... branch ref")
    # git-check-ref-format's full-ref rules, without invoking Git during
    # offline plan validation. The prefix above supplies the required slash
    # and rules out a lone '@'; do not apply the stricter --branch rules.
    if (ref.endswith(".") or ".." in ref or "@{" in ref
            or any(not part or part.startswith(".") or part.endswith(".lock")
                   for part in ref.split("/"))
            or any(ord(c) < 32 or ord(c) == 127 or c in " ~^:?*[\\"
                   for c in ref)):
        raise PlanError(prefix + ".ref is not a valid refs/heads/... branch ref")
    for field in ("base", "head"):
        value = seed.get(field)
        if (not isinstance(value, str)
                or re.fullmatch(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})", value) is None):
            raise PlanError(prefix + "." + field + " must be a full 40/64-hex commit id")
    if "evidence" in seed:
        value = seed["evidence"]
        if not isinstance(value, str) or not value or "\0" in value:
            raise PlanError(prefix + ".evidence must be a non-empty path string without NUL")
    return seed


def declared_scope(u):
    """Return optional fnmatch patterns without normalizing deciding bytes."""
    if "scope" not in u:
        return None
    patterns = u["scope"]
    if (not isinstance(patterns, list)
            or any(not isinstance(p, str) for p in patterns)):
        raise PlanError(f"unit {u.get('id', '?')!r}: scope must be a JSON "
                        "list of strings")
    for pattern in patterns:
        if (not pattern or "\0" in pattern
                or pattern.startswith(("/", "\\"))
                or re.match(r"^[A-Za-z]:", pattern)
                or ".." in re.split(r"[/\\]", pattern)):
            raise PlanError(f"unit {u.get('id', '?')!r}: scope pattern "
                            f"{pattern!r} must be repo-relative, non-empty, "
                            "and contain no '..' segments")
    return patterns


def _norm_scope(scope):
    """A write scope as a comparable path prefix. From start-a-sprint."""
    s = str(scope).strip().replace("\\", "/")
    while s.startswith("./"):
        s = s[2:]
    return s.rstrip("/") + "/" if s and not s.endswith("/") else s


def _scopes_overlap(a, b):
    """True when either scope contains the other. Two units that can write the
    same place cannot both have an exclusive write root, which is the property
    the whole done-predicate rests on."""
    na, nb = _norm_scope(a), _norm_scope(b)
    if not na or not nb:
        return True                      # an empty scope is everything
    return na == nb or na.startswith(nb) or nb.startswith(na)


def declared_account(u):
    """The account a unit charges, or None. Same five spellings as partition."""
    args = [str(a) for a in (u.get("sbatch") or [])]
    for i, a in enumerate(args):
        if a.startswith("--account="):
            return a.split("=", 1)[1]
        if a.startswith("-A="):
            return a.split("=", 1)[1]
        if a in ("--account", "-A") and i + 1 < len(args):
            return args[i + 1]
        if a.startswith("-A") and len(a) > 2 and not a.startswith("--"):
            return a[2:]
    return None


def declared_partition(u):
    """The partition a unit asks for, or None.

    Handles BOTH spellings: `--partition=cpu` and `--partition cpu`. Reading
    only the equals form reported a unit that plainly declares a partition as
    declaring none, which would make the validator's own honesty message a
    false statement."""
    args = [str(a) for a in (u.get("sbatch") or [])]
    for i, a in enumerate(args):
        if a.startswith("--partition="):
            return a.split("=", 1)[1]
        if a.startswith("-p="):
            return a.split("=", 1)[1]
        if a in ("--partition", "-p") and i + 1 < len(args):
            return args[i + 1]
        # `-pcpu`, the attached short form. Missing it made the advisory line
        # claim a unit declared no partition when it plainly did, which turns
        # an honesty message into a false one.
        if a.startswith("-p") and len(a) > 2 and not a.startswith("--"):
            return a[2:]
    return None


def _known_partitions():
    """Partition names this cluster actually has, or None if it cannot be told.

    None is not an empty set. A host without `sinfo`, or a scheduler that does
    not answer, means UNKNOWN, and a validator that refuses on unknown is a
    validator that blocks honest work on the first flaky day."""
    if not shutil.which("sinfo"):
        return None
    rc, out, _ = U.run(["sinfo", "-h", "-o", "%P"], timeout=30)
    if rc != 0 or not (out or "").strip():
        return None
    names = set()
    for line in out.splitlines():
        n = line.strip().rstrip("*")        # the default partition carries a *
        if n:
            names.add(n)
    return names or None


# A sentinel, because None already means something. `known=None` was doing
# double duty -- "could not be determined" AND "go and look it up" -- and the
# two coincide only on a machine WITHOUT sinfo. On a real cluster a caller
# passing None to mean "unknown" triggered a live lookup instead, so a test
# asserting "unknown refuses nothing" passed on a laptop and failed on
# andromeda. Found by running the suite where it will actually run.
_LOOK_IT_UP = object()


def partition_problems(units, known=_LOOK_IT_UP):
    """Which units name a partition this cluster does not have.

    A project runs on ONE server and its plan carries that server's sbatch
    flags, so a plan written for lambda and run on chimera names partitions
    that do not exist here. Caught at validate, it is one clear line; caught
    at submit, it is a half-dispatched DAG and an sbatch error per unit."""
    if known is _LOOK_IT_UP:
        known = _known_partitions()
    # None means UNKNOWN and must refuse nothing. An empty set means the query
    # came back empty, which is also not evidence that a partition is absent.
    if not known:
        return []
    bad = []
    for u in units:
        if not isinstance(u, dict):
            continue
        name = declared_partition(u)
        if name and name not in known:
            bad.append((u.get("id", "?"), name))
    return bad


# --- what the survey already knows ----------------------------------------
#
# `survey.py` records this cluster BEFORE the plan is written: whether the
# scheduler has a default memory, and who each partition will accept. Three
# parts of the system agreed those facts matter -- the survey computes them,
# the skill tells the planner to plan around them, the report prints them --
# and the one component that can refuse a plan never looked. A unit charging
# an account its partition denies validated clean and then spent hours in
# QOSGrpCpuLimit beside 202 idle CPUs.
#
# THE RULE FOR `unknown` IS THE WHOLE DESIGN. Every allowance is recorded as
# {"state": "set"|"unrestricted"|"unknown", "value": ...}, and unknown means
# the query did not answer -- scontrol was missing, sacctmgr failed, the
# survey predates the field. A refusal built on that is a guess wearing a
# number: it blocks honest work on the first flaky day, and a validator that
# cries wolf gets switched off. So unknown refuses NOTHING here. It is not
# reported as satisfied either: silence from these checks means "not
# checked", which is why `validate` names the survey it read, or says it read
# none.
SURVEY_FILE = os.path.join(".swarm", "survey.json")

# survey.py's vocabulary, imported as words rather than as a habit.
LIMIT_SET, LIMIT_OPEN, LIMIT_UNKNOWN = "set", "unrestricted", "unknown"

def read_survey(path):
    """One survey file as a mapping. Returns (survey, error)."""
    data, err = U.read_json(path)
    if err:
        return None, err
    if not isinstance(data, dict):
        return None, "it is not a JSON object"
    return data, None


def discover_survey(plan_path=None, cwd=None):
    """The survey recorded beside a plan. Returns (survey, note).

    `None` is the answer when there is nothing to read, and the note says
    which file was looked for or why the one found was not used. A validator
    with no survey knows nothing about this cluster's allowances, and that is
    `unknown`, which refuses nothing.

    Two candidates, in order: the plan's own directory, then the working
    directory, both at `.swarm/survey.json` -- the path hanig-project's very
    first command writes.

    A survey is an observation of ONE host. Applying another machine's
    allowances here would refuse a unit on evidence about a different
    cluster, so a hostname mismatch is unknown rather than authority. A
    survey named explicitly on the command line is used as given: that is the
    operator asserting it applies.
    """
    here = os.uname().nodename
    tried = []
    for base in (os.path.dirname(os.path.abspath(plan_path)) if plan_path
                 else None, cwd or os.getcwd()):
        if not base:
            continue
        cand = os.path.join(base, SURVEY_FILE)
        if cand in tried:
            continue
        tried.append(cand)
        if not os.path.isfile(cand):
            continue
        data, err = read_survey(cand)
        if err:
            return None, (f"the survey at {cand} could not be read ({err}), "
                          f"so nothing in it was applied")
        machine = data.get("machine") or {}
        host = str(machine.get("hostname") or "").strip()
        if host and host != here:
            return None, (f"the survey at {cand} was taken on {host}, not "
                          f"{here}. A survey is evidence about the host it "
                          f"ran on, so nothing in it was applied here; pass "
                          f"--survey to use it deliberately")
        return data, cand
    return None, (f"no survey was read (looked for {' and '.join(tried)}). "
                  f"Record one with hanig-project's "
                  f"`survey.py --repo . --out {SURVEY_FILE}`")


def _scheduler_facts(survey):
    sched = (survey or {}).get("scheduler")
    return sched if isinstance(sched, dict) else {}


def surveyed_partition(survey, name):
    """The surveyed record for ONE partition, or None if the survey is silent
    about it. Silence is unknown: `partitions_unavailable` and a partition
    the query never listed are both "nothing was established here"."""
    for part in (_scheduler_facts(survey).get("partitions") or []):
        if isinstance(part, dict) and part.get("partition") == name:
            return part
    return None


def limit_state(part, field):
    """(state, value) for one allowance field, in the survey's vocabulary.

    An ABSENT field is unknown, not unrestricted. schema_version 1 predates
    the allowance block entirely, so a survey that never asked the question
    must not read as one that asked and found no restriction."""
    lim = (part or {}).get(field)
    if not isinstance(lim, dict) or lim.get("state") not in (
            LIMIT_SET, LIMIT_OPEN, LIMIT_UNKNOWN):
        return LIMIT_UNKNOWN, None
    return lim["state"], lim.get("value")


def _account_names(value):
    """An allowance's value as a list of account names, or None.

    A bare string here would make `account in value` a SUBSTRING test, so
    `lab` would pass an allow-list of `goodarzilab` and a deny-list naming
    `goodarzilab` would refuse `lab`. Anything that is not a list is not an
    account list, and not-a-list is unknown."""
    if isinstance(value, (list, tuple)):
        return [str(a) for a in value]
    return None


# Slurm's three ways to state a job's memory. `--mem` is MUTUALLY EXCLUSIVE
# with `--mem-per-cpu` at submission, so insisting on `--mem` by name would
# refuse a unit that has already answered the question in the only other
# spelling it is allowed to use.
MEM_FLAGS = ("--mem", "--mem-per-cpu", "--mem-per-gpu")


def declared_memory(u):
    """The memory request a unit makes, or None. Both spellings, as with
    partition and account: `--mem=64G` and `--mem 64G`.

    A flag with no value (`--mem=` or a trailing `--mem`) is not a request --
    sbatch rejects it -- but it does not mask a later, well-formed one."""
    args = [str(a) for a in (u.get("sbatch") or [])]
    for i, a in enumerate(args):
        name, sep, value = a.partition("=")
        if name not in MEM_FLAGS:
            continue
        if sep:
            if value.strip():
                return a
        elif i + 1 < len(args) and not args[i + 1].startswith("-"):
            return f"{a} {args[i + 1]}"
    return None


def memory_flag_problems(units, survey):
    """Which slurm units state no memory on a cluster that has no default.

    `mem_flag_required` is the survey's reading of `DefMemPerNode`. Absent,
    it is UNKNOWN -- `scontrol show config` did not answer, or the survey
    predates the field -- and unknown refuses nothing and satisfies nothing.
    """
    sched = _scheduler_facts(survey)
    if not sched.get("present") or "mem_flag_required" not in sched:
        return []
    if not sched.get("mem_flag_required"):
        return []                       # answered: this cluster has a default
    return [u.get("id", "?") for u in units
            if isinstance(u, dict) and u.get("kind") == "slurm"
            and not declared_memory(u)]


def account_problems(units, survey):
    """Which units charge an account their declared partition will not take.

    BOTH HALVES of the rule, because Slurm prints `DenyAccounts` INSTEAD of
    `AllowAccounts`: a partition that denies this account reads as wide open
    to anyone who consults only the allowance. Checked only where the survey
    ANSWERED -- `unknown` on one field still lets the other decide, and
    `unknown` on both decides nothing.

    Returns [(unit id, account, partition, what the survey says)].
    """
    bad = []
    for u in units:
        if not isinstance(u, dict) or u.get("kind") != "slurm":
            continue
        account, name = declared_account(u), declared_partition(u)
        if not account or not name:
            # An undeclared account is the association default and an
            # undeclared partition is the cluster default. Neither is in the
            # plan, so neither can be checked against the survey.
            continue
        part = surveyed_partition(survey, name)
        if part is None:
            continue
        state, value = limit_state(part, "deny_accounts")
        denied = _account_names(value)
        if state == LIMIT_SET and denied is not None and account in denied:
            bad.append((u.get("id", "?"), account, name,
                        f"the survey records deny_accounts="
                        f"{', '.join(denied)} on that partition, so the job "
                        f"is refused there however long it queues"))
            continue
        state, value = limit_state(part, "allow_accounts")
        allowed = _account_names(value)
        if (state == LIMIT_SET and allowed is not None
                and account not in allowed):
            bad.append((u.get("id", "?"), account, name,
                        f"that partition's allow_accounts is set to "
                        f"{', '.join(allowed)}, which does not include it"))
    return bad


def resolve_promote_to(raw, root=None):
    """Return (path, error) for a declared promotion destination.

    Three ways this went wrong, all found by trying them, all on the ONE path
    where this tool writes where other people read:

      - a RELATIVE path resolves against the coordinator's cwd, and cron runs
        from a different directory than an interactive shell, so the same plan
        published to two different places depending on how it was invoked
      - "~/canonical" is not expanded by the filesystem, so it created a
        directory literally named "~" and quietly put the results somewhere
        nobody would ever look
      - a destination INSIDE the run root published back into the exclusive
        write area, muddling the isolation everything else rests on
    """
    if not isinstance(raw, str) or not raw.strip():
        return None, "it is empty"
    text = raw.strip()
    if text.startswith("~"):
        text = os.path.expanduser(text)
        if text.startswith("~"):
            return None, (f"{raw!r} starts with ~ but no home directory could "
                          f"be resolved. Write the absolute path.")
    if not os.path.isabs(text):
        return None, (f"{raw!r} is a relative path. It would resolve against "
                      f"whatever directory the coordinator happens to run in, "
                      f"which differs between cron and a shell, so the same "
                      f"plan would publish to two different places. Write an "
                      f"absolute path.")
    dest = Path(text).resolve()
    if root:
        try:
            dest.relative_to(Path(root).resolve())
            return None, (f"{raw!r} is inside the run root {root}. Promotion "
                          f"publishes OUT of the exclusive write area; a "
                          f"destination inside it defeats the isolation the "
                          f"predicate depends on. Choose a path outside it.")
        except ValueError:
            pass
    return dest, None


RESOLUTIONS = ("direct", "path", "conda", "container", "module", "uv",
               "wrapper")

ENV_KINDS = ("model_line", "orchestration")
ENV_KEYS = frozenset({
    "path", "python", "lock_file", "build", "model_line", "kind",
    "notes", "description",
})
_ENV_PYTHON_RE = re.compile(r"^\d+\.\d+(?:\.\d+)?$")
_BARE_PYTHON_RE = re.compile(r"^python(?:\d+(?:\.\d+)*)?$", re.IGNORECASE)
_PYTHON_EXE_RE = re.compile(
    r"^(?:platform-python(?:\d+(?:\.\d+)*)?|pythonw?(?:\d+(?:\.\d+)*)?(?:[tdmu]+|-dbg|-debug)?|"
    r"pypy(?:\d+(?:\.\d+)*)?|pip(?:x|\d+(?:\.\d+)*)?|ipython(?:\d+)?|ptpython(?:\d+)?|bpython(?:\d+)?|"
    r"py\.?test(?:-?\d+(?:\.\d+)*)?|nosetests(?:\d+)?|jupyter(?:-[a-z0-9._-]+)?|"
    r"torchrun|accelerate|deepspeed|snakemake|poetry|pdm|hatch|flask|uvicorn|gunicorn|celery|ray|wandb|mlflow|"
    r"dask(?:-[a-z0-9._-]+)?|conda|mamba|micromamba)$",
    re.IGNORECASE,
)
_BAD_SHELL_CHARS = ("'", '"', "`", "$", ";", "|", "&", "<", ">", "\\", "*", "?", "[", "]", "{", "}", "(", ")", "\n", "\r", "\0")
_CONDA_ROOT_RE = re.compile(r"^(?:\.conda|micromamba|(?:miniconda|miniforge|anaconda|mambaforge)(?:\d+)?)$", re.I)
CONDA_ROOT_NAMES = frozenset({"conda", "miniconda", "miniconda3", "miniforge", "miniforge3", "mambaforge", "anaconda", "anaconda3", "micromamba"})
SHARED_ENV_NAMES = frozenset({"base", "workbook", "default", "shared", "personal", "common", "global", "root"})
_pt = lambda *xs: tuple(tuple(x.lower().split("/")) for x in xs)
_SYSTEM_BIN_PREFIXES = _pt("usr/bin", "usr/local/bin", "usr/sbin", "usr/libexec", "bin", "sbin", "opt/homebrew/bin", "home/linuxbrew/.linuxbrew/bin",
                           "Library/Developer/CommandLineTools/usr/bin", "Applications/Xcode.app/Contents/Developer/usr/bin")
_SHARED_ENV_ROOT_EXACT = frozenset(_pt("usr", "usr/local", "usr/bin", "usr/local/bin", "usr/sbin", "usr/lib", "usr/local/lib", "usr/share",
                                       "usr/local/share", "bin", "sbin", "opt/homebrew", "opt/homebrew/bin", "home/linuxbrew/.linuxbrew", "home/linuxbrew/.linuxbrew/bin"))
_SHARED_ENV_ROOT_PREFIXES = _pt("usr/bin", "usr/local/bin", "usr/sbin", "bin", "sbin", "opt/homebrew/bin", "home/linuxbrew/.linuxbrew/bin",
                                "System/Library", "Library/Developer/CommandLineTools", "Applications/Xcode.app")
_PYTHON_SHARED_PREFIXES = _pt("Library/Frameworks/Python.framework", "System/Library/Frameworks/Python.framework",
                              "Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework",
                              "Library/Developer/CommandLineTools/Library/Frameworks/Python.framework",
                              "Applications/Xcode.app/Contents/Developer/Library/Frameworks/Python3.framework",
                              "Applications/Xcode.app/Contents/Developer/Library/Frameworks/Python.framework")
_HOME_VENV_LAYOUTS = ((("home",), 2), (("Users",), 2), (("mnt", "weka", "home"), 4), (("root",), 1),
                      (("System", "Volumes", "Data", "home"), 5), (("System", "Volumes", "Data", "Users"), 5))
_HB_PY_PREFIXES = _pt("opt/homebrew/opt", "opt/homebrew/cellar", "usr/local/opt", "usr/local/cellar", "home/linuxbrew/.linuxbrew/opt", "home/linuxbrew/.linuxbrew/cellar")
_SHARED_LMOD_CATS = frozenset({"lang", "languages", "compilers", "compiler", "tools", "system", "shared", "default", "core", "devel", "common", "global", "mpi", "apps", "software"})
_FLAG_SPACE_RE = re.compile(r"\s--?[A-Za-z0-9]")

def _clean_abs_parts(path):
    if not isinstance(path, str) or not path.strip(): return None, "must be non-empty absolute"
    if path != path.strip() or any(c in path for c in ("\0", "\n", "\r")): return None, "whitespace/control chars"
    if not path.startswith("/"): return None, "not absolute"
    ps = [p for p in path.split("/") if p]
    return (None, "invalid path") if (not ps or any(p in (".", "..") for p in ps)) else (tuple(ps), None)

def shared_env_reason(path, is_env_root=False, declared_prefix=None):
    """Return why a Python path/root is a shared personal/system env, or None (lexical only)."""
    if not isinstance(path, str) or not path.strip(): return None
    t = path.strip()
    try: t.encode("utf-8")
    except UnicodeEncodeError: return f"{t!r} invalid UTF-8 surrogate"
    if any(c in t for c in _BAD_SHELL_CHARS): return f"{t!r} shell quotes/metachars"
    toks = t.split()
    if not is_env_root:
        if toks and toks[0] in ("env", "/usr/bin/env", "/bin/env", "/usr/local/bin/env", "/opt/homebrew/bin/env"): return f"{t!r} uses 'env' wrapper"
        if not t.startswith("/"): return f"{t!r} has flags/arguments" if len(toks) > 1 else (f"{t!r} is a flag" if t.startswith("-") else f"{t!r} must be absolute")
        if t.endswith("/"): return f"{t!r} ends with '/'"
    elif not t.startswith("/"): return f"{t!r} must be absolute"
    ps, err = _clean_abs_parts(t)
    if err: return f"{t!r} not clean absolute ({err})"
    if any(p != p.strip() for p in ps): return f"{t!r} has malformed spacing"
    if not is_env_root:
        lf = ps[-1].split()
        if ps[-1] == "env" or (lf and lf[0] == "env"): return f"{t!r} uses 'env' wrapper"
        dp, derr = _clean_abs_parts(declared_prefix) if declared_prefix else (None, True)
        chk_ps = ps[len(dp):] if (not derr and dp and len(ps) >= len(dp) and ps[:len(dp)] == dp) else ps
        if any(_FLAG_SPACE_RE.search(p) or (len(p.split()) > 1 and (_PYTHON_EXE_RE.fullmatch(p.split()[0]) or (p.split()[0] == "env" and (idx == len(chk_ps) - 1 or (idx > 0 and chk_ps[idx - 1].lower() in ("bin", "scripts")))))) for idx, p in enumerate(chk_ps)):
            return f"{t!r} includes flags/arguments"
    lo, py = tuple(p.lower() for p in ps), bool(_PYTHON_EXE_RE.fullmatch(ps[-1]))
    for pr in _PYTHON_SHARED_PREFIXES:
        if lo[:len(pr)] == pr: return f"{t!r} in shared framework"
    for hb in _HB_PY_PREFIXES:
        if lo[:len(hb)] == hb and len(lo) > len(hb) and (lo[len(hb)] == "python" or lo[len(hb)].startswith(("python@", "python3"))): return f"{t!r} in shared Homebrew Python"
    if is_env_root:
        if lo in _SHARED_ENV_ROOT_EXACT or any(lo[:len(rp)] == rp for rp in _SHARED_ENV_ROOT_PREFIXES): return f"{t!r} in shared system dir"
    elif py and any(lo[:len(pr)] == pr for pr in _SYSTEM_BIN_PREFIXES): return f"{t!r} in shared system Python"
    for i, s in enumerate(lo):
        if s in (".pyenv", "pyenv") and i + 1 < len(lo) and lo[i + 1] in ("shims", "versions"): return f"{t!r} in shared pyenv"
    for h, d in _HOME_VENV_LAYOUTS:
        if ps[:len(h)] == h and len(ps) > d and ps[d] in (".venv", "venv"): return f"{t!r} in shared home venv ({ps[d]!r})"
    for i in range(len(lo) if is_env_root else len(lo) - 1):
        if i > 0 and lo[i - 1] == "envs": continue
        s = lo[i]
        std_c = ((i == 1 and lo[0] in ("opt", "usr")) or (i == 2 and lo[:2] == ("usr", "local")) or (i > 0 and lo[i - 1] == "caskroom")
                 or any(lo[:len(h)] == tuple(x.lower() for x in h) and i == d for h, d in _HOME_VENV_LAYOUTS))
        if (_CONDA_ROOT_RE.fullmatch(s) or s == "conda") and (std_c or (i + 1 < len(lo) and lo[i + 1] in ("base", "envs", "condabin"))):
            r = lo[i + 1:]
            bo = 1 if (r and (r[0] == "base" or (i > 0 and lo[i - 1] == "caskroom" and r[0] not in ("bin", "scripts", "condabin", "envs")))) else 0
            r = r[bo:]
            if not r or r[0] in ("bin", "scripts", "condabin"): return f"{t!r} in conda base ({ps[i]!r})"
            if r[0] == "envs" and len(r) >= 2:
                if r[1] in SHARED_ENV_NAMES: return f"{t!r} in shared conda env {ps[i + 2 + bo]!r}"
                if len(r) == 2 or r[2] in ("bin", "scripts"): break
    for i, s in enumerate(lo):
        if s in ("envs", "virtualenvs", ".virtualenvs") and i + 1 < len(lo) and lo[i + 1] in SHARED_ENV_NAMES: return f"{t!r} in shared personal env {ps[i + 1]!r}"
    return None


def _env_path_contains(env_path, target_path, strict=False):
    ep, eerr = _clean_abs_parts(env_path)
    tp, terr = _clean_abs_parts(target_path)
    if eerr or terr: return False
    return (len(tp) > len(ep) if strict else len(tp) >= len(ep)) and tp[:len(ep)] == ep


def _interpreter_belongs_to_env(env_path, entrypoint):
    if isinstance(entrypoint, str) and entrypoint.endswith("/"): return False
    ep, eerr = _clean_abs_parts(env_path)
    tp, terr = _clean_abs_parts(entrypoint)
    return (not eerr and not terr and len(tp) == len(ep) + 2 and tp[:len(ep)] == ep and tp[len(ep)].lower() in ("bin", "scripts"))


def _inferred_env_root(entrypoint, resolution=None):
    if resolution in ("conda", "module") and isinstance(entrypoint, str):
        t = entrypoint.strip()
        if t and not t.startswith("/"): return f"{resolution}:{t}"
    ps, err = _clean_abs_parts(entrypoint)
    if err or not ps or (resolution == "uv" and not _PYTHON_EXE_RE.fullmatch(ps[-1])): return None
    if len(ps) >= 2 and ps[-2].lower() in ("bin", "scripts"): return "/" + "/".join(ps[:-2]) if len(ps) > 2 else "/"
    return "/" + "/".join(ps) if resolution == "conda" else ("/" + "/".join(ps[:-1]) if len(ps) > 1 else "/")

def resolve_environment(plan, unit):
    """Return (env_dict, env_id) declared or matched for a unit, or (None, env_id_or_None)."""
    envs, u_env = (plan.get("environments") if isinstance(plan.get("environments"), dict) else {}), unit.get("environment")
    rt, _ = resolve_runtime(plan, unit)
    env_ref = u_env if u_env is not None else (rt.get("environment") if isinstance(rt, dict) else None)
    if env_ref == "none": return "none", "none"
    if isinstance(env_ref, str): return envs.get(env_ref), env_ref
    if envs and isinstance(rt, dict):
        raw_ep = rt.get("entrypoint"); entry, res = (raw_ep.strip() if isinstance(raw_ep, str) else ""), rt.get("resolution")
        if entry and res != "container":
            if res == "conda" and not entry.startswith("/"):
                m = [k for k, sp in sorted(envs.items()) if isinstance(sp, dict) and (_clean_abs_parts(sp.get("path"))[0] or ("",))[-1] == entry]
                if len(m) == 1: return envs[m[0]], m[0]
            else:
                ps, err = _clean_abs_parts(entry)
                if not err and ps:
                    norm = "/" + "/".join(ps)
                    for k, sp in sorted(envs.items()):
                        if not isinstance(sp, dict): continue
                        ep, eerr = _clean_abs_parts(sp.get("path"))
                        if eerr: continue
                        if (res == "conda" and norm == "/" + "/".join(ep)) or ((res == "uv" or _PYTHON_EXE_RE.fullmatch(ps[-1])) and _interpreter_belongs_to_env(sp.get("path"), entry)): return sp, k
    return None, None

# An isolation profile is deliberately narrower than a general container
# launcher.  These two backends expose the same bind/contain interface on the
# clusters this skill targets.  Docker being installed on a submit host is not
# enough: its daemon, user mapping, environment forwarding, and mount host are
# separate facts, so silently translating this contract to `docker run` would
# claim an enforcement path the plan did not declare.
ISOLATION_BACKENDS = ("apptainer", "singularity")
ISOLATION_KEYS = frozenset(
    {"kind", "backend", "image", "writable", "read_only"})
ISOLATION_ROOT_TOKENS = ("$SWARM_UNIT_DIR", "${SWARM_UNIT_DIR}")
ISOLATION_SHELL_EXECUTABLES = frozenset({
    "sh", "ash", "bash", "dash", "zsh", "ksh", "mksh", "posh", "yash",
    "csh", "tcsh", "fish",
})
ISOLATION_MULTICALL_EXECUTABLES = frozenset({"busybox", "toybox"})
ISOLATION_LAUNCHER_EXECUTABLES = frozenset({
    "env", "nice", "nohup", "setsid", "stdbuf", "timeout",
})


def _unquoted_shell_syntax(command):
    """Return shell syntax outside single quotes (and expansions in double)."""
    quote = None
    escaped = False
    word_start = True
    syntax = ";&|<>(){}\n\r\x00$`*?["
    for char in command:
        if escaped:
            escaped = False
            word_start = False
            continue
        if quote == "'":
            if char == "'":
                quote = None
            continue
        if quote == '"':
            if char == '"':
                quote = None
            elif char == "\\":
                escaped = True
            elif char in "$`":
                return char
            continue
        if char in "'\"":
            quote = char
            word_start = False
        elif char == "\\":
            escaped = True
        elif char.isspace():
            word_start = True
        elif char in "#~" and word_start:
            return char
        elif char in syntax:
            return char
        else:
            word_start = False
    return None


def _isolation_command_argv(command):
    """Parse the deliberately small command language an isolated unit uses.

    A container runtime accepts an argv, while an ordinary unit command is a
    shell program.  Treating the latter as the former silently changes shell
    expansion, and putting another shell inside the image both assumes that
    image contains one and detaches background work from the pipeline
    wrapper's process tree.  Refuse shell programs and preserve simple argv
    commands exactly through shlex instead.
    """
    if not isinstance(command, str) or not command.strip():
        return None, "its command must be a non-empty string"
    # This is intentionally conservative.  Every spelling below has shell
    # semantics that direct exec cannot preserve without assuming an
    # interpreter exists inside the declared image.
    found = _unquoted_shell_syntax(command)
    if found is not None:
        return None, (
            f"its command contains shell syntax {found!r}; isolated commands "
            f"must be one executable plus literal arguments")
    try:
        argv = shlex.split(command, posix=True)
    except ValueError as e:
        return None, f"its command cannot be parsed as a direct argv: {e}"
    if not argv:
        return None, "its command has no executable"
    executable = os.path.basename(argv[0])
    if executable in ISOLATION_SHELL_EXECUTABLES:
        return None, (
            f"its executable {argv[0]!r} is a shell; isolated commands must "
            f"run the workload directly, not an image-side interpreter")
    if executable in ISOLATION_LAUNCHER_EXECUTABLES:
        return None, (
            f"its executable {argv[0]!r} is a process launcher that can hide "
            f"a shell; isolated commands must run the workload directly")
    if (executable in ISOLATION_MULTICALL_EXECUTABLES and len(argv) > 1
            and os.path.basename(argv[1]) in ISOLATION_SHELL_EXECUTABLES):
        return None, (
            f"its executable sequence {argv[:2]!r} selects a shell; "
            f"isolated commands must run the workload directly")
    if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", argv[0]):
        return None, (
            "its command starts with a shell environment assignment; put "
            "configuration in the workload or image instead")
    return argv, None


def isolation_problem(u):
    """Why a unit's optional host-write isolation cannot be enforced.

    This validates declarations only.  It does not choose a backend from PATH,
    infer one from the image spelling, or claim that a submit-host executable
    exists on a compute node.  Runtime availability remains a runtime/canary
    fact; the shape of the writable host surface is knowable here.
    """
    profile = u.get("isolation")
    if profile is None:
        return None
    uid = u.get("id", "?")
    if u.get("kind") not in _NEEDS_RUNTIME:
        return (f"unit {uid!r} is kind={u.get('kind')!r} and declares "
                f"'isolation'. Only slurm and pipeline workloads are launched "
                f"through a container backend; code units run in Paseo "
                f"worktrees and cannot enforce this profile.")
    if not isinstance(profile, dict):
        return (f"unit {uid!r} has isolation={profile!r}, a "
                f"{type(profile).__name__}; it must be an object")
    unknown = sorted(set(profile) - ISOLATION_KEYS)
    if unknown:
        return (f"unit {uid!r} isolation has unrecognised key(s) "
                f"{', '.join(unknown)}; it reads only "
                f"{', '.join(sorted(ISOLATION_KEYS))}. A misspelled boundary "
                f"must be refused rather than silently dropped.")
    if profile.get("kind") != "container":
        return (f"unit {uid!r} isolation.kind={profile.get('kind')!r}; the "
                f"only enforced kind is 'container'.")
    backend = profile.get("backend")
    if backend not in ISOLATION_BACKENDS:
        return (f"unit {uid!r} isolation.backend={backend!r}; use one of "
                f"{', '.join(ISOLATION_BACKENDS)}. The coordinator never "
                f"selects a backend from PATH or translates a profile to a "
                f"different runtime.")
    image = profile.get("image")
    if not isinstance(image, str) or not image.strip():
        return f"unit {uid!r} isolation declares no container image."
    is_uri = re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", image) is not None
    if (image.lstrip().startswith("-") or "\n" in image or "\r" in image
            or _looks_unresolved(image)
            or not (os.path.isabs(image) or is_uri)):
        return (f"unit {uid!r} isolation.image={image!r} is not a concrete "
                f"absolute path or container URI, or is parsed as a runtime "
                f"option.")

    for output in (u.get("outputs") or []):
        if isinstance(output, str) and os.path.normpath(output) == (
                SE.ISOLATION_MARKER_NAME):
            return (f"unit {uid!r} output {output!r} is reserved for the "
                    f"coordinator's isolation application evidence. Choose "
                    f"a different declared output path.")

    writable = profile.get("writable")
    if not isinstance(writable, list):
        return (f"unit {uid!r} isolation.writable={writable!r}; it must be a "
                f"list containing exactly '$SWARM_UNIT_DIR'.")
    if len(writable) != 1 or writable[0] not in ISOLATION_ROOT_TOKENS:
        return (f"unit {uid!r} isolation.writable must resolve to the attempt "
                f"root and NOTHING ELSE. Declare exactly "
                f'["$SWARM_UNIT_DIR"]; got {writable!r}.')

    read_only = profile.get("read_only")
    if not isinstance(read_only, list):
        return (f"unit {uid!r} isolation.read_only={read_only!r}; it must be "
                f"a list naming every declared input.")
    inputs = u.get("inputs") or []
    normalized = []
    for path in read_only:
        if not isinstance(path, str) or not path.strip():
            return (f"unit {uid!r} isolation.read_only contains {path!r}; "
                    f"every bind source must be a non-empty absolute path.")
        if (not os.path.isabs(path) or _looks_unresolved(path)
                or any(ch in path for ch in "*?[\n\r,:")):
            return (f"unit {uid!r} isolation.read_only path {path!r} cannot "
                    f"be enforced as one unambiguous read-only bind. Use a "
                    f"concrete absolute path without glob, template, comma, "
                    f"or colon syntax.")
        norm = os.path.normpath(path)
        if norm == os.path.sep:
            return (f"unit {uid!r} isolation.read_only may not bind the host "
                    f"filesystem root into the container.")
        normalized.append(norm)
    if len(set(normalized)) != len(normalized):
        return f"unit {uid!r} isolation.read_only contains duplicate binds."

    declared = []
    for path in inputs:
        if not isinstance(path, str):
            return (f"unit {uid!r} input {path!r} cannot be matched to an "
                    f"isolation.read_only bind.")
        declared.append(os.path.normpath(path))
    if sorted(normalized) != sorted(declared):
        return (f"unit {uid!r} isolation.read_only must name exactly its "
                f"declared inputs. Declared inputs are {inputs!r}; read-only "
                f"binds are {read_only!r}.")
    _argv, command_error = _isolation_command_argv(u.get("command"))
    if command_error:
        return (f"unit {uid!r} isolation cannot preserve {command_error}. "
                f"Refusing the profile avoids relying on /bin/sh or another "
                f"undeclared interpreter inside the image.")
    return None

# Kinds whose work runs somewhere else, so the runtime there is not knowable
# from here and must be declared.
_NEEDS_RUNTIME = ("slurm", "pipeline")


def _ancestors(uid, units_by_id, seen=None):
    """Every unit that must close before `uid` starts."""
    seen = seen if seen is not None else set()
    for dep in (units_by_id.get(uid, {}).get("needs") or []):
        if dep in seen:
            continue
        seen.add(dep)
        _ancestors(dep, units_by_id, seen)
    return seen


def resolve_runtime(plan, unit):
    """A unit's runtime profile, whether inline or referenced by id."""
    rt = unit.get("runtime")
    if rt == "none":
        return "none", "none"
    if isinstance(rt, str):
        return (plan.get("runtimes") or {}).get(rt), rt
    return rt, None


# ${VAR} $VAR {{VAR}} {VAR} %(VAR)s %VAR% @VAR@ <VAR>, anywhere in the string.
_PLACEHOLDER_RE = re.compile(
    r"\$\{[^}]*\}"
    r"|\$[A-Za-z_][A-Za-z0-9_]*"
    r"|\{\{[^}]*\}\}"
    r"|\{[A-Za-z_][A-Za-z0-9_]*\}"
    r"|%\([^)]*\)"
    r"|%[A-Za-z_][A-Za-z0-9_]*%"
    r"|@[A-Za-z_][A-Za-z0-9_]*@"
    r"|<[^>]*>")

_PLACEHOLDER_EXACT = ("...", "PATH", "TBD", "TODO", "FIXME", "CHANGEME",
                      "XXX", "N/A", "NA", "-")
_PLACEHOLDER_PREFIX = ("<", "TODO", "FIXME", "CHANGEME", "XXX")


def _looks_unresolved(text):
    """Is this input still a hole somebody meant to fill in?

    The first version checked four prefixes and three exact strings, so
    "${CORPUS}" sailed straight through: not one of the exact values, does not
    begin with "<", and looks like an ordinary relative path to everything
    else. A plan built on it validates, has tickets filed against it, and then
    waits for a value nobody was asked for, which is the exact failure this
    check exists to prevent.

    Shell and template syntax are how these are usually spelled, so they are
    named here. This cannot be exhaustive, and it is not the real defence: the
    interview is. Reaching this refusal means the interview already failed.
    """
    t = text.strip()
    if t in _PLACEHOLDER_EXACT or t.startswith(_PLACEHOLDER_PREFIX):
        return True
    if t.endswith(">"):
        return True
    # EMBEDDED counts. Checking only the string's edges accepted
    # "data/$CORPUS/shard.fastq" and "samples/{sample}.fastq", which are just
    # as unresolved as the bare form and are the way people actually write
    # them. A dispatched command would open that literal path.
    return bool(_PLACEHOLDER_RE.search(t))


def _runtime_identity(rt):
    """What makes two runtimes the same THING to execute in.

    Not the whole profile: a canary's own `verified_by` necessarily differs
    from the unit it vouches for, because it cannot be verified by itself.
    What must match is what actually gets run.
    """
    if rt == "none":
        return ("none",)
    if not isinstance(rt, dict):
        return (None,)
    return (rt.get("id"), rt.get("resolution"), rt.get("entrypoint"))


def _validate_environments(plan, units):
    """Validate declared dedicated project environments, unit bindings, and model-line isolation (lexical only)."""
    has_envs, raw_envs = "environments" in plan, plan.get("environments")
    envs, env_paths, orch_ids = {}, {}, []
    if has_envs:
        if not isinstance(raw_envs, dict) or not raw_envs: raise PlanError("'environments' must be a non-empty object")
        for eid, sp in raw_envs.items():
            if not isinstance(eid, str) or not eid.strip() or eid == "none": raise PlanError(f"invalid 'environments' key {eid!r}")
            if not isinstance(sp, dict): raise PlanError(f"environments[{eid!r}] must be an object")
            unk = sorted(set(sp) - ENV_KEYS)
            if unk: raise PlanError(f"environments[{eid!r}] unknown key(s): {', '.join(unk)}")
            k = sp["kind"] if ("kind" in sp and sp["kind"] is not None) else ("orchestration" if eid == "orchestration" else "model_line")
            if k not in ENV_KINDS or (eid == "orchestration" and k != "orchestration"): raise PlanError(f"environments[{eid!r}].kind={k!r} invalid")
            if k == "orchestration": orch_ids.append(eid)
            rp = sp.get("path"); ps, pe = _clean_abs_parts(rp)
            if pe or _looks_unresolved(rp): raise PlanError(f"environments[{eid!r}].path={rp!r} {pe or 'unresolved'}")
            sh = shared_env_reason(rp, is_env_root=True)
            if sh: raise PlanError(f"environments[{eid!r}].path refused: {sh}")
            pv, lf, bd, ml = sp.get("python"), sp.get("lock_file"), sp.get("build"), sp.get("model_line")
            if not isinstance(pv, str) or not _ENV_PYTHON_RE.fullmatch(pv.strip()): raise PlanError(f"environments[{eid!r}].python={pv!r} invalid")
            if not isinstance(lf, str) or not lf.strip() or "\0" in lf or _looks_unresolved(lf) or (lf.strip() == "none" and k != "orchestration"): raise PlanError(f"environments[{eid!r}].lock_file={lf!r} invalid")
            if not isinstance(bd, str) or not bd.strip() or "\0" in bd or _looks_unresolved(bd): raise PlanError(f"environments[{eid!r}].build={bd!r} invalid")
            if (k == "orchestration" and ml is not None) or (ml is not None and (not isinstance(ml, str) or not ml.strip())): raise PlanError(f"environments[{eid!r}].model_line={ml!r} invalid")
            envs[eid] = dict(sp, _kind=k, _model_line=(ml.strip() if isinstance(ml, str) and ml.strip() else None))
            env_paths[eid] = "/" + "/".join(ps)
        if not orch_ids: raise PlanError("plan declares 'environments' with no orchestration environment")
        if len(orch_ids) > 1: raise PlanError(f"multiple orchestration environments: {', '.join(sorted(orch_ids))}")
        eids = sorted(env_paths)
        for i, a in enumerate(eids):
            for b in eids[i + 1:]:
                if _env_path_contains(env_paths[a], env_paths[b]) or _env_path_contains(env_paths[b], env_paths[a]): raise PlanError(f"environments {a!r} and {b!r} share/nest path")

    def _conda_name_matches_env(ep, cn):
        p, e = _clean_abs_parts(ep)
        return bool(not e and p and p[-1] == cn)

    def _matching_conda_envs(cn): return [eid for eid, ep in sorted(env_paths.items()) if _conda_name_matches_env(ep, cn)]

    def _is_lmod_module(e):
        return bool(e) and not e.startswith(("/", ".", "-")) and not any(c.isspace() or c in _BAD_SHELL_CHARS for c in e) and all(s and s not in (".", "..") and s.lower() not in ("bin", "scripts") for s in e.split("/"))

    def _entrypoint_is_python_exe(e):
        if not e: return False
        p, err = _clean_abs_parts(e)
        return bool(_PYTHON_EXE_RE.fullmatch(p[-1])) if (not err and p) else any(_PYTHON_EXE_RE.fullmatch(s) or _CONDA_ROOT_RE.fullmatch(s) for s in e.split("/"))

    def _is_shared_lmod_python(e):
        s = [x.lower() for x in e.split("/")]
        pc = lambda x: bool(_PYTHON_EXE_RE.fullmatch(x) or _CONDA_ROOT_RE.fullmatch(x))
        return pc(s[0]) or (len(s) >= 2 and s[0] in _SHARED_LMOD_CATS and pc(s[1]))

    def _is_abs_conda_prefix(e, r):
        if r != "conda" or not e.startswith("/"): return False
        p, err = _clean_abs_parts(e)
        return False if (err or not p) else (("/" + "/".join(p)) in env_paths.values() or not (len(p) >= 2 and p[-2].lower() in ("bin", "scripts")))

    def _is_python_targeting(e, r, er):
        if r == "container": return False
        if not e: return bool(er and er != "none")
        return r in ("conda", "uv") or _entrypoint_is_python_exe(e) or (r == "wrapper" and bool(er and er != "none"))

    def _ref(lb, m): raise PlanError(f"{lb} interpreter refused: {m}")

    def _check_rt(entry, res, er, lb, allow_u=False):
        if entry:
            try: entry.encode("utf-8")
            except UnicodeEncodeError: _ref(lb, "invalid UTF-8 surrogate")
            if res != "container" and entry != entry.strip(): _ref(lb, f"{entry!r} leading/trailing whitespace")
        nc = bool(entry) and res == "conda" and not entry.startswith(("/", "-")) and "/" not in entry and not any(c.isspace() or c in _BAD_SHELL_CHARS for c in entry)
        lm, ac, pt = (bool(entry) and res == "module" and _is_lmod_module(entry)), _is_abs_conda_prefix(entry, res), _is_python_targeting(entry, res, er)
        if nc and entry.lower() in SHARED_ENV_NAMES: raise PlanError(f"{lb} conda env {entry!r} is shared")
        if entry and res != "container" and not nc:
            if lm:
                if _is_shared_lmod_python(entry): _ref(lb, f"module {entry!r} is shared Python/conda")
                if er not in envs and not allow_u and has_envs and _entrypoint_is_python_exe(entry): _ref(lb, f"module {entry!r} unbound Python")
            else:
                decl_pref = next((ep for ep in sorted(env_paths.values(), key=len, reverse=True) if _env_path_contains(ep, entry)), None)
                sh = shared_env_reason(entry, is_env_root=ac, declared_prefix=decl_pref)
                if sh: _ref(lb, sh)
                if res == "conda" and not ac:
                    psc, _ = _clean_abs_parts(entry)
                    par_ps = psc[:-2]
                    if par_ps and par_ps[-1].lower() in SHARED_ENV_NAMES: _ref(lb, f"{entry!r} shared conda env {par_ps[-1]!r}")
                    pr_sh = shared_env_reason("/" + "/".join(par_ps) if par_ps else "/", is_env_root=True)
                    if pr_sh: _ref(lb, pr_sh)
                if ac:
                    psc, _ = _clean_abs_parts(entry)
                    if psc[-1].lower() in SHARED_ENV_NAMES: _ref(lb, f"{entry!r} shared conda env {psc[-1]!r}")
                    if ("/" + "/".join(psc)) not in env_paths.values() and (_FLAG_SPACE_RE.search(entry) or any(c.isspace() for c in psc[-1]) or any(p != p.strip() for p in psc)): _ref(lb, f"{entry!r} includes flags/arguments")
        if entry and res != "container" and any(_env_path_contains(env_paths[oid], entry) for oid in orch_ids): raise PlanError(f"{lb} {entry!r} targets orchestration env")
        if entry and res not in ("conda", "container"):
            eps, eerr = _clean_abs_parts(entry)
            if not eerr and eps and ("/" + "/".join(eps)) in env_paths.values(): raise PlanError(f"{lb} {entry!r} is an env directory")
        m = None
        if pt and entry:
            if er == "none":
                for eid, ep in env_paths.items():
                    if _interpreter_belongs_to_env(ep, entry): raise PlanError(f"{lb} environment='none' conflicts with {eid!r}")
                raise PlanError(f"{lb} environment='none' names Python {entry!r}")
            if er and er != "none":
                exp = env_paths[er]
                if nc:
                    if not _conda_name_matches_env(exp, entry): raise PlanError(f"{lb} conda {entry!r} mismatches {er!r}")
                    amb = _matching_conda_envs(entry)
                    if len(amb) > 1: raise PlanError(f"{lb} conda {entry!r} ambiguous: {', '.join(amb)}")
                elif ac:
                    ps, _ = _clean_abs_parts(entry)
                    if "/" + "/".join(ps) != exp: raise PlanError(f"{lb} conda prefix {entry!r} mismatches {er!r}")
                elif lm: pass
                elif res == "uv" and not _entrypoint_is_python_exe(entry) and not any(_env_path_contains(ep, entry) for ep in env_paths.values()): pass
                elif res == "wrapper":
                    for eid, ep in env_paths.items():
                        if eid != er and _env_path_contains(ep, entry): raise PlanError(f"{lb} wrapper {entry!r} in {eid!r}, not {er!r}")
                    if _entrypoint_is_python_exe(entry) and not _interpreter_belongs_to_env(exp, entry): raise PlanError(f"{lb} wrapper {entry!r} outside {er!r}")
                elif not _interpreter_belongs_to_env(exp, entry): raise PlanError(f"{lb} {entry!r} does not belong directly to {er!r} ({exp})")
                m = er
            elif has_envs:
                if nc:
                    amb = _matching_conda_envs(entry)
                    if len(amb) > 1: raise PlanError(f"{lb} conda {entry!r} ambiguous: {', '.join(amb)}")
                    m = amb[0] if amb else None
                elif ac:
                    ps, _ = _clean_abs_parts(entry)
                    m = next((eid for eid, ep in env_paths.items() if ep == "/" + "/".join(ps)), None)
                else: m = next((eid for eid, ep in env_paths.items() if _interpreter_belongs_to_env(ep, entry)), None)
                if m is None:
                    if allow_u and (lm or (res == "uv" and not _entrypoint_is_python_exe(entry))): return None, nc, pt
                    raise PlanError(f"{lb} {entry!r} in undeclared env")
                if envs[m]["_kind"] == "orchestration": raise PlanError(f"{lb} {entry!r} targets orchestration env {m!r}")
        else:
            if entry and res != "container" and not nc and not lm:
                own = next((eid for eid, ep in env_paths.items() if _env_path_contains(ep, entry)), None)
                if own is not None:
                    if er and own != er: raise PlanError(f"{lb} {entry!r} belongs to declared env {own!r}, not {er!r}")
                    if er is None: m = own
            if m is None and er and er != "none": m = er
        return m, nc, pt

    root_own, id_own, cname_own = {}, {}, {}
    for eid, sp in envs.items():
        if sp["_kind"] == "model_line" and sp["_model_line"]: root_own[env_paths[eid]] = id_own[eid] = (sp["_model_line"], f"environments[{eid!r}]")

    def _claim_ml(ml, eid, root, src):
        if not ml: return
        for tbl, key, lbl in ((id_own, eid if (eid and eid != "none") else None, "environment"), (root_own, root, "environment root")):
            if key:
                pv = tbl.get(key)
                if pv and pv[0] != ml: raise PlanError(f"model lines {pv[0]!r} ({pv[1]}) and {ml!r} ({src}) share {lbl} {key!r}")
                tbl.setdefault(key, (ml, src))

    def _claim_cn(cn, eid, src):
        if not cn or not eid or eid == "none": return
        pv = cname_own.get(cn)
        if pv and pv[0] != eid: raise PlanError(f"environments {pv[0]!r} and {eid!r} both bind conda env {cn!r}")
        cname_own.setdefault(cn, (eid, src))

    def _looks_env_root(e):
        ps, err = _clean_abs_parts(e)
        return bool(not err and ps and len(ps) >= 3 and ps[-2].lower() in ("bin", "scripts") and any(x.lower() in ("envs", "venv", ".venv", ".venvs", "venvs", "virtualenvs", ".virtualenvs") or x.lower().startswith(".venv") or x.lower().endswith(("-venv", "_venv", "-env", "_env")) for x in ps[:-2]))

    def _chk_env_ref(er, lb):
        if er is not None and er != "none":
            if not isinstance(er, str) or not er.strip(): raise PlanError(f"{lb} invalid environment={er!r}")
            if er not in envs: raise PlanError(f"{lb} environment {er!r} not declared")
            if envs[er]["_kind"] == "orchestration": raise PlanError(f"{lb} targets orchestration env {er!r}")

    def _claim_all(ml, mid, entry, res, nc, pt, er, lb):
        if nc and mid: _claim_cn(entry, mid, lb)
        if ml and ((mid in env_paths) or (pt and er != "none") or _looks_env_root(entry)):
            _claim_ml(ml, mid, (env_paths[mid] if mid in env_paths else _inferred_env_root(entry, res)), lb)
            if nc: _claim_ml(ml, None, "conda:" + entry, lb)
            if res == "module" and _entrypoint_is_python_exe(entry): _claim_ml(ml, None, "module:" + entry, lb)

    for rid, rt in (plan.get("runtimes") if isinstance(plan.get("runtimes"), dict) else {}).items():
        if not isinstance(rt, dict): continue
        lb, re_, rm, raw_ep = f"runtimes[{rid!r}]", rt.get("environment"), rt.get("model_line"), rt.get("entrypoint")
        _chk_env_ref(re_, lb)
        if rm is not None and (not isinstance(rm, str) or not rm.strip()): raise PlanError(f"{lb}.model_line invalid")
        if not isinstance(raw_ep, str) or not raw_ep.strip(): raise PlanError(f"{lb}.entrypoint must be a non-empty string")
        entry, res = raw_ep, rt.get("resolution")
        mr, nc, pt = _check_rt(entry, res, re_, lb, True)
        _claim_all((rm.strip() if rm else None), mr, entry, res, nc, pt, re_, lb)

    unit_envs = {}
    for u in units:
        if not isinstance(u, dict): continue
        uid, kind, u_env = u.get("id", "?"), u.get("kind"), u.get("environment")
        lb = f"unit {uid!r}"
        if u_env is not None and (not isinstance(u_env, str) or not u_env.strip()): raise PlanError(f"{lb} invalid environment={u_env!r}")
        rt, rt_ref = resolve_runtime(plan, u)
        if kind == "code" and isinstance(u.get("runtime"), str) and u.get("runtime") != "none" and not isinstance(rt, dict): raise PlanError(f"{lb} undefined runtime {rt_ref!r}")
        rt_env = rt.get("environment") if isinstance(rt, dict) else None
        if rt_env is not None and (not isinstance(rt_env, str) or not rt_env.strip()): raise PlanError(f"{lb} runtime invalid environment={rt_env!r}")
        if u_env is not None and rt_env is not None and u_env != rt_env: raise PlanError(f"{lb} environment={u_env!r} != runtime environment={rt_env!r}")
        er = u_env if u_env is not None else rt_env
        _chk_env_ref(er, lb)
        raw_ep = rt.get("entrypoint") if isinstance(rt, dict) else None
        if isinstance(rt, dict) and (("entrypoint" in rt and not isinstance(raw_ep, str)) or (kind == "code" and (not isinstance(raw_ep, str) or not raw_ep.strip()))): raise PlanError(f"{lb} runtime.entrypoint must be a non-empty string")
        entry, res = (raw_ep if isinstance(raw_ep, str) else ""), (rt.get("resolution") if isinstance(rt, dict) else None)
        mid, nc, pt = _check_rt(entry, res, er, lb)
        unit_envs[uid] = mid or er or "none"
        if has_envs and kind == "code" and er is None: raise PlanError(f"{lb} (code) omits 'environment'")
        u_ml, rt_ml = u.get("model_line"), (rt.get("model_line") if isinstance(rt, dict) else None)
        for v in (u_ml, rt_ml):
            if v is not None and (not isinstance(v, str) or not v.strip()): raise PlanError(f"{lb} invalid model_line={v!r}")
        if isinstance(u_ml, str) and u_ml.strip() and isinstance(rt_ml, str) and rt_ml.strip() and u_ml.strip() != rt_ml.strip(): raise PlanError(f"{lb} model_line mismatch")
        eml = (u_ml.strip() if isinstance(u_ml, str) and u_ml.strip() else rt_ml.strip() if isinstance(rt_ml, str) and rt_ml.strip() else envs[mid]["_model_line"] if mid in envs else None)
        _claim_all(eml, mid, entry, res, nc, pt, er, lb)
    return unit_envs


def _validate_runtimes(plan, units):
    by_id = {u.get("id"): u for u in units if isinstance(u, dict)}
    catalogue = plan.get("runtimes") or {}
    if catalogue and not isinstance(catalogue, dict):
        raise PlanError("'runtimes' must be an object of id -> profile")
    unit_envs = _validate_environments(plan, units)

    for u in units:
        if not isinstance(u, dict) or u.get("kind") not in _NEEDS_RUNTIME:
            continue
        uid = u.get("id", "?")
        # "none" is a DECLARATION, not an omission: this unit runs only tools
        # the base image guarantees (coreutils, tar, the scheduler itself).
        # Without it a unit running `sha256sum` would have to invent a runtime
        # profile, and a required field that is noise for a third of its uses
        # becomes a rubber stamp: everyone pastes "unverified: n/a" and the
        # declaration stops meaning anything. "none" is a specific claim, and
        # it stays visible at the approval gate and in the report.
        if u.get("runtime") == "none":
            continue

        rt, ref = resolve_runtime(plan, u)

        # A dangling reference must be reported AS a dangling reference. It
        # resolves to None, so an ordering slip here reports "declares no
        # runtime" about a unit that plainly declares one, which is the same
        # false-honesty bug the partition message had.
        if isinstance(u.get("runtime"), str) and not isinstance(rt, dict):
            raise PlanError(
                f"unit {uid!r} references runtime {ref!r}, which is not "
                f"defined in the plan's 'runtimes'.")

        if rt is None:
            raise PlanError(
                f"unit {uid!r} is kind={u.get('kind')!r} and declares no "
                f"'runtime'. Which interpreter or image runs this, and what "
                f"establishes that it works where the job lands? That is not "
                f"discoverable from the command: shell hides it behind "
                f"wrappers, modules, containers and variables. Declare it:\n"
                f'    "runtime": {{"id": "...", "resolution": "direct", '
                f'"entrypoint": "/abs/path", "verified_by": "canary:<unit>"}}'
                f"\n  or reference a profile from the plan's 'runtimes', "
                f'or declare "runtime": "none" if this runs only tools the '
                f"base image guarantees.")
        if not isinstance(rt, dict):
            raise PlanError(f"unit {uid!r} has a 'runtime' that is not an "
                            f"object")

        res = rt.get("resolution")
        if res not in RESOLUTIONS:
            raise PlanError(
                f"unit {uid!r} runtime.resolution={res!r}; use one of: "
                f"{', '.join(RESOLUTIONS)}. This says HOW the runtime is "
                f"reached, which is the part a reader cannot infer.")
        if not str(rt.get("entrypoint") or "").strip():
            raise PlanError(
                f"unit {uid!r} runtime declares no 'entrypoint'. Name the "
                f"interpreter, image or wrapper this actually executes.")

        vb = str(rt.get("verified_by") or "").strip()
        if not vb:
            raise PlanError(
                f"unit {uid!r} runtime declares no 'verified_by'. A declared "
                f"runtime that nothing checks is a hope. Use "
                f"'canary:<unit-id>', 'preflight', or "
                f"'unverified:<why that is acceptable here>'.")

        if vb.startswith("canary:"):
            canary = vb.split(":", 1)[1].strip()
            if canary not in by_id:
                raise PlanError(
                    f"unit {uid!r} says its runtime is verified by canary "
                    f"{canary!r}, which is not a unit in this plan.")
            if canary == uid:
                raise PlanError(
                    f"unit {uid!r} names ITSELF as its runtime canary. The "
                    f"workload must not be its own probe: by the time it "
                    f"fails, the fan-out has already been dispatched.")
            if canary not in _ancestors(uid, by_id):
                raise PlanError(
                    f"unit {uid!r} is verified by canary {canary!r}, but "
                    f"{canary!r} is not an ancestor of it, so nothing stops "
                    f"{uid!r} starting before the probe closes. Add it to "
                    f"'needs' (directly or upstream).")

            # ORDERING IS NOT PROOF. Ancestry alone let a `runtime: "none"`
            # probe on the cpu partition stand as evidence for a python
            # runtime on gpu: it ran first, and it established nothing about
            # the thing it was vouching for. A canary has to exercise the
            # same runtime, in the same place.
            crt, _cref = resolve_runtime(plan, by_id[canary])
            if _runtime_identity(crt) != _runtime_identity(rt):
                raise PlanError(
                    f"unit {uid!r} is verified by canary {canary!r}, but "
                    f"{canary!r} declares a different runtime. A probe that "
                    f"does not run the runtime it vouches for proves nothing "
                    f"about it. Give the canary the same runtime.")
            cenv, uenv = unit_envs.get(canary, "none"), unit_envs.get(uid, "none")
            if cenv != uenv:
                raise PlanError(
                    f"unit {uid!r} declares environment {uenv!r} but its canary "
                    f"{canary!r} declares environment {cenv!r}; give the canary "
                    "the same environment.")
            # A canary whose command is `true` exercises nothing. We cannot
            # tell from arbitrary shell whether a command runs a runtime, and
            # parsing it is the move already rejected here. So compare
            # DECLARED to DECLARED: the profile states its probe, and the
            # canary must be running exactly that.
            probe = str((rt or {}).get("probe") or "").strip()
            if not probe:
                raise PlanError(
                    f"runtime for unit {uid!r} is verified by canary "
                    f"{canary!r} but declares no 'probe'. State the command "
                    f"that establishes this runtime works, so the canary can "
                    f"be checked against it rather than trusted.")
            ccmd = str(by_id[canary].get("command") or "").strip()
            if ccmd != probe:
                raise PlanError(
                    f"canary {canary!r} does not run the runtime's declared "
                    f"probe. Its command is {ccmd!r}; the probe is {probe!r}. "
                    f"A canary that closes without exercising the runtime "
                    f"proves the runtime works exactly as much as `true` "
                    f"does.")

            cacct = declared_account(by_id[canary])
            uacct = declared_account(u)
            if cacct != uacct:
                raise PlanError(
                    f"unit {uid!r} is charged to "
                    f"{uacct or 'the default account'} but its canary "
                    f"{canary!r} runs under "
                    f"{cacct or 'the default account'}. Access to a runtime "
                    f"and its files can differ by account, so a probe under "
                    f"another one does not establish this one works.")

            cpart = declared_partition(by_id[canary])
            upart = declared_partition(u)
            # ABSENCE IS A VALUE. "Declares no partition" means "the cluster
            # default", which is a specific partition, not a wildcard. Writing
            # `if cpart and upart and cpart != upart` let a canary with no
            # partition vouch for a unit on gpu: it ran on the default cpu
            # queue and established nothing. Compare them directly so None
            # only matches None.
            if cpart != upart:
                raise PlanError(
                    f"unit {uid!r} runs on partition "
                    f"{upart or 'the cluster default'} but its canary "
                    f"{canary!r} runs on {cpart or 'the cluster default'}. A "
                    f"runtime that resolves on one partition may not resolve "
                    f"on another, so the probe has to land where the work "
                    f"lands. Declare the same partition on both.")
        elif vb == "preflight":
            pass
        elif vb.startswith("unverified:"):
            why = vb.split(":", 1)[1].strip()
            if len(why) < 12:
                raise PlanError(
                    f"unit {uid!r} declares its runtime unverified but gives "
                    f"no real reason. An unverified runtime is allowed and "
                    f"is sometimes right; it has to be a decision somebody "
                    f"made on purpose and can be held to.")
        else:
            raise PlanError(
                f"unit {uid!r} runtime.verified_by={vb!r}; use "
                f"'canary:<unit-id>', 'preflight', or 'unverified:<why>'.")

    # NOTE what is deliberately absent: no stat of the entrypoint, and no
    # parsing of the command. Both would assert facts about a machine this
    # one cannot see.


CONVERGE_UNIT_KEYS = frozenset({"metrics", "criterion", "diverge", "budget",
                                "sparse_metric"})


def converge_problem(u):
    """Why a unit's declared convergence block cannot be evaluated, or None.

    Checked at PLAN time as well as at judge time. `converge.py` says it
    plainly -- "declaring the criterion BEFORE the run is the whole point" --
    and a criterion accepted here but rejected forty thousand steps later has
    cost a real job for a typo.
    """
    spec = u.get("converge")
    if spec is None:
        return None
    uid = u.get("id", "?")
    if u.get("kind") == "code":
        return (f"unit {uid!r} is kind=code and declares 'converge'. A code "
                f"unit has no metrics series and is closed by a merged pull "
                f"request, so the gate would never be reached. Drop the "
                f"block, or declare the work as kind=slurm or kind=pipeline.")
    if not isinstance(spec, dict):
        return (f"unit {uid!r} has converge={spec!r}, a "
                f"{type(spec).__name__}; it must be an object like "
                f'{{"metrics": "metrics.jsonl", "criterion": '
                f'{{"metric": "val_loss", "mode": "min", "threshold": 0.5}}}}.')
    unknown = sorted(set(spec) - CONVERGE_UNIT_KEYS)
    if unknown:
        return (f"unit {uid!r} converge has unrecognised key(s) "
                f"{', '.join(unknown)}; it reads only "
                f"{', '.join(sorted(CONVERGE_UNIT_KEYS))}. A typo here would "
                f"drop the gate silently, which is the one failure mode this "
                f"whole family of checks exists to prevent.")
    metrics = spec.get("metrics")
    if not isinstance(metrics, str) or not metrics.strip():
        return (f"unit {uid!r} converge declares metrics={metrics!r}; name "
                f"the JSONL file the run appends its evaluations to, RELATIVE "
                f"to the attempt write root.")
    if metrics not in (u.get("outputs") or []):
        return (f"unit {uid!r} judges convergence over {metrics!r}, which is "
                f"not one of its declared outputs. The gate is conclusive "
                f"only over a file inside the exclusive write root, and only "
                f"a declared output is checked to be there at all -- "
                f"otherwise a missing metrics file reads as 'cannot judge' "
                f"instead of 'the run produced nothing'. Add {metrics!r} to "
                f"'outputs'.")
    problem = CV.criterion_problem(spec.get("criterion"))
    if problem:
        return f"unit {uid!r} converge criterion: {problem}"
    if not isinstance(spec.get("sparse_metric", False), bool):
        return (f"unit {uid!r} has converge.sparse_metric="
                f"{spec['sparse_metric']!r}; it must be true or false.")
    rules = spec.get("diverge")
    if rules is not None and not isinstance(rules, list):
        return (f"unit {uid!r} has converge.diverge={rules!r}, a "
                f"{type(rules).__name__}, but it must be a LIST of rule "
                f"objects. A single object here is iterated over its KEYS, so "
                f"the bound is silently lost and nothing is checked.")
    for i, rule in enumerate(rules or []):
        if not isinstance(rule, dict):
            return (f"unit {uid!r} converge.diverge[{i}]={rule!r} is not an "
                    f"object; each rule looks like "
                    f'{{"metric": "train_loss", "above": 1e9}}.')
        if not isinstance(rule.get("metric"), str) or not rule["metric"].strip():
            return (f"unit {uid!r} converge.diverge[{i}] names no metric; "
                    f"give the metric's name exactly as the run writes it.")
        if not any(k in rule for k in ("above", "below")):
            return (f"unit {uid!r} converge.diverge[{i}] declares no bound; "
                    f"add 'above' or 'below', or remove the rule. A rule with "
                    f"no bound cannot show the run stayed inside it.")
        problem = CV.unread_key_problem(rule, CV.DIVERGE_KEYS,
                                        f"converge.diverge[{i}]")
        if problem:
            return f"unit {uid!r} {problem}"
    if spec.get("budget") is not None:
        budget, err = CV.finite_number(spec["budget"])
        if err or budget < 0:
            return (f"unit {uid!r} has converge.budget={spec['budget']!r}, "
                    f"which {err or 'must not be negative'}. The budget is "
                    f"the step count whose exhaustion is NOT convergence.")
    return None


def _units_with_canary(plan):
    """Effective dependencies, without editing the plan or launch intents.

    Use the ordinary needs readers for validation, dispatch and status. The
    raw plan is still the digest input; plans without canary keep their bytes.
    """
    units = plan.get("units") or []
    if "canary" not in plan:
        return units
    canary = plan["canary"]
    return [dict(u, needs=[*(u.get("needs") or []), canary])
            if isinstance(u, dict) and u.get("id") != canary
            and canary not in (u.get("needs") or []) else u for u in units]


# Fields that cannot change what a dispatch DOES. Everything else is
# digested. Listing the cosmetic fields rather than the meaningful ones is
# deliberate: four reviewers broke the previous inclusion list by naming
# fields it had simply forgotten (inputs, gpu_hours, charge_to, write_scopes,
# max_attempts, timeout_s, prompt, provider). An exclusion list fails in the
# safe direction, because a field added next year is covered until someone
# argues it is cosmetic.
COSMETIC_FIELDS = {"description", "comment", "notes", "title", "owner",
                   "tags", "_comment", "author_history"}


def plan_digest(plan):
    """Canonical digest of the plan's DISPATCHABLE content.

    Comments, ordering and formatting must not invalidate a live run. Anything
    that reaches `unit.py allocate` or the submitted script must."""
    def unit_payload(u):
        return {k: v for k, v in sorted(u.items())
                if k not in COSMETIC_FIELDS}
    units = sorted((unit_payload(u) for u in (plan.get("units") or [])),
                   key=lambda d: json.dumps(d, sort_keys=True))
    content = {"units": units,
               "budget": plan.get("budget"),
               "root": plan.get("root")}
    if "canary" in plan:
        content["canary"] = plan["canary"]
    if "environments" in plan:
        content["environments"] = plan["environments"]
    payload = json.dumps(content, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()
