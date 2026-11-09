#!/usr/bin/env python3
"""Wait for an unseen report."""
import argparse
import datetime
import fcntl
import json
from pathlib import Path
import re
import time

from hourly_facts import atomic_write, positive

REPORT_NAME = r"\d{8}-\d{2}(?:[+-]\d{4})?\.md"


def report_time(path):
  try:
    return datetime.datetime.strptime(path.stem, "%Y%m%d-%H%z")
  except ValueError:
    return datetime.datetime.strptime(path.stem, "%Y%m%d-%H").astimezone()


def take_report(directory):
  """Serialize seen-record read/replace across waiters on this operator host."""
  directory.mkdir(parents=True, exist_ok=True)
  with (directory / ".seen.lock").open("a") as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    seen_path = directory / ".seen.json"
    try:
      record = json.loads(seen_path.read_bytes())
    except FileNotFoundError:
      record = []
    except (OSError, ValueError) as exc:
      raise ValueError("invalid seen record {}: {}".format(seen_path, exc)) from exc
    if not isinstance(record, list) or any(
        not isinstance(name, str) or not re.fullmatch(REPORT_NAME, name) for name in record):
      raise ValueError("invalid seen record {}: expected a JSON list of report file names".format(seen_path))
    seen = set(record)
    paths = [p for p in directory.glob("*.md") if re.fullmatch(REPORT_NAME, p.name)]
    for path in sorted(paths, key=report_time):
      if path.name in seen:
        continue
      report = path.read_bytes()
      status = re.search(rb"^Status: ATTENTION count: (\d+); live work: (yes|no)\.$", report, re.M)
      if status is None:
        raise ValueError("missing report status: {}".format(path))
      seen.add(path.name)
      atomic_write(seen_path, json.dumps(sorted(seen)).encode())
      if int(status[1]) or status[2] == b"yes":
        return path
  return None


def main(argv=None):
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--output-dir", required=True, type=Path)
  parser.add_argument("--poll-seconds", type=positive, default=60)
  args = parser.parse_args(argv)
  while True:
    path = take_report(args.output_dir.absolute())
    if path is not None:
      print(path, flush=True)
      return
    time.sleep(args.poll_seconds)


if __name__ == "__main__":
  main()
