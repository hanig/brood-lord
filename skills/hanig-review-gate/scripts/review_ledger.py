"""Round-ledger reservation, locking, and review-journal adjudication helpers.

Python 3.8+, standard library only; never imports the review facade.
"""

import datetime
import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import time
from pathlib import Path


def change_identity(target="origin/main", plan_name=None, unit=None, facade=None):
    """Hash exact Git identity bytes, never the changing diff or tip commit.

    Fix commits preserve this identity while the target merge-base is unchanged.
    Renaming the branch, changing the remote, rebasing, or changing an explicit
    plan/unit declaration defines another identity; this is not a tamper barrier.
    """
    _sp = facade.subprocess if facade is not None else subprocess

    def git(*argv, fix):
        try:
            result = _sp.run(["git", *argv], stdout=_sp.PIPE,
                             stderr=_sp.PIPE, timeout=15)
        except (OSError, _sp.SubprocessError) as exc:
            raise ValueError("cannot read change identity from Git; " + fix) from exc
        if result.returncode or not result.stdout.endswith(b"\n"):
            raise ValueError("cannot derive change identity; " + fix)
        return result.stdout[:-1].decode("utf-8", "surrogateescape")

    if not target or target.startswith("-"):
        raise ValueError("--target must name a Git commit/ref; pass --target REF")
    for value in (plan_name, unit):
        if value is not None and not value.strip():
            raise ValueError("--plan-name and --unit must be non-empty when given")
    remote = git("remote", "get-url", "origin",
                 fix="run in the intended Git checkout and configure its origin remote")
    target_head = git("rev-parse", "--verify", target + "^{commit}",
                      fix="fetch the target or pass --target REF naming an existing target commit")
    base = git("merge-base", "HEAD", target_head,
               fix="commit the checkout and fetch shared history with --target REF")
    subject = (["plan-unit", plan_name, unit]
               if plan_name is not None or unit is not None
               else ["branch", git("symbolic-ref", "--short", "HEAD",
                                   fix="check out the change branch or pass --plan-name NAME/--unit UNIT")])
    payload = json.dumps([remote, base, subject], ensure_ascii=True,
                         separators=(",", ":"))
    return hashlib.sha256(payload.encode("ascii")).hexdigest()


def round_ledger_root():
    """Enforcing state has one location and no audit-journal fallback."""
    configured = os.environ.get("XDG_STATE_HOME")
    base = Path(configured) if configured else Path.home() / ".local" / "state"
    if not base.is_absolute():
        raise ValueError("round ledger requires an absolute XDG_STATE_HOME or HOME")
    return (base / "hanig-review-gate" / "rounds").resolve()


def round_marker_name(cycle, number):
    return "cycle-{}-round-{}.json".format(cycle, number)


def require_exhausted_rounds(entries, max_rounds):
    """A validated prefix is exhausted only when all max_rounds exist."""
    if entries[-1].get("attempts", [{}])[-1].get("released_at"):
        raise ValueError("released round is not exhausted")
    cycle = entries[-1]["cycle"]
    missing = list(range(entries[-1]["round"] + 1, max_rounds + 1))
    if missing:
        raise ValueError("cycle is not exhausted; missing rounds: {}; files: {}".format(
            ", ".join(map(str, missing)), ", ".join(
                round_marker_name(cycle, number) for number in missing)))


def validate_round_history(directory, identity, facade):
    """Read one closed grammar under the identity lock; return its last cycle.

    This validates present shape, not write order, deleted history or the
    authenticity of hand-edited bytes. No audit journal is consulted.
    """
    max_rounds = facade.MAX_ROUNDS
    ladder = facade.LADDER
    max_releases = facade.MAX_ROUND_RELEASES
    cycles = {}
    for name in sorted(os.listdir(directory)):
        if name == "lock":
            continue
        match = re.fullmatch(r"cycle-([1-9][0-9]*)-round-([1-9][0-9]*)\.json", name)
        if match is None:
            raise ValueError("unrecognized round ledger entry: {}".format(name))
        try:
            cycle, number = map(int, match.groups())
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                         dir_fd=directory)
            with os.fdopen(fd, "r", encoding="utf-8") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise ValueError("marker is not a regular file")
                content = stream.read(4097)
            if len(content) > 4096:
                raise ValueError("oversized marker")
            record = json.loads(content, object_pairs_hook=facade.reject_duplicate_keys)
            if (not isinstance(record, dict)
                    or set(record) not in (
                        {"identity", "cycle", "round", "profile", "predecessor"},
                        {"identity", "cycle", "round", "profile", "predecessor", "attempts"})
                    or record["identity"] != identity
                    or type(record["cycle"]) is not int or record["cycle"] != cycle
                    or type(record["round"]) is not int or record["round"] != number
                    or not 1 <= number <= max_rounds
                    or record["profile"] not in ("plan", *ladder)):
                raise ValueError("invalid marker fields")
            if "attempts" in record:
                attempts = record["attempts"]
                if not isinstance(attempts, list) or not 1 <= len(attempts) <= max_releases + 1:
                    raise ValueError("invalid reservation attempts")
                tokens = set()
                for index, attempt in enumerate(attempts):
                    if (not isinstance(attempt, dict)
                            or set(attempt) not in ({"token", "reserved_at"},
                                {"token", "reserved_at", "released_at", "verdict", "completed"})
                            or not isinstance(attempt.get("token"), str)
                            or not re.fullmatch(r"[0-9a-f]{32}", attempt["token"])
                            or attempt["token"] in tokens):
                        raise ValueError("invalid reservation attempt")
                    tokens.add(attempt["token"])
                    released = "released_at" in attempt
                    if ((index < len(attempts) - 1 and not released)
                            or (released and (index >= max_releases
                                or attempt["verdict"] != "REVIEW_UNAVAILABLE"
                                or type(attempt["completed"]) is not int
                                or attempt["completed"] != 0))):
                        raise ValueError("invalid round release")
                    for field in (("reserved_at", "released_at") if released else ("reserved_at",)):
                        if (not isinstance(attempt[field], str)
                                or not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d{4}", attempt[field])):
                            raise ValueError("invalid reservation timestamp")
                        datetime.datetime.strptime(attempt[field], "%Y-%m-%dT%H:%M:%S%z")
        except (OSError, ValueError, TypeError, RecursionError) as exc:
            raise ValueError("invalid round ledger marker {}: {}".format(name, exc)) from exc
        cycles.setdefault(cycle, []).append(record)

    previous = []
    for expected_cycle, cycle in enumerate(sorted(cycles), 1):
        entries = sorted(cycles[cycle], key=lambda r: r["round"])
        names = ", ".join(facade.round_marker_name(cycle, e["round"]) for e in entries)
        if cycle != expected_cycle:
            raise ValueError("{}: missing predecessor cycle; expected {}".format(
                names, facade.round_marker_name(expected_cycle, 1)))
        missing = sorted(set(range(1, entries[-1]["round"] + 1))
                         - {e["round"] for e in entries})
        if missing:
            raise ValueError("{}: missing rounds: {}; files: {}".format(
                names, ", ".join(map(str, missing)), ", ".join(
                    facade.round_marker_name(cycle, number) for number in missing)))
        if any(e.get("attempts", [{}])[-1].get("released_at") for e in entries[:-1]):
            raise ValueError("{}: released round before a later reservation".format(names))
        if any(e["profile"] != entries[0]["profile"] for e in entries):
            raise ValueError("{}: profiles differ within cycle".format(names))
        expected = None
        if previous:
            try:
                facade.require_exhausted_rounds(previous)
            except ValueError as exc:
                raise ValueError("{}: incomplete predecessor: {}".format(names, exc)) from exc
            expected = {"cycle": cycle - 1, "profile": previous[0]["profile"]}
        elif isinstance(entries[0]["predecessor"], dict):
            declaration = entries[0]["predecessor"]
            if (declaration.get("cycle") == "none declared"
                    and declaration.get("profile") in ladder):
                expected = {"cycle": "none declared", "profile": declaration["profile"]}
        for entry in entries:
            predecessor = entry["predecessor"]
            if (predecessor != expected or (expected is not None and (
                    not isinstance(predecessor, dict)
                    or (previous and type(predecessor.get("cycle")) is not int)))):
                raise ValueError("{}: invalid round ledger predecessor".format(
                    facade.round_marker_name(cycle, entry["round"])))
        previous = entries
    return previous


def acquire_round_lock(lock, facade):
    """Wait for a short reservation critical section, with a monotonic bound."""
    wait_seconds = facade.ROUND_LOCK_WAIT_SECONDS
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError as exc:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError("round reservation lock wait exceeded {} seconds; "
                                 "inspect the ledger before retrying".format(
                                     wait_seconds)) from exc
            time.sleep(min(0.05, remaining))


def replace_round_marker(directory, name, record):
    """Stage outside the identity grammar; rename is the settlement point."""
    parent = os.open("..", os.O_RDONLY | os.O_DIRECTORY, dir_fd=directory)
    pending = ".round-pending-" + os.urandom(16).hex()
    fd = None
    try:
        fd = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(record, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(pending, name, src_dir_fd=parent, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        try:
            if fd is not None:
                os.unlink(pending, dir_fd=parent)
        except FileNotFoundError:
            pass
        finally:
            os.close(parent)


def reserve_round(identity, round_no, profile, fresh_from, facade):
    """Reserve under flock and O_EXCL; only settled zero-completion unavailable
    runs may release, five times. Partial markers fail closed. No audit access.
    """
    max_rounds = facade.MAX_ROUNDS
    ladder = facade.LADDER
    if (not re.fullmatch(r"[0-9a-f]{64}", identity)
            or type(round_no) is not int or not 1 <= round_no <= max_rounds
            or profile not in ("plan", *ladder)
            or (fresh_from is not None and fresh_from not in ladder)):
        raise ValueError("invalid round reservation")
    ledger = facade.round_ledger_root() / identity
    directory = facade._open_directory_chain(ledger)
    try:
        lock = os.open("lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
                       0o600, dir_fd=directory)
        try:
            if not stat.S_ISREG(os.fstat(lock).st_mode):
                raise ValueError("lock: not a regular file")
            facade.acquire_round_lock(lock)
            entries = facade.validate_round_history(directory, identity)
            last = entries[-1] if entries else None
            cycle = last["cycle"] if last else 1
            predecessor = last["predecessor"] if last else None
            names = ", ".join(facade.round_marker_name(cycle, e["round"]) for e in entries)
            retry = bool(last and last.get("attempts", [{}])[-1].get("released_at"))
            if retry:
                if (round_no != last["round"] or profile != last["profile"]
                        or fresh_from != (predecessor["profile"] if predecessor else None)):
                    raise ValueError("released round must be reserved again with the same profile and predecessor")
            elif last and round_no == 1 and fresh_from is not None:
                facade.require_exhausted_rounds(entries)
                if fresh_from != last["profile"]:
                    raise ValueError("{}: --fresh-cycle-from must name the exhausted "
                                     "cycle's profile".format(names))
                predecessor = {"cycle": cycle, "profile": last["profile"]}
                cycle += 1
            else:
                if last and round_no <= last["round"]:
                    raise ValueError("{}: round is already reserved or lower than the last "
                                     "reservation; use the next round, or after exhaustion "
                                     "--round 1 --fresh-cycle-from PROFILE".format(names))
                if round_no != (last["round"] + 1 if last else 1):
                    raise ValueError("{}: cannot reserve round {}; missing round {}; file: {}".format(
                        names, round_no, round_no - 1, facade.round_marker_name(cycle, round_no - 1)))
                if last and profile != last["profile"]:
                    raise ValueError("{}: profile must match this cycle's {}".format(
                        names, last["profile"]))
                if last is None and fresh_from is not None:
                    predecessor = {"cycle": "none declared", "profile": fresh_from}
                expected = predecessor["profile"] if predecessor else None
                if fresh_from != expected:
                    raise ValueError("{}: --fresh-cycle-from must match this cycle's "
                                     "recorded predecessor; a new cycle needs exhaustion".format(names))
            record = {"identity": identity, "cycle": cycle, "round": round_no,
                      "profile": profile, "predecessor": predecessor}
            record["attempts"] = (last["attempts"][:] if retry else []) + [
                {"token": os.urandom(16).hex(), "reserved_at": facade.now()}]
            name = facade.round_marker_name(cycle, round_no)
            if retry:
                facade.replace_round_marker(directory, name, record)
                return record
            try:
                fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                             0o600, dir_fd=directory)
            except FileExistsError as exc:
                raise ValueError("round is already reserved; marker retained: {}".format(name)) from exc
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(record, stream, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.fsync(directory)
            except OSError as exc:
                cleanup_error = ""
                try:
                    os.unlink(name, dir_fd=directory)
                except OSError as unlink_exc:
                    cleanup_error = "; marker removal failed for {}: {}".format(name, unlink_exc)
                raise ValueError("directory fsync failed for {}: {}; reservation was not "
                                 "recorded{}".format(ledger, exc, cleanup_error)) from exc
            return record
        finally:
            os.close(lock)
    finally:
        os.close(directory)


def reserve_review_round(args, facade):
    """CLI boundary: ledger failure is a review error, never an audit warning."""
    try:
        identity = facade.change_identity(
            "origin/main" if args.target is None else args.target, args.plan_name, args.unit)
    except (OSError, ValueError, RuntimeError) as exc:
        facade.config_error("implementation identity required: {}. Pass a stable "
                            "--plan-name/--unit when branch identity is unavailable; "
                            "origin and a shared target commit are still required.".format(exc))
    try:
        record = facade.reserve_round(identity, args.round, args.selected_profile,
                                      args.fresh_cycle_from)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        facade.config_error("round reservation refused: {}. Inspect the enforcing ledger "
                            "and XDG_STATE_HOME/HOME; pass the next admissible round; do not delete "
                            "reservations to restart a review.".format(exc))
    args.round_reservation = (facade.round_ledger_root() / identity, record)
    return record


def release_round(args, completed, verdict, facade):
    """Release our settled zero-completion unavailable round, never a watchdog's."""
    if verdict != "REVIEW_UNAVAILABLE" or completed:
        return
    ledger, owned = args.round_reservation
    directory = None
    try:
        directory = facade._open_directory_chain(ledger)
        lock = os.open("lock", os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
                       dir_fd=directory)
        try:
            facade.acquire_round_lock(lock)
            entries = facade.validate_round_history(directory, owned["identity"])
            if owned not in entries:
                raise ValueError("reservation no longer belongs to this run")
            if entries[-1] != owned:
                print("Round kept: later round reserved.", file=sys.stderr)
                return
            attempts = owned["attempts"]
            if len(attempts) > facade.MAX_ROUND_RELEASES:
                print("Round kept: release cap of 5 reached.", file=sys.stderr)
                return
            record = dict(owned, attempts=attempts[:-1] + [dict(
                attempts[-1], released_at=facade.now(), verdict=verdict, completed=0)])
            name = facade.round_marker_name(owned["cycle"], owned["round"])
            facade.replace_round_marker(directory, name, record)
        finally:
            os.close(lock)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        facade.config_error("round release refused: {}. Pass ledger {} to the operator for inspection.".format(exc, ledger))
    finally:
        if directory is not None:
            os.close(directory)


def finding_digest(location, summary):
    """Stable key for a prior finding's human-visible identity."""
    identity = json.dumps([location, summary], ensure_ascii=False,
                          separators=(",", ":"))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def read_review_journal(path, facade):
    """Read canonical records only; damaged/unreadable history is not empty."""
    try:
        events = sorted(Path(path).iterdir())
    except FileNotFoundError:
        return []
    records = []
    for event in events:
        record_path = event / "record.jsonl"
        try:
            record_path.lstat()
        except FileNotFoundError:
            continue
        raw, error = facade.read_text_bounded(record_path)
        if error:
            raise ValueError(f"cannot read {record_path}: {error}")
        if not raw.endswith("\n") or raw.count("\n") != 1:
            raise ValueError(f"{record_path} is not one JSON line")
        record = json.loads(raw, object_pairs_hook=facade.reject_duplicate_keys)
        if not isinstance(record, dict):
            raise ValueError(f"{record_path} is not a JSON object")
        records.append((record_path, record))
    return records


def adjudicator_is_author(accepted_by, authors, facade):
    """A bare acceptor can be a human name or a legacy model leaf."""
    if "/" in accepted_by:
        roster = [dict(zip(("provider", "model"), value.split("/", 1)))
                  for value in [accepted_by, *authors] if "/" in value]
        return bool(facade.author_model_ids([accepted_by], roster)
                    & facade.author_model_ids(authors, roster))
    return any(author.partition("/")[2] == accepted_by for author in authors)


SEAT_ROUTES = ("lor", "cli_slurm", "cli_local", "api")
SEAT_STATUSES = ("completed", "failed", "unavailable")


def _job_id(value):
    return isinstance(value, str) and bool(value.strip())


def seat_job_problems(name, row):
    """slurm_job_id is a non-empty string; slurm_job_ids, when present, is a
    non-empty list of non-empty strings ending with slurm_job_id."""
    problems = []
    if "slurm_job_id" in row and not _job_id(row["slurm_job_id"]):
        problems.append(f"seat_routes[{name!r}].slurm_job_id is not a non-empty string")
    if "slurm_job_ids" in row:
        ids = row["slurm_job_ids"]
        if not (isinstance(ids, list) and ids and all(_job_id(i) for i in ids)):
            problems.append(f"seat_routes[{name!r}].slurm_job_ids is not a non-empty "
                            "list of non-empty strings")
        elif row.get("slurm_job_id") != ids[-1]:
            problems.append(f"seat_routes[{name!r}].slurm_job_ids does not end with "
                            "its slurm_job_id")
    return problems


def journal_identity_problems(record, allow_unbound, facade):
    """Check every persisted identity without inferring or repairing it."""
    if record.get("type") == "evidence":
        return facade.evidence_record_problems(record)
    problems = []

    def check(field):
        if field not in record or not facade.valid_ledger_field(field, record[field]):
            problems.append(field + " is missing or invalid")

    for field in ("type", "schema_version", "round"):
        check(field)
    if not (allow_unbound and record.get("reviewed_head") is None
            and "reviewed_head" in record and record.get("type") == "review_round"):
        check("reviewed_head")
    if record.get("type") == "adjudication":
        for field in ("finding_digest", "decision", "accepted_by", "timestamp"):
            check(field)
        authors = record.get("author")
        if (not isinstance(authors, list) or not authors
                or any(not facade.valid_author(author) for author in authors)):
            problems.append("author must be a nonempty list of PROVIDER/MODEL tokens")
        elif facade.valid_identity(record.get("accepted_by")) and facade.adjudicator_is_author(
                record["accepted_by"], authors):
            problems.append("self-accepted adjudication record")
        if not isinstance(record.get("reason"), str) or not record["reason"].strip():
            problems.append("reason must be nonempty text")
        if "notes" in record and not isinstance(record["notes"], str):
            problems.append("notes must be text")
    elif record.get("type") == "review_round":
        for field in ("kind", "date", "verdict"):
            check(field)
        digests = record.get("claim_digests")
        if not isinstance(digests, list) or any(not facade.valid_digest(d) for d in digests):
            problems.append("claim_digests is missing or contains an invalid digest")
        results = record.get("results")
        if not isinstance(results, list):
            problems.append("results is missing or not a list")
        else:
            for i, result in enumerate(results):
                if (not isinstance(result, dict)
                        or not isinstance(result.get("findings"), list)):
                    problems.append(f"results[{i}].findings is missing or not a list")
                    continue
                for j, finding in enumerate(result["findings"]):
                    if (not isinstance(finding, dict)
                            or not facade.valid_digest(finding.get("finding_digest"))):
                        problems.append(f"results[{i}].findings[{j}].finding_digest "
                                        "is missing or not a SHA-256 digest")
                    if (not isinstance(finding, dict)
                            or not facade.valid_ledger_field("confirmed", finding.get("confirmed"))):
                        problems.append(f"results[{i}].findings[{j}].confirmed "
                                        "is missing or not a boolean")
        routes = record.get("seat_routes")
        if routes is not None and not isinstance(routes, dict):
            problems.append("seat_routes is not an object")
        for name, row in (routes.items() if isinstance(routes, dict) else ()):
            if (not isinstance(row, dict) or row.get("status") not in SEAT_STATUSES
                    or row.get("route") not in SEAT_ROUTES + (None,)
                    or any(not isinstance(row.get(key), (str, type(None)))
                           for key in ("provider", "model"))):
                problems.append(f"seat_routes[{name!r}] needs provider, model, a known "
                                "route and a completed/failed/unavailable status")
                continue
            problems.extend(seat_job_problems(name, row))
    else:
        problems.append("record type is missing or unknown")
    return problems


def validate_journal_record(record, allow_unbound, facade):
    """Refuse invalid decision data before redaction or publication."""
    problems = facade.journal_identity_problems(record, allow_unbound=allow_unbound)
    if problems:
        raise ValueError("invalid ledger record: " + "; ".join(problems))


def journal_findings(records, head, facade):
    """Attribute decoded records only by their exact, valid recorded head."""
    findings, adjudicated, unattributable, unreadable = {}, set(), [], []
    for record_path, record in records:
        recorded_head = record.get("reviewed_head")
        if facade.valid_head(recorded_head) and recorded_head != head:
            continue
        problems = facade.journal_identity_problems(record)
        if problems:
            damaged = {
                "record_path": str(record_path), "problems": problems,
                "reviewed_head": recorded_head,
                "type": record.get("type"), "round": record.get("round"),
            }
            if facade.valid_head(recorded_head):
                unattributable.append(damaged)
            else:
                unreadable.append(damaged)
            continue
        if record["type"] == "evidence":
            continue
        round_no = record["round"]
        if record["type"] == "adjudication":
            adjudicated.add((round_no, record["finding_digest"]))
            continue
        for result in record["results"]:
            for finding in result["findings"]:
                if not finding["confirmed"]:
                    continue
                digest = finding.get("finding_digest")
                findings[(round_no, digest)] = {
                    **finding, "round": round_no, "reviewed_head": head,
                    "reviewer": result.get("name"),
                }
    return findings, adjudicated, unattributable, unreadable


def record_adjudication(args, round_no, facade):
    """Append the external decision without revising any review verdict."""
    record = {
        "type": "adjudication", "schema_version": 2,
        "finding_digest": args.adjudicate, "round": round_no,
        "reviewed_head": args.head, "decision": args.decision,
        "accepted_by": args.accepted_by, "reason": args.reason,
        "author": list(args.author), "timestamp": facade.journal_timestamp(),
    }
    facade.validate_journal_record(record)

    def write():
        line = json.dumps(facade.redact_ledger(record), sort_keys=True) + "\n"
        return facade._run_journal_append(args.file, line)
    return facade._record_journal(write)


def record_evidence(record, facade):
    """Append evidence; no verdict or severity is revised."""
    facade.validate_journal_record(record)
    line = json.dumps(facade.redact_ledger(record), sort_keys=True) + "\n"
    return facade._record_journal(lambda: facade._run_journal_append([], line))

