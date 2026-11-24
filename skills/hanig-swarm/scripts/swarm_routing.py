"""Data-loaded agent routing; no coordinator or network dependency.

Python 3.8+, standard library only.
"""

import json
import os
import re
import sys
from pathlib import Path

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


def _orchestrator_token(value):
    """A lowercase host token for orchestrator keys (no whitespace, / or :)."""
    return (isinstance(value, str)
            and re.fullmatch(r"[a-z][a-z0-9_-]*", value) is not None)


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
VALID_ORCHESTRATORS = frozenset(
    {"claude", "codex", "antigravity"}
    | set(ORCHESTRATOR_DEFAULTS)
    | set(FORBID_WHEN_ORCHESTRATOR)
)
ACTIVE_ORCHESTRATOR = None


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


def effective_default_provider(plan=None):
    """Default code-agent provider for plan, without leaking another plan's state."""
    if isinstance(plan, dict):
        orch = _plan_orchestrator(plan=plan)
        if orch == ACTIVE_ORCHESTRATOR:
            return DEFAULT_AGENT_PROVIDER
        if orch in ORCHESTRATOR_DEFAULTS:
            return ORCHESTRATOR_DEFAULTS[orch]["provider"]
        return PINNED_AGENT_PROVIDER
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
    orch = _plan_orchestrator(plan_path=plan_path, plan=plan)
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
    try:
        family = _model_family()
        if family is None or not isinstance(routing, dict):
            return DEFAULT_AGENT_PROVIDER
        digests = family.config_digests(
            Path(family.__file__).resolve().parent.parent / "reviewers.json",
            AGENTS_FILE)
        provider, note = family.resolved_orchestrator_agent(
            orch, routing, digests, start=Path(plan_path).resolve().parent)
    except Exception as exc:
        # Resolution never stops validate or run: anything wrong here leaves
        # the shipped pin in force.
        print("swarm: routing: snapshot not applied, using the pin: %s: %s"
              % (type(exc).__name__, exc), file=sys.stderr)
        return DEFAULT_AGENT_PROVIDER
    if provider and note and provider != base_provider:
        THINKING_BY_MODEL[provider] = family.agent_thinking(
            seat or (routing.get("default") or {}),
            routing.get("thinking_by_model") or {},
            provider)
        DEFAULT_AGENT_PROVIDER = provider
        print("swarm: routing: " + note, file=sys.stderr)
    return DEFAULT_AGENT_PROVIDER


def default_thinking_for(u):
    """The reasoning effort one unit gets when it declares none.

    Keyed on the model that will actually run, which is the provider string
    unless the unit names a `model` separately -- both spellings reach paseo
    the same way, so both have to resolve here or the mapping would apply to
    one plan and not its equivalent. An unrecognised model falls back rather
    than refusing: a new model on the roster should dispatch at a sane effort,
    and the fallback is the level the two strongest models use.
    """
    provider = str(u.get("provider") or DEFAULT_AGENT_PROVIDER)
    model = u.get("model")
    key = "%s/%s" % (provider.split("/", 1)[0], model) if model else provider
    return THINKING_BY_MODEL.get(key, DEFAULT_AGENT_THINKING)
