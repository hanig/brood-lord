"""Build continuation units from coordinator anchors and live Git only."""

import copy
import fcntl
import io
import json
import os
import stat
import sys
import tempfile
from contextlib import ExitStack, contextmanager, redirect_stdout
from pathlib import Path

import coordinator_paths as CP
import swarm_authors as SA
import swarm_code_launch as SCL
import swarm_evidence as SE
import swarm_plan_rules as SPR
import swarm_preflight as SP
import swarm_state as SS
import unit as U
from swarm_types import PlanError


def _remote_head(repo, raw, remote, ref):
    SPR.declared_seed({"id": "continue", "kind": "code", "seed": {
        "ref": ref, "base": "0" * 40, "head": "0" * 40}})
    rc, listing, err = SP._git_push_destination(
        repo, raw, remote, 2, "ls-remote", "--exit-code", raw, ref)
    head = SP._exact_remote_ref_head(listing, ref)
    if rc or head is None:
        raise PlanError(f"ref {ref!r} does not resolve: {err}")
    return head


def _continuation(plan, state, args, published=None):
    units = plan.get("units")
    if not isinstance(units, list):
        raise PlanError("plan units must be a JSON list")
    if any(u.get("id") == args.new_id for u in units):
        raise PlanError(f"unit {args.new_id!r} already exists")
    matches = [u for u in units if u.get("id") == args.unit]
    if len(matches) != 1:
        raise PlanError(f"OLD unit {args.unit!r} must occur once in the plan")
    old = matches[0]
    if old.get("kind") != "code":
        raise PlanError("continue requires a code unit")
    us = (state.get("units") or {}).get(args.unit) or {}
    attempt_dir = us.get("attempt_dir")
    if not isinstance(attempt_dir, str) or not attempt_dir:
        raise PlanError(f"unit {args.unit!r} has no recorded attempt")
    attempt = Path(attempt_dir).name
    intent = (us.get("attempt_launch_intents") or {}).get(attempt)
    problem = SCL._code_launch_intent_problem(intent, old, attempt)
    if problem:
        raise PlanError(problem)
    repo, recorded_base = intent["repo"], intent["base_commit"]
    CP.resolve_paths(args.state_dir, plan=plan, extra_repos=[repo])
    target, problem = SP._resolve_dispatch_target(old)
    if problem:
        raise PlanError(problem)
    if target["repo"] != repo:
        raise PlanError("plan repository differs from the anchored repository")
    remote, raw = intent["repository_remote"], target["repository_remote_raw"]
    if (target["repository_remote"] != remote
            or (intent.get("repository_remote_raw") is not None
                and intent["repository_remote_raw"] != raw)):
        raise PlanError("origin push destination differs from the anchored repository")
    ref = (args.head_ref if args.head_ref is not None
           else "refs/heads/" + intent["branch"])
    judged = SE.trusted_produced_head(state, args.unit, attempt_dir)
    head = (_remote_head(repo, raw, remote, ref)
            if args.head_ref is not None or judged is None else judged)
    seed = {"ref": ref, "head": head, "base": recorded_base,
            "evidence": str(Path(attempt_dir) / "evidence.md")}
    SPR.declared_seed({"id": args.new_id, "kind": "code", "seed": seed})
    problem = SP._seed_reachability_problem(seed, repo, raw, remote)
    if problem:
        raise PlanError("recorded base/head ancestry check failed: " + problem)
    target_tip = target["target_commit"]
    problem = SP._seed_reachability_problem(
        {"ref": target["target_ref"], "base": target_tip, "head": target_tip},
        repo, raw, remote)
    if problem:
        raise PlanError(problem)
    rc, base, err = SP._git(repo, "merge-base", target_tip, head)
    if rc:
        raise PlanError("target/head ancestry check failed: " + err)
    seed["base"] = base
    if published is not None:
        frozen = SPR.declared_seed(published)
        seed.update(head=frozen["head"], base=frozen["base"])
        for check in (seed, dict(seed, base=recorded_base),
                      {"ref": target["target_ref"], "base": seed["base"], "head": seed["base"]}):
            problem = SP._seed_reachability_problem(check, repo, raw, remote)
            if problem:
                raise PlanError(problem)
    new = copy.deepcopy(old)
    new.update(id=args.new_id, seed=seed, continuation_of=args.unit, description="Continuation of " + args.unit,
               author_history=SA.collect_author_history(old, us),
               # Byte decoding preserves CRLF in UTF-8 prompts.
               prompt=Path(args.prompt_file).read_bytes().decode("utf-8"))
    return new


@contextmanager
def _plan_file(path, write):
    while True:
        stream = path.open("rb")
        try:
            if write:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            current, opened = path.stat(), os.fstat(stream.fileno())
            if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
                stream.close()
                continue
            yield stream
            return
        finally:
            stream.close()


def _append(path, before, plan, mode):
    temp = None
    try:
        with tempfile.NamedTemporaryFile(dir=str(path.parent), delete=False) as out:
            temp = out.name
            os.fchmod(out.fileno(), stat.S_IMODE(mode))
            out.write(plan if isinstance(plan, bytes) else
                      (json.dumps(plan, indent=2, ensure_ascii=True) + "\n").encode("utf-8"))
            out.flush()
            os.fsync(out.fileno())
        if path.read_bytes() != before:
            raise PlanError("plan changed during continuation validation; retry")
        os.replace(temp, str(path))
        temp = None
    finally:
        if temp is not None:
            os.unlink(temp)


def _repoint(plan, state, old, new):
    dependents = []
    for unit in plan["units"]:
        needs = unit.get("needs", [])
        if not isinstance(needs, list):
            raise PlanError(f"unit {unit['id']!r} needs must be a JSON list")
        if old in needs:
            dependents.append(unit)
    started = []
    for unit in dependents:
        us = (state.get("units") or {}).get(unit["id"]) or {}
        if (us.get("attempts") or us.get("attempt_dir")
                or us.get("attempt_launch_intents")):
            started.append(unit["id"])
    if started:
        raise PlanError("dependents have recorded attempts: " + ", ".join(started))
    for unit in dependents:
        unit["needs"] = [new if need == old else need
                         for need in unit["needs"]]


def _parts(text):
    decoder = json.JSONDecoder(parse_int=lambda n: (n,))
    value = decoder.decode(text)
    if not isinstance(value, (dict, list)):
        raise PlanError("tickets entries must be objects; issues must be a list")
    fields = {} if isinstance(value, dict) else []
    pos = len(text) - len(text.lstrip()) + 1
    while True:
        pos += len(text[pos:]) - len(text[pos:].lstrip())
        if text[pos] in "}]":
            return fields, pos
        if isinstance(fields, dict):
            key, pos = decoder.raw_decode(text, pos)
            if key in fields:
                raise PlanError("duplicate tickets field: " + key)
            pos = text.index(":", pos) + 1
            pos += len(text[pos:]) - len(text[pos:].lstrip())
        start = pos
        _, pos = decoder.raw_decode(text, pos)
        if isinstance(fields, dict):
            fields[key] = (start, pos)
        else:
            fields.append((start, pos))
        pos += len(text[pos:]) - len(text[pos:].lstrip())
        if text[pos] == ",":
            pos += 1


def _ticket_bytes(before, old, new):
    encoding = json.detect_encoding(before)
    prefix = b""
    if encoding in ("utf-16", "utf-32"):
        size = 2 if encoding == "utf-16" else 4
        prefix, before = before[:size], before[size:]
        encoding += "-le" if prefix.startswith(b"\xff\xfe") else "-be"
    text = before.decode(encoding)
    root, _ = _parts(text)
    if "issues" not in root:
        raise PlanError(f"tickets predecessor {old!r} has no entry")
    start, end = root["issues"]
    entries, close = _parts(text[start:end])
    if not isinstance(entries, list):
        raise PlanError("tickets issues must be a JSON list")
    entries = [text[start+a:start+b] for a, b in entries]
    prior = [e for e in entries if json.loads(e, parse_int=lambda n: (n,)).get("unit") == old]
    if len(prior) != 1:
        raise PlanError(f"tickets predecessor {old!r} must have one entry")
    entry = prior[0]
    fields, end = _parts(entry)
    changes = [(fields["unit"], json.dumps(new))]
    if "continuation_of" in fields:
        changes.append((fields["continuation_of"], json.dumps(old)))
    else:
        changes.append(((end, end), ', "continuation_of": ' + json.dumps(old)))
    for (a, b), value in sorted(changes, reverse=True):
        entry = entry[:a] + value + entry[b:]
    existing = [e for e in entries if json.loads(e, parse_int=lambda n: (n,)).get("unit") == new]
    if existing:
        if existing != [entry]:
            raise PlanError(f"tickets successor {new!r} differs from expected entry")
        return prefix + before
    pos = start + close
    return prefix + (text[:pos] + "," + entry + text[pos:]).encode(encoding)


def continue_plan(args, validate):
    try:
        path = Path(args.plan).resolve()
        tickets = getattr(args, "tickets", None)
        repoint = getattr(args, "repoint_dependents", False)
        with ExitStack() as stack:
            stream = stack.enter_context(_plan_file(path, args.write))
            before = stream.read()
            plan = json.loads(before)
            draft_path = Path(tickets).resolve() if tickets else None
            if draft_path is not None and (draft_path == path
                                          or os.path.samefile(path, draft_path)):
                raise PlanError("plan and tickets must be different files")
            draft_changed = False
            if draft_path is not None:
                draft_stream = stack.enter_context(_plan_file(draft_path, args.write))
                draft_before = draft_stream.read()
                draft = _ticket_bytes(draft_before, args.unit, args.new_id)
                draft_changed = draft != draft_before
            state_dir, _, _ = CP.resolve_paths(args.state_dir, plan=plan)
            state, error = U.read_json(state_dir / SS.STATE_FILE)
            if error or not isinstance(state, dict):
                raise PlanError(f"no readable coordinator state: {error}")
            existing = [u for u in plan["units"] if u.get("id") == args.new_id]
            source = copy.deepcopy(plan)
            if tickets or repoint:
                source["units"] = [u for u in source["units"]
                                   if u.get("id") != args.new_id]
            new = _continuation(source, state, args,
                                existing[0] if existing and (tickets or repoint) else None)
            if existing:
                if len(existing) != 1 or existing[0] != new:
                    raise PlanError(f"unit {args.new_id!r} already exists and differs")
            else:
                plan["units"].append(new)
            if repoint:
                _repoint(plan, state, args.unit, args.new_id)
            with redirect_stdout(io.StringIO()):
                validate(plan)
            if args.write:
                if plan != json.loads(before):
                    _append(path, before, plan, os.fstat(stream.fileno()).st_mode)
                if draft_changed:
                    _append(draft_path, draft_before,
                            draft,
                            os.fstat(draft_stream.fileno()).st_mode)
        print(json.dumps(new, indent=2, ensure_ascii=True))
        return 0
    except (PlanError, CP.PathPolicyError, OSError, ValueError,
            TypeError, KeyError, AttributeError) as exc:
        print(f"error: cannot continue: {exc}", file=sys.stderr)
        return 1
