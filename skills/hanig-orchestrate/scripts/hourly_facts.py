#!/usr/bin/env python3
"""Read-only hourly facts."""
import argparse
import datetime
import json
import math
import os
import stat
from pathlib import Path
import subprocess
import sys
import tempfile
import time

sys.dont_write_bytecode = True
from skill_paths import sibling_skill_root

TERMINAL = {"DONE", "ABANDONED", "SUPERSEDED"}
ACTIVE = {"SUBMITTED", "RUNNING"}
RUNNING_AGENTS = {"running", "working", "busy"}
BENIGN = {"SUCCESS", "NEUTRAL", "SKIPPED"}


def local_time(timestamp=None):
  return datetime.datetime.fromtimestamp(
    time.time() if timestamp is None else timestamp).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z%z")


def one_line(value):
  return " ".join(str(value).splitlines()).encode("utf-8", "backslashreplace").decode("utf-8")


def read_json(path):
  return json.loads(path.read_bytes())


def run(argv, repo):
  env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
  result = subprocess.run(argv, cwd=str(repo), stdout=subprocess.PIPE,
              stderr=subprocess.PIPE, timeout=120, env=env)
  if result.returncode:
    raise ValueError("exit {}: {}".format(result.returncode,
            one_line((result.stderr or result.stdout).decode("utf-8", "replace"))))
  return result.stdout


def list_source(argv, repo):
  data = json.loads(run(argv, repo))
  if isinstance(data, dict) and "agents" in data:
    data = data["agents"]
  if not isinstance(data, list) or any(not isinstance(row, dict) for row in data):
    raise ValueError("expected a JSON list of objects: {}".format(one_line(data)))
  return data


def atomic_write(path, data):
  fd, name = tempfile.mkstemp(prefix=".hourly-", dir=str(path.parent))
  try:
    with os.fdopen(fd, "wb") as handle:
      handle.write(data)
    os.replace(name, str(path))
  finally:
    if os.path.exists(name):
      os.unlink(name)


def state_facts(root, live_hours, agents):
  lines, ready, leftovers, live_work, active = [], [], 0, False, False
  now = time.time()
  skipped = 0
  try:
    if not root.is_dir():
      raise OSError("not a state directory")
    directories = sorted(root.iterdir())
  except OSError as exc:
    return [(True, "State root unavailable (observed {}): {}".format(local_time(), exc))], [], 0, False, False
  for directory in directories:
    sf = directory / "state" / "swarm-state.json"
    observed = local_time()
    label = "{} (observed {})".format(directory.name, observed)
    try:
      try:
        mode = sf.parent.stat().st_mode
      except (FileNotFoundError, NotADirectoryError):
        continue
      if not stat.S_ISDIR(mode):
        continue
      try:
        sf.lstat()
      except FileNotFoundError:
        skipped += 1
        continue
      units = read_json(sf)["units"]
      if not isinstance(units, dict) or any(
          not isinstance(u, dict) or (u.get("state") is not None and not isinstance(u["state"], str))
          for u in units.values()):
        raise ValueError("units must map ids to objects with string or null states")
      states = {uid: "UNALLOCATED" if u.get("state") is None else u["state"]
                for uid, u in units.items()}
      successors = {}
      plan_units = []
      plan_readable = True
      try:
        plan_units = read_json(directory / "plan.json")["units"]
        if not isinstance(plan_units, list):
          raise ValueError("plan units must be a list")
        for unit in plan_units:
          uid, needs = unit["id"], unit.get("needs", [])
          if not isinstance(uid, str) or not isinstance(needs, list) or any(
              not isinstance(need, str) for need in needs):
            raise ValueError("plan id must be a string and needs a list of strings")
          history = unit.get("author_history", [])
          if not isinstance(history, list) or any(
              not isinstance(row, dict) or not isinstance(row.get("unit"), str)
              for row in history):
            raise ValueError("author_history must list objects with string unit ids")
          for row in history:
            if row["unit"] != uid:
              successors.setdefault(row["unit"], set()).add(uid)
      except (OSError, ValueError, RecursionError, KeyError, TypeError, AttributeError) as exc:
        lines.append((True, "{}: unreadable plan: {}".format(label, exc)))
        plan_units, successors = [], {}
        plan_readable = False
      # Unknown membership must preserve state diagnostics, not assert absence.
      plan_ids = {unit["id"] for unit in plan_units} if plan_readable else set(states)
      counts = {}
      for uid, status in states.items():
        if uid not in plan_ids:
          counts[status] = counts.get(status, 0) + 1
      if counts:
        lines.append((False, "{}: not in plan.json: {}".format(label,
          ", ".join("{} {}".format(count, status) for status, count in sorted(counts.items())))))
      for unit in plan_units:
        states.setdefault(unit["id"], "UNALLOCATED")
      is_live = now - sf.stat().st_mtime <= live_hours * 3600 or any(
        status in ACTIVE for status in states.values())
      # Propagate decision roots backwards through HELD needs. Each HELD is
      # visited at most once, so cycles terminate without recursion or alerts
      # unless a decision root is reachable.
      held_followers = {}
      for unit in plan_units:
        if states[unit["id"]] == "HELD":
          for need in unit.get("needs", []):
            held_followers.setdefault(need, set()).add(unit["id"])
      decisions = {uid for uid, status in states.items() if status in {
        "FAILED", "FAILED_EVIDENCE", "NEEDS_HUMAN", "INCOMPLETE"}}
      pending = list(decisions)
      while pending:
        for uid in held_followers.get(pending.pop(), ()):
          if uid not in decisions:
            decisions.add(uid)
            pending.append(uid)
      needs_by_id = {unit["id"]: unit.get("needs", []) for unit in plan_units}
      successors = {uid: following for uid, following in successors.items()
                    if states.get(uid) not in ACTIVE}
      # Retain all recorded states for needs/readiness and the existing live
      # directory rule; filter only which units receive individual diagnostics.
      open_units = {uid: status for uid, status in states.items()
                    if uid in plan_ids and status not in TERMINAL}
      if not is_live:
        leftovers += len(open_units)
        continue
      live_work = live_work or bool(open_units.keys() - successors.keys())
      for uid, status in sorted(open_units.items()):
        if uid in successors:
          lines.append((False, "{}/{}: {}; continued by {}".format(
            label, uid, status, ", ".join(sorted(successors[uid])))))
          continue
        flag = status not in ACTIVE | {"UNALLOCATED"}
        note = ""
        if status == "HELD" and plan_readable:
          flag = uid in decisions
          waiting = ["{} ({})".format(need, states.get(need, "UNALLOCATED"))
                     for need in needs_by_id[uid] if states.get(need) != "DONE"]
          note = "; waiting for " + (", ".join(waiting) or "no non-DONE needs")
        if status in ACTIVE:
          active = True
          agent_id = units[uid].get("job_id")
          agent = (agents or {}).get(str(agent_id), {})
          agent_status = agent.get("status", "UNKNOWN" if agents is None and agent_id else "ABSENT")
          flag = (agents is not None or not agent_id) and str(agent_status).lower() not in RUNNING_AGENTS
          note = "; bound agent {}: {}".format(agent_id or "none", agent_status)
        lines.append((flag, "{}/{}: {}{}".format(label, uid, status, note)))
      for unit in plan_units:
        uid, needs = unit["id"], unit.get("needs", [])
        if states.get(uid, "UNALLOCATED") == "UNALLOCATED" and all(
            states.get(need) == "DONE" for need in needs):
          note = "; continued by " + ", ".join(sorted(successors[uid])) if uid in successors else ""
          ready.append((uid not in successors, "ready: {}/{} (needs {} DONE; observed {}){}".format(
            directory.name, uid, ", ".join(needs) or "nothing", observed, note)))
    except (OSError, ValueError, RecursionError, KeyError, TypeError) as exc:
      lines.append((True, "{}: unreadable state/plan: {}".format(label, exc)))
  if skipped:
    lines.append((False, "Skipped {} directories without state/swarm-state.json (observed {}).".format(
      skipped, local_time())))
  return lines, ready, leftovers, live_work or any(flag for flag, _ in ready), active


def tracker(args):
  if args.binding is None and args.audit is None:
    return b"Tracker: UNAVAILABLE (no binding configured)\n", False
  observed = local_time()
  try:
    loaded = os.environ.get("HANIG_ORCHESTRATE_DIR", str(Path(__file__).absolute().parent.parent))
    sibling = sibling_skill_root(loaded, "hanig-orchestrate", "hanig-project")
    for path in (args.binding, args.audit, sibling / "scripts" / "linear_sync.py"):
      if not path.is_file():
        raise ValueError("missing {}".format(path))
    audit = read_json(args.audit)
    read = datetime.datetime.fromisoformat(audit["read_finished"].replace("Z", "+00:00"))
    if read.tzinfo is None:
      raise ValueError("audit read_finished has no timezone")
    read = read.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z%z")
    section = run([sys.executable, str(sibling / "scripts" / "linear_sync.py"),
           "section", "--binding", str(args.binding), "--audit", str(args.audit)], args.repo)
    fence = b"```"
    while fence in section:
      fence += b"`"
    return ("Tracker observed {}; audit read_finished {}. Offline; never refreshed here.\n".format(
      observed, read).encode() + fence + b"\n" + section + b"\n" + fence + b"\n", False)
  except (OSError, ValueError, RecursionError, KeyError, TypeError, AttributeError, subprocess.SubprocessError) as exc:
    return ("ATTENTION Tracker: UNAVAILABLE (observed {}): {}\n".format(
      observed, one_line(exc)).encode(), True)


def collect(args):
  count = 0
  def render(flag, message):
    nonlocal count
    count += bool(flag)
    return ("ATTENTION " if flag else "- ") + one_line(message)

  agent_obs = local_time()
  agent_error = None
  try:
    agent_rows = list_source([args.paseo, "ls", "--json"], args.repo)
    agents = {str(a["id"]): a for a in agent_rows}
  except (OSError, ValueError, RecursionError, KeyError, TypeError, subprocess.SubprocessError) as exc:
    agent_rows, agents, agent_error = [], None, exc
  lines, ready, leftovers, live, active = state_facts(args.state_root, args.live_hours, agents)
  state_obs = local_time()
  body = ["## 1. Running work", "", "Agents observed {} (liveness only; no progress inference):".format(agent_obs)]
  if agent_error is not None:
    body.append(render(True, "paseo ls unavailable: {}".format(agent_error)))
  for agent in agent_rows:
    body.append(render(False, "agent {} {}: {}".format(agent.get("id"), agent.get("name", ""), agent.get("status", "unknown"))))
  live = live or any(str(a.get("status", "")).lower() in RUNNING_AGENTS for a in agent_rows)
  body.append("Units in live state directories ({} h or active):".format(args.live_hours))
  body.extend(render(flag, message) for flag, message in lines)
  body.append("Older state dirs hold {} non-terminal units; not listed (observed {}).".format(leftovers, state_obs))
  for kind, argv in (
      ("Open PRs", [args.gh, "pr", "list", "--state", "open", "--limit", "1000", "--json",
             "number,title,isDraft,mergeable,statusCheckRollup,updatedAt"]),
      ("Latest run on " + args.branch, [args.gh, "run", "list", "--branch", args.branch,
                      "--limit", "1", "--json", "status,conclusion,headSha,event,createdAt"])):
    try:
      rows = list_source(argv, args.repo)
      if kind == "Open PRs":
        while len(rows) >= int(argv[argv.index("--limit") + 1]):
          argv[argv.index("--limit") + 1] = str(int(argv[argv.index("--limit") + 1]) * 2)
          rows = list_source(argv, args.repo)
      observed = local_time()
      body.append("{} (observed {}):".format(kind, observed))
      if kind == "Open PRs":
        live = live or bool(rows)
        for pr in rows:
          counts = {}
          bad = False
          for check in pr.get("statusCheckRollup") or []:
            status = check.get("conclusion") or check.get("state") or check.get("status") or "PENDING"
            counts[status] = counts.get(status, 0) + 1
            if check.get("conclusion") or check.get("status") == "COMPLETED":
              bad = bad or status not in BENIGN
            elif check.get("state"):
              bad = bad or status not in BENIGN | {"PENDING", "EXPECTED"}
          body.append(render(bad, "PR {} {}{}: {}; mergeable {}".format(
            pr["number"], pr["title"], " (draft)" if pr.get("isDraft") else "",
            " ".join("{}:{}".format(k, v) for k, v in sorted(counts.items())) or "no checks", pr.get("mergeable"))))
      else:
        for row in rows:
          status = row.get("conclusion") or row.get("status") or "unknown"
          body.append(render(str(status).upper() in {"FAILURE", "TIMED_OUT", "STARTUP_FAILURE"}, "{} {} at {}".format(row.get("event"), status, row.get("headSha"))))
      if not rows:
        body.append("- none")
    except (OSError, ValueError, RecursionError, KeyError, TypeError, AttributeError, subprocess.SubprocessError) as exc:
      body.append(render(True, "{} UNAVAILABLE (observed {}): {}".format(kind, local_time(), exc)))
  section, unavailable = tracker(args)
  count += unavailable
  tail = ["## 3. Can start now", ""]
  tail.extend(render(flag, message) for flag, message in ready)
  if not ready:
    tail.append("- nothing: no unallocated unit in a readable live plan has all needs DONE; unreadable sources are flagged above.")
  hour = datetime.datetime.fromtimestamp(time.time() + 600).astimezone()
  header = "# Hourly facts for {:%Y-%m-%d %H}:00 %s\nStatus: ATTENTION count: {}; live work: {}.\nCollected {} (read-only; no model).\n\n".format(
    hour, count, "yes" if live else "no", local_time()) % hour.strftime("%Z")
  report = (header + "\n".join(body) + "\n\n## 2. Tracker\n\n").encode() + section + b"\n" + "\n".join(tail).encode() + b"\n"
  args.output_dir.mkdir(parents=True, exist_ok=True)
  target = args.output_dir / (hour.strftime("%Y%m%d-%H%z") + ".md")
  atomic_write(target, report)
  atomic_write(args.output_dir / "latest.md", report)
  return target


def positive(value):
  number = float(value)
  if not math.isfinite(number) or number <= 0:
    raise argparse.ArgumentTypeError("must be finite and positive")
  return number


def main(argv=None):
  parser = argparse.ArgumentParser(description=__doc__)
  for name in ("state-root", "output-dir", "repo"):
    parser.add_argument("--" + name, required=True, type=lambda p: Path(p).absolute())
  for name in ("binding", "audit"):
    parser.add_argument("--" + name, type=lambda p: Path(p).absolute())
  parser.add_argument("--paseo", default="paseo")
  parser.add_argument("--gh", default="gh")
  parser.add_argument("--branch", default="main")
  parser.add_argument("--live-hours", type=positive, default=72)
  args = parser.parse_args(argv)
  if (args.binding is None) != (args.audit is None):
    parser.error("--binding and --audit must be given together")
  print(collect(args))


if __name__ == "__main__":
  main()
