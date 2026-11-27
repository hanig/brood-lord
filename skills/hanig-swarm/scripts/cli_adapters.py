"""Harness adapters for direct-CLI coding workers: `claude`, `codex`, `pi`.

Each adapter is data plus two pure functions: the headless argv for one turn
and the session id parsed from that turn's stdout. Nothing here spawns a
process, talks to Slurm, reads the coordinator state or names a model: the
model always arrives from routing data (plan, overrides, agents.json).
``cli_io`` is the execution substrate that consumes these specs.

Selection is EXPLICIT. Paseo already spells its providers ``claude/<model>``
and ``codex/<model>``, so a provider string alone never selects a direct
CLI: a unit, the plan's ``code_agent`` or ``swarm.py set-agent`` must say
``backend: claude|codex|pi``. Legacy plans keep the transport they had.

Credential policy (one per adapter, enforced by ``strip_name``):
  claude  subscription (OAuth) login only; API/billing keys withheld.
  codex   ChatGPT subscription login only; API keys withheld.
  pi      explicit opt-in. Provider ``openrouter`` needs OPENROUTER_API_KEY,
          which reaches the CLI only through a 0600 file the turn wrapper
          reads and unlinks before exec; never argv, script or record.

Python 3.8+, standard library only.
"""

import json
import re

CLAUDE = "claude"
CODEX = "codex"
PI = "pi"
CLI_BACKENDS = (CLAUDE, CODEX, PI)
OPENROUTER = "openrouter"
OPENROUTER_KEY = "OPENROUTER_API_KEY"

_CONTROL = re.compile(r"[\x00-\x20\x7f-\x9f]")
_SESSION = re.compile(r"[0-9A-Za-z][0-9A-Za-z_.:-]{7,127}")
# Names every direct CLI child loses, whatever its adapter. LOR_* is matched
# as a prefix: a Lor session token is authority over the coordinator's Lor
# session, never something a coding CLI needs.
_ALWAYS_STRIPPED = frozenset((
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY",
    "OPENROUTER_API_KEY"))
_ALWAYS_STRIPPED_PREFIXES = ("LOR_",)
_ADAPTER_STRIPPED = {
    # Settings that would silently move a subscription login onto metered
    # API billing or another endpoint.
    CLAUDE: frozenset(("ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK",
                       "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY")),
    CODEX: frozenset(("CODEX_API_KEY", "OPENAI_BASE_URL")),
    PI: frozenset(),
}


class AdapterError(ValueError):
    """An adapter, model or effort spelling that cannot be launched."""


def is_cli_backend(backend):
    return backend in CLI_BACKENDS


def valid_token(value):
    """A routing token safe as one argv value: no whitespace, no flag shape."""
    return (isinstance(value, str) and bool(value) and not value.startswith("-")
            and not _CONTROL.search(value))


def valid_model(value):
    return valid_token(value) and all(
        part and not part.startswith("-") for part in value.split("/"))


def valid_session_id(value):
    return isinstance(value, str) and _SESSION.fullmatch(value) is not None


def strip_name(adapter, name):
    """True when environment variable ``name`` must not reach ``adapter``."""
    if adapter not in CLI_BACKENDS:
        raise AdapterError(f"unknown CLI adapter {adapter!r}")
    return (name in _ALWAYS_STRIPPED or name.startswith(_ALWAYS_STRIPPED_PREFIXES)
            or name in _ADAPTER_STRIPPED[adapter])


def split_pi_model(model):
    """``<provider>/<model...>`` -> (provider, model). Raises AdapterError."""
    if not valid_model(model) or "/" not in model:
        raise AdapterError(
            f"pi model {model!r} must be <provider>/<model>, e.g. "
            f"{OPENROUTER}/<vendor>/<model> as `pi --list-models` names it")
    provider, _sep, rest = model.partition("/")
    return provider, rest


def parse_provider(backend, provider, model=None):
    """The CLI model a ``<backend>/<model>`` provider spelling selects.

    ``claude/<m>`` -> ``<m>``; ``codex/<m>`` -> ``<m>``; ``pi/<p>/<m>`` ->
    ``<p>/<m>``. A separate ``model`` overrides an embedded one. Raises
    AdapterError on anything that is not one well-formed model.
    """
    if backend not in CLI_BACKENDS:
        raise AdapterError(f"backend {backend!r} is not a direct CLI backend")
    embedded = ""
    if provider not in (None, ""):
        if not isinstance(provider, str) or not (
                provider == backend or provider.startswith(backend + "/")):
            raise AdapterError(
                f"backend {backend!r} needs provider {backend}/<model>, "
                f"not {provider!r}")
        embedded = provider[len(backend) + 1:]
    chosen = model if model not in (None, "") else embedded
    if not valid_model(chosen):
        raise AdapterError(
            f"backend {backend!r} names no valid model (provider {provider!r}, "
            f"model {model!r}); write {backend}/<model> with no whitespace, "
            f"empty segment or leading dash")
    if backend == PI:
        split_pi_model(chosen)
    return chosen


def author_identity(backend, model):
    """Maker-qualified identity for the review gate's --author exclusion.

    claude and codex keep ``<route>/<model>``, the same token Paseo attempts
    on those routes already record. pi is a harness, not a maker: an
    OpenRouter ``<vendor>/<model>`` is authored by that vendor, and a direct
    ``<provider>/<model>`` by that provider.
    """
    if backend == PI:
        provider, rest = split_pi_model(model)
        return rest if "/" in rest else f"{provider}/{rest}"
    return f"{backend}/{model}"


def key_requirement(adapter, model):
    """Env name whose value the adapter needs delivered, or None."""
    if adapter == PI and split_pi_model(model)[0] == OPENROUTER:
        return OPENROUTER_KEY
    return None


def preassigned_session(adapter):
    """Whether the coordinator chooses the session id before turn 1."""
    return adapter in (CLAUDE, PI)


def build_argv(adapter, binary, *, model, worktree, session_id=None,
               thinking=None, resume=False, session_dir=None):
    """Headless argv for one turn. The prompt is ALWAYS delivered on stdin."""
    if adapter not in CLI_BACKENDS:
        raise AdapterError(f"unknown CLI adapter {adapter!r}")
    if not valid_model(model):
        raise AdapterError(f"invalid {adapter} model {model!r}")
    if thinking is not None and not valid_token(thinking):
        raise AdapterError(f"invalid {adapter} effort {thinking!r}")
    if (resume or preassigned_session(adapter)) and not valid_session_id(session_id):
        raise AdapterError(f"invalid {adapter} session id {session_id!r}")
    if adapter == CLAUDE:
        argv = [binary, "-p", "--output-format", "json", "--model", model,
                "--permission-mode", "bypassPermissions"]
        if thinking:
            argv += ["--effort", thinking]
        argv += (["--resume", session_id] if resume
                 else ["--session-id", session_id])
        return argv
    if adapter == CODEX:
        common = ["--json", "-m", model,
                  "--dangerously-bypass-approvals-and-sandbox",
                  "--skip-git-repo-check"]
        if thinking:
            common += ["-c", f"model_reasoning_effort={thinking}"]
        if resume:
            # `exec resume` has no -C; the turn runs with cwd = worktree.
            return [binary, "exec", "resume", session_id] + common + ["-"]
        return [binary, "exec", "--json", "-C", str(worktree)] + common[1:] + ["-"]
    provider, pi_model = split_pi_model(model)
    if not session_dir:
        raise AdapterError("pi needs a --session-dir inside the attempt run dir")
    argv = [binary, "-p", "--mode", "json", "--session-dir", str(session_dir)]
    argv += (["--session", session_id] if resume
             else ["--session-id", session_id])
    argv += ["--provider", provider, "--model", pi_model,
             "--no-extensions", "--no-skills", "--no-mcp"]
    if thinking:
        argv += ["--thinking", thinking]
    return argv


def _json_lines(text):
    for line in (text or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            yield obj


def parse_session(adapter, stdout_text):
    """Session id a finished turn reports on stdout, or None. Never guesses."""
    if adapter == CLAUDE:
        candidates = []
        text = (stdout_text or "").strip()
        try:
            whole = json.loads(text) if text else None
        except ValueError:
            whole = None
        if isinstance(whole, dict):
            candidates.append(whole)
        elif isinstance(whole, list):
            candidates.extend(o for o in whole if isinstance(o, dict))
        candidates.extend(_json_lines(stdout_text))
        for obj in reversed(candidates):
            sid = obj.get("session_id")
            if valid_session_id(sid):
                return sid
        return None
    if adapter == CODEX:
        for obj in _json_lines(stdout_text):
            for key in ("thread_id", "session_id"):
                sid = obj.get(key)
                if valid_session_id(sid):
                    return sid
            msg = obj.get("msg") if isinstance(obj.get("msg"), dict) else {}
            sid = msg.get("session_id") or msg.get("thread_id")
            if valid_session_id(sid):
                return sid
        return None
    if adapter == PI:
        for obj in _json_lines(stdout_text):
            if obj.get("type") == "session" and valid_session_id(obj.get("id")):
                return obj["id"]
        return None
    raise AdapterError(f"unknown CLI adapter {adapter!r}")
