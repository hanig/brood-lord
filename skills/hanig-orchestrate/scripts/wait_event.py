#!/usr/bin/env python3
"""Wait for a coordinator state event, without inspecting worker liveness."""
import argparse
import datetime
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import time

QUIET = {"UNALLOCATED", "SUBMITTED", "RUNNING", "DONE", "ABANDONED", "SUPERSEDED"}


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
  deadline = None if args.timeout is None else time.monotonic() + args.timeout
  warned = set()
  try:
    snapshot = read_snapshot(path)
    while True:
      events, readable = observe(directories, snapshot, warned)
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
          print(json.dumps(dict(event="END", observed=end, lines=len(events))), flush=True)
        else:
          print("(end of pass {}, {} line(s))".format(end, len(events)), flush=True)
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
