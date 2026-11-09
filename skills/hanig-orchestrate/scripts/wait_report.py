#!/usr/bin/env python3
"""Wait once for an unseen hourly report; persist consumption before printing."""
import argparse
import fcntl
import json
from pathlib import Path
import re
import time

from hourly_facts import atomic_write, positive


def take_report(directory):
    """Serialize seen-record read/replace across waiters on this operator host."""
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".seen.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        seen_path = directory / ".seen.json"
        seen = set(json.loads(seen_path.read_bytes())) if seen_path.exists() else set()
        for path in sorted(directory.glob("*.md")):
            if not re.fullmatch(r"\d{8}-\d{2}(?:[+-]\d{4})?\.md", path.name) or path.name in seen:
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
