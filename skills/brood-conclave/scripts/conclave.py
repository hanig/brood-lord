#!/usr/bin/env python3
"""brood-conclave: a structured, multi-round panel of frontier models.

One seat per model family (families.json). Every round, all seats answer in
parallel through the best harness each one has, always with web search on and
reasoning effort at the requested level (xhigh by default, clamped to the
model's ceiling). Round 0 is blind. Later rounds cross-examine anonymized
posts on the cruxes the moderator lists. A ratification vote ends the panel.

    conclave.py resolve                 top model per family, from live catalogs
    conclave.py doctor                  prove each route: model access + web search
    conclave.py new BOARD --brief F     create a board
    conclave.py round BOARD K           run round K for every seat
    conclave.py digest BOARD K          a round's claims and positions, for the moderator
    conclave.py status BOARD            per-crux stances, changes, whether to stop
    conclave.py cruxes BOARD            carry open cruxes into the next round's file
    conclave.py ratify BOARD            send statement.md to every seat for a vote
    conclave.py tally BOARD             summarize the votes
    conclave.py export BOARD            full transcript with real model names

No model id is written in this file: lines live in families.json and ids come
from the catalogs. Standard library only.

The `lor` route runs a seat as a Lor subagent (`lor session start`) on the
Lor router with full sandbox tools and web search (`lor web search` /
`lor web fetch`). Lor subagent mode is the default (`"lor": true` in
`families.json`); pass `--no-lor` (or set `CONCLAVE_LOR=0` / `"lor": false`)
to use the other harnesses instead.
"""

import argparse
import concurrent.futures
import datetime
import functools
import hashlib
import json
import os
import random
import re
import shlex
import shutil
import signal
import string
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
FAMILIES_PATH = ROOT / "families.json"
TEMPLATES = ROOT / "templates"
CATALOG_URL = "https://openrouter.ai/api/v1/models"
EFFORTS = ("minimal", "low", "medium", "high", "xhigh", "max")
VERSION_TOKEN = re.compile(r"^v?\d+(?:\.\d+)*$")
SNAPSHOT_TOKEN = re.compile(r"^\d{6,}$")
# Tokens that mark a variant of a line in any family. A soft variant may be
# picked but loses ties; a hard one is never picked.
SOFT_VARIANTS = {"preview", "exp", "experimental"}
GLOBAL_VARIANTS = {"latest", "contributor", "customtools", "image", "audio",
                   "tts", "realtime", "fast", "ultrafast", "free", "snapshot"}
FAILURE_MARKERS = ("usage limit", "rate limit", "rate_limit", "quota", "zdr",
                   "429", "credits", "overloaded", "capacity", "unauthorized",
                   "401", "403", "not found", "does not exist", "not available",
                   "no access", "invalid model")
MIN_ANSWER_CHARS = 200
DOCTOR_TTL_DAYS = 7
ROSTER_TTL_HOURS = 24
PROBE_REPO = "anomalyco/opencode"
PROBE_PROMPT = ("Search the web now; do not answer from memory, which is out "
                "of date. What is the tag name of the most recent release of "
                "the GitHub repository %s? Reply on one line exactly as: "
                "TAG <tag> URL <the page you found it on>" % PROBE_REPO)
ATTACHED_PACKET = ("Follow the instructions in the attached packet.md. It is "
                   "the whole task.")
AGY_NOTE = ("\n\nEverything you need is in this message. Do not read local "
            "files.")
KEY_NAMES = ("OPENROUTER_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY")
# opencode-direct calls the vendor's own API, so it needs the vendor's key.
DIRECT_KEYS = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}
OPENCODE_HARNESSES = ("opencode", "opencode-direct")
# The lor route: a Lor subagent on the Lor router. Its id for a model can
# differ from OpenRouter's (dots become dashes; a few carry a version), so a
# route carries candidate ids, tried in order; a wrong id is refused at once
# and costs nothing. The id that works is remembered in lor_ids.json.
# Subagents omit --tools so they inherit every tool the sandbox offers (shell,
# read, write, edit, glob, grep, etc.) plus `lor web search` / `lor web fetch`.
LOR_POLL_SECONDS = 5
LOR_START_TIMEOUT = 120
LOR_MODEL_REJECT_MARKERS = ("invalid_argument", "not found", "unknown model",
                            "no context window")
LOR_NOTE = (
    "\n\n---\nHow to work in this session:\n"
    "- Web search: run `lor web search <query>` and `lor web fetch <url>` in "
    "the shell (and `lor browser` if a dynamic page needs rendering). Use "
    "them; the task requires current facts.\n"
    "- Tools: you have access to your shell and file tools to verify claims, "
    "run calculations or scripts, and inspect figures or files referenced in "
    "the brief. Keep any temporary files inside your working directory.\n"
    "- Panel isolation: do not read, list, or modify other panelists' "
    "files%s (other than figures or attachments listed below). Do not start "
    "subagents, create artifacts, use the desktop, or message anyone.\n"
    "- Your final message is your whole answer, exactly in the requested "
    "format. Do not call `pass`, put nothing after the JSON block, and do not "
    "end with a summary of what you did.\n")
LOR_FIGURES = "\n\nFigures for this task (open each with the read tool):\n"
LOR_ATTACHMENTS = "\n\nOther attached files for this task:\n"
TRUTHY = {"1", "true", "yes", "on"}
FALSY = {"0", "false", "no", "off"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
URL = re.compile(r"https?://[^\s)\]>\"']+")
FENCE = re.compile(r"```(?:json)?[ \t]*\n(.*?)\n[ \t]*```", re.S)
# Self-identification only ("I am Claude", "As GPT-6,"); vendor names in the
# substance of a post ("companies such as Google") must survive.
# Case-sensitive on purpose: the trailing words must be capitalized or numeric
# ("Gemini 4 Argon"), so "I am Claude and ..." keeps its "and".
_MODEL_NAME = (r"(?:GPT|Claude|Gemini|Muse)[\w.\-]*"
               r"(?:\s+(?:\d[\w.\-]*|[A-Z][a-z]+)){0,2}")
SELF_ID = (
    re.compile(r"\b(?:I am|I'm|I’m|[Ss]peaking as)\s+(?:an?\s+|the\s+)?"
               r"(?:(?:OpenAI|Anthropic|Google|DeepMind|Meta)\b|%s)"
               r"(?:\s+(?:model|assistant|AI))?\b" % _MODEL_NAME),
    re.compile(r"\bAs (?:an? )?%s(?: (?:model|assistant))?," % _MODEL_NAME))


# ---------------------------------------------------------------- utilities

def die(message, code=2):
    print("conclave: %s" % message, file=sys.stderr)
    sys.exit(code)


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def age_hours(stamp):
    try:
        then = datetime.datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc)
    except (TypeError, ValueError):
        return float("inf")
    now = datetime.datetime.now(datetime.timezone.utc)
    return (now - then).total_seconds() / 3600.0


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=False) + "\n")
    tmp.replace(path)


def state_dir():
    # Live state keeps its hanig- spelling across the brood- skill rename, like
    # every other authored skill's state directory.
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser(
        "~/.local/state")
    path = Path(base) / "hanig-conclave"
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_config():
    config = read_json(FAMILIES_PATH)
    if not config or not isinstance(config.get("families"), dict):
        die("cannot read %s" % FAMILIES_PATH)
    return config


def render(name, **values):
    text = (TEMPLATES / name).read_text()
    return string.Template(text).safe_substitute(**values)


def api_keys():
    """API keys for pi and opencode, from env or ~/.zshrc. Never passed to
    codex or claude, which use their own logins."""
    keys = {k: os.environ[k] for k in KEY_NAMES if os.environ.get(k)}
    missing = [k for k in KEY_NAMES if k not in keys]
    if missing:
        try:
            lines = (Path.home() / ".zshrc").read_text().splitlines()
        except OSError:
            lines = []
        pattern = re.compile(r"\s*export\s+(%s)=(.*)$" % "|".join(missing))
        for line in lines:
            match = pattern.match(line)
            if match:
                keys[match.group(1)] = match.group(2).strip().strip("'\"")
    return keys


# ------------------------------------------------------- catalog and ranking

def fetch_catalog(timeout=30):
    request = urllib.request.Request(
        CATALOG_URL, headers={"User-Agent": "brood-conclave"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))["data"]


def _run_quiet(argv, timeout=60):
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout if proc.returncode == 0 else ""


def lor_models():
    """Canonical model ids listed by `lor model list` inside a Lor session."""
    if not (shutil.which("lor") and os.environ.get("LOR_SESSION_ID")):
        return []
    raw = _run_quiet(["lor", "model", "list"], timeout=30)
    try:
        rows = json.loads(raw or "{}").get("models") or []
    except (ValueError, AttributeError):
        return []
    return [r["canonical_model_id"] for r in rows
            if isinstance(r, dict) and r.get("canonical_model_id")]


def native_catalogs():
    """Model lists the native harnesses report: codex slugs, agy ids,
    opencode's direct-provider ids, and pinned Lor model ids."""
    native = {"codex": {}, "agy": [], "opencode": set(), "lor": lor_models()}
    if shutil.which("opencode"):
        for vendor in DIRECT_KEYS:
            native["opencode"].update(
                line.strip() for line in
                _run_quiet(["opencode", "models", vendor]).splitlines()
                if line.strip().startswith(vendor + "/"))
    if shutil.which("codex"):
        try:
            rows = json.loads(_run_quiet(["codex", "debug", "models"]) or "[]")
        except ValueError:
            rows = []
        if isinstance(rows, dict):
            rows = rows.get("models", [])
        for row in rows:
            if isinstance(row, dict) and row.get("slug") and \
                    row.get("visibility") != "hide":
                native["codex"][row["slug"]] = [
                    level.get("effort") for level in
                    row.get("supported_reasoning_levels") or []]
    if shutil.which("agy"):
        for line in _run_quiet(["agy", "models"]).splitlines():
            ident = line.split("\t")[0].strip()
            if ident and " " not in ident:
                native["agy"].append(ident)
    return native


def parse_name(name, variants=()):
    """(line, version, flags) for a catalog name such as gpt-6.1-sol-pro."""
    line, version, flags = [], (), set()
    hard = set(variants) | GLOBAL_VARIANTS
    previous_numeric = False
    for token in name.lower().split("-"):
        if SNAPSHOT_TOKEN.match(token):
            flags.add("snapshot")
            previous_numeric = False
        elif VERSION_TOKEN.match(token) and (not version or previous_numeric):
            version += tuple(int(x) for x in token.lstrip("v").split("."))
            previous_numeric = True
        else:
            previous_numeric = False
            if token in hard or token in SOFT_VARIANTS:
                flags.add(token)
            else:
                line.append(token)
    return "-".join(line), version, flags


def same_model(a, b):
    return parse_name(a)[:2] == parse_name(b)[:2]


def candidates(catalog, family, today):
    """Text-output, non-variant, unexpired catalog rows for one family."""
    vendor = family["vendor"] + "/"
    hard = set(family.get("variants", [])) | GLOBAL_VARIANTS
    rows = []
    for model in catalog:
        ident = model.get("id", "")
        if not ident.startswith(vendor) or ":" in ident:
            continue
        modalities = (model.get("architecture") or {}).get(
            "output_modalities") or ["text"]
        if set(modalities) != {"text"}:
            continue
        expires = model.get("expiration_date")
        if expires and str(expires)[:10] < today:
            continue
        try:
            price = float((model.get("pricing") or {}).get("completion") or 0)
        except (TypeError, ValueError):
            price = 0.0
        if price <= 0:
            continue
        line, version, flags = parse_name(ident[len(vendor):],
                                          family.get("variants", []))
        if flags & hard:
            continue
        reasoning = model.get("reasoning") or {}
        rows.append({"id": ident, "name": model.get("name") or ident,
                     "line": line, "version": version,
                     "preview": bool(flags & SOFT_VARIANTS),
                     "created": int(model.get("created") or 0),
                     "price_out": price,
                     "efforts": [e for e in reasoning.get("supported_efforts")
                                 or [] if e in EFFORTS]})
    return rows


def _newest(rows):
    return max(rows, key=lambda r: (r["version"], not r["preview"],
                                    r["created"]))


def select(rows, family, adopt_ratio):
    """Pick the family's top model. Returns (pick, status, why, unranked)."""
    lines = family["lines"]
    lower = set(family.get("lower_lines", []))
    current = None
    for rank, line in enumerate(lines):
        cands = [r for r in rows if r["line"] == line]
        if cands:
            current = _newest(cands)
            why = "line %s (rank %d of %d), newest version" % (
                line, rank + 1, len(lines))
            break
    unknown = [r for r in rows if r["line"] not in lines
               and r["line"] not in lower]
    if current is None:
        adopt = unknown
    else:
        floor = adopt_ratio * current["price_out"]
        adopt = [r for r in unknown if r["created"] > current["created"]
                 and r["price_out"] >= floor]
    if adopt:
        pick = max(adopt, key=lambda r: (r["created"], r["price_out"]))
        why = ("unknown line %s, released after the known top pick and priced "
               "at least %d%% of it; add it to lines in families.json"
               % (pick["line"], round(adopt_ratio * 100)))
        rest = [r for r in unknown if r is not pick and current
                and r["created"] > current["created"]]
        return pick, "NEW_LINE", why, rest
    if current is None:
        return None, "NONE", "no model of this family is listed", []
    rest = [r for r in unknown if r["created"] > current["created"]]
    return current, "OK", why, rest


def native_upgrade(pick, family, native):
    """A newer version of a known line that a native harness lists before
    OpenRouter does. Returns (pick, notes)."""
    lines = family["lines"]
    lower = set(family.get("lower_lines", []))
    vendor = family["vendor"]
    names = []
    if vendor == "openai":
        names = list(native["codex"])
    elif vendor == "google":
        names = sorted({i.rpartition("-")[0] if i.rpartition("-")[2] in EFFORTS
                        else i for i in native["agy"] if i.startswith("gemini")})
    notes = []
    best = pick

    def rank(row):
        # An adopted unknown line outranks every known line.
        if row is None:
            return len(lines)
        return lines.index(row["line"]) if row["line"] in lines else -1

    for name in names:
        line, version, flags = parse_name(name, family.get("variants", []))
        if flags & (set(family.get("variants", [])) | GLOBAL_VARIANTS):
            continue
        known = best and parse_name(best["id"].split("/", 1)[1])[:2]
        if known and (line, version) == known:
            continue
        if line in lines:
            if lines.index(line) < rank(best) or (
                    best and line == best["line"] and
                    version > best["version"]):
                best = {"id": "%s/%s" % (vendor, name), "name": name,
                        "line": line, "version": version, "preview": False,
                        "created": 0, "price_out": 0.0, "efforts": [],
                        "native_only": True}
                notes.append("%s is listed natively but not on OpenRouter yet; "
                             "using it" % name)
        elif line not in lower:
            notes.append("native harness lists %s (unknown line %s); pin it in "
                         "families.json if it is the new flagship" % (name, line))
    return best, notes


def clamp(want, supported):
    """The requested effort if supported, else the highest one below it."""
    supported = [e for e in EFFORTS if e in set(supported or ())] or \
        list(EFFORTS)
    if want in supported:
        return want
    below = [e for e in supported if EFFORTS.index(e) <= EFFORTS.index(want)]
    return below[-1] if below else supported[0]


def plan_route(harness, pick, native, effort):
    """One harness route for a pick, or (None, reason)."""
    name = pick["id"].split("/", 1)[1]
    if harness == "opencode-direct":
        if not shutil.which("opencode"):
            return None, "opencode is not installed"
        return plan_direct(pick, native, effort)
    if not shutil.which(harness):
        return None, "%s is not installed" % harness
    if harness == "codex":
        levels = native["codex"].get(name)
        if levels is None:
            return None, "%s is not in the codex catalog" % name
        return {"harness": "codex", "model": name, "supported": levels,
                "effort": clamp(effort, levels)}, ""
    if harness == "agy":
        tiers = {}
        for ident in native["agy"]:
            base, _, tier = ident.rpartition("-")
            if tier in EFFORTS and same_model(base, name):
                tiers[tier] = ident
        if not tiers:
            return None, "%s is not in agy models" % name
        tier = clamp(effort, list(tiers))
        return {"harness": "agy", "model": tiers[tier], "tiers": tiers,
                "supported": sorted(tiers, key=EFFORTS.index),
                "effort": tier}, ""
    supported = pick["efforts"] or list(EFFORTS[1:])
    if harness == "lor":
        if not os.environ.get("LOR_SESSION_ID"):
            return None, "not inside a Lor session (LOR_SESSION_ID unset)"
        ids = lor_candidates(pick["id"], known_models=native.get("lor"))
        return {"harness": "lor", "model": ids[0], "candidates": ids,
                "catalog_id": pick["id"], "supported": supported,
                "effort": clamp(effort, supported)}, ""
    if harness == "claude":
        return {"harness": "claude", "model": name.replace(".", "-"),
                "supported": supported,
                "effort": clamp(effort, supported)}, ""
    if pick.get("native_only"):
        return None, "%s is not on OpenRouter" % pick["id"]
    if harness == "pi":
        return {"harness": "pi", "model": "openrouter/%s:online" % pick["id"],
                "supported": supported,
                "effort": clamp(effort, supported)}, ""
    if harness == "opencode":
        return {"harness": "opencode", "model": "openrouter/%s" % pick["id"],
                "supported": supported,
                "effort": clamp(effort, supported)}, ""
    return None, "unknown harness %s" % harness


def plan_direct(pick, native, effort):
    """opencode against the vendor's own API (no OpenRouter), Exa search."""
    vendor, name = pick["id"].split("/", 1)
    if vendor not in DIRECT_KEYS:
        return None, "no direct provider for %s" % vendor
    if not api_keys().get(DIRECT_KEYS[vendor]):
        return None, "%s is not set" % DIRECT_KEYS[vendor]
    for ident in ("%s/%s" % (vendor, name),
                  "%s/%s" % (vendor, name.replace(".", "-"))):
        if ident in native.get("opencode", ()):
            supported = pick["efforts"] or list(EFFORTS[1:])
            return {"harness": "opencode-direct", "model": ident,
                    "supported": supported,
                    "effort": clamp(effort, supported)}, ""
    return None, "%s is not in opencode's %s models" % (name, vendor)


def lor_candidates(catalog_id, config=None, remembered=None,
                   known_models=None):
    """Lor router ids to try for an OpenRouter-style id, best first: an
    explicit `lor_ids` entry in families.json, the id that worked last time,
    any exact/dash match in `lor model list`, the id itself, then the id with
    dots as dashes (claude-opus-5.5 is claude-opus-5-5 on Lor)."""
    if config is None:
        config = read_json(FAMILIES_PATH, {}) or {}
    if remembered is None:
        remembered = read_json(state_dir() / "lor_ids.json", {}) or {}
    vendor, _, name = catalog_id.partition("/")
    dashed = ("%s/%s" % (vendor, name.replace(".", "-"))
              if vendor else name.replace(".", "-"))
    known = set(known_models or ())
    matched = [ident for ident in (catalog_id, dashed) if ident in known]
    ids = [(config.get("lor_ids") or {}).get(catalog_id),
           remembered.get(catalog_id), *matched, catalog_id, dashed]
    out = []
    for ident in ids:
        if ident and ident not in out:
            out.append(ident)
    return out


def resolve_lor_mode(flag, board_meta=None, config=None, environ=None):
    """Whether to run all seats exclusively as Lor subagents.
    Precedence: CLI flag (--lor / --no-lor) > board.json > CONCLAVE_LOR env >
    families.json `lor` (default True)."""
    if flag is not None:
        return bool(flag)
    if board_meta is not None and "lor" in board_meta:
        return bool(board_meta["lor"])
    env = os.environ if environ is None else environ
    raw = (env.get("CONCLAVE_LOR") or "").strip().lower()
    if raw in TRUTHY:
        return True
    if raw in FALSY:
        return False
    if config is None:
        config = read_json(FAMILIES_PATH, {}) or {}
    return bool(config.get("lor", True))


def ensure_lor_available():
    if not shutil.which("lor"):
        die("Lor subagent mode requires the lor CLI on PATH "
            "(pass --no-lor to use other harnesses)")
    if not os.environ.get("LOR_SESSION_ID"):
        die("Lor subagent mode requires running inside a Lor session "
            "(LOR_SESSION_ID unset; pass --no-lor to use other harnesses)")


def ensure_lor_route(seat, effort, native=None):
    """Return the seat's lor route, synthesizing it if the cached roster was
    resolved outside a Lor session."""
    for route in seat.get("routes") or []:
        if route["harness"] == "lor":
            return with_effort(route, effort)
    if not seat.get("model"):
        return None
    supported = next((r.get("supported") for r in seat.get("routes") or []
                      if r.get("supported")), [])
    pick = {"id": seat["model"], "efforts": supported}
    route, _ = plan_route("lor", pick, native or {"lor": lor_models()}, effort)
    return with_effort(route, effort) if route else None


def select_routes(seat, effort, prefer=None, lor_only=False, native=None):
    """Ordered list of routes to try for a seat. Under lor_only (unless
    overridden for this seat by --route seat=<other>), returns only the lor
    route so a failure never falls back to other harnesses."""
    if lor_only and (not prefer or prefer == "lor"):
        route = ensure_lor_route(seat, effort, native=native)
        return [route] if route else []
    routes = [with_effort(r, effort) for r in seat.get("routes") or []]
    if prefer == "lor" and not any(r["harness"] == "lor" for r in routes):
        route = ensure_lor_route(seat, effort, native=native)
        if route:
            routes.append(route)
    if prefer:
        routes.sort(key=lambda r: r["harness"] != prefer)
    failed = doctor_failures()
    healthy = [r for r in routes if route_key(r) not in failed]
    return healthy + [r for r in routes if r not in healthy]


def remember_lor_id(catalog_id, ident):
    path = state_dir() / "lor_ids.json"
    data = read_json(path, {}) or {}
    if data.get(catalog_id) != ident:
        data[catalog_id] = ident
        write_json(path, data)


def with_effort(route, effort):
    route = dict(route)
    route["effort"] = clamp(effort, route.get("supported"))
    if route.get("tiers"):
        route["model"] = route["tiers"][route["effort"]]
    return route


def route_key(route):
    return "%s|%s|%s" % (route["harness"], route["model"], route["effort"])


def resolve(effort=None, quiet=False, lor_only=False):
    config = load_config()
    if lor_only:
        ensure_lor_available()
    try:
        catalog = fetch_catalog()
    except (OSError, ValueError, KeyError) as exc:
        die("cannot read the OpenRouter catalog: %s" % exc, 3)
    native = native_catalogs()
    previous = read_json(state_dir() / "roster.json", {}) or {}
    today = datetime.date.today().isoformat()
    effort = effort or config.get("effort", "xhigh")
    roster = {"schema": 1, "resolved_at": utc_now(), "effort": effort,
              "seats": {}, "seen": {}}
    for key, family in config["families"].items():
        rows = candidates(catalog, family, today)
        notes = []
        if family.get("pin"):
            pinned = [r for r in rows if r["id"] == family["pin"]]
            pick = pinned[0] if pinned else {
                "id": family["pin"], "name": family["pin"],
                "line": parse_name(family["pin"].split("/", 1)[1])[0],
                "version": (), "preview": False, "created": 0,
                "price_out": 0.0, "efforts": [], "native_only": True}
            status, why, unranked = "PINNED", "pinned in families.json", []
        else:
            pick, status, why, unranked = select(
                rows, family, config.get("adopt_ratio", 0.5))
            upgraded, notes = native_upgrade(pick, family, native)
            if upgraded is not pick:
                pick, status, why = upgraded, "NATIVE", notes[-1]
        if pick is None:
            roster["seats"][key] = {"status": "NONE", "why": why,
                                    "routes": [], "skipped": []}
            continue
        routes, skipped = [], []
        for harness in family["routes"]:
            route, reason = plan_route(harness, pick, native, effort)
            if route:
                routes.append(route)
            else:
                skipped.append({"harness": harness, "reason": reason})
        before = set((previous.get("seen") or {}).get(key, []))
        was = ((previous.get("seats") or {}).get(key) or {}).get("model")
        roster["seats"][key] = {
            "model": pick["id"], "name": pick["name"], "status": status,
            "why": why, "price_out_per_m": round(pick["price_out"] * 1e6, 2),
            "routes": routes, "skipped": skipped, "notes": notes,
            "previous": was if was and was != pick["id"] else None,
            "new_in_catalog": sorted(r["id"] for r in rows
                                     if before and r["id"] not in before),
            "unranked": ["%s (line %s)" % (r["id"], r["line"])
                         for r in unranked]}
        roster["seen"][key] = sorted(r["id"] for r in rows)
    write_json(state_dir() / "roster.json", roster)
    if not quiet:
        print_roster(roster, lor_only=lor_only)
    return roster


def print_roster(roster, lor_only=False):
    mode = ", mode lor subagents" if lor_only else ""
    print("roster %s, effort %s%s" % (roster["resolved_at"], roster["effort"],
                                      mode))
    for key, seat in roster["seats"].items():
        if not seat.get("model"):
            print("  %-10s NONE: %s" % (key, seat.get("why")))
            continue
        routes = ([r for r in seat["routes"] if r["harness"] == "lor"]
                  if lor_only else seat["routes"])
        chain = " > ".join("%s %s@%s" % (r["harness"], r["model"], r["effort"])
                           for r in routes) or "NO ROUTE"
        print("  %-10s %-34s %-8s %s" % (key, seat["model"], seat["status"],
                                         seat["why"]))
        print("  %-10s routes: %s" % ("", chain))
        if seat.get("previous"):
            print("  %-10s CHANGED: was %s" % ("", seat["previous"]))
        for item in seat.get("skipped", []):
            if lor_only and item["harness"] != "lor":
                continue
            print("  %-10s skipped %s: %s" % ("", item["harness"],
                                              item["reason"]))
        for note in seat.get("notes", []):
            print("  %-10s note: %s" % ("", note))
        if seat.get("new_in_catalog"):
            print("  %-10s new in catalog: %s" % (
                "", ", ".join(seat["new_in_catalog"])))
        if seat.get("unranked"):
            print("  %-10s unranked new lines: %s" % (
                "", ", ".join(seat["unranked"])))


def current_roster(refresh=False, effort=None):
    roster = read_json(state_dir() / "roster.json")
    if refresh or not roster or \
            age_hours(roster.get("resolved_at")) > ROSTER_TTL_HOURS:
        roster = resolve(effort=effort)
    return roster


# ------------------------------------------------------------------ runners

def _spawn(argv, cwd, env, stdin_text, timeout):
    try:
        proc = subprocess.Popen(
            argv, cwd=str(cwd), env=env, text=True, start_new_session=True,
            stdin=subprocess.PIPE if stdin_text is not None
            else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as exc:
        return 127, "", str(exc)
    try:
        out, err = proc.communicate(stdin_text, timeout=timeout)
        return proc.returncode, out, err
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        out, err = proc.communicate()
        return None, out or "", err or ""


@functools.lru_cache(maxsize=None)
def _pi_has(flag):
    """Older pi releases lack some flags (0.87 has no --no-mcp)."""
    return flag in _run_quiet(["pi", "--help"])


def _opencode_text(stdout):
    """The last assistant message's text from opencode --format json."""
    by_message, order = {}, []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        part = event.get("part") or {}
        if part.get("type") != "text":
            continue
        mid = part.get("messageID") or ""
        if mid not in by_message:
            by_message[mid] = []
            order.append(mid)
        by_message[mid].append(part.get("text") or "")
    return "".join(by_message[order[-1]]) if order else ""


def build_argv(route, packet, packet_path, images, workdir, out_file):
    """(argv, env, stdin_text) for one route."""
    env = dict(os.environ)
    harness, model, effort = route["harness"], route["model"], route["effort"]
    if harness == "codex":
        argv = ["codex", "--search", "exec", "-m", model, "-c",
                'model_reasoning_effort="%s"' % effort, "-s", "read-only",
                "--skip-git-repo-check", "-C", str(workdir),
                "-o", str(out_file)]
        if images:
            argv += ["-i", ",".join(images)]
        return argv + ["-"], env, packet
    if harness == "agy":
        return ["agy", "--model", model, "--sandbox",
                "--print=" + packet + AGY_NOTE], env, None
    if harness == "pi":
        env.update(api_keys())
        argv = ["pi", "--print", "--model", model, "--thinking", effort,
                "--no-tools", "--no-session", "--no-context-files",
                "--no-skills", "--no-extensions", "--no-prompt-templates"]
        if _pi_has("--no-mcp"):
            argv.append("--no-mcp")
        argv += ["@" + str(packet_path)] + ["@" + i for i in images]
        return argv + [ATTACHED_PACKET], env, None
    if harness in OPENCODE_HARNESSES:
        env.update(api_keys())
        env["OPENCODE_ENABLE_EXA"] = "1"
        env["OPENCODE_CONFIG_CONTENT"] = json.dumps(
            {"permission": {"edit": "deny", "bash": "deny"}})
        argv = ["opencode", "run", ATTACHED_PACKET, "--format", "json",
                "-m", model, "--variant", effort, "--dir", str(workdir),
                "-f", str(packet_path)]
        return argv + list(images), env, None
    if harness == "claude":
        tools = "WebSearch,WebFetch" + (",Read" if images else "")
        argv = ["claude", "-p", "--model", model, "--effort", effort,
                "--allowedTools", tools, "--output-format", "text",
                "--no-session-persistence", "--strict-mcp-config",
                "--setting-sources", "project"]
        if images:
            argv += ["--add-dir", str(Path(images[0]).parent)]
            packet += "\n\nFigures for this task (read them with the Read " \
                "tool):\n" + "\n".join("- " + i for i in images)
        return argv, env, packet
    raise ValueError("unknown harness %s" % harness)


def _failure_detail(rc, out, err, text, timeout):
    if rc is None:
        return "timeout after %d min" % (timeout // 60)
    tail = (err + "\n" + out)[-4000:]
    lowered = tail.lower()
    marker = next((m for m in FAILURE_MARKERS if m in lowered), "")
    last = next((l.strip() for l in reversed(tail.splitlines())
                 if l.strip()), "")[:200]
    if rc == 0 and len(text.strip()) < MIN_ANSWER_CHARS:
        return "answer too short (%d chars)%s" % (
            len(text.strip()), ": " + marker if marker else "")
    return "exit %s%s: %s" % (rc, " [" + marker + "]" if marker else "", last)


def run_route(route, packet, images, workdir, timeout, log_path,
              min_chars=MIN_ANSWER_CHARS, board_dir=None, attachments=None):
    """Run one route; returns (ok, text, detail, seconds)."""
    workdir = Path(workdir)
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True)
    packet_path = workdir / "packet.md"
    packet_path.write_text(packet)
    out_file = workdir / "answer.md"
    if route["harness"] == "lor":
        return _finish_route(
            route,
            *run_lor(route, packet, images, workdir, timeout,
                     board_dir=board_dir, attachments=attachments),
            log_path, min_chars, timeout)
    if route["harness"] == "agy":
        images = []
    argv, env, stdin_text = build_argv(route, packet, packet_path, images,
                                       workdir, out_file)
    started = time.time()
    rc, out, err = _spawn(argv, workdir, env, stdin_text, timeout)
    seconds = round(time.time() - started)
    if route["harness"] == "codex":
        text = out_file.read_text() if out_file.exists() else ""
    elif route["harness"] in OPENCODE_HARNESSES:
        text = _opencode_text(out)
    else:
        text = out
    return _finish_route(route, argv, rc, out, err, text, seconds, log_path,
                         min_chars, timeout)


def _finish_route(route, argv, rc, out, err, text, seconds, log_path,
                  min_chars, timeout):
    shown = [a if len(a) < 300 else a[:120] + "...[%d chars]" % len(a)
             for a in argv]
    Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    Path(log_path).write_text("$ %s\nexit %s after %ss\n\n--- stdout\n%s\n"
                              "--- stderr\n%s\n" % (shlex.join(shown), rc,
                                                    seconds, out, err))
    ok = rc == 0 and len(text.strip()) >= min_chars
    detail = "" if ok else _failure_detail(rc, out, err, text, timeout)
    return ok, text, detail, seconds


def lor_task(packet, images, board_dir=None, attachments=None):
    scope = (" in the conclave board directory (%s)" % board_dir
             if board_dir else "")
    task = packet + (LOR_NOTE % scope)
    if images:
        task += LOR_FIGURES + "".join("- %s\n" % i for i in images)
    extra = [a for a in (attachments or []) if a not in set(images or ())]
    if extra:
        task += LOR_ATTACHMENTS + "".join("- %s\n" % a for a in extra)
    return task


def lor_start_argv(ident, effort, title, task_path, workdir):
    """Build `lor session start` argv. Omits --tools so the subagent inherits
    every tool the caller's sandbox offers (shell, read, write, edit, glob,
    grep, etc.) and sets --working-directory to isolate parallel seats and
    guarantee a fresh (non-forked) subagent."""
    argv = ["lor", "session", "start", "--canonical-model-id", ident]
    if effort:
        argv += ["--effort", effort]
    argv += ["--title", title[:80], "--working-directory", str(workdir),
             "--task", "@" + str(task_path)]
    return argv


def lor_final_text(events):
    """(finished, reason, text) from `lor session inspect` events: whether a
    turn has ended with a final answer, how, and that answer. The first turn
    that stops (rather than yields) is the answer to the packet. Anything
    later comes from the seat being woken again, by its own background work
    for example, and is not part of the round: in the first Lor board a seat
    woken that way invented a cross-examination of panelists that did not
    exist."""
    for end, row in enumerate(events):
        finish = (row.get("event") or {}).get("finish")
        if finish is None:
            continue
        reason = (finish or {}).get("reason", "")
        if reason == "FINISH_REASON_YIELDED":
            continue
        last_id, parts = None, []
        for prior in reversed(events[:end]):
            event = prior.get("event") or {}
            message = event.get("message") or {}
            rid = event.get("model_response_id")
            if message.get("role") == "MESSAGE_ROLE_USER" and last_id:
                break
            if not rid or message.get("role") != "MESSAGE_ROLE_ASSISTANT":
                continue
            if last_id is None:
                last_id = rid
            if rid != last_id:
                break
            parts.append("".join(c.get("text") or ""
                                 for c in message.get("content") or []))
        return True, reason, "".join(reversed(parts))
    return False, "", ""


def lor_error(events):
    for row in reversed(events):
        err = (row.get("event") or {}).get("error")
        if err:
            return str(err.get("message") if isinstance(err, dict) else err)
    return ""


def lor_cost(events):
    total = 0.0
    for row in events:
        cost = ((row.get("event") or {}).get("metrics") or {}).get("cost")
        try:
            total += float((cost or {}).get("amount") or 0)
        except ValueError:
            pass
    return round(total, 4)


def _lor_events(session_id):
    rc, out, err = _spawn(["lor", "session", "inspect", "--session-id",
                           session_id], Path.cwd(), dict(os.environ), None, 120)
    events = []
    for line in (out or "").splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    return rc, events, err


_ACTIVE_LOR_SESSIONS = set()
_ACTIVE_LOR_LOCK = threading.Lock()


def _stop_lor_session(session_id, workdir=None):
    _spawn(["lor", "session", "stop", "--session-id", session_id],
           workdir or Path.cwd(), dict(os.environ), None, 60)


def _on_sigterm(signum, frame):
    with _ACTIVE_LOR_LOCK:
        sessions = list(_ACTIVE_LOR_SESSIONS)
    for sid in sessions:
        _stop_lor_session(sid)
    os._exit(128 + signum)


def run_lor(route, packet, images, workdir, timeout, board_dir=None,
            attachments=None):
    """One seat as a Lor subagent with full sandbox tools and web search.
    Returns the run_route tuple pieces (argv, rc, out, err, text, seconds)."""
    started = time.time()
    task_path = workdir / "task.md"
    task_path.write_text(lor_task(packet, images, board_dir=board_dir,
                                  attachments=attachments))
    env = dict(os.environ)
    title = "conclave " + " ".join(
        part for part in workdir.parts[-4:] if part != "work")
    argv, session_id, log = [], None, []
    for ident in route.get("candidates") or [route["model"]]:
        argv = lor_start_argv(ident, route["effort"], title, task_path,
                              workdir)
        rc, out, err = _spawn(argv, workdir, env, None, LOR_START_TIMEOUT)
        log.append("start %s: exit %s %s" % (ident, rc, (err or out)[-300:]))
        if rc == 0:
            try:
                session_id = json.loads(out)["session_id"]
            except (ValueError, KeyError, TypeError):
                return argv, 1, out, err, "", round(time.time() - started)
            route["model"] = ident
            if route.get("catalog_id"):
                remember_lor_id(route["catalog_id"], ident)
            break
        lowered = (err + "\n" + out).lower()
        if not any(m in lowered for m in LOR_MODEL_REJECT_MARKERS):
            return argv, rc, out, "\n".join(log), "", \
                round(time.time() - started)
    if session_id is None:
        return argv, 1, "", "\n".join(log) + "\ninvalid model: no candidate " \
            "id was accepted", "", round(time.time() - started)
    with _ACTIVE_LOR_LOCK:
        _ACTIVE_LOR_SESSIONS.add(session_id)
    log.append("session %s" % session_id)
    deadline = started + timeout
    try:
        while time.time() < deadline:
            time.sleep(LOR_POLL_SECONDS)
            rc, events, err = _lor_events(session_id)
            finished, reason, text = lor_final_text(events)
            if not finished:
                continue
            cost = lor_cost(events)
            err_msg = lor_error(events)
            log.append("finish %s, cost %s USD%s" % (
                reason, cost, ", error: " + err_msg if err_msg else ""))
            (workdir / "session.json").write_text(json.dumps(
                {"session_id": session_id, "model": route["model"],
                 "reason": reason, "cost_usd": cost}) + "\n")
            ok = reason == "FINISH_REASON_STOP"
            return argv, 0 if ok else 1, text, "\n".join(log), \
                text if ok else "", round(time.time() - started)
        return argv, None, "", "\n".join(log), "", round(time.time() - started)
    finally:
        # Stop the session outright, killing any background work it left, so
        # nothing wakes it into another turn (and no orphan stays on timeout).
        with _ACTIVE_LOR_LOCK:
            _ACTIVE_LOR_SESSIONS.discard(session_id)
        _stop_lor_session(session_id, workdir)


def doctor_failures():
    data = read_json(state_dir() / "doctor.json", {}) or {}
    failed = set()
    for key, row in (data.get("routes") or {}).items():
        if not row.get("ok") and age_hours(row.get("at")) < DOCTOR_TTL_DAYS * 24:
            failed.add(key)
    return failed


def run_seat(seat_key, seat, packet, images, out_dir, timeout, effort,
             prefer=None, lor_only=False, board_dir=None, attachments=None):
    """Try the seat's routes in order until one answers; write post + meta."""
    ordered = select_routes(seat, effort, prefer=prefer, lor_only=lor_only)
    failed = doctor_failures()
    attempts = []
    for route in ordered:
        work = out_dir / "work" / seat_key
        ok, text, detail, seconds = run_route(
            route, packet, images, work, timeout,
            out_dir / "logs" / ("%s.%s.log" % (seat_key, route["harness"])),
            board_dir=board_dir, attachments=attachments)
        attempts.append({"harness": route["harness"], "model": route["model"],
                         "effort": route["effort"], "ok": ok,
                         "seconds": seconds, "detail": detail,
                         "doctor_failed": route_key(route) in failed})
        if ok:
            (out_dir / (seat_key + ".md")).write_text(text)
            parsed = extract_json(text)
            if parsed is not None:
                write_json(out_dir / (seat_key + ".json"), parsed)
            meta = {"seat": seat_key, "model": seat["model"],
                    "name": seat.get("name"), "harness": route["harness"],
                    "route_model": route["model"], "effort": route["effort"],
                    "seconds": seconds, "urls": len(set(URL.findall(text))),
                    "json_ok": parsed is not None,
                    "images_passed": bool(images) and
                    route["harness"] != "agy",
                    "attempts": attempts, "finished": utc_now()}
            if route["harness"] == "lor":
                sess = read_json(work / "session.json") or {}
                if sess.get("session_id"):
                    meta["lor_session_id"] = sess["session_id"]
                if "cost_usd" in sess:
                    meta["cost_usd"] = sess["cost_usd"]
            write_json(out_dir / (seat_key + ".meta.json"), meta)
            return meta
    meta = {"seat": seat_key, "model": seat["model"], "failed": True,
            "attempts": attempts, "finished": utc_now()}
    write_json(out_dir / (seat_key + ".meta.json"), meta)
    return meta


# ------------------------------------------------------------- posts parsing

def extract_json(text):
    """The last fenced JSON object in a post, or None."""
    for block in reversed(FENCE.findall(text or "")):
        try:
            data = json.loads(block)
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    start, end = (text or "").rfind("\n{"), (text or "").rfind("}")
    if 0 <= start < end:
        try:
            data = json.loads(text[start:end + 1])
            return data if isinstance(data, dict) else None
        except ValueError:
            return None
    return None


def strip_json(text):
    """A post without its trailing fenced JSON block."""
    blocks = list(FENCE.finditer(text or ""))
    if not blocks:
        return (text or "").strip()
    last = blocks[-1]
    return (text[:last.start()] + text[last.end():]).strip()


def scrub(text):
    text = SELF_ID[0].sub("I am a panelist", text or "")
    return SELF_ID[1].sub("As a panelist,", text)


def stance_of(position):
    word = str((position or {}).get("stance", "")).strip().lower()
    if word in ("agree", "for", "yes", "true", "hold", "holds"):
        return "agree"
    if word in ("disagree", "against", "no", "false", "reject", "rejects"):
        return "disagree"
    return "uncertain"


def claims_text(data):
    rows = []
    for claim in (data or {}).get("claims") or []:
        if isinstance(claim, dict):
            rows.append("- (%s) %s" % (claim.get("confidence", "?"),
                                       claim.get("claim", "")))
    return "\n".join(rows)


# -------------------------------------------------------------------- boards

def load_board(board):
    board = Path(board).expanduser().resolve()
    meta = read_json(board / "board.json")
    roster = read_json(board / "roster.json")
    if not meta or not roster:
        die("%s is not a conclave board (run new first)" % board)
    return board, meta, roster


def round_dir(board, k):
    return board / ("r%d" % k)


def read_post(board, k, seat):
    path = round_dir(board, k) / (seat + ".md")
    if not path.exists():
        return None
    return {"text": path.read_text(),
            "data": read_json(round_dir(board, k) / (seat + ".json")),
            "meta": read_json(round_dir(board, k) / (seat + ".meta.json"))}


def latest_round(board):
    rounds = [int(p.name[1:]) for p in board.glob("r*")
              if p.is_dir() and p.name[1:].isdigit()]
    return max(rounds) if rounds else None


def images_of(board):
    folder = board / "attachments"
    return sorted(str(p) for p in folder.glob("*")
                  if p.suffix.lower() in IMAGE_SUFFIXES) if folder.exists() \
        else []


def attachments_of(board):
    folder = board / "attachments"
    return sorted(str(p) for p in folder.glob("*") if p.is_file()) \
        if folder.exists() else []


def assign_labels(board_id, k, seats):
    """Per reader, the other seats under shuffled Panelist A/B/C labels."""
    labels = {}
    for reader in seats:
        others = [s for s in seats if s != reader]
        random.Random("%s:%d:%s" % (board_id, k, reader)).shuffle(others)
        labels[reader] = {s: "Panelist %s" % chr(65 + i)
                          for i, s in enumerate(others)}
    return labels


def red_team_seat(k, seats):
    return seats[(k - 1) % len(seats)] if k >= 1 and seats else None


def format_cruxes(cruxes, reader, labels):
    def who(seats):
        names = ["you" if s == reader else labels.get(s, "a panelist")
                 for s in seats]
        return ", ".join(sorted(names, key=lambda n: (n != "you", n))) or "-"
    blocks = []
    for crux in cruxes:
        lines = ["**%s.** %s" % (crux["id"], crux["statement"]),
                 "Agree: %s. Disagree: %s. Uncertain: %s." % (
                     who(crux.get("for", [])), who(crux.get("against", [])),
                     who(crux.get("uncertain", [])))]
        if crux.get("note"):
            lines.append("Moderator's note: %s" % crux["note"])
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def crossexam_packet(reader, k, brief, cruxes, posts, labels, red):
    mine = labels[reader]
    own = posts.get(reader)
    others = []
    for seat in sorted(mine, key=lambda s: mine[s]):
        post = posts.get(seat)
        if not post:
            continue
        body = scrub(strip_json(post["text"]))
        listed = claims_text(post["data"])
        others.append("### %s\n\n%s%s" % (
            mine[seat], body,
            "\n\nClaims and confidences:\n" + listed if listed else ""))
    red_block = render("redteam.md") if reader == red else ""
    return render("crossexam.md", round=str(k), n=str(len(labels)),
                  brief=brief, cruxes=format_cruxes(cruxes, reader, mine),
                  own=own["text"].strip() if own else
                  "(you did not post in the previous round)",
                  others="\n\n".join(others) or "(no other posts)",
                  red_team=red_block)


def run_all(jobs, label):
    """jobs: list of (seat, callable). Runs in parallel, prints as done."""
    results = {}
    started = time.time()
    print("%s: %d seats running in parallel" % (label, len(jobs)), flush=True)
    with concurrent.futures.ThreadPoolExecutor(max(1, len(jobs))) as pool:
        futures = {pool.submit(fn): seat for seat, fn in jobs}
        for future in concurrent.futures.as_completed(futures):
            seat = futures[future]
            try:
                meta = future.result()
            except Exception as exc:  # report and keep the other seats
                meta = {"seat": seat, "failed": True, "error": repr(exc)}
            results[seat] = meta
            print("  [%4ds] %s" % (time.time() - started, seat_line(meta)),
                  flush=True)
    return results


def seat_line(meta):
    if meta.get("failed"):
        tried = "; ".join("%s: %s" % (a["harness"], a["detail"])
                          for a in meta.get("attempts", [])) or \
            meta.get("error", "")
        return "%-10s FAILED  %s" % (meta["seat"], tried)
    fallback = [a for a in meta["attempts"] if not a["ok"]]
    note = ""
    if fallback:
        note = "  (after %s)" % "; ".join(
            "%s: %s" % (a["harness"], a["detail"]) for a in fallback)
    warn = "" if meta["urls"] else "  WARNING: no URLs cited"
    warn += "" if meta["json_ok"] else "  WARNING: no JSON block"
    return "%-10s ok  %s %s@%s  %ss  %d urls%s%s" % (
        meta["seat"], meta["harness"], meta["route_model"], meta["effort"],
        meta["seconds"], meta["urls"], note, warn)


# ------------------------------------------------------------------ commands

def cmd_resolve(args):
    resolve(effort=args.effort, lor_only=resolve_lor_mode(args.lor))


def cmd_doctor(args):
    lor_mode = resolve_lor_mode(args.lor)
    if lor_mode:
        ensure_lor_available()
    roster = current_roster(refresh=args.refresh)
    try:
        request = urllib.request.Request(
            "https://api.github.com/repos/%s/releases?per_page=5" % PROBE_REPO,
            headers={"User-Agent": "brood-conclave"})
        with urllib.request.urlopen(request, timeout=30) as response:
            tags = [r["tag_name"] for r in json.loads(response.read())]
    except (OSError, ValueError, KeyError) as exc:
        die("cannot read the probe's ground truth from GitHub: %s" % exc, 3)
    truths = {t.lower() for t in tags} | {t.lower().lstrip("v") for t in tags}
    wanted = set(args.seats.split(",")) if args.seats else None
    harnesses = set(args.harness.split(",")) if args.harness else (
        {"lor"} if lor_mode else None)
    jobs, seat_routes = [], {}
    for key, seat in roster["seats"].items():
        if wanted and key not in wanted:
            continue
        routes = list(seat.get("routes", []))
        if harnesses and "lor" in harnesses and not any(
                r["harness"] == "lor" for r in routes):
            extra = ensure_lor_route(seat, roster.get("effort", "xhigh"))
            if extra:
                routes.append(extra)
        matched = [r for r in routes
                   if not harnesses or r["harness"] in harnesses]
        seat_routes[key] = matched
        for route in matched:
            jobs.append((key, route))
    results = {}
    print("doctor: %d routes, probe = latest release tag of %s (truth: %s)"
          % (len(jobs), PROBE_REPO, ", ".join(tags[:3])), flush=True)

    def probe(key, route):
        work = state_dir() / "doctor" / ("%s-%s" % (key, route["harness"]))
        ok, text, detail, seconds = run_route(
            route, PROBE_PROMPT, [], work, args.timeout * 60,
            work.parent / ("%s-%s.log" % (key, route["harness"])), min_chars=1)
        found = [t for t in truths if re.search(
            r"(?<![\w.])v?%s(?![\w])" % re.escape(t.lstrip("v")), text.lower())]
        if ok and not found:
            ok, detail = False, "no current tag in answer: %r" % \
                text.strip()[:160]
        return {"seat": key, "ok": ok, "seconds": seconds, "at": utc_now(),
                "detail": "web search verified" if ok else detail}

    with concurrent.futures.ThreadPoolExecutor(max(1, min(8, len(jobs)))) as \
            pool:
        futures = {pool.submit(probe, key, route): route for key, route in jobs}
        for future in concurrent.futures.as_completed(futures):
            route = futures[future]
            row = future.result()
            results[route_key(route)] = row
            print("  %-10s %-8s %-44s %s  %ss  %s" % (
                row["seat"], route["harness"], route["model"] + "@" +
                route["effort"], "PASS" if row["ok"] else "FAIL",
                row["seconds"], row["detail"]), flush=True)
    data = read_json(state_dir() / "doctor.json", {}) or {}
    data.setdefault("routes", {}).update(results)
    write_json(state_dir() / "doctor.json", data)
    known = data["routes"]
    unusable = [k for k, routes in seat_routes.items()
                if not any(known.get(route_key(r), {}).get("ok")
                           for r in routes)]
    if unusable:
        print("NO WORKING ROUTE: %s" % ", ".join(unusable))
        sys.exit(3)


def cmd_new(args):
    board = Path(args.board).expanduser().resolve()
    if (board / "board.json").exists():
        die("%s already has a board" % board)
    brief = Path(args.brief).expanduser()
    if not brief.is_file():
        die("no brief at %s" % brief)
    config = load_config()
    lor_mode = resolve_lor_mode(args.lor, config=config)
    if lor_mode:
        ensure_lor_available()
    roster = current_roster(refresh=args.resolve)
    board_seats = {}
    for k, s in roster["seats"].items():
        entry = dict(s, routes=list(s.get("routes") or []))
        if lor_mode and entry.get("model") and not any(
                r["harness"] == "lor" for r in entry["routes"]):
            extra = ensure_lor_route(entry, args.effort or roster["effort"])
            if extra:
                entry["routes"].append(extra)
        board_seats[k] = entry
    if args.seats:
        seats = args.seats.split(",")
    elif lor_mode:
        seats = [k for k, s in board_seats.items()
                 if any(r["harness"] == "lor" for r in s.get("routes", []))]
    else:
        seats = [k for k, s in board_seats.items() if s.get("routes")]
    unknown = [s for s in seats if s not in board_seats]
    if unknown:
        die("unknown seats: %s" % ", ".join(unknown))
    board.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(brief, board / "brief.md")
    for item in args.attach or []:
        source = Path(item).expanduser()
        (board / "attachments").mkdir(exist_ok=True)
        shutil.copyfile(source, board / "attachments" / source.name)
    board_id = hashlib.sha256(("%s:%s" % (board, utc_now())).encode()
                              ).hexdigest()[:12]
    write_json(board / "board.json", {
        "id": board_id, "created": utc_now(), "seats": seats,
        "effort": args.effort or roster["effort"],
        "max_rounds": args.max_rounds or config.get("max_rounds", 3),
        "lor": lor_mode})
    write_json(board / "roster.json", {
        "resolved_at": roster["resolved_at"],
        "seats": {k: board_seats[k] for k in seats}})
    mode = " (lor subagents)" if lor_mode else ""
    print("board %s%s, seats: %s" % (board, mode, ", ".join(
        "%s=%s" % (k, board_seats[k]["model"]) for k in seats)))
    print("next: conclave.py round %s 0" % shlex.quote(str(board)))


def cmd_round(args):
    board, meta, roster = load_board(args.board)
    lor_mode = resolve_lor_mode(args.lor, board_meta=meta)
    if lor_mode:
        ensure_lor_available()
    k = args.round
    out_dir = round_dir(board, k)
    brief = (board / "brief.md").read_text()
    seats = meta["seats"]
    chosen = args.seats.split(",") if args.seats else seats
    prefer = dict(item.split("=", 1) for item in args.route or [])
    if k == 0:
        packets = {s: render("round0.md", n=str(len(seats)), brief=brief)
                   for s in chosen}
    else:
        cruxes = read_json(board / ("r%d-cruxes.json" % k))
        if not cruxes or not cruxes.get("cruxes"):
            die("write %s first (see SKILL.md, step 5)" %
                (board / ("r%d-cruxes.json" % k)))
        posts = {s: read_post(board, k - 1, s) for s in seats}
        missing = [s for s, p in posts.items() if p is None]
        if missing:
            print("note: no round %d post from %s; left out of the packets"
                  % (k - 1, ", ".join(missing)))
        labels = assign_labels(meta["id"], k, seats)
        red = red_team_seat(k, seats)
        write_json(out_dir / "labels.json", {"labels": labels,
                                             "red_team": red})
        packets = {s: crossexam_packet(s, k, brief, cruxes["cruxes"], posts,
                                       labels, red) for s in chosen}
    images = images_of(board)
    attachments = attachments_of(board)
    jobs = []
    for seat in chosen:
        if (out_dir / (seat + ".md")).exists() and not args.force:
            print("  %-10s already posted in round %d (use --force to rerun)"
                  % (seat, k))
            continue
        (out_dir / "packets").mkdir(parents=True, exist_ok=True)
        (out_dir / "packets" / (seat + ".md")).write_text(packets[seat])
        if args.dry_run:
            routes = select_routes(
                roster["seats"][seat], args.effort or meta["effort"],
                prefer.get(seat), lor_only=lor_mode)
            if not routes:
                print("  %-10s NO ROUTE" % seat)
                continue
            route = routes[0]
            work = out_dir / "work" / seat
            if route["harness"] == "lor":
                title = "conclave " + " ".join(
                    part for part in work.parts[-4:] if part != "work")
                argv = lor_start_argv(route["model"], route["effort"], title,
                                      work / "task.md", work)
            else:
                argv, _, _ = build_argv(
                    route, packets[seat], out_dir / "packets" / (seat + ".md"),
                    images, work, work / "answer.md")
            print("  %-10s %s" % (seat, shlex.join(
                [a if len(a) < 200 else a[:60] + "..." for a in argv])))
            continue
        jobs.append((seat, lambda seat=seat: run_seat(
            seat, roster["seats"][seat], packets[seat], images,
            out_dir, args.timeout * 60, args.effort or meta["effort"],
            prefer.get(seat), lor_only=lor_mode, board_dir=board,
            attachments=attachments)))
    if args.dry_run or not jobs:
        return
    results = run_all(jobs, "round %d" % k)
    failed = [s for s, m in results.items() if m.get("failed")]
    if failed:
        print("FAILED seats: %s (rerun: conclave.py round %s %d --seats %s)"
              % (", ".join(failed), shlex.quote(str(board)), k,
                 ",".join(failed)))
        sys.exit(1)
    print("next: conclave.py digest %s %d" % (shlex.quote(str(board)), k))


def cmd_digest(args):
    board, meta, roster = load_board(args.board)
    k = args.round if args.round is not None else latest_round(board)
    if k is None:
        die("no rounds yet")
    print("# Round %d digest (moderator only: real names)\n" % k)
    cruxes = (read_json(board / ("r%d-cruxes.json" % k)) or {}).get(
        "cruxes", []) if k else []
    for seat in meta["seats"]:
        post = read_post(board, k, seat)
        if not post:
            print("## %s: no post\n" % seat)
            continue
        m = post["meta"] or {}
        data = post["data"] or {}
        print("## %s: %s via %s@%s, %ss, %s urls%s\n" % (
            seat, m.get("model"), m.get("harness"), m.get("effort"),
            m.get("seconds"), m.get("urls"),
            "" if post["data"] else ", NO JSON BLOCK (read the .md)"))
        if data.get("bottom_line"):
            print("Bottom line: %s\n" % data["bottom_line"])
        for position in data.get("positions") or []:
            print("- %s: %s (%s)%s %s" % (
                position.get("crux"), stance_of(position),
                position.get("confidence", "?"),
                " CHANGED, moved by: %s" % position.get("moved_by")
                if position.get("changed") else "", position.get("reason", "")))
        if data.get("claims"):
            print(claims_text(data))
        if data.get("would_change_my_mind"):
            print("\nWould change my mind: %s" % "; ".join(
                str(x) for x in data["would_change_my_mind"]))
        print()
    if k == 0:
        print("next: write %s as\n%s" % (
            board / "r1-cruxes.json", json.dumps({"round": 1, "cruxes": [{
                "id": "C1", "statement": "<a declarative claim the seats "
                "split on>", "for": ["<seat>"], "against": ["<seat>"],
                "uncertain": [], "note": "<optional, neutral>"}]}, indent=2)))
    elif cruxes:
        print("next: conclave.py status %s" % shlex.quote(str(board)))


def crux_table(board, k, seats):
    """{crux id: {seat: position}} from round k's posts."""
    table = {}
    for seat in seats:
        data = read_json(round_dir(board, k) / (seat + ".json")) or {}
        for position in data.get("positions") or []:
            if isinstance(position, dict) and position.get("crux"):
                table.setdefault(str(position["crux"]), {})[seat] = position
    return table


def settled(stances):
    present = [s for s in stances.values()]
    return bool(present) and len(set(present)) == 1 and \
        present[0] != "uncertain"


def cmd_status(args):
    board, meta, roster = load_board(args.board)
    k = latest_round(board)
    if k is None:
        die("no rounds yet")
    seats = meta["seats"]
    if k == 0:
        print("round 0 done. next: digest, then write r1-cruxes.json")
        return
    cruxes = (read_json(board / ("r%d-cruxes.json" % k)) or {}).get(
        "cruxes", [])
    if not cruxes:
        die("round %d has no r%d-cruxes.json" % (k, k))
    table = crux_table(board, k, seats)
    labels = read_json(round_dir(board, k) / "labels.json") or {}
    changes, open_ids = [], []
    print("round %d of at most %d, %d seats, red team: %s\n" % (
        k, meta["max_rounds"], len(seats), labels.get("red_team")))
    for crux in cruxes:
        row = table.get(crux["id"], {})
        stances = {s: stance_of(p) for s, p in row.items()}
        cells = []
        for seat in seats:
            position = row.get(seat)
            if not position:
                cells.append("%s -" % seat)
                continue
            mark = "*" if position.get("changed") else ""
            if mark:
                changes.append("%s on %s" % (seat, crux["id"]))
            cells.append("%s %s(%s)%s" % (seat, stances[seat],
                                          position.get("confidence", "?"),
                                          mark))
        done = settled(stances)
        if not done:
            open_ids.append(crux["id"])
        counts = {v: list(stances.values()).count(v)
                  for v in ("agree", "disagree", "uncertain")}
        print("%-4s %s\n     %s  -> %s" % (
            crux["id"], crux["statement"][:110], "  ".join(cells),
            "SETTLED" if done else "open (%d agree, %d disagree, %d "
            "uncertain)" % (counts["agree"], counts["disagree"],
                            counts["uncertain"])))
    print("\nchanged this round: %s" % (", ".join(changes) or "none"))
    if not open_ids:
        print("next: every crux is settled. Draft statement.md, then ratify.")
    elif not changes:
        print("next: positions are stable. Stop: draft statement.md with the "
              "open cruxes as named dissent, then ratify.")
    elif k >= meta["max_rounds"]:
        print("next: round limit reached. Draft statement.md, then ratify.")
    else:
        print("next: %d open cruxes. conclave.py cruxes %s, then round %d."
              % (len(open_ids), shlex.quote(str(board)), k + 1))


def cmd_cruxes(args):
    board, meta, roster = load_board(args.board)
    k = latest_round(board)
    if not k:
        die("cruxes carries round K>=1 forward; for round 1, write "
            "r1-cruxes.json from the round-0 digest")
    target = board / ("r%d-cruxes.json" % (k + 1))
    if target.exists() and not args.force:
        die("%s exists (use --force)" % target)
    cruxes = (read_json(board / ("r%d-cruxes.json" % k)) or {}).get(
        "cruxes", [])
    table = crux_table(board, k, meta["seats"])
    carried, done = [], []
    for crux in cruxes:
        stances = {s: stance_of(p) for s, p in table.get(crux["id"],
                                                          {}).items()}
        if settled(stances):
            done.append(crux["id"])
            continue
        carried.append(dict(crux, **{
            "for": [s for s, v in stances.items() if v == "agree"],
            "against": [s for s, v in stances.items() if v == "disagree"],
            "uncertain": [s for s, v in stances.items() if v == "uncertain"]}))
    if not carried:
        print("all cruxes settled (%s); draft statement.md and ratify"
              % ", ".join(done))
        return
    write_json(target, {"round": k + 1, "cruxes": carried})
    print("wrote %s: %d open (%s), settled: %s. Edit it to add any new crux "
          "the round surfaced, then: conclave.py round %s %d" % (
              target, len(carried), ", ".join(c["id"] for c in carried),
              ", ".join(done) or "none", shlex.quote(str(board)), k + 1))


def cmd_ratify(args):
    board, meta, roster = load_board(args.board)
    lor_mode = resolve_lor_mode(args.lor, board_meta=meta)
    if lor_mode:
        ensure_lor_available()
    statement = board / "statement.md"
    if not statement.exists():
        die("write %s first" % statement)
    votes = board / "votes"
    passes = [int(p.name[1:]) for p in votes.glob("p*")
              if p.name[1:].isdigit()] if votes.exists() else []
    n = args.pass_number or (max(passes) + 1 if passes else 1)
    out_dir = votes / ("p%d" % n)
    last = latest_round(board)
    chosen = args.seats.split(",") if args.seats else meta["seats"]
    prefer = dict(item.split("=", 1) for item in args.route or [])
    jobs = []
    for seat in chosen:
        own = read_post(board, last, seat) if last is not None else None
        # The whole post, JSON included: the statement quotes bottom lines,
        # and a seat must see its own to check the quote.
        packet = render("ratify.md", statement=statement.read_text(),
                        own=own["text"].strip() if own else "(none)",
                        label="one of %d panelists" % len(meta["seats"]))
        (out_dir / "packets").mkdir(parents=True, exist_ok=True)
        (out_dir / "packets" / (seat + ".md")).write_text(packet)
        jobs.append((seat, lambda seat=seat, packet=packet: run_seat(
            seat, roster["seats"][seat], packet, [], out_dir,
            args.timeout * 60, args.effort or meta["effort"],
            prefer.get(seat), lor_only=lor_mode, board_dir=board)))
    run_all(jobs, "ratification pass %d" % n)
    args.pass_number = n
    cmd_tally(args)


def cmd_tally(args):
    board, meta, roster = load_board(args.board)
    votes = board / "votes"
    passes = sorted(int(p.name[1:]) for p in votes.glob("p*")
                    if p.name[1:].isdigit()) if votes.exists() else []
    if not passes:
        die("no ratification yet")
    n = getattr(args, "pass_number", None) or passes[-1]
    out_dir = votes / ("p%d" % n)
    lines = ["# Ratification pass %d\n" % n, "| seat | model | vote |",
             "|---|---|---|"]
    detail, objections = [], 0
    for seat in meta["seats"]:
        data = read_json(out_dir / (seat + ".json")) or {}
        m = read_json(out_dir / (seat + ".meta.json")) or {}
        vote = str(data.get("vote", "missing" if not m else "unparsed"))
        objections += vote == "object"
        lines.append("| %s | %s | %s |" % (seat, m.get("model", "?"), vote))
        for item in data.get("reservations") or []:
            detail.append("- %s reservation: %s" % (seat, item))
        for edit in data.get("edits") or []:
            if isinstance(edit, dict):
                detail.append("- %s edit: replace %r with %r (%s)" % (
                    seat, edit.get("find"), edit.get("replace"),
                    edit.get("why", "")))
    text = "\n".join(lines + [""] + detail) + "\n"
    (out_dir / "tally.md").write_text(text)
    print(text)
    print("next: %s" % (
        "revise statement.md with the objectors' edits and ratify again "
        "(at most one more pass; then record remaining objections as dissent)"
        if objections else "export the transcript and report to the owner"))


def cmd_export(args):
    board, meta, roster = load_board(args.board)
    out = ["# Conclave transcript: %s\n" % board.name,
           "Created %s. Seats:\n" % meta["created"]]
    for seat in meta["seats"]:
        info = roster["seats"][seat]
        out.append("- %s: %s (%s)" % (seat, info.get("name"), info["model"]))
    out.append("\n## Brief\n\n%s" % (board / "brief.md").read_text().strip())
    last = latest_round(board)
    for k in range(0, (last or 0) + 1):
        out.append("\n## Round %d\n" % k)
        cruxes = read_json(board / ("r%d-cruxes.json" % k))
        if cruxes:
            out.append("### Cruxes\n\n```json\n%s\n```" % json.dumps(
                cruxes, indent=2))
        labels = read_json(round_dir(board, k) / "labels.json")
        if labels:
            out.append("Red team: %s. Labels as each reader saw them: %s" % (
                labels.get("red_team"), json.dumps(labels.get("labels"))))
        for seat in meta["seats"]:
            post = read_post(board, k, seat)
            if post:
                m = post["meta"] or {}
                out.append("\n### %s (%s via %s@%s)\n\n%s" % (
                    seat, m.get("model"), m.get("harness"), m.get("effort"),
                    post["text"].strip()))
    if (board / "statement.md").exists():
        out.append("\n## Statement\n\n%s" % (board / "statement.md"
                                              ).read_text().strip())
    for tally in sorted((board / "votes").glob("p*/tally.md")):
        out.append("\n%s" % tally.read_text().strip())
    path = board / "transcript.md"
    path.write_text("\n".join(out) + "\n")
    print("wrote %s" % path)


def _add_lor_flags(parser):
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--lor", dest="lor", action="store_true", default=None,
        help="run all seats exclusively as Lor subagents (with tools and "
        "web search)")
    group.add_argument(
        "--no-lor", dest="lor", action="store_false",
        help="use the normal harness route chain even if Lor mode is set on "
        "the board, env, or families.json")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="conclave.py", description=__doc__,
                                     formatter_class=argparse.
                                     RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("resolve", help="pick the top model per family")
    p.add_argument("--effort", choices=EFFORTS)
    _add_lor_flags(p)
    p.set_defaults(func=cmd_resolve)

    p = sub.add_parser("doctor", help="prove each route works with web search")
    p.add_argument("--seats", help="comma list of seats to probe")
    p.add_argument("--harness", help="comma list of harnesses to probe")
    p.add_argument("--refresh", action="store_true", help="resolve first")
    p.add_argument("--timeout", type=int, default=15, help="minutes per probe")
    _add_lor_flags(p)
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("new", help="create a board from a brief")
    p.add_argument("board")
    p.add_argument("--brief", required=True)
    p.add_argument("--attach", nargs="*", help="figures or other files")
    p.add_argument("--seats", help="comma list (default: every seat)")
    p.add_argument("--effort", choices=EFFORTS)
    p.add_argument("--max-rounds", type=int)
    p.add_argument("--resolve", action="store_true",
                   help="re-resolve the roster even if it is fresh")
    _add_lor_flags(p)
    p.set_defaults(func=cmd_new)

    p = sub.add_parser("round", help="run one round for every seat")
    p.add_argument("board")
    p.add_argument("round", type=int)
    p.add_argument("--seats", help="comma list (default: every seat)")
    p.add_argument("--route", action="append",
                   help="seat=harness to try that harness first")
    p.add_argument("--effort", choices=EFFORTS)
    p.add_argument("--timeout", type=int, default=60,
                   help="minutes per attempt")
    p.add_argument("--force", action="store_true",
                   help="rerun seats that already posted")
    p.add_argument("--dry-run", action="store_true",
                   help="write packets and print commands only")
    _add_lor_flags(p)
    p.set_defaults(func=cmd_round)

    p = sub.add_parser("digest", help="claims and positions for the moderator")
    p.add_argument("board")
    p.add_argument("round", type=int, nargs="?")
    p.set_defaults(func=cmd_digest)

    p = sub.add_parser("status", help="per-crux stances and the stop rule")
    p.add_argument("board")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("cruxes", help="carry open cruxes to the next round")
    p.add_argument("board")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_cruxes)

    p = sub.add_parser("ratify", help="every seat votes on statement.md")
    p.add_argument("board")
    p.add_argument("--seats")
    p.add_argument("--route", action="append",
                   help="seat=harness to try that harness first")
    p.add_argument("--pass", dest="pass_number", type=int)
    p.add_argument("--effort", choices=EFFORTS)
    p.add_argument("--timeout", type=int, default=30)
    _add_lor_flags(p)
    p.set_defaults(func=cmd_ratify)

    p = sub.add_parser("tally", help="summarize the latest votes")
    p.add_argument("board")
    p.add_argument("--pass", dest="pass_number", type=int)
    p.set_defaults(func=cmd_tally)

    p = sub.add_parser("export", help="transcript with real model names")
    p.add_argument("board")
    p.set_defaults(func=cmd_export)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _on_sigterm)
    main()
