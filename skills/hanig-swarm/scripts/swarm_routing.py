"""Data-loaded agent routing; no coordinator or network dependency.

Python 3.8+, standard library only.
"""

from collections import OrderedDict
import hashlib
import json
import os
import re
import sys
from pathlib import Path

import cli_adapters as CA
import lor_io as LIO

_HERE = Path(__file__).resolve().parent

# What a `code` unit runs unless it says otherwise, and the reasoning effort
# each model gets, live in ../agents.json. Models are data: this file names
# none. The file ships inside the skill so a cluster install carries it.
# A missing or malformed file stops the coordinator at import rather than
# dispatching a guessed default; there is deliberately no fallback model.
AGENTS_FILE = _HERE.parent / "agents.json"


def _routing_token(value):
    """A non-empty string with no whitespace anywhere: an id paseo accepts."""
    return (isinstance(value, str) and bool(value)
            and not any(c.isspace() for c in value))


def _routing_model(value):
    """PROVIDER/MODEL[/...]: every slash-separated segment a routing token."""
    return (_routing_token(value) and len(value.split("/")) >= 2
            and all(value.split("/")))


VALID_ORCHESTRATORS = frozenset(("claude", "codex", "antigravity"))


def _orchestrator_token(value):
    """A supported orchestrator host identity (claude, codex, antigravity)."""
    return isinstance(value, str) and value in VALID_ORCHESTRATORS


def _model_matches_substrings(model, substrings):
    if not isinstance(model, str) or not model:
        return False
    for sub in substrings:
        if re.search(r"(?:^|[^a-z])" + re.escape(sub) + r"(?:$|[^a-z])",
                     model, re.I):
            return True
    return False


def _provider_forbidden_for_rule(provider, rule):
    if not isinstance(rule, dict):
        return False
    route, _slash, model = provider.partition("/")
    routes = {r.casefold() for r in rule.get("routes", ())}
    if route.casefold() in routes and not model:
        return True
    return _model_matches_substrings(model, rule.get("model_substrings", ()))


def _parse_agent_routing(path=AGENTS_FILE):
    """Read and check agents.json. Raises SystemExit naming the defect.

    Every value is checked the same way and returned exactly as written:
    nothing is stripped or normalized, because these strings go to paseo
    verbatim and a padded id would dispatch as an ERRORED agent.
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit("swarm: cannot read agent routing %s: %s" % (path, exc))
    default = data.get("default") if isinstance(data, dict) else None
    table = data.get("thinking_by_model") if isinstance(data, dict) else None
    provider = default.get("provider") if isinstance(default, dict) else None
    thinking = default.get("thinking") if isinstance(default, dict) else None
    if not _routing_model(provider):
        raise SystemExit("swarm: %s default.provider must be a PROVIDER/MODEL "
                         "string without whitespace or empty segments" % path)
    if not _routing_token(thinking):
        raise SystemExit("swarm: %s default.thinking must be a non-empty "
                         "thinking id without whitespace" % path)
    # An empty table is refused, not read as "every model at the default":
    # a table emptied by a bad edit would silently downgrade measured efforts.
    if not (isinstance(table, dict) and table and all(
            _routing_model(k) and _routing_token(v) for k, v in table.items())):
        raise SystemExit("swarm: %s thinking_by_model must be a non-empty map "
                         "of PROVIDER/MODEL strings to thinking ids, neither "
                         "with whitespace" % path)
    forbid = {}
    if "forbid_when_orchestrator" in data:
        raw_forbid = data.get("forbid_when_orchestrator")
        if not (isinstance(raw_forbid, dict) and raw_forbid):
            raise SystemExit("swarm: %s forbid_when_orchestrator must be a "
                             "non-empty object" % path)
        for orch, rule in raw_forbid.items():
            if not _orchestrator_token(orch) or not isinstance(rule, dict):
                raise SystemExit("swarm: %s forbid_when_orchestrator[%r] must "
                                 "map an orchestrator token to an object"
                                 % (path, orch))
            routes = rule.get("routes")
            subs = rule.get("model_substrings")
            if not (isinstance(routes, list)
                    and all(_routing_token(r) and "/" not in r for r in routes)):
                raise SystemExit("swarm: %s forbid_when_orchestrator[%r].routes "
                                 "must be a list of tokens" % (path, orch))
            if not (isinstance(subs, list)
                    and all(_routing_token(s) for s in subs)):
                raise SystemExit("swarm: %s forbid_when_orchestrator[%r]."
                                 "model_substrings must be a list of tokens"
                                 % (path, orch))
            if not routes and not subs:
                raise SystemExit("swarm: %s forbid_when_orchestrator[%r] must "
                                 "declare routes or model_substrings"
                                 % (path, orch))
            forbid[orch] = {"routes": list(routes),
                            "model_substrings": list(subs)}
    by_orch = {}
    if "default_by_orchestrator" in data:
        raw_orch = data.get("default_by_orchestrator")
        if not (isinstance(raw_orch, dict) and raw_orch):
            raise SystemExit("swarm: %s default_by_orchestrator must be a "
                             "non-empty object" % path)
        for orch, seat in raw_orch.items():
            if not _orchestrator_token(orch) or not isinstance(seat, dict):
                raise SystemExit("swarm: %s default_by_orchestrator[%r] must "
                                 "map an orchestrator token to an object"
                                 % (path, orch))
            op = seat.get("provider")
            ot = seat.get("thinking")
            if not _routing_model(op):
                raise SystemExit("swarm: %s default_by_orchestrator[%r].provider "
                                 "must be a PROVIDER/MODEL string without "
                                 "whitespace or empty segments" % (path, orch))
            if not _routing_token(ot):
                raise SystemExit("swarm: %s default_by_orchestrator[%r].thinking "
                                 "must be a non-empty thinking id without "
                                 "whitespace" % (path, orch))
            if _provider_forbidden_for_rule(op, forbid.get(orch)):
                raise SystemExit("swarm: %s default_by_orchestrator[%r] provider "
                                 "%r violates forbid_when_orchestrator"
                                 % (path, orch, op))
            by_orch[orch] = {"provider": op, "thinking": ot}
    return provider, thinking, dict(table), by_orch, forbid


def load_agent_routing(path=AGENTS_FILE):
    """Read and check agents.json. Returns (provider, thinking, thinking_by_model)."""
    provider, thinking, table, _by_orch, _forbid = _parse_agent_routing(path)
    return provider, thinking, table


# A unit overrides any of it with `provider`, `model` or `thinking`. Setting
# `thinking` to null or "" turns the flag off entirely for a provider that has
# no such option.
(DEFAULT_AGENT_PROVIDER, DEFAULT_AGENT_THINKING, THINKING_BY_MODEL,
 ORCHESTRATOR_DEFAULTS, FORBID_WHEN_ORCHESTRATOR) = _parse_agent_routing()
# The shipped pin. apply_agent_resolution starts from it every time, so a
# resolution applied for one plan never outlives that plan in this process.
PINNED_AGENT_PROVIDER = DEFAULT_AGENT_PROVIDER
PINNED_AGENT_THINKING = DEFAULT_AGENT_THINKING
PINNED_THINKING_BY_MODEL = dict(THINKING_BY_MODEL)
ACTIVE_ORCHESTRATOR = None
_PLAN_RESOLUTIONS = OrderedDict()
_MAX_PLAN_RESOLUTIONS = 128


def _plan_fingerprint(plan):
    """Lightweight content digest of a plan dict; holds zero reference to plan."""
    try:
        raw = json.dumps(plan, sort_keys=True, default=repr)
    except Exception:
        raw = repr(plan)
    return hashlib.sha256(raw.encode("utf-8", "surrogateescape")).hexdigest()


def _plan_orchestrator(plan_path=None, plan=None):
    """Exact declared orchestrator from plan dict or plan file, or None.

    Never normalizes, strips, or reads ambient session env vars: an absent or
    unrecognized orchestrator fails closed to the default seat, and plan
    validation refuses invalid declarations loudly.
    """
    data = plan
    if data is None and plan_path is not None:
        try:
            loaded = json.loads(Path(plan_path).read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, ValueError):
            data = None
    if not isinstance(data, dict):
        return None
    orch = data.get("orchestrator")
    return orch if isinstance(orch, str) and orch in VALID_ORCHESTRATORS else None


def _plan_routing_entry(plan):
    """Return (provider, thinking, table) for plan without retaining plan."""
    orch = _plan_orchestrator(plan=plan)
    entry = _PLAN_RESOLUTIONS.get(_plan_fingerprint(plan))
    if entry is not None and entry[0] == orch:
        return entry[1], entry[2], entry[3]
    if orch in ORCHESTRATOR_DEFAULTS:
        seat = ORCHESTRATOR_DEFAULTS[orch]
        return seat["provider"], seat["thinking"], PINNED_THINKING_BY_MODEL
    return PINNED_AGENT_PROVIDER, PINNED_AGENT_THINKING, PINNED_THINKING_BY_MODEL


def effective_default_provider(plan=None):
    """Default code-agent provider for plan, without leaking another plan's state."""
    if isinstance(plan, dict):
        return _plan_routing_entry(plan)[0]
    return DEFAULT_AGENT_PROVIDER


def forbid_unit_provider_problem(plan, u, rules=None):
    """Return refusal message when a code unit violates forbid_when_orchestrator."""
    if not isinstance(plan, dict) or not isinstance(u, dict) or u.get("kind") != "code":
        return None
    orch = plan.get("orchestrator")
    if not isinstance(orch, str):
        return None
    rule_map = FORBID_WHEN_ORCHESTRATOR if rules is None else rules
    rule = rule_map.get(orch) if isinstance(rule_map, dict) else None
    if not isinstance(rule, dict):
        return None
    raw_prov = u.get("provider")
    raw_model = u.get("model")
    if raw_prov is not None and str(raw_prov).strip():
        prov_text = str(raw_prov).strip()
        route, _slash, embedded = prov_text.partition("/")
    else:
        default_prov = effective_default_provider(plan)
        route, _slash, embedded = default_prov.partition("/")
    explicit_model = (str(raw_model).strip()
                      if raw_model is not None and str(raw_model).strip()
                      else "")
    models = [m for m in (embedded.strip(), explicit_model) if m]
    routes = {r.casefold() for r in rule.get("routes", ())}
    subs = rule.get("model_substrings", ())
    uid = u.get("id", "?")
    if route.casefold() in routes and not models:
        return (f"unit {uid!r} declares bare provider {raw_prov!r} without a "
                f"non-forbidden model while plan orchestrator is {orch!r}; "
                f"omit 'provider' to use the default or declare an explicit "
                f"non-matching model")
    for model in models:
        if _model_matches_substrings(model, subs):
            return (f"unit {uid!r} selects {route}/{model} while plan "
                    f"orchestrator is {orch!r}; this model line may be used as "
                    f"code writer only when {orch!r} is not the orchestrator")
    return None


def _model_family():
    """The review gate's network-free model_family module, or None.

    Loaded by file path from the sibling hanig-review-gate skill (or
    HANIG_REVIEW_GATE_DIR), never through review.py or committee.py, which
    are network-capable and must not enter this process. Absent means no
    resolution: the pins in agents.json apply.
    """
    import importlib.util
    root = os.environ.get("HANIG_REVIEW_GATE_DIR") or str(
        _HERE.parent.parent / "hanig-review-gate")
    path = Path(root) / "scripts" / "model_family.py"
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("hanig_model_family", str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def apply_agent_resolution(plan_path, plan=None):
    """Adopt this project's resolved code-agent default, if its snapshot is valid.

    Called once when a command loads a plan, so the snapshot is looked up
    from the plan's directory upward to its project. When the plan declares
    an orchestrator listed in agents.json default_by_orchestrator, that seat's
    pin and snapshot override apply; otherwise the default seat applies
    (failing closed when orchestrator is absent or unknown). A unit's own
    provider, model or thinking still wins. The resolved model inherits its
    seat's pinned thinking level unless agents.json names one for it.
    """
    global DEFAULT_AGENT_PROVIDER, DEFAULT_AGENT_THINKING, ACTIVE_ORCHESTRATOR
    DEFAULT_AGENT_PROVIDER = PINNED_AGENT_PROVIDER
    DEFAULT_AGENT_THINKING = PINNED_AGENT_THINKING
    ACTIVE_ORCHESTRATOR = None
    # In place, so every reference to the table sees the reset.
    THINKING_BY_MODEL.clear()
    THINKING_BY_MODEL.update(PINNED_THINKING_BY_MODEL)
    plan_data = plan if isinstance(plan, dict) else None
    if plan_data is None and plan_path is not None:
        try:
            loaded = json.loads(Path(plan_path).read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                plan_data = loaded
        except (OSError, ValueError):
            plan_data = None
    orch = _plan_orchestrator(plan_path=plan_path, plan=plan_data)
    ACTIVE_ORCHESTRATOR = orch
    routing = None
    try:
        routing = json.loads(AGENTS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        routing = None
    seat = None
    if isinstance(routing, dict):
        by_orch = routing.get("default_by_orchestrator")
        if orch and isinstance(by_orch, dict) and isinstance(by_orch.get(orch), dict):
            seat = by_orch[orch]
        elif isinstance(routing.get("default"), dict):
            seat = routing["default"]
    elif orch in ORCHESTRATOR_DEFAULTS:
        seat = ORCHESTRATOR_DEFAULTS[orch]
    if isinstance(seat, dict) and _routing_model(seat.get("provider")):
        DEFAULT_AGENT_PROVIDER = seat["provider"]
    if isinstance(seat, dict) and _routing_token(seat.get("thinking")):
        DEFAULT_AGENT_THINKING = seat["thinking"]
    base_provider = DEFAULT_AGENT_PROVIDER

    def _record_plan(resolved):
        if isinstance(plan_data, dict):
            key = _plan_fingerprint(plan_data)
            _PLAN_RESOLUTIONS.pop(key, None)
            _PLAN_RESOLUTIONS[key] = (
                orch, resolved, DEFAULT_AGENT_THINKING, dict(THINKING_BY_MODEL))
            while len(_PLAN_RESOLUTIONS) > _MAX_PLAN_RESOLUTIONS:
                _PLAN_RESOLUTIONS.popitem(last=False)
        return resolved

    try:
        family = _model_family()
        if family is None or not isinstance(routing, dict):
            return _record_plan(DEFAULT_AGENT_PROVIDER)
        digests = family.config_digests(
            Path(family.__file__).resolve().parent.parent / "reviewers.json",
            AGENTS_FILE)
        if hasattr(family, "resolved_orchestrator_agent"):
            provider, note = family.resolved_orchestrator_agent(
                orch, routing, digests, start=Path(plan_path).resolve().parent)
        else:
            provider, note = family.resolved_agent_default(
                seat or (routing.get("default") or {}),
                routing.get("thinking_by_model") or {},
                digests, start=Path(plan_path).resolve().parent)
    except Exception as exc:
        # Resolution never stops validate or run: anything wrong here leaves
        # the shipped pin in force.
        print("swarm: routing: snapshot not applied, using the pin: %s: %s"
              % (type(exc).__name__, exc), file=sys.stderr)
        return _record_plan(DEFAULT_AGENT_PROVIDER)
    if provider and note and provider != base_provider:
        THINKING_BY_MODEL[provider] = family.agent_thinking(
            seat or (routing.get("default") or {}),
            routing.get("thinking_by_model") or {},
            provider)
        DEFAULT_AGENT_PROVIDER = provider
        print("swarm: routing: " + note, file=sys.stderr)
    return _record_plan(DEFAULT_AGENT_PROVIDER)


def default_thinking_for(u, plan=None):
    """The reasoning effort one unit gets when it declares none.

    Keyed on the model that will actually run, which is the provider string
    unless the unit names a `model` separately -- both spellings reach paseo
    the same way, so both have to resolve here or the mapping would apply to
    one plan and not its equivalent. An unrecognised model falls back rather
    than refusing: a new model on the roster should dispatch at a sane effort,
    and the fallback is the level the two strongest models use. When ``plan``
    is supplied, uses that plan's own resolved provider, default thinking, and
    thinking table without cross-plan leakage.
    """
    if isinstance(plan, dict):
        default_provider, default_thinking, table = _plan_routing_entry(plan)
    else:
        default_provider, default_thinking, table = (
            DEFAULT_AGENT_PROVIDER, DEFAULT_AGENT_THINKING, THINKING_BY_MODEL)
    provider = str(u.get("provider") or default_provider)
    model = u.get("model")
    key = "%s/%s" % (provider.split("/", 1)[0], model) if model else provider
    return table.get(key, default_thinking)



# --- effective code agent: backend, provider, model, thinking, mode --------
# A code attempt runs on exactly one backend, frozen when it is launched:
# "paseo" (a Paseo agent), "lor" (a native Lor subagent selected by
# provider "lor/<canonical_model_id>"), or a direct CLI worker "claude",
# "codex" or "pi" (cli_io). A direct CLI is selected ONLY by an explicit
# backend: Paseo already spells its providers claude/<m> and codex/<m>, and a
# legacy plan must keep the transport it had. Which one a NEW attempt gets is
# resolved here, most specific first:
#   1. per-unit runtime override   (STATE_DIR/agent-overrides.json units[uid])
#   2. the unit's own backend/provider/model in plan.json (an explicit pin)
#   3. project runtime override     (agent-overrides.json project)
#   4. the plan's top-level "code_agent"
#   5. agents.json default_by_orchestrator / default
# Runtime overrides never edit plan.json, so the plan digest stays put.
AGENT_SPEC_KEYS = ("backend", "provider", "model", "thinking", "mode")
# Modes that would make a Lor or direct-CLI coding agent unable to write.
# Neither honors a restrictive Paseo mode (Lor has no permission-mode flag;
# the direct CLIs run with bypassed approvals in a coordinator worktree), so
# such a declaration is refused rather than silently ignored.
LOR_REFUSED_MODES = frozenset(("plan", "read-only", "readonly", "read_only",
                               "readOnly"))
NO_THINKING = ("none", "null", "")


def normalize_agent_spec(spec, where="code_agent"):
    """Return (normalized spec, problem). Never guesses a missing model.

    Accepts {"provider": "lor/<id>"|"route/model", ...} or
    {"backend": "lor"|"paseo"|"claude"|"codex"|"pi", "provider"?: ...,
    "model"?: ...}. The result
    always names one backend and one complete provider string; ``thinking``
    and ``mode`` are carried only when declared (``thinking: null`` is an
    explicit request to send no effort flag).
    """
    if not isinstance(spec, dict):
        return None, f"{where} must be a JSON object"
    unknown = sorted(set(spec) - set(AGENT_SPEC_KEYS) - {"_comment"})
    if unknown:
        return None, (f"{where} has unknown field(s) {', '.join(unknown)}; "
                      f"allowed: {', '.join(AGENT_SPEC_KEYS)}")
    backend = spec.get("backend")
    provider = spec.get("provider")
    model = spec.get("model")
    if backend is not None and backend not in LIO.BACKENDS:
        return None, (f"{where}.backend {backend!r} must be one of "
                      f"{', '.join(LIO.BACKENDS)}")
    for key, value in (("provider", provider), ("model", model)):
        if value is not None and not _routing_token(value):
            return None, (f"{where}.{key} {value!r} must be a non-empty "
                          f"string without whitespace")
    if provider is None and model is None:
        return None, f"{where} names neither a provider nor a model"
    if CA.is_cli_backend(backend):
        try:
            cli_model = CA.parse_provider(backend, provider, model)
        except CA.AdapterError as exc:
            return None, f"{where}: {exc}"
        out = {"backend": backend, "provider": backend + "/" + cli_model}
        return _spec_options(spec, out, where)
    if backend == LIO.LOR_BACKEND and provider is not None and not LIO.is_lor_provider(provider):
        return None, (f"{where} declares backend 'lor' with non-Lor provider "
                      f"{provider!r}; write provider lor/<canonical_model_id> "
                      f"or backend 'lor' with model <canonical_model_id>")
    if backend == LIO.LOR_BACKEND and provider is None:
        provider = LIO.LOR_BACKEND
    if provider is not None and LIO.is_lor_provider(provider):
        if backend == LIO.PASEO_BACKEND:
            return None, (f"{where} declares backend 'paseo' with Lor provider "
                          f"{provider!r}")
        try:
            canonical = LIO.parse_lor_model(provider, model)
        except LIO.LorError as exc:
            return None, f"{where}: {exc}"
        out = {"backend": LIO.LOR_BACKEND,
               "provider": LIO.LOR_BACKEND + "/" + canonical}
    else:
        if provider is None:
            return None, (f"{where} names model {model!r} without a provider; "
                          f"write provider PROVIDER/MODEL or backend 'lor'")
        route, _sep, embedded = provider.partition("/")
        chosen = model or embedded
        if not route or not chosen or chosen.startswith("-") or route.startswith("-"):
            return None, (f"{where} provider {provider!r} (model {model!r}) "
                          f"does not name one PROVIDER/MODEL; a runtime agent "
                          f"must say which model runs")
        out = {"backend": LIO.PASEO_BACKEND, "provider": route + "/" + chosen}
    return _spec_options(spec, out, where)


def _spec_options(spec, out, where):
    """Carry the declared thinking/mode onto a normalized spec."""
    if "thinking" in spec:
        thinking = spec["thinking"]
        if thinking is None or (isinstance(thinking, str)
                                and thinking.strip().lower() in NO_THINKING):
            out["thinking"] = None
        elif _routing_token(thinking) and not thinking.startswith("-"):
            out["thinking"] = thinking
        else:
            return None, f"{where}.thinking {thinking!r} is not an effort id"
    if "mode" in spec:
        mode = spec["mode"]
        if not _routing_token(mode) or mode.startswith("-"):
            return None, (f"{where}.mode {mode!r} must be a non-empty mode "
                          f"id without whitespace")
        if out["backend"] in LIO.NATIVE_BACKENDS and mode in LOR_REFUSED_MODES:
            return None, lor_mode_refusal(where, mode, out["backend"])
        out["mode"] = mode
    return out, None


def lor_mode_refusal(where, mode, backend=LIO.LOR_BACKEND):
    if backend != LIO.LOR_BACKEND:
        return (f"{where} declares mode {mode!r} for a direct {backend} CLI "
                f"coding agent, which runs with approvals bypassed in its "
                f"coordinator worktree, so a restrictive mode cannot be "
                f"honored; refusing rather than running it with full tools. "
                f"Remove 'mode' or choose a Paseo agent.")
    return (f"{where} declares mode {mode!r} for a Lor coding agent. Lor "
            f"subagents have no permission-mode flag, so a restrictive mode "
            f"cannot be honored; refusing rather than running it with full "
            f"tools. Remove 'mode' or choose a Paseo agent.")


def _unit_pins_agent(u):
    return bool(u.get("provider")) or bool(u.get("model")) or bool(u.get("backend"))


def unit_agent_spec(u):
    """(normalized spec, problem) for a unit that declares ``backend``.

    (None, None) when the unit declares no backend, so every existing unit
    resolves exactly as before. A declared backend makes the unit's own
    backend/provider/model/thinking/mode one runtime-style spec.
    """
    if not isinstance(u, dict) or "backend" not in u:
        return None, None
    raw = {k: u[k] for k in AGENT_SPEC_KEYS if k in u}
    return normalize_agent_spec(raw, f"unit {u.get('id', '?')!r}")


def plan_code_agent(plan):
    """The plan's normalized top-level code_agent, or None. Raises nothing."""
    if not isinstance(plan, dict) or "code_agent" not in plan:
        return None
    spec, _problem = normalize_agent_spec(plan["code_agent"], "code_agent")
    return spec


def resolve_effective_agent(plan, u, overrides=None):
    """Resolve the agent a NEW attempt of code unit ``u`` would run.

    Returns a dict: source, backend, provider, model, thinking,
    thinking_explicit, mode, override_revision, unit (the effective unit view
    the launcher consumes). For the unit-pinned and default sources the view
    IS ``u``, so a plan with no runtime override and no code_agent launches
    exactly as before.
    """
    ov = overrides or {}
    uid = u.get("id")
    unit_ov = (ov.get("units") or {}).get(uid) if isinstance(ov.get("units"), dict) else None
    proj_ov = ov.get("project") if isinstance(ov.get("project"), dict) else None
    code_agent = plan_code_agent(plan)
    unit_spec, _unit_problem = unit_agent_spec(u)
    if unit_ov:
        source, spec = "unit-override", unit_ov
    elif unit_spec is not None:
        source, spec = "unit", unit_spec
    elif "backend" in u:
        # An unresolvable declared backend never falls back to another
        # transport; effective_agent_problem reports it.
        return {"source": "unit", "backend": u.get("backend"),
                "provider": u.get("provider"), "model": u.get("model"),
                "canonical_model_id": None, "cli_model": None,
                "thinking": u.get("thinking"), "thinking_explicit": "thinking" in u,
                "mode": u.get("mode"), "override_revision": ov.get("revision"),
                "unit": u, "problem": _unit_problem}
    elif _unit_pins_agent(u):
        source, spec = "unit", None
    elif proj_ov:
        source, spec = "project-override", proj_ov
    elif code_agent:
        source, spec = "plan-code_agent", code_agent
    else:
        source, spec = "default", None
    fallback_mode = next((s.get("mode") for s in (proj_ov, code_agent)
                          if isinstance(s, dict) and s.get("mode")), None)
    if spec is None:
        view = u
        provider = u.get("provider") or DEFAULT_AGENT_PROVIDER
        model = u.get("model")
        if not u.get("provider"):
            _route, sep, default_model = provider.partition("/")
            model = model or (default_model if sep else None)
        backend = (LIO.LOR_BACKEND if LIO.is_lor_provider(u.get("provider"))
                   else LIO.PASEO_BACKEND)
        thinking_explicit = "thinking" in u
        thinking = u.get("thinking", default_thinking_for(u))
        mode = u.get("mode") if "mode" in u else fallback_mode
        if "mode" not in u and mode:
            view = dict(u, mode=mode)
    else:
        view = dict(u)
        backend = spec["backend"]
        if backend == LIO.LOR_BACKEND:
            view["provider"] = spec["provider"]
            view.pop("model", None)
            provider, model = spec["provider"], None
        elif CA.is_cli_backend(backend):
            # route/model, the shape Paseo views have, so thinking defaults
            # and the orchestrator's forbid rule read a direct claude/<m>
            # exactly as they read the Paseo one.
            route, _sep, model = spec["provider"].partition("/")
            view["backend"], view["provider"], view["model"] = backend, route, model
            provider = spec["provider"]
        else:
            route, _sep, model = spec["provider"].partition("/")
            view["provider"], view["model"] = route, model
            provider = spec["provider"]
        if "thinking" in spec:
            view["thinking"] = spec["thinking"]
        elif "thinking" not in u:
            view.pop("thinking", None)
        thinking_explicit = "thinking" in view
        thinking = view.get("thinking", default_thinking_for(view))
        mode = spec.get("mode") or u.get("mode") or fallback_mode
        if mode:
            view["mode"] = mode
        else:
            view.pop("mode", None)
    canonical, cli_model = None, None
    if backend == LIO.LOR_BACKEND:
        try:
            canonical = LIO.parse_lor_model(view.get("provider"), view.get("model"))
        except LIO.LorError:
            canonical = None
    elif CA.is_cli_backend(backend):
        try:
            cli_model = CA.parse_provider(backend, provider)
        except CA.AdapterError:
            cli_model = None
    return {"source": source, "backend": backend, "provider": provider,
            "model": model, "canonical_model_id": canonical,
            "cli_model": cli_model,
            "thinking": thinking, "thinking_explicit": thinking_explicit,
            "mode": mode, "override_revision": ov.get("revision"),
            "unit": view}


def effective_agent_problem(plan, agent):
    """Why a resolved agent cannot be dispatched (backend-independent checks)."""
    view = agent["unit"]
    uid = view.get("id", "?")
    if agent.get("problem"):
        return agent["problem"]
    if CA.is_cli_backend(agent["backend"]):
        if agent.get("cli_model") is None:
            return (f"unit {uid!r}: backend {agent['backend']!r} with provider "
                    f"{agent.get('provider')!r} names no valid model")
        if agent.get("mode") in LOR_REFUSED_MODES:
            return lor_mode_refusal(f"unit {uid!r}", agent["mode"], agent["backend"])
    elif agent["backend"] == LIO.LOR_BACKEND:
        if agent["canonical_model_id"] is None:
            return (f"unit {uid!r}: provider {view.get('provider')!r} names no "
                    f"valid Lor canonical model id")
        if agent.get("mode") in LOR_REFUSED_MODES:
            return lor_mode_refusal(f"unit {uid!r}", agent["mode"])
    elif (agent["source"] not in ("unit", "default")
          and not str(agent.get("mode") or "").strip()):
        # Plan validation already requires 'mode' for a Paseo unit resolved
        # from the plan alone; only a runtime/code_agent switch can reach
        # Paseo without one.
        return (f"unit {uid!r} resolves to Paseo agent {agent['provider']!r} "
                f"({agent['source']}) but no 'mode' is declared on the unit, "
                f"the plan's code_agent or the runtime override. Paseo agents "
                f"under default permissions stall at their first write; set "
                f"one with `swarm.py set-agent ... --mode MODE`.")
    return forbid_unit_provider_problem(plan, view)
