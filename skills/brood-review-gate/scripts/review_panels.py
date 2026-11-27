"""Shared reviewer identities and ordered profile reserves."""

import re
from itertools import combinations

MAKER_ALIASES = {"codex": "openai", "claude": "anthropic", "antigravity": "google"}


def valid_identity(value):
    """A bounded ASCII token, optionally slash-separated; never normalized."""
    return (isinstance(value, str) and 1 <= len(value) <= 256
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:@+-]*"
                             r"(?:/[A-Za-z0-9][A-Za-z0-9_.:@+-]*)*", value)
            is not None)


def model_identity(provider, model):
    """Use the plan contrast rule: vendor segment, otherwise seat provider."""
    if valid_identity(model):
        maker, leaf = model.split("/", 1) if "/" in model else (provider, model)
        if valid_identity(maker) and "/" not in maker:
            return MAKER_ALIASES.get(maker, maker), leaf
    return None


def seat_identity(seat):
    return model_identity(seat.get("provider"), seat.get("model"))


def pin_identity(seat):
    identity = seat_identity(seat)
    return model_identity(identity[0], seat.get("_resolved_from")) if identity else None


def infer_model(model, roster):
    exact, identities = set(), set()
    for seat in roster:
        for value, identity in ((seat.get("model"), seat_identity(seat)),
                                (seat.get("_resolved_from"), pin_identity(seat))):
            if identity:
                identities.add(identity)
                if model == value:
                    exact.add(identity)
    # Prefer an exact model, then the longest qualified suffix.
    while not exact and "/" in model:
        identity = model_identity(None, model)
        if identity in identities:
            return {identity}
        model = model.partition("/")[2]
    return exact


def author_model_ids(authors, roster=(), legacy=False):
    models = set()
    for author in authors:
        matches = [r for r in roster if legacy and r.get("model") == author]
        if legacy and not matches and "/" not in author:
            matches = [r for r in roster if r["name"].casefold() == author.casefold()]
        if matches:
            models.update(seat_identity(r) for r in matches)
        else:
            provider, _, model = author.partition("/")
            maker = model_identity(provider, model)
            if maker and maker[0] not in MAKER_ALIASES.values():
                matches = {seat_identity(r) for r in roster if r.get("model") == author}
                matches = matches or infer_model(model, roster)
                if "/" not in model:
                    maker = None
            models.update(matches or [maker])
    return models


def exclude_authors(reviewers, models):
    """Exclude conflicts and prevent a resolved seat from falling back to one."""
    kept, excluded = [], []
    for reviewer in reviewers:
        identity = seat_identity(reviewer)
        reason = None
        if identity is None or None in models:
            reason = "model maker could not be determined for the seat or an author"
        elif identity in models:
            reason = "authored this change"
        if reason:
            excluded.append(seat_exclusion(reviewer, reason))
        else:
            pin = reviewer.get("_resolved_from")
            if pin and pin_identity(reviewer) in models | {None}:
                reviewer = dict(reviewer, _no_pin_fallback=True)
            kept.append(reviewer)
    return kept, excluded


def seat_exclusion(seat, reason):
    return {"name": seat["name"], "model": seat["model"], "reason": reason}


def reserve_seats(roster, profile):
    return sorted(
        (r for r in roster if r.get("enabled", True)
         and profile in r.get("_reserve_order", {})),
        key=lambda r: r["_reserve_order"][profile])


def panel_makers(panel):
    return {identity[0] for r in panel if (identity := seat_identity(r))}


def reserve_panel(primaries, reserves, size, valid):
    count = size - len(primaries)
    if count <= 0:
        return None
    for added in combinations(reserves, count):
        panel = [*primaries, *added]
        identities = {seat_identity(r) for r in panel}
        if None not in identities and len(identities) == size and valid(panel):
            return panel, list(added)
    return None


def panel_selection(profile, excluded, added, panel):
    return {"profile": profile, "excluded": excluded,
            "substituted": [r["name"] for r in added],
            "providers": sorted({r["provider"] for r in panel}),
            "makers": sorted(panel_makers(panel))}


def configure_reserves(revs, reserves, in_profile, config_error):
    if not isinstance(reserves, dict):
        config_error("reserves must be an object of profile names and seat lists")
    by_name = {r["name"]: r for r in revs}
    for r in revs:
        r.pop("_reserve_order", None)
    for profile, names in reserves.items():
        if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
            config_error(f"reserves for {profile!r} must be a list of seat names")
        for index, name in enumerate(names):
            r = by_name.get(name)
            if r is None or not r.get("enabled", True) or in_profile(r, profile):
                reason = "unknown" if r is None else (
                    "disabled" if not r.get("enabled", True) else "already a primary")
                config_error(f"reserve {name!r} for {profile!r} is {reason}; remove it or name an eligible seat")
            r.setdefault("_reserve_order", {}).setdefault(profile, index)
    for r in revs:
        if in_profile(r, "planning") or "planning" in r.get("_reserve_order", {}):
            maker, model = seat_identity(r) or ("", "")
            if maker == "anthropic" and "opus" in model.casefold():
                config_error(f"Opus seat {r['name']!r} is forbidden; remove it from planning")


def fill_profile(roster, profile, eligible, authors, required, excluded, plan_check,
                 *, substitute_reserves):
    reserves = reserve_seats(roster, profile)
    if not reserves or not excluded:
        return None, False
    if len(eligible) >= required and not (plan_check and plan_check(eligible)):
        return None, False
    candidates, rejected = exclude_authors(substitute_reserves(reserves), authors)
    excluded.extend(rejected)
    return reserve_panel(eligible, candidates, required, lambda panel:
                         plan_check is None or not plan_check(panel)), True
