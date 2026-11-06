"""Audit-only Codex counters. Never read this ledger to decide a transition.

Only counters, model and validated timestamps cross the rollout boundary.
Python 3.8+ (zoneinfo is loaded only by reporting on Python 3.9+).
"""
import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

COUNTERS = ("calls", "input_tokens", "cached_input_tokens",
            "cache_write_input_tokens", "output_tokens", "reasoning_output_tokens")


def _zero():
    return dict.fromkeys(COUNTERS, 0)


def _time(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp needs timezone")
    return parsed.astimezone(timezone.utc).isoformat()


def _add(target, source):
    for key in COUNTERS:
        target[key] = target.get(key, 0) + source[key]


def harvest(codex_home):
    """Read rollout counters without retaining any other event payload.

    Missing optional counters mean zero in the observed format. Missing input
    or output counts, model, or timestamp makes measurement unknown.
    """
    home = Path(codex_home)
    files = sorted((home / "sessions").glob("**/rollout-*.jsonl"))
    result = {"status": "none", "models": {}, "events": [],
              "skipped_lines": 0, "rollout_files": len(files),
              "rollout_set": hashlib.sha256(json.dumps([
                  str(p.relative_to(home)) for p in files]).encode()).hexdigest()}
    bad_format = False
    for path in files:
        model = None
        seen = set()
        with path.open("rb") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                    kind = event.get("type")
                    payload = event.get("payload")
                    if kind == "turn_context":
                        model = payload.get("model")
                        if not isinstance(model, str) or not model:
                            model = None
                        continue
                    if kind != "event_msg" or not isinstance(payload, dict):
                        continue
                    if payload.get("type") != "token_count":
                        continue
                    info = payload.get("info")
                    # Codex also emits token_count rate-limit notifications.
                    if info is None:
                        continue
                    usage = info["last_token_usage"]
                    stamp = _time(event["timestamp"])
                    if model is None or not isinstance(usage, dict):
                        raise ValueError("unrecognized usage")
                    counts = {key: usage.get(key, 0) for key in COUNTERS[1:]}
                    if not all(type(v) is int and v >= 0 for v in counts.values()):
                        raise ValueError("invalid counter")
                    if not all(k in usage for k in ("input_tokens", "output_tokens")):
                        raise ValueError("missing counter")
                    counts["calls"] = 1
                    total = result["models"].setdefault(model, dict(
                        _zero(), first_timestamp=stamp, last_timestamp=stamp,
                        rollout_files=0))
                    _add(total, counts)
                    total["first_timestamp"] = min(total["first_timestamp"], stamp)
                    total["last_timestamp"] = max(total["last_timestamp"], stamp)
                    if model not in seen:
                        total["rollout_files"] += 1
                        seen.add(model)
                    result["events"].append(dict(counts, model=model, timestamp=stamp))
                except (ValueError, TypeError, AttributeError, KeyError, RecursionError):
                    result["skipped_lines"] += 1
                    # Syntactically malformed lines are skipped. An identifiable
                    # usage event with another schema cannot be guessed.
                    try:
                        probe = json.loads(line)
                        if (probe.get("type") == "event_msg"
                                and probe.get("payload", {}).get("type") == "token_count"):
                            bad_format = True
                    except (ValueError, TypeError, AttributeError, RecursionError):
                        pass
    if files:
        result["status"] = "unknown" if bad_format or not result["events"] else "ok"
    return result


def _atomic(path, record):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".usage-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(record, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def record(state_dir, unit, attempt, home, terminal_state=None):
    """Best effort, including storage failures. No caller may rely on success."""
    try:
        if Path(attempt).name != attempt or attempt in ("", ".", ".."):
            return
        path = Path(state_dir) / "usage" / (attempt + ".json")
        try:
            current = harvest(home)
        except Exception as exc:
            current = {"status": "unknown", "error": type(exc).__name__}
        previous = None
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        if previous and previous.get("models"):
            same = previous.get("rollout_set") == current.get("rollout_set")
            lower = any(current.get("models", {}).get(m, {}).get(k, 0) < v[k]
                        for m, v in previous["models"].items() for k in COUNTERS)
            if current["status"] in ("none", "unknown") or (same and lower):
                # Retain the last measurement even if a home vanished or the
                # reader failed. Unknown never becomes a zero measurement.
                old = dict(previous)
                if same and lower:
                    for model, counts in current.get("models", {}).items():
                        saved = old["models"].setdefault(model, dict(counts))
                        for key in COUNTERS:
                            saved[key] = max(saved[key], counts[key])
                old["status"] = ("unknown" if current["status"] == "unknown"
                                 else "regressed")
                if "error" in current:
                    old["error"] = current["error"]
                current = old
        current.update(unit_id=unit, attempt_id=attempt,
                       terminal_state=terminal_state,
                       harvested_at=datetime.now(timezone.utc).isoformat())
        _atomic(path, current)
    except Exception:
        # Audit storage must never block dispatch, judging, merge or cleanup.
        pass


def refresh(state_dir, state):
    """Refresh surviving homes, including pending terminal cleanup, audit only."""
    try:
        for uid, us in state.get("units", {}).items():
            for attempt, meta in (us.get("attempt_codex_homes") or {}).items():
                if isinstance(meta, dict) and not meta.get("removed"):
                    terminal = None
                    if (str(us.get("attempt_dir", "")).endswith("/" + attempt)
                            and us.get("state") in ("DONE", "FAILED", "CANCELLED",
                                "ABANDONED", "SUPERSEDED", "READY_FOR_PR", "NEEDS_HUMAN")):
                        terminal = us["state"]
                    record(state_dir, uid, attempt, meta.get("path"), terminal)
    except Exception:
        pass


def report(state_dir, plan, default_provider):
    """Read durable measurements only; never create or refresh any files."""
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(os.environ.get("HANIG_REPORT_TZ", "UTC"))
    result = {"units": {}, "models": {}, "days": {}, "grand_total": _zero(),
              "unknown": [], "regressed": [], "unmeasured": []}
    for unit in plan.get("units", []):
        if unit.get("kind") == "code" and not unit.get("provider", default_provider).startswith("codex/"):
            result["unmeasured"].append(unit["id"])
    for path in sorted((Path(state_dir) / "usage").glob("*.json")):
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
            if rec["status"] in ("unknown", "regressed"):
                result[rec["status"]].append(rec)
                continue
            uid, attempt = rec["unit_id"], rec["attempt_id"]
            if uid not in {u["id"] for u in plan.get("units", [])}:
                continue
            unit = result["units"].setdefault(uid, {"attempts": {}, "models": {}, "total": _zero()})
            unit["attempts"][attempt] = rec
            for model, counts in rec["models"].items():
                _add(unit["models"].setdefault(model, _zero()), counts)
                _add(result["models"].setdefault(model, _zero()), counts)
                _add(unit["total"], counts)
                _add(result["grand_total"], counts)
            for event in rec["events"]:
                day = datetime.fromisoformat(event["timestamp"]).astimezone(tz).date().isoformat()
                _add(result["days"].setdefault(day, _zero()), event)
        except (OSError, ValueError, KeyError, TypeError):
            result["unknown"].append({"status": "unknown", "error": "UnreadableRecord"})
    result["timezone"] = str(tz)
    return result
