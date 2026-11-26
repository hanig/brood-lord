#!/usr/bin/env python3
"""Wait for a coordinator state event, without inspecting worker liveness.

With --poll-lor it ALSO polls in-flight native Lor coding attempts (backend
"lor" in coordinator state) through hanig-swarm's lor_io, because those have
no detached watcher: it emits `lor_session_finished` once a Lor session
leaves `running`, and exits EXIT_LOR_AUTH (4, distinct from the timeout/no
event code 3) when Lor rejects the session token, so token expiry is never
reported as "still running".
"""
import argparse
import datetime
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import time

import orch_env

QUIET = {"UNALLOCATED", "SUBMITTED", "RUNNING", "DONE", "ABANDONED", "SUPERSEDED"}
EXIT_LOR_AUTH = 4
LOR_LIVE_STATES = {"ALLOCATED", "SUBMITTED", "RUNNING", "INCOMPLETE"}
LOR_SNAPSHOT_PREFIX = "lor:"


def load_lor_io():
  """hanig-swarm's Lor adapter, from the installed sibling skill only."""
  import skill_paths
  swarm = skill_paths.sibling_skill_root(
    Path(__file__).absolute().parents[1], "hanig-orchestrate", "hanig-swarm")
  scripts = str(swarm / "scripts")
  if scripts not in sys.path:
    sys.path.insert(0, scripts)
  import lor_io
  return lor_io


def lor_attempts(directory):
  """In-flight lor attempts recorded in one state directory: (uid, sid, parent)."""
  units = json.loads((directory / "swarm-state.json").read_bytes()).get("units") or {}
  found = []
  for uid, unit in sorted(units.items()):
    if not isinstance(unit, dict) or unit.get("state") not in LOR_LIVE_STATES:
      continue
    attempt, sid = unit.get("attempt_dir"), unit.get("job_id")
    if not attempt or not sid:
      continue
    intent = (unit.get("attempt_launch_intents") or {}).get(Path(str(attempt)).name) or {}
    if intent.get("backend") == "lor":
      found.append((uid, str(sid), intent.get("parent_session_id")))
  return found


def observe_lor(lor_io, directories, snapshot, warned):
  """Return (events, auth_failures). Bounded: one list call per attempt."""
  events, auth = [], []
  for directory in directories:
    try:
      attempts = lor_attempts(directory)
    except (OSError, ValueError, RecursionError, AttributeError, TypeError):
      continue
    key = LOR_SNAPSHOT_PREFIX + str(directory)
    seen = snapshot.setdefault(key, {})
    for uid, sid, parent in attempts:
      obs = lor_io.poll_session(sid, parent_session_id=parent)
      observed = local_time()
      if obs.get("auth_error"):
        auth.append(dict(event="lor_auth_error", observed=observed,
                         directory=directory.name, state_dir=str(directory), unit=uid,
                         session_id=sid, detail=obs.get("detail")))
        continue
      state = obs.get("state")
      if state == "unknown":
        if (directory, sid) not in warned:
          print("LOR UNKNOWN {} {} {}: {}".format(observed, directory.name, uid,
                " ".join(str(obs.get("detail")).splitlines())), file=sys.stderr, flush=True)
          warned.add((directory, sid))
        continue
      mark = "{}:{}".format(sid, state)
      if state != "running" and seen.get(uid) != mark:
        events.append(dict(event="lor_session_finished", observed=observed,
                           directory=directory.name, state_dir=str(directory),
                           unit=uid, session_id=sid, lor_state=state,
                           finish_reason=obs.get("finish_reason"),
                           previous=seen.get(uid)))
      seen[uid] = mark
  return events, auth


def local_time():
  return datetime.datetime.fromtimestamp(time.time()).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z%z")


def seconds(value):
  result = float(value)
  if not math.isfinite(result) or result < 0:
    raise argparse.ArgumentTypeError("expected finite nonnegative seconds")
  return result


def states(value):
  if not isinstance(value, dict) or any(
      not isinstance(unit, dict) or
      (unit.get("state") is not None and not isinstance(unit["state"], str))
      for unit in value.values()):
    raise ValueError("units must map ids to objects with string or null states")
  return {uid: "UNALLOCATED" if unit.get("state") is None else unit["state"]
          for uid, unit in value.items()}


def source_path(path):
  try:
    return path.resolve()
  except (OSError, RuntimeError):
    return path.absolute()


def read_snapshot(path):
  try:
    record = json.loads(path.read_bytes())
  except FileNotFoundError:
    return {}
  if not isinstance(record, dict) or any(
      not isinstance(units, dict) or any(not isinstance(s, str) for s in units.values())
      for units in record.values()):
    raise ValueError("snapshot must map state directory paths to unit state maps")
  return record


def write_snapshot(path, record):
  fd, name = tempfile.mkstemp(prefix=".wait-event-", dir=str(path.parent))
  try:
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
      os.fchmod(handle.fileno(), 0o600)
      json.dump(record, handle, sort_keys=True)
      handle.write("\n")
    os.replace(name, str(path))
  finally:
    if os.path.exists(name):
      os.unlink(name)


def observe(directories, snapshot, warned):
  events, readable = [], 0
  for directory in directories:
    observed = local_time()
    try:
      current = states(json.loads((directory / "swarm-state.json").read_bytes())["units"])
    except (OSError, ValueError, RecursionError, KeyError, TypeError) as exc:
      if directory not in warned:
        print("UNREADABLE {} {}: {}".format(observed, directory.name,
              " ".join(str(exc).splitlines())), file=sys.stderr, flush=True)
        warned.add(directory)
      continue
    readable += 1
    key = str(directory)
    previous = None
    for known in list(snapshot):
      if known == key or samefile(Path(known), directory):
        if previous is not None and previous != snapshot[known]:
          raise ValueError("conflicting directory alias histories; use a fresh --snapshot")
        previous = snapshot[known]
        if known != key:
          del snapshot[known]
    if previous is not None:
      snapshot[key] = previous
    for uid, state in sorted(current.items()):
      changed = previous is not None and uid in previous and previous[uid] != state
      kinds = (["TRANSITION"] if changed else [])
      if state not in QUIET and (previous is None or previous.get(uid) != state):
        kinds.append("STATE")
      for kind in kinds:
        events.append(dict(event=kind, observed=observed, directory=directory.name,
                           state_dir=key, unit=uid, state=state,
                           previous=None if previous is None else previous.get(uid)))
    snapshot.setdefault(key, {}).update(current)
  return events, readable


def samefile(first, second):
  try:
    return first.samefile(second)
  except OSError:
    return False


def inside(path, directory):
  for parent in (path,) + tuple(path.parents):
    if samefile(parent, directory):
      return True
  return False


def main(argv=None):
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--state-dir", required=True, action="append", type=Path)
  parser.add_argument("--snapshot", type=Path)
  parser.add_argument("--interval", type=seconds, default=60)
  parser.add_argument("--timeout", type=seconds)
  parser.add_argument("--once", action="store_true")
  parser.add_argument("--json", action="store_true")
  parser.add_argument("--poll-lor", action="store_true",
                      help="also poll in-flight lor coding attempts at --interval; "
                           "exit 4 if Lor rejects the session token")
  args = parser.parse_args(argv)
  if args.interval == 0:
    parser.error("--interval must be positive")
  directories = list(dict.fromkeys(source_path(path) for path in args.state_dir))
  try:
    path = (args.snapshot or (directories[0].parent / "wait-event-snapshot.json")).resolve()
  except (OSError, RuntimeError) as exc:
    parser.error(str(exc))
  if any(path == directory or directory in path.parents or inside(path, directory) or
         path == source_path(directory / "swarm-state.json") or
         samefile(path, directory / "swarm-state.json") for directory in directories):
    parser.error("snapshot must be outside every state directory")
  plan_candidates = [d.parent / "plan.json" for d in directories] + [Path.cwd() / "plan.json"]
  orch_env.warn_if_declared_mismatch(candidate_plan_paths=plan_candidates)
  deadline = None if args.timeout is None else time.monotonic() + args.timeout
  warned = set()
  lor_io = None
  if args.poll_lor:
    try:
      lor_io = load_lor_io()
    except Exception as exc:  # an absent sibling skill is a usage error, not a wait
      print("wait_event: --poll-lor cannot load hanig-swarm lor_io: {}".format(exc),
            file=sys.stderr)
      return 2
  try:
    snapshot = read_snapshot(path)
    while True:
      events, readable = observe(directories, snapshot, warned)
      lor_events = []
      if lor_io is not None:
        lor_events, auth = observe_lor(lor_io, directories, snapshot, warned)
        if auth:
          for event in auth:
            if args.json:
              print(json.dumps(event), flush=True)
            else:
              print("LOR_AUTH_ERROR {} {} {} session {}: {}".format(
                event["observed"], event["directory"], event["unit"], event["session_id"],
                " ".join(str(event["detail"]).splitlines())), flush=True)
          print("wait_event: Lor rejected the session token; this is not 'still "
                "running'. Re-run from a live Lor session.", file=sys.stderr)
          return EXIT_LOR_AUTH
        for event in lor_events:
          if args.json:
            print(json.dumps(event), flush=True)
          else:
            print("LOR_SESSION_FINISHED {} {} {} session {}: {} [{}]".format(
              event["observed"], event["directory"], event["unit"], event["session_id"],
              event["lor_state"], event["state_dir"]), flush=True)
        if lor_events and not events:
          end = local_time()
          if args.json:
            print(json.dumps(dict(event="END", observed=end, lines=len(lor_events))), flush=True)
          else:
            print("(end of pass {}, {} line(s))".format(end, len(lor_events)), flush=True)
          try:
            write_snapshot(path, snapshot)
          except (OSError, ValueError, RecursionError) as exc:
            print("wait_event: snapshot persistence failed; event may repeat: {}".format(exc), file=sys.stderr)
            return 2
          return 0
      if readable and not events:
        write_snapshot(path, snapshot)
      if events:
        for event in events:
          if args.json:
            print(json.dumps(event), flush=True)
          else:
            print("{} {} {} {}: {} -> {} [{}]".format(
              event["event"], event["observed"],
              *[" ".join(str(event[k]).splitlines()) for k in
                ("directory", "unit", "previous", "state", "state_dir")]), flush=True)
        end = local_time()
        if args.json:
          print(json.dumps(dict(event="END", observed=end,
                                lines=len(events) + len(lor_events))), flush=True)
        else:
          print("(end of pass {}, {} line(s))".format(
            end, len(events) + len(lor_events)), flush=True)
        try:
          write_snapshot(path, snapshot)
        except (OSError, ValueError, RecursionError) as exc:
          print("wait_event: snapshot persistence failed; event may repeat: {}".format(exc), file=sys.stderr)
          return 2
        return 0
      if args.once:
        return 3 if readable else 2
      remaining = None if deadline is None else deadline - time.monotonic()
      if remaining is not None and remaining <= 0:
        return 3
      time.sleep(min(args.interval, 86400 if remaining is None else min(86400, remaining)))
      if deadline is not None and time.monotonic() >= deadline:
        return 3
  except (OSError, ValueError, RecursionError) as exc:
    print("wait_event: {}".format(exc), file=sys.stderr)
    return 2


if __name__ == "__main__":
  sys.exit(main())
