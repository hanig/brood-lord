"""Daemonless execution substrate for direct-CLI coding workers.

One code attempt on backend ``claude``, ``codex`` or ``pi`` is a sequence of
TURNS of one headless CLI session in the coordinator-owned attempt worktree.
Every turn is run by a thin Python turn wrapper (``cli_io.py turn``) that
writes an atomic exit receipt when the CLI exits. There is no daemon and no
watcher: everything an observer needs is on disk under

    STATE_DIR/cli-runs/<attempt_id>/        (0700)
        run.json               coordinator record, atomic; never a secret
        turns.jsonl            append-only, one record per started turn
        turn-<n>.json          the turn's argv/cwd spec (no secret)
        turn-<n>.prompt.txt    0600, the turn's stdin
        turn-<n>.stdout.log / turn-<n>.stderr.log
        turn-<n>.exit.json     atomic receipt from the wrapper
        turn-<n>.key           0600, pi+openrouter only; the wrapper reads
                               and unlinks it before the CLI starts, and
                               refuses to start the CLI if the unlink fails
        .lock                  flock held by launch, resume and cancel

SECRETS. A pi turn's OpenRouter key reaches the CLI only through an inherited
pipe; its stdout and stderr pass through a redacting pump, so the key's value
never reaches a log or the exit receipt. Every CLI child, however the wrapper
was started, runs behind `env -u` for each adapter-withheld name.

PLACEMENT. On a Slurm login or agent host (sbatch, squeue and scancel on
PATH and not inside a compute-node batch job; ``BROOD_SWARM_CLI_SLURM``, or
legacy ``HANIG_SWARM_CLI_SLURM``, = auto|on|off) a turn is submitted with ``sbatch --export=NONE`` and the
wrapper runs under ``env -i`` on a compute node. A failed submission FAILS
CLOSED: a heavy CLI is never run on a login node as a fallback. An agent-host
job missing a Slurm tool refuses rather than running locally, and a resume
of a run recorded ``local`` refuses on a host whose placement is Slurm (its
node-local worktree may be unreachable from a compute node). Elsewhere the
wrapper is a detached process group (``start_new_session``) whose (pid, pgid,
start time) is recorded, so liveness survives coordinator restarts and is
never confused by PID reuse.

AUTHORITY. Nothing here judges. A zero exit is lifecycle (``stopped``), not
completion: ``unit.py check`` still requires declared outputs and a pushed
produced change. Any observation this module cannot establish is
``unknown`` or ``incomplete``, never ``stopped``.

Declared limits: the state directory, the CLI binary and its subscription
login must be visible from compute nodes in Slurm mode; descendants that
leave the wrapper's process group escape ``cancel``; a pending Slurm job
holds its 0600 key file until it starts or is cancelled.

Python 3.8+, standard library only. Never imports swarm.py.
"""

import contextlib
import datetime
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid

import child_environment as CE
import cli_adapters as CA

RUNS_DIR = "cli-runs"
RUN_RECORD = "run.json"
TURNS_LOG = "turns.jsonl"
SCHEMA = 1
LOCAL = "local"
SLURM = "slurm"
MODE_ENV = "HANIG_SWARM_CLI_SLURM"
OPTION_ENV = (
    ("HANIG_SWARM_CLI_SLURM_PARTITION", "--partition"),
    ("HANIG_SWARM_CLI_SLURM_ACCOUNT", "--account"),
    ("HANIG_SWARM_CLI_SLURM_QOS", "--qos"),
)
DEFAULTS = {"cpus": "4", "mem": "16G", "time": "08:00:00"}
OFF = ("0", "off", "false", "no")
ON = ("1", "on", "true", "yes", "force")
TOOLS = ("sbatch", "squeue", "scancel")
OPTIONAL_TOOLS = ("sacct", "scontrol", "sinfo")
AGENT_HOST_JOB = "sh_agent"
JOB_PREFIX = "hanig-cli-"
PARTITION_PROBE_SECONDS = 5.0
COMMAND_TIMEOUT = 30.0
CANCEL_GRACE_SECONDS = 10.0
MANAGED_RUNTIME_ENV = ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "PI_CODING_AGENT_DIR")
_ATTEMPT = re.compile(r"[0-9A-Za-z][0-9A-Za-z._-]{0,127}")
_JOB_ID = re.compile(r"^([0-9]+)(?:;[A-Za-z0-9_.-]+)?$")
_QOS = re.compile(r"(?:^|\s)QoS=(\S+)")
_JOB_STATE = re.compile(r"(?:^|\s)JobState=(\S+)")
SLURM_FAILED = ("FAILED", "CANCELLED", "TIMEOUT", "NODE_FAIL", "OUT_OF_MEMORY",
                "BOOT_FAIL", "DEADLINE", "REVOKED", "SPECIAL_EXIT")
TERMINAL_STATES = ("stopped", "failed", "preempted", "incomplete")
LOCK_NAME = ".lock"
LOCK_WAIT_SECONDS = 60.0
REDACTED = b"[REDACTED]"
PUMP_CHUNK = 65536
PUMP_DRAIN_SECONDS = 10.0


class CliRunError(ValueError):
    """A launch, resume or cancel request that cannot be carried out."""


# --- small helpers -------------------------------------------------------

def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _env(environ):
    return os.environ if environ is None else environ


def setting(env, legacy_name, default=None):
    """BROOD_<X> when set non-blank, else the legacy HANIG_<X> (rename shim)."""
    primary = env.get("BROOD_" + legacy_name[len("HANIG_"):])
    return primary if (primary or "").strip() else env.get(legacy_name, default)


def valid_attempt_id(value):
    return isinstance(value, str) and _ATTEMPT.fullmatch(value) is not None \
        and value not in (".", "..")


def run_dir(state_dir, attempt_id):
    if not valid_attempt_id(attempt_id):
        raise CliRunError(f"invalid attempt id {attempt_id!r}")
    return Path(state_dir).absolute() / RUNS_DIR / attempt_id


def _write_atomic(path, record, mode=0o600):
    path = Path(path)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), mode)
            json.dump(record, handle, indent=1, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(tmp), str(path))
    finally:
        if tmp.exists():
            tmp.unlink()


def _append(path, record):
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _write_private_text(path, text):
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())


def _read_json(path):
    try:
        obj = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


@contextlib.contextmanager
def _run_lock(rdir, wait):
    """Hold the exclusive per-attempt flock on ``<rdir>/.lock``.

    Serializes launch, resume and cancel of one attempt. Raises CliRunError
    when the lock is not acquired within ``wait`` seconds or cannot be taken
    at all: an unlocked mutation is never attempted. The fd is close-on-exec
    and every spawn here closes inherited fds, so no child keeps the lock.
    """
    path = Path(rdir) / LOCK_NAME
    try:
        fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as exc:
        raise CliRunError(f"cannot open the run lock {path}: {exc}")
    try:
        deadline = time.monotonic() + max(0.0, wait)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise CliRunError(
                        f"another launch, resume or cancel of this attempt "
                        f"holds {path}; refusing to race it. Retry once it "
                        f"finishes")
                time.sleep(0.05)
            except OSError as exc:
                raise CliRunError(f"cannot lock {path}: {exc}; refusing an "
                                  f"unserialized run mutation")
        yield
    finally:
        os.close(fd)


def _withheld_names(contained, adapter=None):
    """Names child_env() keeps that this child must still not receive."""
    if adapter is None:
        return sorted(n for n in contained
                      if any(CA.strip_name(a, n) for a in CA.CLI_BACKENDS))
    return sorted(n for n in contained if CA.strip_name(adapter, n))


def _env_binary(path):
    found = shutil.which("env", path=path)
    if found:
        return found
    return "/usr/bin/env" if os.access("/usr/bin/env", os.X_OK) else None


def _contained_argv(argv, adapter=None, setenv=()):
    """Prefix ``argv`` with `env -u` for every adapter-withheld name.

    The process itself always receives ``CE.child_env()`` (the shared
    containment denylist); this prefix withholds the broader per-adapter
    names before the real program starts. Returns (argv, problem).
    """
    contained = CE.child_env()
    names = _withheld_names(contained, adapter)
    if not names and not setenv:
        return list(argv), None
    env_bin = _env_binary(contained.get("PATH") or os.defpath)
    if not env_bin:
        return None, "`env` is unavailable to withhold credentials; nothing was run"
    prefix = [env_bin]
    for name in names:
        prefix += ["-u", name]
    prefix += list(setenv)
    return prefix + list(argv), None


def _call(argv, timeout=COMMAND_TIMEOUT, adapter=None, setenv=()):
    """(rc or None, stdout, stderr) for a short coordinator-side command."""
    full, problem = _contained_argv(argv, adapter, setenv)
    if problem:
        return None, "", problem
    try:
        proc = subprocess.run(full, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              universal_newlines=True, timeout=max(1.0, timeout),
                              env=CE.child_env())
    except (OSError, subprocess.SubprocessError) as exc:
        return None, "", f"{type(exc).__name__}: {exc}"
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _which(name, environ=None):
    path = _env(environ).get("PATH") or os.defpath
    found = shutil.which(name, path=path)
    return os.path.abspath(found) if found else None


# --- placement -----------------------------------------------------------

def slurm_mode(environ=None):
    value = (setting(_env(environ), MODE_ENV) or "").strip().lower()
    return "off" if value in OFF else "on" if value in ON else "auto"


def slurm_tools(environ=None):
    return {name: _which(name, environ) for name in TOOLS + OPTIONAL_TOOLS}


def in_slurm_job(environ=None):
    return bool((_env(environ).get("SLURM_JOB_ID") or "").strip())


def is_agent_host_job(environ=None):
    """An interactive agent-host allocation: a login-like host, not a node job.

    Same rule as the review gate's offload: job name ``sh_agent`` (or a
    ``sh_agent-``/``sh_agent_`` variant), or a pty job carrying a Lor session
    that is not one of our own ``hanig-cli-`` turn jobs.
    """
    env = _env(environ)
    if not in_slurm_job(env):
        return False
    name = (env.get("SLURM_JOB_NAME") or "").strip().lower()
    if name == AGENT_HOST_JOB or name.startswith((AGENT_HOST_JOB + "-",
                                                  AGENT_HOST_JOB + "_")):
        return True
    return bool((env.get("SLURM_PTY_PORT") or "").strip()
                and (env.get("LOR_SESSION_ID") or "").strip()
                and not name.startswith(JOB_PREFIX))


def placement(environ=None):
    """(LOCAL|SLURM, None) or (None, refusal). Never guesses past a refusal."""
    mode = slurm_mode(environ)
    if mode == "off":
        return LOCAL, None
    tools = slurm_tools(environ)
    missing = [name for name in TOOLS if not tools[name]]
    if mode == "on":
        if missing:
            return None, (f"{MODE_ENV}=on but {', '.join(missing)} not on PATH; "
                          f"refusing to run a coding CLI locally instead. Set "
                          f"{MODE_ENV}=off to run it on this host")
        return SLURM, None
    if in_slurm_job(environ):
        if not is_agent_host_job(environ):
            return LOCAL, None      # already inside a compute-node job
        if missing:
            # An agent-host allocation is login-like: never run the CLI here.
            return None, (f"this is a Slurm agent-host job but "
                          f"{', '.join(missing)} not on PATH; refusing to run "
                          f"a coding CLI on it. Put the Slurm tools on PATH, or "
                          f"set {MODE_ENV}=off to run it on this host")
        return SLURM, None
    return (SLURM, None) if not missing else (LOCAL, None)


def _explicit_partition(env):
    if (setting(env, "HANIG_SWARM_CLI_SLURM_PARTITION") or "").strip():
        return True
    try:
        words = shlex.split(setting(env, "HANIG_SWARM_CLI_SLURM_EXTRA_ARGS") or "")
    except ValueError:
        return True
    return any(w in ("-p", "--partition") or w.startswith("--partition=")
               or (w.startswith("-p") and not w.startswith("--")) for w in words)


def auto_batch_partition(tools, environ=None):
    """``<default>_batch`` when the default partition is interactive, else None.

    Read-only and bounded: `sinfo -h -o "%P %a"` names the default partition
    and its ``_batch`` sibling must be ``up``; it is chosen from an agent-host
    job on the default partition, or when `scontrol show partition` reports
    an interactive QoS. Any error leaves the site default (no flag).
    """
    env = _env(environ)
    if not tools.get("sinfo"):
        return None
    end = time.monotonic() + PARTITION_PROBE_SECONDS
    rc, out, _err = _call([tools["sinfo"], "-h", "-o", "%P %a"],
                          timeout=PARTITION_PROBE_SECONDS)
    if rc != 0:
        return None
    default, states = None, {}
    for line in out.splitlines():
        words = line.split()
        if len(words) < 2:
            continue
        name = words[0]
        if name.endswith("*"):
            name = name.rstrip("*")
            default = default or name
        states[name] = words[1].strip().lower()
    if not default or states.get(default + "_batch") != "up":
        return None
    batch = default + "_batch"
    job_partition = (env.get("SLURM_JOB_PARTITION") or "").strip()
    if is_agent_host_job(env) and job_partition in ("", default):
        return batch
    remaining = end - time.monotonic()
    if not tools.get("scontrol") or remaining <= 0:
        return None
    rc, out, _err = _call([tools["scontrol"], "show", "partition", default],
                          timeout=remaining)
    if rc != 0:
        return None
    return batch if any("interact" in q.lower() for q in _QOS.findall(out)) else None


def sbatch_argv(sbatch, attempt_id, rdir, turn, script, environ=None,
                auto_partition=None):
    env = _env(environ)
    name = re.sub(r"[^A-Za-z0-9._-]", "-", attempt_id)[:40] or "attempt"
    log = f"{rdir}/turn-{turn}.slurm.out"
    argv = [sbatch, "--parsable", "--export=NONE", "--no-requeue",
            f"--job-name={JOB_PREFIX}{name}",
            f"--cpus-per-task={(setting(env, 'HANIG_SWARM_CLI_SLURM_CPUS') or DEFAULTS['cpus']).strip()}",
            f"--mem={(setting(env, 'HANIG_SWARM_CLI_SLURM_MEM') or DEFAULTS['mem']).strip()}",
            f"--time={(setting(env, 'HANIG_SWARM_CLI_SLURM_TIME') or DEFAULTS['time']).strip()}",
            f"--chdir={rdir}", f"--output={log}", f"--error={log}"]
    for var, flag in OPTION_ENV:
        value = (setting(env, var) or "").strip()
        if value:
            argv.append(f"{flag}={value}")
    if auto_partition and not _explicit_partition(env):
        argv.append(f"--partition={auto_partition}")
    argv += shlex.split(setting(env, "HANIG_SWARM_CLI_SLURM_EXTRA_ARGS") or "")
    argv.append(str(script))
    return argv


def runtime_env(environ=None):
    """The only variables a Slurm turn wrapper starts with (never a secret)."""
    env = _env(environ)
    out = {"HOME": env.get("HOME") or os.path.expanduser("~"),
           "USER": env.get("USER") or env.get("LOGNAME") or "",
           "PATH": os.pathsep.join(os.path.abspath(os.path.expanduser(p))
                                   for p in (env.get("PATH") or "").split(os.pathsep) if p),
           "LANG": env.get("LANG") or "C.UTF-8",
           "LC_ALL": env.get("LC_ALL") or env.get("LANG") or "C.UTF-8"}
    for name in MANAGED_RUNTIME_ENV:
        value = (env.get(name) or "").strip()
        if value:
            out[name] = value
    return out


def batch_script(rdir, turn, worktree, environ=None):
    """Bash script for one turn. Holds argv paths and safe env only."""
    q = shlex.quote
    words = " ".join(f"{k}={q(v)}" for k, v in runtime_env(environ).items())
    return "\n".join([
        "#!/bin/bash",
        "# hanig-swarm direct-CLI turn (generated; holds no secret)",
        "set -u",
        "umask 077",
        f"cd {q(str(worktree))} || exit 125",
        f'exec env -i {words} TMPDIR="${{SLURM_TMPDIR:-${{TMPDIR:-/tmp}}}}" '
        f"{q(sys.executable)} {q(str(Path(__file__).absolute()))} turn "
        f"{q(str(rdir))} {int(turn)}",
        ""])


# --- process identity ----------------------------------------------------

def _linux_identity(pid):
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
        boot = next(int(line.split()[1]) for line in
                    Path("/proc/stat").read_text().splitlines()
                    if line.startswith("btime "))
    except (OSError, ValueError, StopIteration):
        return None
    fields = raw[raw.rindex(")") + 2:].split()
    ticks = os.sysconf("SC_CLK_TCK")
    start_us = boot * 1000000 + int(fields[19]) * 1000000 // ticks
    return start_us, fields[0] in ("Z", "X")


def _ps_identity(pid):
    ps = _which("ps") or ("/bin/ps" if os.access("/bin/ps", os.X_OK) else None)
    if not ps:
        return "unknown"
    rc, out, _err = _call([ps, "-o", "stat=", "-o", "lstart=", "-p", str(pid)],
                          timeout=10, setenv=("LC_ALL=C", "LANG=C"))
    if rc is None:
        return "unknown"
    words = out.split()
    if rc != 0 or len(words) < 6:
        return None
    try:
        started = time.mktime(time.strptime(" ".join(words[1:6]),
                                            "%a %b %d %H:%M:%S %Y"))
    except ValueError:
        return "unknown"
    return int(started) * 1000000, words[0].startswith("Z")


def process_identity(pid):
    """(start_us, zombie), None when there is no such process, or "unknown"."""
    if not isinstance(pid, int) or pid <= 1:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        pass
    if Path("/proc/self/stat").exists():
        found = _linux_identity(pid)
        return found if found is not None else (
            None if not Path(f"/proc/{pid}").exists() else "unknown")
    return _ps_identity(pid)


def _reap(pid):
    """Collect our own exited child so it never reads as a live zombie."""
    try:
        os.waitpid(pid, os.WNOHANG)
    except (ChildProcessError, OSError):
        pass


def _local_liveness(run):
    """'alive', 'dead' or 'unknown' for the recorded wrapper process."""
    pid, start = run.get("pid"), run.get("pid_start_us")
    if not isinstance(pid, int) or not isinstance(start, int):
        return "unknown"
    _reap(pid)
    ident = process_identity(pid)
    if ident == "unknown":
        return "unknown"
    if ident is None or ident[1] or ident[0] != start:
        return "dead"           # gone, a zombie, or a reused PID
    return "alive"


# --- Slurm observation ---------------------------------------------------

def _squeue(job_id):
    """'active', 'gone' or 'unknown'. Only an answered query reads as gone."""
    tools = slurm_tools()
    if not tools.get("squeue"):
        return "unknown", "squeue not on PATH"
    rc, out, err = _call([tools["squeue"], "-h", "-j", str(job_id), "-o", "%T"])
    if rc == 0:
        state = out.strip().splitlines()[0].strip() if out.strip() else ""
        return ("active", state) if state else ("gone", "")
    if rc is not None and "invalid job id" in (err or "").lower():
        return "gone", ""
    return "unknown", (err or out or "squeue failed").strip()[:200]


def _terminal_slurm_state(job_id):
    """Final scheduler state from scontrol, then sacct, else None."""
    tools = slurm_tools()
    if tools.get("scontrol"):
        rc, out, _err = _call([tools["scontrol"], "show", "job", "-o", str(job_id)])
        if rc == 0:
            match = _JOB_STATE.search(out)
            if match:
                return match.group(1).upper()
    if tools.get("sacct"):
        rc, out, _err = _call([tools["sacct"], "-n", "-X", "-P", "-j", str(job_id),
                               "-o", "State"])
        if rc == 0 and out.strip():
            return out.strip().splitlines()[0].split()[0].upper()
    return None


# --- records -------------------------------------------------------------

def load_run(state_dir, attempt_id):
    return _read_json(run_dir(state_dir, attempt_id) / RUN_RECORD)


def _receipt(rdir, turn):
    rec = _read_json(Path(rdir) / f"turn-{turn}.exit.json")
    if rec is None or rec.get("turn") != turn:
        return None
    return rec if isinstance(rec.get("returncode"), int) else None


def _observation(state, run, detail, **extra):
    out = {"state": state, "turn": run.get("turn") if run else None,
           "session_id": run.get("session_id") if run else None,
           "returncode": None, "mode": run.get("mode") if run else None,
           "worktree": run.get("worktree") if run else None,
           "adapter": run.get("adapter") if run else None,
           "slurm_job_id": run.get("slurm_job_id") if run else None,
           "slurm_state": None,
           "cancelled": bool(run and run.get("status") == "cancelled"),
           "detail": detail}
    out.update(extra)
    return out


def _from_receipt(run, receipt):
    sid = receipt.get("session_id") or run.get("session_id")
    rc = receipt["returncode"]
    state = "stopped" if rc == 0 else "failed"
    detail = (f"turn {run.get('turn')} exited 0" if rc == 0 else
              f"turn {run.get('turn')} exited {rc}: "
              f"{receipt.get('error') or 'see turn stderr log'}")
    return _observation(state, run, detail, session_id=sid, returncode=rc)


def inspect_run_dir(rdir):
    """Truthful lifecycle of the current turn in one run directory."""
    rdir = Path(rdir)
    run = _read_json(rdir / RUN_RECORD)
    if run is None or not isinstance(run.get("turn"), int):
        return _observation("unknown", None, f"no readable {RUN_RECORD} in {rdir}")
    turn = run["turn"]
    receipt = _receipt(rdir, turn)
    if receipt is not None:
        return _from_receipt(run, receipt)
    if run.get("status") == "launch_failed":
        return _observation("failed", run, "the turn was never started: "
                            + str(run.get("error") or "launch failed"))
    if run.get("mode") == SLURM:
        job = run.get("slurm_job_id")
        if not job:
            return _observation("unknown", run, "slurm run records no job id")
        queued, info = _squeue(job)
        if queued == "active":
            return _observation("running", run, f"slurm job {job} is {info}",
                                slurm_state=info)
        if queued == "unknown":
            return _observation("unknown", run, f"slurm job {job}: {info}")
        receipt = _receipt(rdir, turn)
        if receipt is not None:
            return _from_receipt(run, receipt)
        final = _terminal_slurm_state(job)
        if final is not None and final.startswith("PREEMPTED"):
            return _observation("preempted", run, f"slurm job {job} was "
                                "preempted before the turn wrote a receipt",
                                slurm_state=final)
        if final is not None and final.split("+")[0] in SLURM_FAILED:
            return _observation("failed", run, f"slurm job {job} ended {final} "
                                "without a turn receipt", slurm_state=final)
        return _observation("incomplete", run, f"slurm job {job} is gone "
                            f"(final state {final or 'unknown'}) and the turn "
                            "wrote no receipt", slurm_state=final)
    live = _local_liveness(run)
    if live == "alive":
        return _observation("running", run, f"turn {turn} wrapper pid "
                            f"{run.get('pid')} is running")
    receipt = _receipt(rdir, turn)
    if receipt is not None:
        return _from_receipt(run, receipt)
    if live == "unknown":
        return _observation("unknown", run, "the wrapper process could not be "
                            "identified; not guessing whether it is alive")
    return _observation("incomplete", run, f"turn {turn} wrapper pid "
                        f"{run.get('pid')} is gone and wrote no receipt")


def inspect_run(state_dir, attempt_id):
    return inspect_run_dir(run_dir(state_dir, attempt_id))


def find_run(state_dir, attempt_id):
    """Whether a run directory exists for this attempt (orphan recovery)."""
    try:
        return (run_dir(state_dir, attempt_id) / RUN_RECORD).is_file()
    except CliRunError:
        return False


# --- launch / resume ---------------------------------------------------

def availability(adapter, model, environ=None):
    """None when a turn can be started from here, else an actionable refusal."""
    if adapter not in CA.CLI_BACKENDS:
        return f"unknown CLI adapter {adapter!r}"
    try:
        need = CA.key_requirement(adapter, model)
    except CA.AdapterError as exc:
        return str(exc)
    if need and not str(_env(environ).get(need) or "").strip():
        return (f"backend pi with provider {CA.OPENROUTER} needs {need} in the "
                f"coordinator environment; it is unset, so nothing was started")
    _mode, problem = placement(environ)
    if problem:
        return problem
    if not _which(adapter, environ):
        return (f"the `{adapter}` CLI is not on PATH, so a {adapter} coding "
                f"worker cannot be started from this host. No Paseo fallback "
                f"was attempted.")
    return None


def _start_turn(rdir, run, turn, prompt, *, resume_session_id=None,
                environ=None, key_value=None):
    """Write the turn's files and start it. Returns (updates, error)."""
    adapter, worktree = run["adapter"], run["worktree"]
    binary = _which(adapter, environ)
    if not binary:
        return None, f"the `{adapter}` CLI is not on PATH"
    session_id = resume_session_id or run.get("session_id")
    try:
        argv = CA.build_argv(adapter, binary, model=run["model"], worktree=worktree,
                             session_id=session_id, thinking=run.get("thinking"),
                             resume=resume_session_id is not None,
                             session_dir=run.get("session_dir"))
    except CA.AdapterError as exc:
        return None, str(exc)
    names = {k: rdir / f"turn-{turn}.{k}" for k in (
        "prompt.txt", "stdout.log", "stderr.log", "exit.json", "key", "json")}
    # Only files this call created are ever removed: an existing turn-N file
    # belongs to another (or a crashed) caller and is left exactly as found.
    created = []

    def discard():
        # The key goes first. Cleanup runs inside error paths, so it never
        # raises: a file it cannot remove is left for the operator.
        doomed = list(reversed(created))
        for path in sorted(doomed, key=lambda path: path != names["key"]):
            try:
                path.unlink()
            except OSError:
                pass
    try:
        _write_private_text(names["prompt.txt"], str(prompt))
        created.append(names["prompt.txt"])
        key_file = None
        if key_value is not None:
            _write_private_text(names["key"], key_value)
            created.append(names["key"])
            key_file = str(names["key"])
        if names["json"].exists():
            raise FileExistsError(f"{names['json']} already exists")
        _write_atomic(names["json"], {
            "turn": turn, "adapter": adapter, "argv": argv, "cwd": worktree,
            "stdin": str(names["prompt.txt"]), "stdout": str(names["stdout.log"]),
            "stderr": str(names["stderr.log"]), "exit": str(names["exit.json"]),
            "key_file": key_file,
            "key_env": CA.OPENROUTER_KEY if key_file else None,
            "fallback_session_id": session_id})
        created.append(names["json"])
    except FileExistsError as exc:
        discard()
        return None, (f"turn {turn} files already exist in {rdir} ({exc}); "
                      f"another or an interrupted launch created them. Nothing "
                      f"was started or removed; inspect them before retrying")
    except OSError as exc:
        discard()
        return None, f"cannot write turn {turn} files in {rdir}: {exc}"
    record = {"turn": turn, "mode": run["mode"], "started_at": _now(),
              "resume_of": resume_session_id, "argv": argv}
    if run["mode"] == SLURM:
        updates, error = _submit_slurm(rdir, run, turn, environ, created)
    else:
        updates, error = _spawn_local(rdir, run, turn)
    if error:
        # Nothing was started: remove the files this call created (the key
        # first) so a later resume can create turn N again with O_EXCL. The
        # sbatch script is removed only if _submit_slurm recorded creating it.
        discard()
        return None, error
    record.update(updates)
    _append(rdir / TURNS_LOG, record)
    return updates, None


def _spawn_local(rdir, run, turn):
    argv, problem = _contained_argv(
        [sys.executable, str(Path(__file__).absolute()), "turn", str(rdir), str(turn)],
        run["adapter"])
    if problem:
        return None, problem
    log = os.fdopen(os.open(str(rdir / f"turn-{turn}.wrapper.log"),
                            os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "ab")
    try:
        proc = subprocess.Popen(argv, cwd=run["worktree"], stdin=subprocess.DEVNULL,
                                stdout=log, stderr=log, close_fds=True,
                                start_new_session=True, env=CE.child_env())
    except OSError as exc:
        return None, f"cannot start the turn wrapper: {exc}"
    finally:
        log.close()
    ident = process_identity(proc.pid)
    start = ident[0] if isinstance(ident, tuple) else None
    try:
        pgid = os.getpgid(proc.pid)
    except OSError:
        pgid = proc.pid
    return {"pid": proc.pid, "pgid": pgid, "pid_start_us": start,
            "slurm_job_id": None}, None


def _submit_slurm(rdir, run, turn, environ=None, created=None):
    """Write turn N's batch script and submit it. Returns (updates, error).

    The script is created with O_EXCL and appended to ``created`` the moment
    it exists, so the caller's cleanup removes it only if this call made it;
    a pre-existing script is refused and left as found.
    """
    tools = slurm_tools(environ)
    missing = [name for name in TOOLS if not tools[name]]
    if missing:
        return None, (f"slurm placement needs {', '.join(missing)} on PATH; "
                      f"refusing to run the CLI on this login host")
    script = rdir / f"turn-{turn}.sbatch.sh"
    try:
        _write_exclusive_text(script, batch_script(rdir, turn, run["worktree"], environ),
                              created)
    except FileExistsError:
        return None, (f"{script} already exists; another or an interrupted "
                      f"launch created it. Nothing was submitted and it was "
                      f"left in place; inspect it before retrying")
    except OSError as exc:
        return None, f"cannot write {script}: {exc}"
    env = _env(environ)
    auto = None if _explicit_partition(env) else auto_batch_partition(tools, env)
    rc, out, err = _call(sbatch_argv(tools["sbatch"], run["attempt_id"], rdir, turn,
                                     script, env, auto))
    match = _JOB_ID.match((out or "").strip().splitlines()[0].strip()) if (
        rc == 0 and (out or "").strip()) else None
    if not match:
        return None, (f"sbatch refused the turn ({(err or out or 'no job id').strip()[:200]}); "
                      f"failing closed: the CLI was not run on this login host")
    return {"pid": None, "pgid": None, "pid_start_us": None,
            "slurm_job_id": match.group(1)}, None


def _write_exclusive_text(path, text, created=None):
    """Create ``path`` (0700) with O_EXCL; record it in ``created`` once it exists."""
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700)
    if created is not None:
        created.append(Path(path))
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)


def launch(adapter, model, thinking, worktree, prompt, state_dir, attempt_id, *,
           env=None, resume_session_id=None):
    """Start turn 1 of a fresh run. Returns (run record, error).

    ``env`` is the environment the policy decisions read (placement and the
    pi key); the spawned process always receives ``CE.child_env()`` minus the
    adapter's withheld names. Refuses before ANY side effect when the
    adapter, key, placement or worktree cannot be honored.
    """
    source = _env(env)
    if adapter not in CA.CLI_BACKENDS:
        return None, f"unknown CLI adapter {adapter!r}"
    if not CA.valid_model(model):
        return None, f"invalid {adapter} model {model!r}"
    if thinking is not None and not CA.valid_token(thinking):
        return None, f"invalid {adapter} effort {thinking!r}"
    if not isinstance(worktree, str) or not os.path.isabs(worktree) \
            or not os.path.isdir(worktree):
        return None, f"worktree must be an existing absolute directory: {worktree!r}"
    problem = availability(adapter, model, source)
    if problem:
        return None, problem
    mode, problem = placement(source)
    if problem:
        return None, problem
    key_name = CA.key_requirement(adapter, model)
    key_value = str(source.get(key_name)).strip() if key_name else None
    try:
        rdir = run_dir(state_dir, attempt_id)
    except CliRunError as exc:
        return None, str(exc)
    try:
        rdir.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.mkdir(str(rdir), 0o700)
    except FileExistsError:
        return None, (f"a direct-CLI run already exists for attempt {attempt_id!r} "
                      f"at {rdir}; inspect or resume it instead")
    except OSError as exc:
        return None, f"cannot create {rdir}: {exc}"
    os.chmod(str(rdir), 0o700)
    try:
        with _run_lock(rdir, LOCK_WAIT_SECONDS):
            return _launch_locked(adapter, model, thinking, worktree, prompt,
                                  rdir, attempt_id, mode, source,
                                  resume_session_id, key_value)
    except CliRunError as exc:
        return None, str(exc)


def _launch_locked(adapter, model, thinking, worktree, prompt, rdir,
                   attempt_id, mode, source, resume_session_id, key_value):
    session_id = resume_session_id or (
        str(uuid.uuid4()) if CA.preassigned_session(adapter) else None)
    run = {"schema_version": SCHEMA, "attempt_id": attempt_id, "adapter": adapter,
           "model": model, "thinking": thinking, "mode": mode,
           "worktree": str(Path(worktree).absolute()), "session_id": session_id,
           "session_dir": str(rdir / "pi-sessions") if adapter == CA.PI else None,
           "created_at": _now(), "turn": 1, "pid": None, "pgid": None,
           "pid_start_us": None, "slurm_job_id": None, "status": "launching"}
    if run["session_dir"]:
        os.mkdir(run["session_dir"], 0o700)
    _write_atomic(rdir / RUN_RECORD, run)
    updates, error = _start_turn(rdir, run, 1, prompt, environ=source,
                                 resume_session_id=resume_session_id,
                                 key_value=key_value)
    if error:
        run.update(status="launch_failed", error=error)
        _write_atomic(rdir / RUN_RECORD, run)
        return None, error
    run.update(updates, status="launched")
    _write_atomic(rdir / RUN_RECORD, run)
    return run, None


def resume(state_dir, attempt_id, prompt, *, env=None):
    """Start turn N+1 of an existing run in the same worktree and session.

    Holds the run lock without waiting: a competing launch, resume or cancel
    makes this call refuse, never race. Placement is re-evaluated for THIS
    host: a run recorded ``local`` refuses where placement is now Slurm.
    """
    source = _env(env)
    try:
        rdir = run_dir(state_dir, attempt_id)
    except CliRunError as exc:
        return None, str(exc)
    if not (rdir / RUN_RECORD).is_file():
        return None, f"no direct-CLI run for attempt {attempt_id!r}"
    try:
        with _run_lock(rdir, 0):
            return _resume_locked(rdir, attempt_id, prompt, source)
    except CliRunError as exc:
        return None, str(exc)


def _resume_locked(rdir, attempt_id, prompt, source):
    run = _read_json(rdir / RUN_RECORD)
    if run is None:
        return None, f"no direct-CLI run for attempt {attempt_id!r}"
    obs = inspect_run_dir(rdir)
    if obs["state"] in ("running", "unknown"):
        return None, (f"turn {run.get('turn')} is {obs['state']} "
                      f"({obs['detail']}); refusing to start another turn")
    here, problem = placement(source)
    if problem:
        return None, problem
    if run.get("mode") == LOCAL and here == SLURM:
        return None, (f"attempt {attempt_id!r} was launched with local "
                      f"placement, but this host places coding CLIs on Slurm "
                      f"(a Slurm login or agent host, or {MODE_ENV}=on). "
                      f"Refusing both to run the CLI on this host and to "
                      f"submit a node-local worktree to Slurm. Resume from the "
                      f"host that launched it, or set {MODE_ENV}=off to run "
                      f"it here deliberately")
    session_id = obs.get("session_id") or run.get("session_id")
    if not CA.valid_session_id(session_id):
        return None, (f"attempt {attempt_id!r} has no recorded {run.get('adapter')} "
                      f"session id to resume")
    problem = availability(run["adapter"], run["model"], source)
    if problem:
        return None, problem
    key_name = CA.key_requirement(run["adapter"], run["model"])
    key_value = str(source.get(key_name)).strip() if key_name else None
    turn = int(run["turn"]) + 1
    previous = dict(run)
    run.update(turn=turn, session_id=session_id, status="launching",
               pid=None, pgid=None, pid_start_us=None, slurm_job_id=None)
    _write_atomic(rdir / RUN_RECORD, run)
    updates, error = _start_turn(rdir, run, turn, prompt, environ=source,
                                 resume_session_id=session_id, key_value=key_value)
    if error:
        previous["resume_error"] = error
        _write_atomic(rdir / RUN_RECORD, previous)
        return None, error
    run.update(updates, status="launched")
    _write_atomic(rdir / RUN_RECORD, run)
    return run, None


def cancel(state_dir, attempt_id, grace=CANCEL_GRACE_SECONDS):
    """Stop the current turn. Returns (ok, detail). Idempotent.

    Local: SIGTERM to the recorded process group, only after the recorded
    start time still identifies the wrapper, then SIGKILL after ``grace``.
    Slurm: `scancel`. Never signals a process it cannot identify.
    """
    try:
        rdir = run_dir(state_dir, attempt_id)
    except CliRunError as exc:
        return False, str(exc)
    if not (rdir / RUN_RECORD).is_file():
        return False, f"no direct-CLI run for attempt {attempt_id!r}"
    try:
        with _run_lock(rdir, LOCK_WAIT_SECONDS):
            return _cancel_locked(rdir, attempt_id, grace)
    except CliRunError as exc:
        return False, str(exc)


def _cancel_locked(rdir, attempt_id, grace):
    run = _read_json(rdir / RUN_RECORD)
    if run is None:
        return False, f"no direct-CLI run for attempt {attempt_id!r}"
    if _receipt(rdir, run.get("turn")) is not None:
        return True, f"turn {run.get('turn')} had already finished"
    try:
        (rdir / f"turn-{run.get('turn')}.key").unlink()
    except (FileNotFoundError, OSError):
        pass
    if run.get("mode") == SLURM:
        job = run.get("slurm_job_id")
        if not job:
            return False, "slurm run records no job id"
        tools = slurm_tools()
        if not tools.get("scancel"):
            return False, "scancel not on PATH"
        rc, out, err = _call([tools["scancel"], str(job)])
        queued, _info = _squeue(job)
        if rc != 0 and queued != "gone":
            return False, f"scancel {job} failed: {(err or out).strip()[:200]}"
        detail = f"scancel {job}"
    else:
        live = _local_liveness(run)
        if live == "unknown":
            return False, "the wrapper process could not be identified; not signalling"
        if live == "dead":
            detail = "the wrapper was not running"
        else:
            pid, pgid = run["pid"], run["pgid"]
            try:
                if os.getpgid(pid) != pgid:
                    return False, f"pid {pid} is no longer leader of group {pgid}"
                os.killpg(pgid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except OSError as exc:
                return False, f"cannot signal process group {pgid}: {exc}"
            deadline = time.monotonic() + max(0.0, grace)
            while time.monotonic() < deadline and _local_liveness(run) == "alive":
                time.sleep(0.1)
            if _local_liveness(run) == "alive":
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except OSError:
                    pass
            detail = f"signalled process group {pgid}"
    run.update(status="cancelled", cancelled_at=_now())
    _write_atomic(rdir / RUN_RECORD, run)
    return True, detail


# --- the turn wrapper (runs detached, or inside the Slurm job) ----------

def _exec_with_key(fd, argv):
    """Read one credential from inherited pipe ``fd`` and exec ``argv``.

    The ONLY delivery path for a pi provider key: it never appears in argv,
    a file that outlives the wrapper's first read, or any record. The name
    is fixed by the adapter (cli_adapters.OPENROUTER_KEY).
    """
    with os.fdopen(int(fd), "r", encoding="utf-8") as handle:
        value = handle.read().strip()
    if value:
        os.environ[CA.OPENROUTER_KEY] = value
    os.execv(argv[0], argv)


class _RedactingSink:
    """Write bytes to ``out`` with every occurrence of ``secret`` replaced.

    The last ``len(secret) - 1`` bytes are held back between writes, so an
    occurrence split across any chunk boundary is still replaced: an
    occurrence starting before the held tail lies wholly inside the buffer.

    ``write`` and ``close`` share a lock, and ``close`` is idempotent: the
    turn wrapper may flush a sink whose pump is still blocked on a pipe a
    descendant holds open, and that pump's later ``write``/``close`` must
    neither interleave with the flush nor write after it (late bytes are
    dropped, never unredacted).
    """

    def __init__(self, out, secret):
        self._out, self._secret, self._buf = out, secret, b""
        self._keep = len(secret) - 1
        self._lock = threading.Lock()
        self._closed = False

    def write(self, data):
        with self._lock:
            if self._closed:
                return
            self._buf = (self._buf + data).replace(self._secret, REDACTED)
            cut = len(self._buf) - self._keep
            if cut > 0:
                self._out.write(self._buf[:cut])
                self._buf = self._buf[cut:]

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._out.write(self._buf.replace(self._secret, REDACTED))
                self._buf = b""
            finally:
                self._out.close()


def _pump(read_fd, sink):
    """Copy a pipe into a redacting sink until EOF, then close both."""
    try:
        while True:
            chunk = os.read(read_fd, PUMP_CHUNK)
            if not chunk:
                break
            sink.write(chunk)
    except OSError:
        pass
    finally:
        os.close(read_fd)
        try:
            sink.close()
        except OSError:
            pass


class _TurnLogs:
    """The CLI's stdout/stderr targets for one turn.

    Without a secret they are the log files themselves. With one, the CLI
    writes into pipes and a pump per stream redacts ``secret`` before any
    byte reaches its log file.
    """

    def __init__(self, stdout_path, stderr_path, secret=None):
        self._files, self._threads, self._write_fds = [], [], []
        self._sinks = []
        try:
            for path in (stdout_path, stderr_path):
                self._files.append(open(path, "wb"))
            self.targets = list(self._files)
            if secret:
                self.targets = []
                for handle in self._files:
                    read_fd, write_fd = os.pipe()
                    self._write_fds.append(write_fd)
                    sink = _RedactingSink(handle, secret.encode("utf-8"))
                    self._sinks.append(sink)
                    thread = threading.Thread(
                        target=_pump, daemon=True, args=(read_fd, sink))
                    thread.start()
                    self._threads.append(thread)
                    self.targets.append(write_fd)
        except BaseException:
            self.close(0)
            raise

    def spawned(self):
        """Drop the parent's write ends once the CLI holds its own."""
        for fd in self._write_fds:
            os.close(fd)
        self._write_fds = []

    def close(self, wait=None):
        self.spawned()
        if not self._threads:
            for handle in self._files:
                handle.close()
            return
        wait = PUMP_DRAIN_SECONDS if wait is None else wait
        for thread in self._threads:
            thread.join(wait)
        # A pump still blocked after ``wait`` (a descendant kept the pipe
        # open) stays a daemon, but its sink is flushed and closed here, so
        # the held-back tail reaches the log before the exit receipt is
        # written. Bytes the pump reads later are dropped, never unredacted.
        for sink in self._sinks:
            try:
                sink.close()
            except OSError:
                pass


def run_turn(rdir, turn):
    """Run one turn's CLI and write its exit receipt. Returns the exit code."""
    rdir = Path(rdir)
    os.umask(0o077)
    spec = _read_json(rdir / f"turn-{turn}.json") or {}
    started = _now()
    key_value, error, rc, child = None, None, 125, None
    key_file = spec.get("key_file")
    if key_file:
        try:
            key_value = Path(key_file).read_text(encoding="utf-8").strip() or None
        except OSError as exc:
            error = f"cannot read the private key file: {exc}"
        try:
            os.unlink(key_file)
        except FileNotFoundError:
            pass
        except OSError as exc:
            # Fail closed: the CLI never starts while its key is on disk.
            key_value = None
            error = (f"cannot remove the private key file {key_file} ({exc}); "
                     f"refusing to start the CLI while the key remains on disk")
    secret = key_value
    terminated = []

    def on_term(signum, _frame):
        terminated.append(signum)
        if child is not None and child.poll() is None:
            child.send_signal(signum)
    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGHUP, on_term)
    logs = None
    try:
        argv = spec.get("argv")
        if not isinstance(argv, list) or not argv:
            raise CliRunError(f"turn {turn} spec has no argv")
        if error:
            raise CliRunError(error)
        # Defense in depth: whoever started this wrapper, the CLI runs behind
        # `env -u` for every name its adapter withholds (an unknown adapter
        # withholds every adapter's names).
        adapter = spec.get("adapter")
        prefix, problem = _contained_argv(
            [], adapter if adapter in CA.CLI_BACKENDS else None)
        if problem:
            raise CliRunError(problem)
        pass_fds = ()
        if key_value:
            read_fd, write_fd = os.pipe()
            os.write(write_fd, key_value.encode("utf-8"))
            os.close(write_fd)
            argv = [sys.executable, str(Path(__file__).absolute()), "exec-with-key",
                    str(read_fd), "--"] + argv
            pass_fds = (read_fd,)
        key_value = None
        argv = prefix + argv
        try:
            logs = _TurnLogs(spec["stdout"], spec["stderr"], secret)
            with open(spec["stdin"], "rb") as stdin:
                child = subprocess.Popen(argv, cwd=spec.get("cwd"), stdin=stdin,
                                         stdout=logs.targets[0],
                                         stderr=logs.targets[1],
                                         pass_fds=pass_fds, close_fds=True,
                                         env=CE.child_env())
        finally:
            for fd in pass_fds:
                os.close(fd)
            if logs is not None:
                logs.spawned()
        while True:
            try:
                rc = child.wait()
                break
            except InterruptedError:
                continue
        if rc != 0:
            error = (f"terminated by signal {-rc}" if rc < 0 else f"exit {rc}")
    except (OSError, CliRunError, KeyError) as exc:
        error, rc = f"{type(exc).__name__}: {exc}", 127
    finally:
        if logs is not None:
            logs.close()
    sid = None
    try:
        sid = CA.parse_session(spec.get("adapter"),
                               Path(spec["stdout"]).read_text(errors="replace"))
    except (OSError, KeyError, CA.AdapterError):
        sid = None
    sid = sid or spec.get("fallback_session_id")
    if secret:
        text = REDACTED.decode("ascii")
        error = error.replace(secret, text) if error else error
        sid = sid.replace(secret, text) if isinstance(sid, str) else sid
    _write_atomic(spec.get("exit") or (rdir / f"turn-{turn}.exit.json"), {
        "turn": turn, "returncode": rc, "started_at": started,
        "finished_at": _now(), "session_id": sid,
        "error": error, "signalled": bool(terminated)})
    return 0 if rc == 0 else 1


# --- unit.py / lor_io glue ---------------------------------------------

def binding_fields(job_id, backend, cli_run_dir):
    """Validate a direct-CLI bind; return the unit.json fields. SystemExit on refusal."""
    if not valid_attempt_id(str(job_id)):
        raise SystemExit(f"error: implausible direct-CLI run id {job_id!r}; pass "
                         f"the attempt id the coordinator launched")
    if not cli_run_dir or not os.path.isabs(str(cli_run_dir)) \
            or Path(str(cli_run_dir)).name != str(job_id):
        raise SystemExit("error: a direct-CLI attempt must be bound with the "
                         "absolute --cli-run-dir STATE_DIR/cli-runs/<attempt>")
    return {"backend": backend, "cli_run_dir": str(cli_run_dir)}


def code_lifecycle(spec, notes):
    """Direct-CLI half of unit.py's code predicate: (verdict or None, status).

    A turn that exited 0 returns (None, "idle"): lifecycle only, so unit.py's
    output and repository checks decide. Anything not established is
    INCOMPLETE; a preempted Slurm turn is PREEMPTED; a failed turn FAILED.
    """
    run_id, rdir = spec.get("job_id"), spec.get("cli_run_dir")
    if not run_id or not rdir:
        notes.append("direct-CLI attempt records no run directory. Not judged.")
        return "INCOMPLETE", "unknown"
    obs = inspect_run_dir(rdir)
    label = f"{spec.get('backend')} run {run_id} turn {obs.get('turn')}"
    if obs["state"] == "running":
        notes.append(f"{label}: still working ({obs['detail']}).")
        return "RUNNING", "running"
    if obs["state"] == "stopped":
        return None, "idle"
    if obs["state"] == "failed":
        notes.append(f"{label} failed: {obs['detail']}. See {rdir}.")
        return "FAILED", "failed"
    if obs["state"] == "preempted":
        notes.append(f"{label}: {obs['detail']}.")
        return "PREEMPTED", "preempted"
    notes.append(f"{label} could not be judged: {obs['detail']}. Not judged; "
                 f"retry later.")
    return "INCOMPLETE", "unknown"


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["turn"] and len(args) == 3:
        return run_turn(args[1], int(args[2]))
    if args[:1] == ["exec-with-key"] and len(args) > 3 and args[2] == "--":
        _exec_with_key(args[1], args[3:])
    sys.stderr.write("usage: cli_io.py turn RUN_DIR N\n")
    return 2


if __name__ == "__main__":
    sys.exit(main())
