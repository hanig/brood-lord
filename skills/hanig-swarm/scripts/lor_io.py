"""Thin adapter over the `lor session` CLI for native Lor coding agents.

A code unit whose provider is ``lor/<canonical_model_id>`` runs as a Lor
subagent of the coordinator's own Lor session instead of a Paseo agent. This
module is the only place that spells the Lor CLI. It never imports swarm.py
and holds no judgment authority: ``unit.py check`` still judges, and every
answer this module cannot establish is ``unknown``, never ``idle``/``failed``.

FAIL CLOSED. There is deliberately no fallback from Lor to Paseo. A plan or
operator that selected a Lor model gets a Lor agent or an actionable refusal,
never a silently substituted model on another transport.

Checked CLI contracts (lor 0.3.35, 2026-11-26):
  lor session start --canonical-model-id M [--effort E] --working-directory D
                    --title T --task @-          -> JSON with session_id
  lor session list --parent-session-id P [--title-pattern RE]
                   [--page-size N] [--page-token T]
                                                 -> {"sessions": [...],
                                                     "next_page_token": ...}
  lor session stop --session-id S
  lor session send --recipient-session-id S --delivery queue --text @-

Python 3.8+, standard library only.
"""

import json
import os
import re
import shutil
import subprocess

import child_environment as CE
import paseo_io as PIO

LOR_BACKEND = "lor"
PASEO_BACKEND = "paseo"
BACKENDS = (PASEO_BACKEND, LOR_BACKEND)

# A session id is passed as a CLI argument value; refuse anything that could
# read as a flag or carry whitespace rather than normalizing it.
_SESSION_ID = re.compile(r"[0-9A-Za-z][0-9A-Za-z_-]{7,127}")
_CONTROL = re.compile(r"[\x00-\x20\x7f-\x9f]")
_AUTH_ERROR = re.compile(
    r"unauthenticated|unauthori[sz]ed|\b401\b|\b403\b|forbidden|"
    r"token (?:has |is )?expired|expired token|invalid token|"
    r"session token|refresh (?:proof|token)|not logged in|login required|"
    r"authentication (?:failed|required)|permission denied",
    re.I)
# Finish reasons that mean "the turn ended normally": lifecycle only. Like
# Paseo's `idle`, this never establishes completion; unit.py still requires
# declared outputs and a pushed produced change.
IDLE_FINISH_REASONS = frozenset(("stop", "end_turn", "completed", "complete"))
FAILED_FINISH_REASONS = frozenset((
    "error", "failed", "failure", "content_filter", "refusal", "aborted"))
CANCELLED_FINISH_REASONS = frozenset(("cancelled", "canceled", "stopped",
                                      "interrupted", "killed"))
LIST_PAGE_SIZE = 200
LIST_MAX_PAGES = 10
# Third-party API, billing and session secrets that a Lor CLI child never
# needs. LOR_* variables are kept: the CLI authenticates with them.
_STRIPPED_PREFIXES = ("OPENAI_", "OPENROUTER_", "ANTHROPIC_",
                      "SWARM_UNIT_", "SWARM_DEP_")
_STRIPPED_SUFFIXES = ("_API_KEY", "_SECRET_ACCESS_KEY")


class LorError(ValueError):
    """A Lor provider spelling or argument that cannot be dispatched."""


def is_lor_provider(provider_spec):
    """True iff ``provider_spec`` selects the native Lor backend."""
    return isinstance(provider_spec, str) and (
        provider_spec == LOR_BACKEND or provider_spec.startswith(LOR_BACKEND + "/"))


def _valid_model_id(value):
    if not isinstance(value, str) or not value:
        return False
    if value.startswith("-") or _CONTROL.search(value):
        return False
    return all(part and not part.startswith("-") for part in value.split("/"))


def parse_lor_model(provider_spec, model=None):
    """Return the canonical model id a Lor provider spelling selects.

    ``lor/openai/gpt-x`` -> ``openai/gpt-x``; ``lor`` with ``model`` ->
    ``model``. A separate ``model`` overrides an embedded one, matching how a
    unit's ``model`` overrides its provider's model on Paseo. Raises LorError
    for empty, whitespace, control-character or leading-dash ids.
    """
    if not is_lor_provider(provider_spec):
        raise LorError(f"provider {provider_spec!r} is not a lor/<model> spelling")
    embedded = provider_spec[len(LOR_BACKEND) + 1:] if provider_spec != LOR_BACKEND else ""
    chosen = model if model not in (None, "") else embedded
    if not _valid_model_id(chosen):
        raise LorError(
            f"provider {provider_spec!r} (model {model!r}) names no valid Lor "
            f"canonical model id; write lor/<canonical_model_id>, e.g. as "
            f"`lor model list` prints it, with no whitespace, empty segment "
            f"or leading dash")
    return chosen


def canonical_author_identity(provider_spec, model=None):
    """Author identity with the transport prefix removed.

    ``lor/<vendor>/<model>`` was written by ``<vendor>/<model>``: Lor is the
    transport, not the maker, so the review gate's maker exclusion must see
    the vendor. A single-segment id has no vendor and is returned bare.
    Non-Lor spellings are returned as provider/model unchanged.
    """
    if is_lor_provider(provider_spec):
        return parse_lor_model(provider_spec, model)
    if not isinstance(provider_spec, str) or not provider_spec:
        raise LorError(f"provider {provider_spec!r} is not a provider spelling")
    route, _sep, embedded = provider_spec.partition("/")
    chosen = model or embedded
    return f"{route}/{chosen}" if chosen else route


def build_lor_env(environ=None):
    """Environment for a `lor` CLI child: LOR_* kept, third-party secrets cut."""
    source = os.environ if environ is None else environ
    out = {}
    for name, value in source.items():
        if not name.startswith("LOR_"):
            if name in CE.DENIED_ENV_NAMES:
                continue
            if name.startswith(_STRIPPED_PREFIXES) or name.endswith(_STRIPPED_SUFFIXES):
                continue
        out[name] = value
    return out


def lor_binary(environ=None):
    env = build_lor_env(environ)
    return shutil.which("lor", path=env.get("PATH") or os.defpath)


def lor_availability(environ=None):
    """None when a Lor agent can be started from here, else an actionable refusal.

    Requires a non-empty LOR_SESSION_ID (the parent the subagent reports to)
    and a `lor` executable on the sanitized PATH. Never falls back to Paseo.
    """
    source = os.environ if environ is None else environ
    if not str(source.get("LOR_SESSION_ID") or "").strip():
        return ("LOR_SESSION_ID is unset or empty: a lor/<model> code agent is "
                "a subagent of the coordinator's Lor session, so it can only "
                "be dispatched by swarm.py running inside an active Lor "
                "session. Run the coordinator from a Lor session shell, or "
                "switch this work to Paseo with `swarm.py set-agent ... "
                "--backend paseo --model PROVIDER/MODEL`. No Paseo fallback "
                "was attempted.")
    if not lor_binary(source):
        return ("the `lor` CLI is not on PATH, so a lor/<model> code agent "
                "cannot be started or observed from this host. No Paseo "
                "fallback was attempted.")
    return None


def _auth_error(text):
    return bool(_AUTH_ERROR.search(text or ""))


def _lor_only_stripped_names(contained):
    """Names ``child_env()`` keeps that a `lor` CLI child must still not get.

    ``child_env()`` removes only the exact shared denylist. The Lor CLI
    authenticates with LOR_* alone, so the broader third-party prefixes and
    suffixes in ``build_lor_env`` are also withheld from it. Names only, never
    values: they are passed to ``env -u`` on the command line.
    """
    keep = build_lor_env(contained)
    return sorted(name for name in contained if name not in keep)


def _env_binary(path):
    found = shutil.which("env", path=path)
    if found:
        return found
    return "/usr/bin/env" if os.access("/usr/bin/env", os.X_OK) else None


def _run(args, stdin_text=None, timeout=60):
    """Run `lor ARGS`; return (rc or None, stdout, stderr).

    The child environment is ``child_env()`` passed directly, so the shared
    containment denylist applies (tests/test_environment_containment.py).
    The Lor-specific names it does not cover are unset with ``env -u`` before
    `lor` executes, so `lor` receives exactly ``build_lor_env(os.environ)``.
    If `env` cannot be found while such names are present, nothing is run.
    """
    contained = CE.child_env()
    path = contained.get("PATH") or os.defpath
    binary = shutil.which("lor", path=path)
    if not binary:
        return None, "", "lor CLI is not on PATH"
    argv = [binary] + list(args)
    stripped = _lor_only_stripped_names(contained)
    if stripped:
        env_binary = _env_binary(path)
        if not env_binary:
            return None, "", (f"lor {' '.join(args[:2])} was not run: `env` is "
                              f"unavailable to withhold non-Lor credentials")
        unset = []
        for name in stripped:
            unset += ["-u", name]
        argv = [env_binary] + unset + argv
    try:
        proc = subprocess.run(
            argv, input=stdin_text if stdin_text is not None else "",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, timeout=timeout, env=CE.child_env())
    except subprocess.TimeoutExpired:
        return None, "", f"lor {' '.join(args[:2])} timed out after {timeout}s"
    except OSError as exc:
        return None, "", f"lor {' '.join(args[:2])} could not run: {exc}"
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _first_json(text):
    return PIO.first_json_object(text)


def valid_session_id(value):
    return isinstance(value, str) and _SESSION_ID.fullmatch(value) is not None


def _detail(rc, out, err):
    text = " ".join((err or out or "").split())
    return (text or f"exit {rc}")[:300]


def start_session(*, canonical_model_id, working_directory, title, task,
                  effort=None, timeout=180):
    """Start a Lor subagent; return (session_id, error).

    No ``--tools`` (it inherits the parent's full sandbox tools), no
    ``--env`` and no ``--mode``: the subagent's context is the prompt.
    """
    if not _valid_model_id(canonical_model_id):
        return None, f"invalid Lor canonical model id {canonical_model_id!r}"
    if not isinstance(working_directory, str) or not os.path.isabs(working_directory):
        return None, f"Lor working directory must be absolute: {working_directory!r}"
    if effort is not None and (not isinstance(effort, str) or not effort
                               or effort.startswith("-") or _CONTROL.search(effort)):
        return None, f"invalid Lor effort {effort!r}"
    args = ["session", "start", "--canonical-model-id", canonical_model_id]
    if effort:
        args += ["--effort", effort]
    args += ["--working-directory", working_directory, "--title", str(title),
             "--task", "@-"]
    rc, out, err = _run(args, stdin_text=str(task), timeout=timeout)
    if rc != 0:
        return None, f"lor session start failed: {_detail(rc, out, err)}"
    rec = _first_json(out) or {}
    sid = rec.get("session_id") or rec.get("sessionId")
    if sid is None and isinstance(rec.get("session"), dict):
        sid = rec["session"].get("session_id") or rec["session"].get("sessionId")
    if not valid_session_id(sid):
        return None, (f"lor session start returned no usable session_id: "
                      f"{_detail(rc, out, err)}")
    return sid, None


def _list_children(parent_session_id, title_pattern=None, timeout=60):
    """Return (sessions, problem, auth_error) for one parent, bounded pages."""
    if not valid_session_id(parent_session_id):
        return None, f"invalid parent session id {parent_session_id!r}", False
    sessions, token = [], None
    for _page in range(LIST_MAX_PAGES):
        args = ["session", "list", "--parent-session-id", parent_session_id,
                "--page-size", str(LIST_PAGE_SIZE)]
        if title_pattern:
            args += ["--title-pattern", title_pattern]
        if token:
            args += ["--page-token", token]
        rc, out, err = _run(args, timeout=timeout)
        if rc != 0:
            detail = _detail(rc, out, err)
            return None, f"lor session list failed: {detail}", _auth_error(
                (err or "") + " " + (out or ""))
        rec = _first_json(out)
        if not isinstance(rec, dict) or not isinstance(rec.get("sessions"), list):
            return None, "lor session list returned no readable sessions list", False
        sessions.extend(s for s in rec["sessions"] if isinstance(s, dict))
        token = rec.get("next_page_token")
        if not token:
            return sessions, None, False
    return None, (f"lor session list did not finish within {LIST_MAX_PAGES} "
                  f"pages; the session cannot be established"), False


def _parent_for(parent_session_id, environ=None):
    if parent_session_id:
        return parent_session_id
    source = os.environ if environ is None else environ
    return str(source.get("LOR_SESSION_ID") or "").strip() or None


def _unknown(detail, auth_error=False):
    return {"state": "unknown", "finish_reason": None, "stopped_at": None,
            "working_directory": None, "canonical_model_id": None,
            "detail": detail, "auth_error": bool(auth_error)}


def classify_session(rec):
    """Map one `lor session list` record to a lifecycle observation."""
    sandbox = rec.get("sandbox") if isinstance(rec.get("sandbox"), dict) else {}
    result = {
        "finish_reason": rec.get("last_finish_reason"),
        "stopped_at": rec.get("stopped_at"),
        "working_directory": sandbox.get("working_directory"),
        "canonical_model_id": rec.get("canonical_model_id"),
        "auth_error": False,
    }
    running = rec.get("running")
    reason = str(rec.get("last_finish_reason") or "").strip().lower()
    if running is True:
        result.update(state="running", detail="lor session is running a turn")
    elif running is not False:
        result.update(state="unknown",
                      detail=f"lor session reported no running flag ({running!r})")
    elif rec.get("stopped_at") or reason in CANCELLED_FINISH_REASONS:
        result.update(state="cancelled",
                      detail=f"lor session was stopped at {rec.get('stopped_at')!r}"
                             f" (finish reason {reason or None!r})")
    elif reason in FAILED_FINISH_REASONS:
        result.update(state="failed",
                      detail=f"lor session turn finished with {reason!r}")
    elif reason in IDLE_FINISH_REASONS:
        result.update(state="idle",
                      detail=f"lor session turn finished with {reason!r}")
    else:
        result.update(state="unknown",
                      detail=f"lor session is not running and reported "
                             f"unrecognised finish reason {reason or None!r}; "
                             f"not guessing whether that is terminal")
    return result


def poll_session(session_id, *, parent_session_id=None, timeout=60, environ=None):
    """Observe one Lor subagent. Anything not established is ``unknown``."""
    if not valid_session_id(session_id):
        return _unknown(f"invalid Lor session id {session_id!r}")
    parent = _parent_for(parent_session_id, environ)
    if not parent:
        return _unknown("no parent Lor session is recorded and LOR_SESSION_ID is "
                        "unset, so the subagent cannot be listed")
    sessions, problem, auth = _list_children(parent, timeout=timeout)
    if problem:
        return _unknown(problem, auth)
    matches = [s for s in sessions if s.get("session_id") == session_id]
    if len(matches) != 1:
        return _unknown(f"Lor session {session_id} is not listed under parent "
                        f"{parent} ({len(matches)} match(es))")
    return classify_session(matches[0])


def find_session_by_title(title, *, parent_session_id=None, timeout=60,
                          environ=None):
    """Return (session_record or None, problem). problem means UNKNOWN."""
    parent = _parent_for(parent_session_id, environ)
    if not parent:
        return None, "no parent Lor session to search"
    pattern = "^" + re.escape(str(title)) + "$"
    sessions, problem, _auth = _list_children(
        parent, title_pattern=pattern, timeout=timeout)
    if problem:
        return None, problem
    matches = [s for s in sessions if s.get("title") == title
               and valid_session_id(s.get("session_id"))]
    if len(matches) > 1:
        return None, (f"{len(matches)} Lor sessions are titled {title!r}; "
                      f"refusing to guess which one owns the attempt")
    return (matches[0] if matches else None), None


def stop_session(session_id, timeout=60):
    """Stop a Lor subagent; return (ok, detail). Idempotent on the CLI side."""
    if not valid_session_id(session_id):
        return False, f"invalid Lor session id {session_id!r}"
    rc, out, err = _run(["session", "stop", "--session-id", session_id],
                        timeout=timeout)
    if rc == 0:
        return True, "stopped"
    return False, f"lor session stop failed: {_detail(rc, out, err)}"


def send_continuation(session_id, text, timeout=60):
    """Queue a continuation message; return (ok, detail)."""
    if not valid_session_id(session_id):
        return False, f"invalid Lor session id {session_id!r}"
    rc, out, err = _run(["session", "send", "--recipient-session-id", session_id,
                         "--delivery", "queue", "--text", "@-"],
                        stdin_text=str(text), timeout=timeout)
    if rc == 0:
        return True, "queued"
    return False, f"lor session send failed: {_detail(rc, out, err)}"


# --- unit.py glue ---------------------------------------------------------
# unit.py is size-guarded (tests/test_swarm.py) and judges; the backend rules
# it consumes live here, beside the transport they describe.

def add_bind_arguments(parser):
    """`unit.py bind` flags that freeze a code attempt's lifecycle backend."""
    parser.add_argument("--backend", choices=BACKENDS, default=None,
                        help="kind=code lifecycle backend; absent reads as paseo")
    parser.add_argument("--parent-session-id", default=None,
                        help="the Lor session that started a --backend lor agent")


def code_binding(kind, job_id, backend, parent_session_id):
    """Validate a bind's id/backend; return the unit.json fields to record.

    Exits (SystemExit, as unit.py's other refusals do) on an invalid shape.
    A kind=code id without --backend keeps the original Paseo rule and
    records nothing new, so a legacy bind is byte-for-byte what it was.
    """
    if kind != "code":
        if backend is not None or parent_session_id is not None:
            raise SystemExit("error: --backend and --parent-session-id apply "
                             "only to kind=code attempts")
        return {}
    if backend == LOR_BACKEND:
        if not valid_session_id(str(job_id)):
            raise SystemExit(f"error: implausible lor session id {job_id!r}. "
                             f"Pass the session_id `lor session start` printed.")
        if not valid_session_id(parent_session_id):
            raise SystemExit("error: a lor attempt must be bound with the "
                             "--parent-session-id of the Lor session that "
                             "started it")
        return {"backend": LOR_BACKEND, "parent_session_id": parent_session_id}
    if parent_session_id is not None:
        raise SystemExit("error: --parent-session-id applies only to --backend lor")
    if not re.fullmatch(r"[0-9a-fA-F-]{8,64}", str(job_id)):
        raise SystemExit(f"error: implausible agent id {job_id!r} for a "
                         f"kind=code unit. Pass the agent id paseo printed, as "
                         f"shown by `paseo ls`.")
    return {"backend": backend} if backend is not None else {}


def code_lifecycle(spec, notes, environ=None):
    """Lor half of unit.py's code predicate: (verdict or None, status).

    (None, None) for an attempt not bound as ``backend: lor`` -- unit.py then
    reads its lifecycle from Paseo exactly as before.

    A Lor observation that cannot be established -- CLI, transport or token
    failure, or a session missing from its parent's listing -- is INCOMPLETE
    (retryable), never DONE or FAILED. A turn that ended normally returns
    (None, "idle"): lifecycle only, so the shared output and repository
    checks in unit.py still decide, exactly as for a Paseo `idle`.
    """
    agent = spec.get("job_id")
    if spec.get("backend") != LOR_BACKEND or not agent:
        return None, None
    obs = poll_session(str(agent), parent_session_id=spec.get("parent_session_id"),
                       environ=environ)
    state = obs.get("state")
    if state == "running":
        notes.append(f"lor session {agent}: still working.")
        return "RUNNING", "running"
    if state in ("failed", "cancelled"):
        notes.append(f"lor session {agent} is {state}: {obs.get('detail')}. "
                     f"See `lor session inspect --session-id {agent}`.")
        return "FAILED", state
    if state == "idle":
        return None, "idle"
    hint = (" The Lor session token looks expired or rejected; re-run from a "
            "live Lor session." if obs.get("auth_error") else "")
    notes.append(f"lor session {agent} could not be observed: "
                 f"{obs.get('detail')}.{hint} Not judged; retry later.")
    return "INCOMPLETE", "unknown"
