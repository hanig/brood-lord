"""Execution-class placement: run a `claude`/`codex` CLI review seat as a Slurm job.

On a Slurm login node the heavy Node CLIs must not run locally. When
`use_slurm_offload()` holds, review_subscriptions hands the exact CLI argv to
`run_cli_via_slurm()`, which stages it in a private 0700 directory under SHARED
state (never /tmp: /tmp is node-local, while `$XDG_STATE_HOME` or
`$HOME/.local/state` is shared between login and compute nodes on the measured
clusters), submits it with `sbatch --parsable --export=NONE`, polls for an
atomic `rc.txt`, and always cancels the job and removes the staging directory.

Design E (secrets never reach compute nodes):
- the `sbatch` submission environment drops every `LOR_*` variable and every
  variable the caller's `strip_key` predicate flags (API/billing names,
  prefixes and suffixes), and `--export=NONE` keeps even that environment off
  the job;
- the generated batch script runs every CLI under `env -i` with only HOME,
  USER, a sanitized PATH, LANG, LC_ALL, a fresh node-local TMPDIR (removed on
  EXIT) and the caller's explicitly managed, non-secret CLI overrides;
- staged files hold the CLI argv and prompt only; the directory is removed in
  `finally`, so no prompt bytes stay on disk after the seat completes.

Declared limits: the staging root must be on a filesystem the compute nodes
see (override with BROOD_REVIEW_SLURM_STAGE_DIR, or legacy
HANIG_REVIEW_SLURM_STAGE_DIR); the CLI binaries and their
subscription credentials must be reachable from compute nodes; an explicitly
configured stage directory is trusted as given; a process killed by SIGKILL
cannot run `scancel`, so `--time` bounds any such orphan.

Agent hosts: an interactive agent-host allocation (`SLURM_JOB_NAME=sh_agent`,
or `SLURM_PTY_PORT` plus `LOR_SESSION_ID` outside a `hanig-rev-*` seat job) is
a constrained login-like host, so `auto` still offloads from it, while any
other Slurm job (a batch job or an offloaded seat) never offloads again. When
no partition is configured, `auto_batch_partition` may pick `<default>_batch`
from a read-only, 5s-bounded `sinfo` (and optional `scontrol`) query; any
missing tool or error leaves the partition to the site default.

Shutdown: every staging directory is tracked from creation until removal and
every submitted job until it is cancelled, so `cancel_active_jobs()` (watchdog
and SIGTERM paths, which skip `finally`) cancels all of them and removes every
staging directory, including one whose `sbatch` had not yet returned. A job
whose submission races that cleanup finds its staging directory gone and is
cancelled by its own seat once `sbatch` returns.

Every HANIG_REVIEW_SLURM* setting is read through `setting()`: a non-blank
BROOD_<X> wins, otherwise the legacy HANIG_<X> applies unchanged.

Python 3.8+, standard library only. Must never import review.py.
"""

import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import subprocess
import threading
import time

MODE_ENV = "HANIG_REVIEW_SLURM"
STAGE_ENV = "HANIG_REVIEW_SLURM_STAGE_DIR"
OFF = ("0", "off", "false", "no")
FORCE = ("1", "on", "true", "yes", "force")
TOOLS = ("sbatch", "squeue", "scancel")
DEFAULTS = {"cpus": "2", "mem": "8G", "time": "00:20:00"}
OPTION_ENV = (
    ("HANIG_REVIEW_SLURM_PARTITION", "--partition"),
    ("HANIG_REVIEW_SLURM_ACCOUNT", "--account"),
    ("HANIG_REVIEW_SLURM_QOS", "--qos"),
)
SAFE_RUNTIME_ENV = ("HOME", "USER", "PATH", "LANG", "LC_ALL")
MANAGED_CLI_ENV = ("CLAUDE_CONFIG_DIR", "CLAUDE_CODE_MAX_OUTPUT_TOKENS", "CODEX_HOME")
EMPTY_CWD = "__HANIG_EMPTY_CWD__"
PREFLIGHT_REFUSED_RC = 86
POLL_SECONDS = 1.0
SQUEUE_SECONDS = 10.0
GRACE_SECONDS = 15.0
COMMAND_TIMEOUT = 30.0
OPTIONAL_TOOLS = ("sacct", "sinfo", "scontrol")
AGENT_HOST_JOB = "sh_agent"
SEAT_JOB_PREFIX = "hanig-rev-"
PARTITION_PROBE_SECONDS = 5.0
_JOB_ID = re.compile(r"^([0-9]+)(?:;[A-Za-z0-9_.-]+)?$")
_QOS = re.compile(r"(?:^|\s)QoS=(\S+)")
_ACTIVE = {}
_ACTIVE_STAGES = set()
# Reentrant: the SIGTERM/watchdog handler runs cancel_active_jobs() on the main
# thread, which may already hold the lock between two bytecodes.
_ACTIVE_LOCK = threading.RLock()


def _env(environ):
    return os.environ if environ is None else environ


def setting(env, legacy_name, default=None):
    """BROOD_<X> when set non-blank, else the legacy HANIG_<X> (rename shim)."""
    primary = env.get("BROOD_" + legacy_name[len("HANIG_"):])
    return primary if (primary or "").strip() else env.get(legacy_name, default)


def slurm_mode(environ=None):
    """'off', 'force' or 'auto' from BROOD_/HANIG_REVIEW_SLURM (unknown values are auto)."""
    value = (setting(_env(environ), MODE_ENV) or "").strip().lower()
    return "off" if value in OFF else "force" if value in FORCE else "auto"


def _sanitized_path(raw):
    return os.pathsep.join(os.path.abspath(os.path.expanduser(p))
                           for p in (raw or "").split(os.pathsep) if p)


def _which(name, environ=None):
    path = _sanitized_path(_env(environ).get("PATH", ""))
    found = path and shutil.which(name, path=path)
    return os.path.abspath(found) if found else None


def slurm_tools(environ=None):
    """{tool: absolute path or None}: required sbatch, squeue and scancel, plus
    optional sacct, sinfo and scontrol (never needed to submit)."""
    return {name: _which(name, environ) for name in TOOLS + OPTIONAL_TOOLS}


def in_slurm_job(environ=None):
    return bool((_env(environ).get("SLURM_JOB_ID") or "").strip())


def is_agent_host_job(environ=None):
    """Whether this Slurm job is an interactive agent-host allocation.

    True inside a Slurm job named `sh_agent` (or `sh_agent-*`/`sh_agent_*`), or
    one with a pty (`SLURM_PTY_PORT`) and a Lor session (`LOR_SESSION_ID`)
    whose name is not an offloaded seat's `hanig-rev-*`. Offloaded seats run
    with `--export=NONE` under `env -i`, so they never carry LOR_SESSION_ID.
    """
    env = _env(environ)
    if not in_slurm_job(env):
        return False
    name = (env.get("SLURM_JOB_NAME") or "").strip().lower()
    if name == AGENT_HOST_JOB or name.startswith((AGENT_HOST_JOB + "-", AGENT_HOST_JOB + "_")):
        return True
    return bool((env.get("SLURM_PTY_PORT") or "").strip()
                and (env.get("LOR_SESSION_ID") or "").strip()
                and not name.startswith(SEAT_JOB_PREFIX))


def use_slurm_offload(environ=None):
    """Whether claude/codex CLI seats run as Slurm jobs instead of locally.

    auto: sbatch, squeue and scancel are on PATH and this process is either
    outside any Slurm job or inside an agent-host job (`is_agent_host_job`);
    inside any other Slurm job (batch work or an offloaded seat) it never
    offloads again. force: always (a missing tool then fails the seat closed
    instead of running the CLI on the login node). off: never.
    """
    mode = slurm_mode(environ)
    if mode == "off":
        return False
    if mode == "force":
        return True
    if in_slurm_job(environ) and not is_agent_host_job(environ):
        return False
    tools = slurm_tools(environ)
    return all(tools[name] for name in TOOLS)


def tools_problem(environ=None):
    """Why offload cannot submit, or None."""
    missing = [name for name, path in slurm_tools(environ).items()
               if path is None and name in TOOLS]
    if missing:
        return (f"Slurm offload requested ({MODE_ENV}={slurm_mode(environ)}) but "
                f"{', '.join(missing)} not found on PATH; set {MODE_ENV}=off to run "
                f"the CLI locally")
    return None


def stage_root(environ=None):
    """Shared-state staging root; never derived from TMPDIR or /tmp."""
    env = _env(environ)
    explicit = (setting(env, STAGE_ENV) or "").strip()
    if explicit:
        return Path(os.path.abspath(os.path.expanduser(explicit)))
    state = (env.get("XDG_STATE_HOME") or "").strip()
    if state and os.path.isabs(state):
        base = Path(state)
    else:
        home = (env.get("HOME") or "").strip() or os.path.expanduser("~")
        base = Path(home) / ".local" / "state"
    return base / "hanig-review-gate" / "slurm-seats"


def _private_dir(root):
    """Create a fresh 0700 seat directory owned by this user (exist_ok=False)."""
    os.makedirs(str(root), mode=0o700, exist_ok=True)
    seat = root / secrets.token_hex(16)
    os.mkdir(str(seat), 0o700)
    with _ACTIVE_LOCK:  # tracked until _release_stage() removes it
        _ACTIVE_STAGES.add(seat)
    try:
        os.chmod(str(seat), 0o700)
        st = os.lstat(str(seat))
        if (not os.path.isdir(str(seat)) or os.path.islink(str(seat))
                or st.st_uid != os.getuid() or st.st_mode & 0o077):
            raise OSError(f"staging directory {seat} is not a private 0700 directory")
    except OSError:
        _release_stage(seat)
        raise
    return seat


def submission_env(strip_key, environ=None):
    """Environment for sbatch/squeue/scancel: no LOR_* and no stripped key."""
    env = {k: v for k, v in _env(environ).items()
           if not k.upper().startswith("LOR_") and not strip_key(k)}
    if "PATH" in env:
        env["PATH"] = _sanitized_path(env["PATH"])
    return env


def runtime_env(managed_env, environ=None):
    """The only variables a staged CLI sees, besides TMPDIR set in the job."""
    env = _env(environ)
    out = {"HOME": (env.get("HOME") or os.path.expanduser("~")),
           "USER": env.get("USER") or env.get("LOGNAME") or "",
           "PATH": _sanitized_path(env.get("PATH", "")),
           "LANG": env.get("LANG") or "C.UTF-8",
           "LC_ALL": env.get("LC_ALL") or env.get("LANG") or "C.UTF-8"}
    for key, value in (managed_env or {}).items():
        if key not in MANAGED_CLI_ENV:
            raise ValueError(f"managed CLI override {key!r} is not allowed")
        out[key] = str(value)
    return out


def _render_argv(argv):
    return " ".join('"$work"' if arg == EMPTY_CWD else shlex.quote(str(arg))
                    for arg in argv)


def batch_script(cmd, stage, runtime, preflight=None):
    """Bash batch script for one seat. Carries no secret: argv and safe env only.

    preflight is (argv, gate_shell): argv runs first with its stdout in
    "$stage/preflight.txt"; gate_shell must exit 0 for the review CLI to run.
    """
    q = shlex.quote
    env_words = " ".join(f"{k}={q(v)}" for k, v in runtime.items())
    lines = [
        "#!/bin/bash",
        "# hanig-review-gate Slurm CLI seat (generated; holds no secret)",
        "set -u",
        "umask 077",
        f"stage={q(str(stage))}",
        'finish() { printf \'%s\\n\' "$1" > "$stage/rc.tmp" && mv -f "$stage/rc.tmp" "$stage/rc.txt"; }',
        'work=$(mktemp -d "${SLURM_TMPDIR:-${TMPDIR:-/tmp}}/hanig-review-seat.XXXXXXXX") || '
        '{ echo "hanig-review: mktemp failed on $(hostname)" > "$stage/stderr.txt"; finish 125; exit 125; }',
        "trap 'rm -rf \"$work\"' EXIT",
        "trap 'exit 143' TERM INT HUP",
        'cd "$work" || { finish 125; exit 125; }',
        # A symlinked temp root (macOS /tmp -> /private/tmp) would leave TMPDIR,
        # -C and the EXIT trap on the unresolved path while getcwd() reports the
        # physical one; canonicalize once so every consumer sees the same path.
        'work=$(pwd -P) || { finish 125; exit 125; }',
        f'run() {{ env -i {env_words} TMPDIR="$work" "$@"; }}',
    ]
    if preflight:
        pre_argv, gate = preflight
        lines += [
            f'run {_render_argv(pre_argv)} < /dev/null > "$stage/preflight.txt" 2> "$stage/preflight.err"',
            f"if ! ( {gate} ); then",
            '  echo "hanig-review: compute-node preflight refused the CLI login; '
            'review CLI not started" > "$stage/stderr.txt"',
            '  : > "$stage/stdout.txt"',
            f"  finish {PREFLIGHT_REFUSED_RC}",
            f"  exit {PREFLIGHT_REFUSED_RC}",
            "fi",
        ]
    lines += [
        f'run {_render_argv(cmd)} < "$stage/stdin.txt" > "$stage/stdout.txt" 2> "$stage/stderr.txt"',
        'finish "$?"',
        "",
    ]
    return "\n".join(lines)


def explicit_partition(environ=None):
    """Whether the site already chose a partition: [BROOD|HANIG]_REVIEW_SLURM_PARTITION
    or -p/--partition in [BROOD|HANIG]_REVIEW_SLURM_EXTRA_ARGS (never overridden)."""
    env = _env(environ)
    if (setting(env, "HANIG_REVIEW_SLURM_PARTITION") or "").strip():
        return True
    try:
        words = shlex.split(setting(env, "HANIG_REVIEW_SLURM_EXTRA_ARGS") or "")
    except ValueError:
        return True  # unparseable: leave the submission exactly as configured
    return any(w in ("-p", "--partition") or w.startswith("--partition=")
               or (w.startswith("-p") and not w.startswith("--")) for w in words)


def _sinfo_partitions(text):
    """(default partition or None, {partition: availability})."""
    default, states = None, {}
    for line in (text or "").splitlines():
        words = line.split()
        if len(words) < 2:
            continue
        name, state = words[0], words[1].strip().lower()
        if name.endswith("*"):
            name = name.rstrip("*")
            default = default or name
        if name:
            states[name] = state
    return default, states


def auto_batch_partition(tools, env, cwd, run_env=None):
    """`<default>_batch` when the default partition is interactive, else None.

    Read-only and bounded (PARTITION_PROBE_SECONDS in total): `sinfo -h -o
    "%P %a"` names the default partition (`name*`); its `<name>_batch` sibling
    must be listed `up`. It is chosen when this is an agent-host job on the
    default partition (or with no job partition), or when `scontrol show
    partition <default>` reports an interactive QoS. A missing tool, timeout,
    error or any other shape returns None (no --partition flag). env drives
    the decision; run_env (default: env without LOR_*) runs the commands.
    """
    if not tools.get("sinfo"):
        return None
    env = _env(env)
    if run_env is None:
        run_env = {k: v for k, v in env.items() if not k.upper().startswith("LOR_")}
    end = time.monotonic() + PARTITION_PROBE_SECONDS
    rc, out, _err = _call([tools["sinfo"], "-h", "-o", "%P %a"], run_env, cwd,
                          PARTITION_PROBE_SECONDS)
    if rc != 0:
        return None
    default, states = _sinfo_partitions(out)
    if not default:
        return None
    batch = f"{default}_batch"
    if states.get(batch) != "up":
        return None
    job_partition = (env.get("SLURM_JOB_PARTITION") or "").strip()
    if is_agent_host_job(env) and job_partition in ("", default):
        return batch
    remaining = end - time.monotonic()
    if not tools.get("scontrol") or remaining <= 0:
        return None
    rc, out, _err = _call([tools["scontrol"], "show", "partition", default], run_env, cwd,
                          remaining)
    if rc != 0:
        return None
    return batch if any("interact" in qos.lower() for qos in _QOS.findall(out or "")) else None


def sbatch_argv(sbatch, seat, stage, environ=None, auto_partition=None):
    """sbatch argv; auto_partition applies only when none is configured."""
    env = _env(environ)
    name = re.sub(r"[^A-Za-z0-9._-]", "-", str(seat or "seat"))[:40] or "seat"
    argv = [sbatch, "--parsable", "--export=NONE", f"--job-name=hanig-rev-{name}",
            f"--cpus-per-task={(setting(env, 'HANIG_REVIEW_SLURM_CPUS') or DEFAULTS['cpus']).strip()}",
            f"--mem={(setting(env, 'HANIG_REVIEW_SLURM_MEM') or DEFAULTS['mem']).strip()}",
            f"--time={(setting(env, 'HANIG_REVIEW_SLURM_TIME') or DEFAULTS['time']).strip()}",
            f"--chdir={stage}", f"--output={stage}/slurm.out",
            f"--error={stage}/slurm.out"]
    for var, flag in OPTION_ENV:
        value = (setting(env, var) or "").strip()
        if value:
            argv.append(f"{flag}={value}")
    if auto_partition and not explicit_partition(env):
        argv.append(f"--partition={auto_partition}")
    argv += shlex.split(setting(env, "HANIG_REVIEW_SLURM_EXTRA_ARGS") or "")
    argv.append(str(stage / "job.sh"))
    return argv


def _call(argv, env, cwd, seconds=COMMAND_TIMEOUT):
    """(returncode, stdout, stderr); a launch failure or timeout is rc None."""
    try:
        proc = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, env=env, cwd=cwd,
                              timeout=max(1.0, seconds))
    except (OSError, subprocess.SubprocessError) as exc:
        return None, "", f"{type(exc).__name__}: {exc}"
    return (proc.returncode, proc.stdout.decode("utf-8", "replace"),
            proc.stderr.decode("utf-8", "replace"))


def parse_job_id(stdout):
    first = (stdout or "").strip().splitlines()[:1]
    match = _JOB_ID.match(first[0].strip()) if first else None
    return match.group(1) if match else None


def _read(path):
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _job_state(tools, job_id, env, cwd):
    """'active', 'gone' or 'unknown'. Only an answered query reads as gone:
    a transient squeue failure never ends a running seat early."""
    rc, out, err = _call([tools["squeue"], "-h", "-j", job_id, "-o", "%T"], env, cwd)
    if rc == 0:
        return "active" if (out or "").strip() else "gone"
    if rc is not None and "invalid job id" in (err or "").lower():
        return "gone"
    return "unknown"


def _sacct_state(tools, job_id, env, cwd):
    if not tools.get("sacct"):
        return "unknown (sacct not on PATH)"
    _rc, out, _err = _call([tools["sacct"], "-n", "-X", "-P", "-j", job_id, "-o", "State"],
                           env, cwd)
    return (out or "").strip().splitlines()[0].strip() if (out or "").strip() else "unknown"


def _remove(stage):
    for _ in range(3):
        shutil.rmtree(str(stage), ignore_errors=True)
        if not os.path.lexists(str(stage)):
            return True
        time.sleep(0.2)
    return False


def _release_stage(stage):
    """Remove a staging directory, then stop tracking it."""
    try:
        return _remove(stage)
    finally:
        with _ACTIVE_LOCK:
            _ACTIVE_STAGES.discard(stage)


def _cancel(tools, job_id, env, cwd):
    _call([tools["scancel"], job_id], env, cwd, seconds=10.0)


def cancel_active_jobs():
    """scancel every submitted, unfinished seat and remove every tracked
    staging directory, including one whose sbatch has not returned yet.

    For paths that skip `finally` (the review watchdog's and SIGTERM handler's
    os._exit). Idempotent: each job and directory is claimed exactly once, and
    no failure escapes, so it is safe in a signal handler or shutdown path."""
    with _ACTIVE_LOCK:
        active = list(_ACTIVE.items())
        stages = set(_ACTIVE_STAGES)
        _ACTIVE.clear()
        _ACTIVE_STAGES.clear()
    for job_id, (tools, env, stage) in active:
        stages.add(stage)
        try:
            _cancel(tools, job_id, env, str(Path(stage).parent))
        except Exception:  # never let one job stop the rest of the cleanup
            pass
    for stage in stages:
        try:
            _remove(stage)
        except Exception:
            pass


def run_cli_via_slurm(cmd, stdin_text, *, seat, seconds, strip_key,
                      managed_env=None, preflight=None, preflight_check=None,
                      environ=None):
    """Run one CLI argv as a Slurm job; (rc, stdout, stderr, error, job_id).

    rc/stdout/stderr come from the CLI on the compute node, unchanged, so the
    caller parses them exactly as a local run. Any error returns rc None. The
    job is cancelled and the staging directory removed on every exit path.
    """
    env = _env(environ)
    if tools_problem(env):
        return None, None, None, tools_problem(env), None
    tools = slurm_tools(env)
    sub_env = submission_env(strip_key, env)
    end = time.monotonic() + float(seconds)
    stage = job_id = None
    try:
        try:
            runtime = runtime_env(managed_env, env)
            stage = _private_dir(stage_root(env))
            for name, text, mode in (("stdin.txt", stdin_text, 0o600),
                                     ("job.sh", batch_script(cmd, stage, runtime, preflight), 0o700)):
                fd = os.open(str(stage / name), os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(text)
        except (OSError, ValueError) as exc:
            return None, None, None, f"Slurm staging failed: {type(exc).__name__}: {exc}", None
        partition = (None if explicit_partition(env)
                     else auto_batch_partition(tools, env, str(stage), run_env=sub_env))
        rc, out, err = _call(sbatch_argv(tools["sbatch"], seat, stage, env, partition), sub_env,
                             str(stage), min(COMMAND_TIMEOUT, max(1.0, end - time.monotonic())))
        job_id = parse_job_id(out) if rc == 0 else None
        if not job_id:
            detail = (err or out or f"exit {rc}").strip()[:300]
            return None, None, None, f"sbatch submission failed: {detail}", None
        with _ACTIVE_LOCK:
            shutting_down = stage not in _ACTIVE_STAGES
            if not shutting_down:
                _ACTIVE[job_id] = (tools, sub_env, stage)
        if shutting_down:  # cancel_active_jobs() ran while sbatch was in flight
            return (None, None, None,
                    f"slurm job {job_id} cancelled: shutdown during submission", job_id)
        next_squeue, gone_at = 0.0, None
        while True:
            raw_rc = _read(stage / "rc.txt")
            if raw_rc is not None:
                break
            now = time.monotonic()
            if now >= end:
                return (None, None, None,
                        f"slurm job {job_id} timed out after {float(seconds):.1f}s", job_id)
            if gone_at is None and now >= next_squeue:
                next_squeue = now + SQUEUE_SECONDS
                if _job_state(tools, job_id, sub_env, str(stage)) == "gone":
                    gone_at = now
            if gone_at is not None and now - gone_at >= GRACE_SECONDS:
                return (None, None, None,
                        f"slurm job {job_id} ended ({_sacct_state(tools, job_id, sub_env, str(stage))}) "
                        f"without writing rc.txt", job_id)
            time.sleep(POLL_SECONDS)
        try:
            cli_rc = int(raw_rc.strip())
        except ValueError:
            return None, None, None, f"slurm job {job_id} wrote unreadable rc.txt", job_id
        stdout = _read(stage / "stdout.txt") or ""
        stderr = _read(stage / "stderr.txt") or ""
        if preflight is not None:
            why = (preflight_check(_read(stage / "preflight.txt") or "")
                   if preflight_check else None)
            if cli_rc == PREFLIGHT_REFUSED_RC or why:
                return (None, None, None,
                        f"slurm job {job_id}: {why or stderr.strip()[:300]}", job_id)
        return cli_rc, stdout, stderr, None, job_id
    finally:
        if job_id:
            with _ACTIVE_LOCK:
                _ACTIVE.pop(job_id, None)
            _cancel(tools, job_id, sub_env, str(stage.parent))
        if stage is not None:
            _release_stage(stage)
