"""Verdict JSON parsing, claim assessment schema, and disposition map helpers.

Python 3.8+, standard library only; never imports the review facade.
"""

import json
import re
from pathlib import Path


def parse_verdict(text, _depth=0):
    """Models sometimes wrap JSON in fences or prose. Recover the object."""
    if not text or not text.strip():
        return None, "empty response"
    s = text.strip()
    if s.startswith("```"):
        s = s.split("```")[1] if "```" in s[3:] else s[3:]
        if s.lstrip().startswith("json"):
            s = s.lstrip()[4:]
    start, depth, found = None, 0, None
    in_str, esc = False, False
    for i, ch in enumerate(s):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                found = s[start:i + 1]
                break
    if found:
        try:
            obj = json.loads(found)
        except (ValueError, RecursionError) as exc:
            error = f"malformed JSON: {exc}"
        else:
            if "verdict" in obj:
                return obj, None
            if _depth == 0:
                analysis = obj.get("analysis")
                if isinstance(analysis, str):
                    analysis, _ = parse_verdict(analysis, _depth=1)
                if isinstance(analysis, dict) and "verdict" in analysis:
                    return analysis, None
            # A valid object is the reply, not a search space for example verdicts.
            return obj, None
    elif start is not None:
        error = ("response truncated mid-JSON (raise max_output_tokens "
                 f"for this reviewer); {len(s)} chars received")
    else:
        return None, f"no JSON object found in: {text[:150]}"

    decoder = json.JSONDecoder()
    wrapper = re.match(r'\A\{\s*"analysis"\s*:\s*(?=\{)', s)
    if wrapper:
        # Recover only an analysis object missing the outer closing brace.
        try:
            candidate, end = decoder.raw_decode(s, wrapper.end())
        except (ValueError, RecursionError):
            pass
        else:
            if isinstance(candidate, dict) and "verdict" in candidate and not s[end:].strip():
                return candidate, None

    # Otherwise recover only a standalone trailing verdict. Compute the suffix
    # boundary once, skip decoded objects, and bound failed decodes that reread text.
    reply_end = len(s.rstrip())
    if s.endswith("```", 0, reply_end):
        reply_end = len(s[:reply_end - 3].rstrip())
    end = failures = 0
    for line in re.finditer(r"(?m)^[ \t]*\{", s):
        pos = line.end() - 1
        if pos < end:
            continue
        try:
            candidate, end = decoder.raw_decode(s, pos)
        except (ValueError, RecursionError):
            failures += 1
            if failures >= 64:
                break
            continue
        if end == reply_end and isinstance(candidate, dict) and "verdict" in candidate:
            if s[:pos].rstrip().endswith((":", "[", ",")):
                break  # A JSON value continued on the next line isn't standalone.
            return candidate, None
    return None, error


def norm(v):
    """Normalise model-emitted enums case-insensitively."""
    return str(v).strip().lower() if v is not None else ""


CLAIM_STOPWORDS = frozenset("""
claim claims about that this then than there their they when where which while
with without from into onto over under only ever never always cannot must
should would could shall will does done doing have been being were was are
same such each every both either neither also just even more most less least
other another using used uses use make makes made give given gives take taken
case cases thing things text output input value values return returns
""".split())


def claim_key(s):
    """Normalise a claim for matching on distinctive content words."""
    words = [w for w in re.findall(r"[a-z0-9_]+", str(s).lower())
             if len(w) > 3 and w not in CLAIM_STOPWORDS]
    return set(words)


def unassessed_claims(returned, asserted):
    """Asserted claims with no corresponding entry in the reviewer's response.

    Assignment is one-to-one and exclusive: an entry accounts only for the
    claim it most resembles.
    """
    missing = []
    used = set()
    entries = [(j, c) for j, c in enumerate(returned) if isinstance(c, dict)]

    by_index, spent_text = {}, set()
    for j, c in entries:
        idx = c.get("claim_index")
        if not (isinstance(idx, int) and not isinstance(idx, bool)
                and 0 <= idx < len(asserted) and idx not in by_index):
            continue
        if not isinstance(c.get("claim"), str) or not c["claim"].strip():
            continue
        akey, ckey = claim_key(asserted[idx]), claim_key(c["claim"])
        if akey and (not ckey
                     or len(akey & ckey) < max(1, int(0.3 * len(akey)))):
            continue
        sig = frozenset(claim_key(c.get("claim", "")))
        if sig and sig in spent_text:
            continue
        if sig:
            spent_text.add(sig)
        by_index[idx] = j

    for i, a in enumerate(asserted):
        if i in by_index:
            used.add(by_index[i])

    pending, scores = [], []
    for i, a in enumerate(asserted):
        if i in by_index:
            continue
        akey = claim_key(a)
        if not akey:
            missing.append(a[:70] + "  [no distinctive terms; needs claim_index]")
            continue
        pending.append(i)
        need = max(2, int(0.6 * len(akey)))
        for j, c in entries:
            ckey = claim_key(c.get("claim", ""))
            if not ckey or frozenset(ckey) in spent_text:
                continue
            overlap = len(akey & ckey)
            if overlap < need:
                continue
            scores.append((overlap / len(akey | ckey), overlap, i, j))
    best_for_entry = {}
    for score, overlap, i, j in scores:
        prev = best_for_entry.get(j)
        if prev is None or (score, overlap) > prev[0]:
            best_for_entry[j] = ((score, overlap), i)

    scores.sort(reverse=True)
    assigned = {}
    for score, overlap, i, j in scores:
        if i in assigned or j in used:
            continue
        owner = best_for_entry.get(j)
        if owner is not None and owner[1] != i:
            continue
        assigned[i] = j
        used.add(j)
    for i in pending:
        if i not in assigned:
            missing.append(asserted[i][:70])
    return missing


DEFECT_CLASS = re.compile(r"[a-z][a-z0-9_-]{0,63}")


def valid_defect_class(value):
    """Optional premise strike key: an exact lowercase slug, never normalized."""
    return isinstance(value, str) and DEFECT_CLASS.fullmatch(value) is not None


def empty_evidence_value(value):
    """Recognize only null and blank strings, without changing stored bytes."""
    return value is None or (isinstance(value, str) and not value.strip())


def counterexample_placeholder(value):
    """An empty object or one containing only blank/null values is absent."""
    return value is None or (isinstance(value, dict)
                             and all(empty_evidence_value(v) for v in value.values()))


def finding_for_text(finding):
    """Omit optional placeholders from a display copy, never the audit value."""
    return {key: value for key, value in finding.items()
            if not ((key == "counterexample" and counterexample_placeholder(value))
                    or (key == "non_executable_reason" and empty_evidence_value(value)))}


def verdict_schema_error(v, require_claims=0, asserted=None, facade=None):
    """Reject anything that is not actually a structured review verdict."""
    _norm = facade.norm if facade is not None else norm
    _unassessed = (facade.unassessed_claims if facade is not None
                   else unassessed_claims)
    if not isinstance(v, dict):
        return "not an object"
    if _norm(v.get("verdict")) not in ("upheld", "refuted"):
        return f"verdict must be 'upheld' or 'refuted', got {v.get('verdict')!r}"
    for key in ("findings", "claims"):
        if key not in v:
            return f"missing required '{key}' array"
        if not isinstance(v[key], list):
            return f"{key} must be a list"
    for f in v.get("findings") or []:
        if not isinstance(f, dict):
            return "findings must contain objects"
        sev = _norm(f.get("severity"))
        if sev not in ("critical", "major", "minor"):
            return f"finding severity must be critical/major/minor, got {f.get('severity')!r}"
        if _norm(f.get("confidence")) not in ("high", "medium", "low"):
            return f"finding confidence must be high/medium/low, got {f.get('confidence')!r}"
        scen = f.get("failure_scenario")
        if sev in ("critical", "major") and \
                (not isinstance(scen, str) or not scen.strip()):
            return (f"{sev} finding has no failure_scenario: "
                    f"{str(f.get('summary'))[:60]!r}")
        example = f.get("counterexample")
        if not counterexample_placeholder(example):
            if not isinstance(example, dict):
                return "finding counterexample must be an object"
            for field in ("setup", "input", "expected", "observed"):
                value = example.get(field)
                if not isinstance(value, str) or not value.strip():
                    return f"finding counterexample.{field} must be a non-empty string"
        reason = f.get("non_executable_reason")
        if reason is not None and not isinstance(reason, str):
            return "finding non_executable_reason must be a string or null"
    for c in v.get("claims") or []:
        if not isinstance(c, dict):
            return "claims must contain objects"
        if _norm(c.get("status")) not in ("supported", "refuted", "unverifiable"):
            return (f"claim entry has no valid status: "
                    f"{str(c.get('status'))[:40]!r}")
        why = str(c.get("why", "")).strip()
        words = {w for w in re.findall(r"[a-z0-9_]{2,}", why.lower())}
        if len(words) < 4:
            return (f"claim entry's 'why' is not substantive "
                    f"({len(words)} distinct word(s)): {why[:40]!r}")
        if not isinstance(c.get("claim"), str) or not c["claim"].strip():
            return ("claim entry has no claim TEXT (a non-string value does "
                    "not identify a claim); an index alone does not show "
                    "which claim was assessed")
    if require_claims and len(v.get("claims") or []) < require_claims:
        return (f"assessed {len(v.get('claims') or [])} of {require_claims} "
                f"asserted claims")
    if asserted:
        missing = _unassessed(v.get("claims") or [], asserted)
        if missing:
            return f"did not assess: {'; '.join(missing)}"
    return None


def is_confirmed(f, facade=None):
    """A finding counts against the gate only if serious, confident, concrete, and in scope."""
    if f.get("in_scope") is False:
        return False
    _norm = facade.norm if facade is not None else norm
    return (_norm(f.get("severity")) in ("critical", "major")
            and _norm(f.get("confidence")) in ("high", "medium")
            and bool(str(f.get("failure_scenario", "")).strip()))


def reject_duplicate_keys(pairs):
    """JSON object hook that refuses data lost by duplicate-key overwrite."""
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError(f"duplicate object key {key!r}")
        obj[key] = value
    return obj


def load_dispositions(path, facade):
    """Read and validate a digest-keyed prior-finding disposition map."""
    p = Path(path)
    if not p.exists():
        facade.config_error(f"no dispositions file at {path}. Pass an existing JSON "
                            f"file to --dispositions.")
    raw, err = facade.read_text_bounded(p)
    if err:
        facade.config_error(f"cannot read dispositions file {path}: {err}. Pass a "
                            f"readable regular JSON file to --dispositions.")
    try:
        data = json.loads(raw, object_pairs_hook=facade.reject_duplicate_keys)
    except (json.JSONDecodeError, ValueError) as e:
        facade.config_error(f"invalid JSON in dispositions file {path}: {e}. Pass a "
                            f"JSON object keyed by finding digest.")
    if not isinstance(data, dict):
        facade.config_error("--dispositions must contain a JSON object keyed by "
                            "finding digest. Pass an object, not a list or scalar.")

    entries = []
    for digest, entry in data.items():
        if not isinstance(digest, str) or not isinstance(entry, dict):
            facade.config_error("each --dispositions key must be a digest and each "
                                "value must be an object. Pass digest: {...} entries.")
        location = entry.get("location")
        summary = entry.get("summary")
        disposition = entry.get("disposition")
        reason = entry.get("reason")
        if not isinstance(location, str) or not location.strip():
            facade.config_error(f"disposition {digest!r} needs a non-empty location. "
                                f"Pass the prior finding's file:line location.")
        if not isinstance(summary, str) or not summary.strip():
            facade.config_error(f"disposition {digest!r} needs a non-empty summary. "
                                f"Pass the prior confirmed finding's summary.")
        try:
            expected = facade.finding_digest(location, summary)
        except UnicodeEncodeError:
            facade.config_error(
                f"disposition {digest!r} has a location or summary that is "
                f"not valid UTF-8 text. Pass text without lone surrogates.")
        if digest != expected:
            facade.config_error(
                f"disposition key {digest!r} does not match the SHA-256 digest "
                f"of its location and summary (expected {expected}). Recompute "
                f"the key with finding_digest(location, summary), then Pass "
                f"that digest as the map key.")
        if not isinstance(disposition, str) or disposition not in facade.DISPOSITIONS:
            facade.config_error(
                f"disposition {digest} must be reproduced, not-reproduced, or "
                f"deferred, got {disposition!r}. Pass one of those values.")
        if (not isinstance(reason, str) or not reason.strip()
                or reason.splitlines() != [reason]):
            facade.config_error(f"disposition {digest} needs a non-empty one-line "
                                f"reason. Pass the reason without a line break.")
        try:
            reason.encode("utf-8")
        except UnicodeEncodeError:
            facade.config_error(f"disposition {digest!r} has a reason that is not "
                                f"valid UTF-8 text. Pass text without lone surrogates.")
        if "defect_class" in entry and not valid_defect_class(entry["defect_class"]):
            facade.config_error(f"disposition {digest} defect_class must be a lowercase "
                                "slug matching [a-z][a-z0-9_-]{0,63}, or omitted.")
        if disposition in ("reproduced", "not-reproduced"):
            reproduction = entry.get("reproduction")
            if not isinstance(reproduction, dict):
                facade.config_error(f"disposition {digest} needs a reproduction "
                                    "object. Pass setup, command, expected, observed "
                                    "and revision fields.")
            for field in ("setup", "command", "expected", "observed", "revision"):
                value = reproduction.get(field)
                if not isinstance(value, str) or not value.strip():
                    facade.config_error(f"disposition {digest} needs a non-empty "
                                        f"reproduction.{field} string. Pass the "
                                        "recorded reproduction evidence.")
                try:
                    value.encode("utf-8")
                except UnicodeEncodeError:
                    facade.config_error(f"disposition {digest} reproduction.{field} "
                                        "is not valid UTF-8 text. Pass text without "
                                        "lone surrogates.")
            if not facade.valid_head(reproduction["revision"]):
                facade.config_error(f"disposition {digest} reproduction.revision "
                                    "must be a full commit id. Pass 40 or 64 "
                                    "lowercase hexadecimal characters.")
        entries.append(entry)
    return entries


def has_honest_run_claim(claims, honest_run_claim):
    """The required assertion may vary only in case and a terminal period."""
    allowed = {honest_run_claim.casefold(),
               honest_run_claim.rstrip(".").casefold()}
    return any(isinstance(claim, str)
               and claim.strip().casefold() in allowed
               for claim in claims)
