#!/usr/bin/env python3
"""Fail-closed, read-only PR watcher.

Stops at the first blocker: fork/null-head refusal (before any other gh call),
conflicts (UNKNOWN is never clean), open review threads, CI on headRefOid.
No mutating gh call, no PR checkout. Equal --patch-id values prove only an
unchanged aggregate delta; a null patch_id is unavailable and not comparable.
Echoed strings lose ANSI and Cc/Cf/Cs/Co/Cn/Zl/Zp characters but tab/newline.
Exit codes below (error: config or API failure).
"""
import argparse, datetime, json, os, re, subprocess as SP, sys, tempfile, time, unicodedata

EXIT_CODES = dict(ready=0, ci_pending=10, conflicts=11, threads_open=12, ci_failed=13,
                  ci_zero_step_flake=14, unknown=15, refused_fork=16, error=2)
PATCH_ID_NOTE = "aggregate delta identity only; never certifies CI validity"
API_BYTE_CAP, DIFF_BYTE_CAP, MAX_PAGES = 16 << 20, 32 << 20, 50
PR_FIELDS = ("number,title,author,url,state,headRefName,headRefOid,headRepository,"
             "headRepositoryOwner,isCrossRepository,mergeable")
PASSING = {"success", "neutral", "skipped"}
SHA_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
REPO_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/(?!\.\.?$)[A-Za-z0-9._-]{1,100}")
BANNED = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})
ANSI_RE = re.compile(r"(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]"
                     r"|(?:\x1b\]|\x9d)[^\x07\x1b\x9c]*(?:\x07|\x1b\\|\x9c)?"
                     r"|\x1b[PX^_][^\x1b\x9c]*(?:\x1b\\|\x9c)?|\x1b[ -/]*[0-~]")
QUERY = ("query($o:String!,$n:String!,$p:Int!,$c:String){repository(owner:$o,name:$n){"
         "pullRequest(number:$p){reviewThreads(first:100,after:$c){pageInfo{hasNextPage "
         "endCursor} nodes{id isResolved path line comments(first:1){nodes{author{login} "
         "body url}}}}}}}")

class WatchError(Exception):
  """Configuration or API failure: exit 2."""

def sanitize(text):
  # ESC is Cc, so no escape survives a regex miss.
  text = ANSI_RE.sub("", text if isinstance(text, str) else str(text))
  return "".join(c for c in text if c in "\t\n" or unicodedata.category(c) not in BANNED)

def clean(v):
  if isinstance(v, dict): return {clean(k): clean(x) for k, x in v.items()}
  return [clean(x) for x in v] if isinstance(v, list) else sanitize(v) if isinstance(v, str) else v

def run_command(argv, input_bytes=None, max_bytes=None, cwd=None):
  """Return (rc, stdout, stderr, over_cap); output over the cap is dropped."""
  env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
  env.update(GH_PROMPT_DISABLED="1", GH_NO_UPDATE_NOTIFIER="1", NO_COLOR="1", GH_PAGER="cat")
  try:
    proc = SP.run(argv, input=input_bytes or b"", capture_output=True, cwd=cwd, env=env, timeout=120)
  except (OSError, SP.TimeoutExpired) as exc:
    return 127, b"", str(exc).encode("utf-8", "replace"), False
  over = len(proc.stdout) > (API_BYTE_CAP if max_bytes is None else max_bytes)
  return proc.returncode, b"" if over else proc.stdout, proc.stderr[:65536], over

def is_id(v):
  return type(v) is int and v > 0

def gh_json(runner, args):
  rc, out, err, over = runner(["gh"] + args)
  if rc != 0 or over: raise WatchError("gh %s: exit %s, over cap %s: %s" % (args[:2], rc, over, err[:500]))
  try: return json.loads(out.decode("utf-8"))
  except ValueError: raise WatchError("gh %s returned malformed JSON" % args[0])

def paged(runner, path, key):
  items = []
  for page in range(1, MAX_PAGES + 1):
    doc = gh_json(runner, ["api", "%s%sper_page=100&page=%d" % (path, "&?"["?" not in path], page)])
    total = doc.get("total_count") if isinstance(doc, dict) else None
    if type(total) is not int or not isinstance(doc.get(key), list):
      raise WatchError("unexpected %s listing" % key)
    items += doc[key]
    if len(items) >= total or not doc[key]:
      if len(items) < total: raise WatchError("%s listing incomplete" % key)
      return items
  raise WatchError("%s listing exceeds %d pages" % (key, MAX_PAGES))

def fork_refusal(pr, owner, name):
  repo, who = pr.get("headRepository"), pr.get("headRepositoryOwner")
  head = (who.get("login"), repo.get("name")) if isinstance(repo, dict) and isinstance(who, dict) else (0,)
  if not all(isinstance(x, str) and x for x in head): return "head repository, owner or name is missing"
  if tuple(x.lower() for x in head) != (owner.lower(), name.lower()):
    return "head repository %s/%s differs from base %s/%s" % (head + (owner, name))
  if pr.get("isCrossRepository") is not False: return "isCrossRepository is not false"
  return None

def open_threads(runner, owner, name, number):
  found, cursor = [], None
  for _ in range(MAX_PAGES):
    args = ["api", "graphql", "-f", "query=" + QUERY, "-f", "o=" + owner, "-f", "n=" + name,
            "-F", "p=%d" % number] + (["-f", "c=" + cursor] if cursor else [])
    doc = gh_json(runner, args)
    if doc.get("errors"): raise WatchError("GraphQL errors")
    threads = doc["data"]["repository"]["pullRequest"]["reviewThreads"]
    nodes, info = list(threads["nodes"]), dict(threads["pageInfo"])
    for node in nodes:
      if node.get("isResolved") is True:
        continue  # anything but an explicit true is open
      first = ((node.get("comments") or {}).get("nodes") or [{}])[0]
      found.append(dict(id=node.get("id"), path=node.get("path"), line=node.get("line"), url=first.get("url"),
        author=(first.get("author") or {}).get("login"), excerpt=sanitize(first.get("body") or "")[:200]))
    if type(info.get("hasNextPage")) is not bool: raise WatchError("hasNextPage is not a boolean")
    if not info["hasNextPage"]: return found
    cursor = info["endCursor"] + ""  # a missing cursor is an error, not page 1 again
  raise WatchError("review threads exceed %d pages" % MAX_PAGES)

def when(v):  # aware ISO-8601 datetime or None; 3.9 rejects a trailing Z
  try: t = datetime.datetime.fromisoformat(re.sub(r"Z\Z", "+00:00", v))
  except (TypeError, ValueError): return None
  return t if t.tzinfo else None

def superseded(run, current):
  """A newer non-cancelled same-workflow run exists, or order is unprovable."""
  wid, mine = run.get("workflow_id"), (when(run.get("created_at")), run["id"])
  if not is_id(wid) or mine[0] is None: return True
  for other in current:
    if other is not run and other.get("workflow_id") == wid and other.get("conclusion") != "cancelled":
      theirs = (when(other.get("created_at")), other.get("id"))
      if theirs[0] is None or not is_id(theirs[1]) or theirs > mine: return True
  return False

def judge_run(runner, repo, head, run, current, zero_step_jobs):
  rid, status, conclusion = run.get("id"), run.get("status"), run.get("conclusion")
  item = dict(kind="workflow_run", id=rid, name=run.get("name"), url=run.get("html_url"), verdict="failed")
  fail = lambda reason: dict(item, reason=reason)
  if not is_id(rid): return fail("run_id_missing")
  if status != "completed": return dict(item, verdict="pending")
  if not isinstance(conclusion, str) or not conclusion: return fail("conclusion_missing")
  if conclusion in PASSING: return dict(item, verdict="pass")
  try:  # a missing job payload is never assumed empty
    jobs = paged(runner, "repos/%s/actions/runs/%d/jobs" % (repo, rid), "jobs")
  except WatchError:
    if conclusion == "cancelled": raise  # API error, not CI failure
    jobs = None
  if conclusion != "cancelled":
    stepless = bool(jobs) and all(j.get("steps") == [] for j in jobs)
    return fail("zero_step_failure_unclassified" if stepless else conclusion)
  if not jobs or any(not isinstance(j.get("steps"), list) for j in jobs):
    return fail("job_data_missing")
  if any(j.get("head_sha") != head for j in jobs): return fail("job_head_mismatch")
  if any(j["steps"] for j in jobs): return fail("started_steps")
  if any(j.get("status") != "completed" or j.get("conclusion") not in ("cancelled", "skipped")
         for j in jobs) or not any(j.get("conclusion") == "cancelled" for j in jobs):
    return fail("job_not_cancelled")
  if superseded(run, current): return fail("cancelled_superseded_by_newer_run")
  for j in jobs:  # job id or check_run_url id
    m = re.search(r"/check-runs/([1-9][0-9]*)\Z", str(j.get("check_run_url")))
    zero_step_jobs.update([j.get("id")] + ([int(m.group(1))] if m else []))
  return dict(item, verdict="zero_step")

def judge_check(check, zero_step_jobs):
  actions = (check.get("app") or {}).get("slug") == "github-actions"
  status, conclusion, cid = check.get("status"), check.get("conclusion"), check.get("id")
  item = dict(kind="check_run", id=cid, name=check.get("name"), url=check.get("details_url"))
  if status != "completed": return dict(item, verdict="pending")
  if not isinstance(conclusion, str) or not conclusion:
    return dict(item, verdict="failed", reason="conclusion_missing")
  if conclusion in PASSING: return dict(item, verdict="pass")
  if actions and conclusion == "cancelled" and is_id(cid) and cid in zero_step_jobs:
    return dict(item, verdict="zero_step")
  return dict(item, verdict="failed", reason=("" if actions else "non_actions_check_") + conclusion)

def judge_status(st):
  state = st.get("state")
  item = dict(kind="status", name=st.get("context"), url=st.get("target_url"))
  if state in ("success", "pending"): return dict(item, verdict="pass" if state == "success" else "pending")
  return dict(item, verdict="failed", reason="non_actions_status_%s" % state)

def evaluate_ci(runner, repo, head):
  """Classify CI on head: failed > pending > zero-step flake > ready."""
  runs = paged(runner, "repos/%s/actions/runs?head_sha=%s" % (repo, head), "workflow_runs")
  checks = paged(runner, "repos/%s/commits/%s/check-runs" % (repo, head), "check_runs")
  statuses = paged(runner, "repos/%s/commits/%s/status" % (repo, head), "statuses")
  current = [r for r in runs if r.get("head_sha") == head]
  if any(c.get("head_sha") != head for c in checks): raise WatchError("check run not on head")
  zero_step_jobs = set()
  items = [judge_run(runner, repo, head, r, current, zero_step_jobs) for r in current]
  items += [judge_check(c, zero_step_jobs) for c in checks] + [judge_status(s) for s in statuses]
  verdicts = {i["verdict"] for i in items}
  if not items: return "ci_pending", ["no checks reported on the current head"], items, []
  state = ("ci_failed" if "failed" in verdicts else "ci_pending" if "pending" in verdicts
           else "ci_zero_step_flake" if "zero_step" in verdicts else "ready")
  flakes = sorted(i["id"] for i in items if i["kind"] == "workflow_run" and i["verdict"] == "zero_step")
  reasons = ["%s %s: %s" % (i["kind"], i["name"], i.get("reason") or i["verdict"])
             for i in items if i["verdict"] != "pass"]
  return state, reasons, items, flakes if state == "ci_zero_step_flake" else []

def aggregate_patch_id(runner, repo, number):
  rc, diff, _, over = runner(["gh", "pr", "diff", str(number), "--repo", repo, "--color", "never"],
                             max_bytes=DIFF_BYTE_CAP)
  if rc != 0 or over or not diff.strip(): return None
  try:
    with tempfile.TemporaryDirectory() as scratch:
      rc, out = runner(["git", "patch-id", "--stable"], input_bytes=diff, cwd=scratch)[:2]
  except OSError: return None
  words = out.decode("ascii", "replace").split()
  return words[0] if rc == 0 and len(words) == 2 and SHA_RE.fullmatch(words[0]) else None

def finish(report, state, reasons):
  report.update(state=state, reasons=reasons, action_required=state not in ("ready", "ci_pending"))
  return report

def new_report(opts):
  now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
  return dict(schema_version=1, observed_at=now, pr=opts.pr, head_sha=None)

def observe(opts, runner):
  report = new_report(opts)
  owner, name = opts.repo.split("/")
  pr = gh_json(runner, ["pr", "view", str(opts.pr), "--repo", opts.repo, "--json", PR_FIELDS])
  if not isinstance(pr, dict): raise WatchError("gh pr view returned no object")
  head = pr.get("headRefOid")
  author = pr.get("author") if isinstance(pr.get("author"), dict) else {}
  report.update(head_sha=head if isinstance(head, str) else None, pr_info=dict(
    title=pr.get("title"), url=pr.get("url"), author=author.get("login"), head_ref=pr.get("headRefName")))
  refusal = fork_refusal(pr, owner, name)
  if refusal: return finish(report, "refused_fork", [refusal])
  if pr.get("number") != opts.pr: raise WatchError("gh pr view returned another PR")
  if pr.get("state") != "OPEN":
    return finish(report, "unknown", ["pull request state is %s" % pr.get("state")])
  mergeable = pr.get("mergeable")
  if mergeable == "CONFLICTING": return finish(report, "conflicts", ["mergeable is CONFLICTING"])
  if mergeable != "MERGEABLE":
    return finish(report, "unknown", ["mergeable is %s" % (mergeable or "missing")])
  threads = open_threads(runner, owner, name, opts.pr)
  if threads:
    report["threads"] = threads
    return finish(report, "threads_open", ["%d unresolved review thread(s)" % len(threads)])
  if not isinstance(head, str) or not SHA_RE.fullmatch(head):
    raise WatchError("headRefOid is not a commit id")
  state, reasons, items, flakes = evaluate_ci(runner, opts.repo, head)
  report["checks"] = [i for i in items if i["verdict"] != "pass"]
  finish(report, state, reasons)
  if flakes:
    report["zero_step_runs"] = flakes
  return report

def render(report, as_json):
  report = clean(report)
  if as_json: return json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
  head = "PR #%s %s (exit %d)" % (report["pr"], report["state"], EXIT_CODES[report["state"]])
  return "\n".join([head] + ["  %s: %s" % (k, json.dumps(report[k], ensure_ascii=False, sort_keys=True))
                             for k in sorted(report) if k not in ("pr", "state")]) + "\n"

def main(argv=None, runner=run_command, out=None, err=None, sleep=time.sleep):
  out, err = out or sys.stdout, err or sys.stderr
  p = argparse.ArgumentParser(description=__doc__ + ", ".join("%s %d" % e for e in EXIT_CODES.items()),
                              formatter_class=argparse.RawDescriptionHelpFormatter)
  p.add_argument("--repo", required=True, help="base repository OWNER/REPO")
  p.add_argument("--pr", required=True, type=int)
  p.add_argument("--interval", type=float, help="poll while ci_pending or unknown")
  p.add_argument("--max-polls", type=int)
  for flag in ("--once", "--json", "--patch-id"):  # --once is the default
    p.add_argument(flag, action="store_true")
  opts = p.parse_args(argv)
  try:
    if not REPO_RE.fullmatch(opts.repo) or opts.pr <= 0 or (opts.interval is None) != (
        opts.max_polls is None) or opts.max_polls is not None and (
        opts.once or not opts.interval > 0 or opts.max_polls < 1):
      raise WatchError("need OWNER/REPO, --pr > 0, and --interval > 0 with --max-polls >= 1, not --once")
    polls = opts.max_polls or 1
    for poll in range(1, polls + 1):
      report = observe(opts, runner)
      if report["state"] not in ("ci_pending", "unknown") or poll == polls:
        break
      err.write("poll %d/%d: %s\n" % (poll, polls, report["state"]))
      sleep(opts.interval)
    if opts.patch_id and report["state"] != "refused_fork":
      pid = aggregate_patch_id(runner, opts.repo, opts.pr)
      report.update(patch_id=pid, patch_id_status=pid and "ok" or "unavailable", patch_id_note=PATCH_ID_NOTE)
  except (WatchError, AttributeError, KeyError, TypeError, ValueError) as exc:  # malformed: never a state
    report = finish(new_report(opts), "error", ["%s: %s" % (type(exc).__name__, exc)])
  out.write(render(report, opts.json))
  return EXIT_CODES[report["state"]]

if __name__ == "__main__":
  sys.exit(main())
