#!/usr/bin/env python3
"""Fail-closed, read-only pull-request watcher.

Checks run in a fixed order and stop at the first blocker:
  1. fork or null-head refusal (before any other gh call),
  2. merge conflicts (``UNKNOWN`` or missing mergeability is never clean),
  3. unresolved review threads,
  4. CI on the current ``headRefOid``.

A default run makes no mutating gh call and never checks out PR code. Only
``--rerun-zero-step-flake`` on a ``ci_zero_step_flake`` observation re-reads
the head and asks GitHub to rerun each qualifying cancelled workflow run, at
most once per ``(run_id, head_sha)``, recorded in a JSON state file kept
outside every Git worktree.

``--patch-id`` pipes plain ``gh pr diff`` (the aggregate PR delta) into
``git patch-id --stable``. Equal values across a restack prove only that the
aggregate delta is unchanged; they never certify CI.

Every GitHub-sourced string is sanitized before output: ANSI CSI/OSC
sequences are stripped and every character whose Unicode category is Cc, Cf,
Cs, Co, Cn, Zl or Zp is removed, except tab and newline.

Exit codes: ready 0, ci_pending 10, conflicts 11, threads_open 12,
ci_failed 13, ci_zero_step_flake 14, unknown 15, refused_fork 16,
configuration or API error 2.
"""
import argparse
import contextlib
import datetime
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import unicodedata

SCHEMA_VERSION = 1
EXIT_CODES = {
    "ready": 0, "ci_pending": 10, "conflicts": 11, "threads_open": 12,
    "ci_failed": 13, "ci_zero_step_flake": 14, "unknown": 15,
    "refused_fork": 16, "error": 2,
}
ACTIONS = {
    "ready": "none: checks are green on the current head; merging is a separate decision",
    "ci_pending": "wait: CI on the current head has not finished",
    "conflicts": "rebase or resolve merge conflicts",
    "threads_open": "resolve the listed review threads",
    "ci_failed": "inspect the listed CI failures on the current head",
    "ci_zero_step_flake": "rerun the listed zero-step cancelled runs (opt-in: --rerun-zero-step-flake)",
    "unknown": "re-query later; GitHub has not reported a usable merge state",
    "refused_fork": "refused: head is not a branch of the base repository",
    "error": "fix the configuration or gh access and rerun",
}
NO_ACTION = frozenset({"ready", "ci_pending"})
PATCH_ID_NOTE = "aggregate delta identity only; never certifies CI validity"
API_BYTE_CAP = 16 * 1024 * 1024
DIFF_BYTE_CAP = 32 * 1024 * 1024
COMMAND_TIMEOUT = 120
PER_PAGE = 100
MAX_PAGES = 50
STATE_FILE = "watch-pr-reruns.json"
LOCK_FILE = "watch-pr-reruns.lock"
DEFAULT_STATE_DIR = "~/.local/state/hanig-orchestrate"
PR_FIELDS = ("number,title,author,url,state,headRefName,headRefOid,headRepository,"
             "headRepositoryOwner,isCrossRepository,mergeable")
PASSING = frozenset({"success", "neutral", "skipped"})
SHA_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
REPO_RE = re.compile(r"([A-Za-z0-9][A-Za-z0-9-]{0,38})/([A-Za-z0-9._-]{1,100})")
BANNED_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})
KEPT_CONTROLS = frozenset("\t\n")
ANSI_RE = re.compile(
    r"(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]"                     # CSI
    r"|(?:\x1b\]|\x9d)[^\x07\x1b\x9c]*(?:\x07|\x1b\\|\x9c)?"  # OSC
    r"|\x1b[PX^_][^\x1b\x9c]*(?:\x1b\\|\x9c)?"              # DCS, SOS, PM, APC
    r"|\x1b[ -/]*[0-~]")                                    # other ESC sequences
THREADS_QUERY = (
    "query($owner:String!,$name:String!,$number:Int!,$cursor:String){"
    "repository(owner:$owner,name:$name){pullRequest(number:$number){"
    "reviewThreads(first:100,after:$cursor){pageInfo{hasNextPage endCursor}"
    "nodes{id isResolved isOutdated path line "
    "comments(first:1){nodes{author{login} body url}}}}}}}")


class WatchError(Exception):
    """Configuration or API failure: exit 2."""


def sanitize(text):
    """Strip ANSI escapes, then drop every banned-category character but tab/newline.

    The category filter is the security bound: ESC itself is Cc, so no escape
    sequence survives even when the regex does not match it.
    """
    if not isinstance(text, str):
        text = str(text)
    text = ANSI_RE.sub("", text)
    return "".join(c for c in text
                   if c in KEPT_CONTROLS or unicodedata.category(c) not in BANNED_CATEGORIES)


def clean(value):
    if isinstance(value, str):
        return sanitize(value)
    if isinstance(value, dict):
        return {(sanitize(k) if isinstance(k, str) else k): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    return value


class CommandResult(object):
    def __init__(self, returncode, stdout=b"", stderr=b"", truncated=False):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.truncated = truncated


def child_env():
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GH_PROMPT_DISABLED="1", GH_NO_UPDATE_NOTIFIER="1", NO_COLOR="1",
               CLICOLOR="0", GH_PAGER="cat", PAGER="cat", GIT_PAGER="cat")
    return env


def run_command(argv, input_bytes=None, max_bytes=API_BYTE_CAP, cwd=None):
    """Run argv; stdout goes to a temporary file so memory stays bounded by max_bytes."""
    try:
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            proc = subprocess.Popen(
                argv, cwd=cwd, env=child_env(), stdout=out, stderr=err,
                stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL)
            try:
                proc.communicate(input=input_bytes, timeout=COMMAND_TIMEOUT)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                return CommandResult(124, b"", b"command timed out")
            size = out.seek(0, 2)
            out.seek(0)
            err.seek(0)
            stderr = err.read(65536)
            if size > max_bytes:
                return CommandResult(proc.returncode, b"", stderr, truncated=True)
            return CommandResult(proc.returncode, out.read(), stderr)
    except OSError as exc:
        return CommandResult(127, b"", str(exc).encode("utf-8", "replace"))


def is_id(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def gh_json(runner, args):
    res = runner(["gh"] + args)
    if res.truncated:
        raise WatchError("gh %s output exceeded the byte cap" % args[0])
    if res.returncode != 0:
        raise WatchError("gh %s failed (exit %s): %s" % (
            " ".join(args[:2]), res.returncode,
            res.stderr.decode("utf-8", "replace").strip()[:500]))
    try:
        return json.loads(res.stdout.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise WatchError("gh %s returned malformed JSON" % args[0])


def paged(runner, path, key):
    """Return every item of a REST listing; incomplete or unbounded listings are errors."""
    items, total = [], None
    sep = "&" if "?" in path else "?"
    for page in range(1, MAX_PAGES + 1):
        doc = gh_json(runner, ["api", "%s%sper_page=%d&page=%d" % (path, sep, PER_PAGE, page)])
        if (not isinstance(doc, dict) or not isinstance(doc.get(key), list)
                or not isinstance(doc.get("total_count"), int)
                or isinstance(doc.get("total_count"), bool)):
            raise WatchError("unexpected %s listing shape" % key)
        if total is None:
            total = doc["total_count"]
        items.extend(doc[key])
        if len(items) >= total or not doc[key]:
            break
    else:
        raise WatchError("%s listing exceeds %d pages" % (key, MAX_PAGES))
    if len(items) < total:
        raise WatchError("%s listing incomplete: %d of %d" % (key, len(items), total))
    return items


def fork_refusal(pr, owner, name):
    repo, who = pr.get("headRepository"), pr.get("headRepositoryOwner")
    if not isinstance(repo, dict) or not isinstance(who, dict):
        return "head repository is missing or null"
    head_name, head_owner = repo.get("name"), who.get("login")
    if not isinstance(head_name, str) or not head_name or not isinstance(head_owner, str) or not head_owner:
        return "head repository owner or name is missing"
    # Exact comparison: a case-only mismatch refuses, which is the safe direction.
    if head_owner != owner or head_name != name:
        return "head repository %s/%s differs from base %s/%s" % (head_owner, head_name, owner, name)
    if pr.get("isCrossRepository") is not False:
        return "isCrossRepository is not false"
    return None


def open_threads(runner, owner, name, number):
    found, cursor = [], None
    for _ in range(MAX_PAGES):
        args = ["api", "graphql", "-f", "query=" + THREADS_QUERY, "-f", "owner=" + owner,
                "-f", "name=" + name, "-F", "number=%d" % number]
        if cursor is not None:
            args += ["-f", "cursor=" + cursor]
        doc = gh_json(runner, args)
        try:
            if doc.get("errors"):
                raise WatchError("GraphQL errors reading review threads")
            threads = doc["data"]["repository"]["pullRequest"]["reviewThreads"]
            nodes, info = threads["nodes"], threads["pageInfo"]
            if not isinstance(nodes, list) or not isinstance(info, dict):
                raise TypeError
        except (AttributeError, KeyError, TypeError):
            raise WatchError("unexpected review-thread response shape")
        for node in nodes:
            node = node if isinstance(node, dict) else {}
            if node.get("isResolved") is True:
                continue  # anything but an explicit true counts as open
            comments = ((node.get("comments") or {}).get("nodes") or [{}])
            first = comments[0] if isinstance(comments[0], dict) else {}
            found.append({
                "id": node.get("id"), "path": node.get("path"), "line": node.get("line"),
                "is_outdated": node.get("isOutdated"),
                "author": (first.get("author") or {}).get("login"),
                "excerpt": sanitize(first.get("body") or "")[:200], "url": first.get("url"),
            })
        if info.get("hasNextPage") is not True:
            return found
        cursor = info.get("endCursor")
        if not isinstance(cursor, str) or not cursor:
            raise WatchError("review-thread pagination cursor missing")
    raise WatchError("review threads exceed %d pages" % MAX_PAGES)


def fetch_jobs(runner, repo, run_id):
    """Jobs of a run, or None when the payload is unavailable (never assumed empty)."""
    try:
        return paged(runner, "repos/%s/actions/runs/%d/jobs" % (repo, run_id), "jobs")
    except WatchError:
        return None


def run_order(run):
    created = run.get("created_at")
    return (created, run.get("id")) if isinstance(created, str) else None


def superseded(run, current):
    """True when a newer non-cancelled run of the same workflow exists, or order is unprovable."""
    wid, mine = run.get("workflow_id"), run_order(run)
    if not is_id(wid) or mine is None:
        return True
    for other in current:
        if other is run or other.get("workflow_id") != wid or other.get("conclusion") == "cancelled":
            continue
        theirs = run_order(other)
        if theirs is None or not is_id(theirs[1]) or theirs > mine:
            return True
    return False


def judge_run(runner, repo, head, run, current, zero_step_jobs):
    rid, status, conclusion = run.get("id"), run.get("status"), run.get("conclusion")
    item = {"kind": "workflow_run", "id": rid, "name": run.get("name"),
            "url": run.get("html_url"), "status": status, "conclusion": conclusion}

    def verdict(v, reason=None):
        item["verdict"] = v
        if reason:
            item["reason"] = reason
        return item

    if not is_id(rid):
        return verdict("failed", "run_id_missing")
    if status != "completed":
        return verdict("pending", "not_completed")
    if not isinstance(conclusion, str) or not conclusion:
        return verdict("failed", "conclusion_missing")
    if conclusion in PASSING:
        return verdict("pass")
    jobs = fetch_jobs(runner, repo, rid)
    stepless = (bool(jobs) and all(isinstance(j, dict) and j.get("steps") == [] for j in jobs))
    if conclusion != "cancelled":
        return verdict("failed", "zero_step_failure_unclassified" if stepless else conclusion)
    if not jobs or any(not isinstance(j, dict) or not isinstance(j.get("steps"), list) for j in jobs):
        return verdict("failed", "job_data_missing")
    if any(j.get("head_sha") != head for j in jobs):
        return verdict("failed", "job_head_mismatch")
    if any(j["steps"] for j in jobs):
        return verdict("failed", "started_steps")
    if any(j.get("status") != "completed" or j.get("conclusion") not in ("cancelled", "skipped")
           for j in jobs) or not any(j.get("conclusion") == "cancelled" for j in jobs):
        return verdict("failed", "job_not_cancelled")
    if superseded(run, current):
        return verdict("failed", "cancelled_superseded_by_newer_run")
    zero_step_jobs.update(j.get("id") for j in jobs)
    return verdict("zero_step")


def judge_check(check, zero_step_jobs):
    app = (check.get("app") or {}).get("slug") if isinstance(check.get("app"), dict) else None
    status, conclusion = check.get("status"), check.get("conclusion")
    item = {"kind": "check_run", "id": check.get("id"), "name": check.get("name"), "app": app,
            "url": check.get("details_url") or check.get("html_url"),
            "status": status, "conclusion": conclusion}
    actions = app == "github-actions"
    if status != "completed":
        item["verdict"] = "pending"
    elif not isinstance(conclusion, str) or not conclusion:
        item.update(verdict="failed", reason="conclusion_missing")
    elif conclusion in PASSING:
        item["verdict"] = "pass"
    elif actions and conclusion == "cancelled" and is_id(check.get("id")) and check["id"] in zero_step_jobs:
        item["verdict"] = "zero_step"
    else:
        item.update(verdict="failed", reason=conclusion if actions else "non_actions_check_" + conclusion)
    return item


def judge_status(status):
    state = status.get("state")
    item = {"kind": "status", "name": status.get("context"), "url": status.get("target_url"),
            "description": status.get("description"), "status": state}
    if state == "success":
        item["verdict"] = "pass"
    elif state == "pending":
        item["verdict"] = "pending"
    else:
        item.update(verdict="failed", reason="non_actions_status_%s" % state)
    return item


def evaluate_ci(runner, repo, head):
    """Classify CI on ``head``: failed > pending > zero-step flake > ready."""
    runs = paged(runner, "repos/%s/actions/runs?head_sha=%s" % (repo, head), "workflow_runs")
    checks = paged(runner, "repos/%s/commits/%s/check-runs" % (repo, head), "check_runs")
    statuses = paged(runner, "repos/%s/commits/%s/status" % (repo, head), "statuses")
    # Stale-SHA entries are ignored; only the current head decides.
    current = [r for r in runs if isinstance(r, dict) and r.get("head_sha") == head]
    zero_step_jobs = set()
    items = [judge_run(runner, repo, head, r, current, zero_step_jobs) for r in current]
    items += [judge_check(c, zero_step_jobs) for c in checks
              if isinstance(c, dict) and c.get("head_sha") == head]
    items += [judge_status(s) for s in statuses if isinstance(s, dict)]
    verdicts = {i["verdict"] for i in items}
    flakes = sorted(i["id"] for i in items if i["kind"] == "workflow_run" and i["verdict"] == "zero_step")
    if "failed" in verdicts:
        state = "ci_failed"
    elif "pending" in verdicts:
        state = "ci_pending"
    elif not items:
        return "ci_pending", ["no checks reported on the current head"], items, []
    elif "zero_step" in verdicts:
        state = "ci_zero_step_flake"
    else:
        state = "ready"
    reasons = ["%s %s: %s" % (i["kind"], i.get("name"), i.get("reason") or i["verdict"])
               for i in items if i["verdict"] != "pass"]
    return state, reasons, items, (flakes if state == "ci_zero_step_flake" else [])


def check_state_dir(path):
    for candidate in (path, path.resolve()):
        for parent in (candidate,) + tuple(candidate.parents):
            if (parent / ".git").exists():
                raise WatchError("state dir %s is inside a Git worktree (%s)" % (path, parent))


def load_state(path):
    if not path.exists():
        return {"schema_version": SCHEMA_VERSION, "reruns": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        raise WatchError("rerun state file %s is unreadable" % path)
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION \
            or not isinstance(data.get("reruns"), dict):
        raise WatchError("rerun state file %s has an unexpected shape" % path)
    return data


def save_state(path, data):
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".watch-pr-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, str(path))
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def rerun_flakes(runner, repo, number, head, run_ids, state_dir, now):
    """Rerun each qualifying run at most once per (run_id, head_sha), claimed before the call."""
    try:
        latest = gh_json(runner, ["pr", "view", str(number), "--repo", repo, "--json", "headRefOid"])
        new_head = latest.get("headRefOid") if isinstance(latest, dict) else None
    except WatchError:
        return {"status": "aborted", "reason": "head_requery_failed", "runs": []}
    if new_head != head:
        return {"status": "aborted", "reason": "head_moved", "new_head": new_head, "runs": []}
    check_state_dir(state_dir)
    try:
        state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        return _rerun_locked(runner, repo, head, run_ids, state_dir, now)
    except OSError as exc:
        raise WatchError("rerun state dir %s unusable: %s" % (state_dir, exc))


def _rerun_locked(runner, repo, head, run_ids, state_dir, now):
    path, results = state_dir / STATE_FILE, []
    with open(str(state_dir / LOCK_FILE), "a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        data = load_state(path)
        for rid in run_ids:
            key = "%d:%s" % (rid, head)
            if key in data["reruns"]:
                results.append({"run_id": rid, "result": "already_rerun"})
                continue
            record = {"run_id": rid, "head_sha": head, "claimed_at": now, "result": "claimed"}
            data["reruns"][key] = record
            save_state(path, data)
            res = runner(["gh", "api", "-X", "POST",
                          "repos/%s/actions/runs/%d/rerun-failed-jobs" % (repo, rid)])
            record["result"] = "requested" if res.returncode == 0 else "request_failed"
            save_state(path, data)
            results.append({"run_id": rid, "result": record["result"]})
    return {"status": "done", "runs": results}


def aggregate_patch_id(runner, repo, number):
    res = runner(["gh", "pr", "diff", str(number), "--repo", repo, "--color", "never"],
                 max_bytes=DIFF_BYTE_CAP)
    if res.truncated:
        return "unavailable", "gh pr diff exceeded the byte cap"
    if res.returncode != 0:
        return "unavailable", "gh pr diff failed (exit %s)" % res.returncode
    if not res.stdout.strip():
        return "unavailable", "empty diff"
    with tempfile.TemporaryDirectory() as scratch:  # no repository needed
        pid = runner(["git", "patch-id", "--stable"], input_bytes=res.stdout, cwd=scratch)
    lines = [l for l in pid.stdout.decode("ascii", "replace").splitlines() if l.strip()]
    if pid.returncode != 0 or len(lines) != 1:
        return "unavailable", "git patch-id produced no single result"
    value = lines[0].split()[0]
    if not SHA_RE.fullmatch(value):
        return "unavailable", "git patch-id output is not a hash"
    return value, None


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_report(opts):
    return {"schema_version": SCHEMA_VERSION, "observed_at": utc_now(), "repo": opts.repo,
            "pr": opts.pr, "state": None, "head_sha": None, "action_required": True,
            "reasons": []}


def finish(report, state, reasons):
    report.update(state=state, reasons=list(reasons), action=ACTIONS[state],
                  action_required=state not in NO_ACTION)
    return report


def observe(opts, runner, state_dir):
    report = new_report(opts)
    owner, name = opts.repo.split("/", 1)
    pr = gh_json(runner, ["pr", "view", str(opts.pr), "--repo", opts.repo, "--json", PR_FIELDS])
    if not isinstance(pr, dict):
        raise WatchError("gh pr view returned no object")
    head = pr.get("headRefOid")
    report["head_sha"] = head if isinstance(head, str) else None
    report["pr_info"] = {"title": pr.get("title"), "url": pr.get("url"),
                         "author": (pr.get("author") or {}).get("login")
                         if isinstance(pr.get("author"), dict) else None,
                         "head_ref": pr.get("headRefName")}
    refusal = fork_refusal(pr, owner, name)
    if refusal:
        return finish(report, "refused_fork", [refusal])
    if pr.get("number") != opts.pr:
        raise WatchError("gh pr view returned a different pull request")
    if pr.get("state") != "OPEN":
        return finish(report, "unknown", ["pull request state is %s, not OPEN" % pr.get("state")])
    mergeable = pr.get("mergeable")
    if mergeable == "CONFLICTING":
        return finish(report, "conflicts", ["mergeable is CONFLICTING"])
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
    report["passing_checks"] = sum(1 for i in items if i["verdict"] == "pass")
    finish(report, state, reasons)
    if flakes:
        report["zero_step_runs"] = flakes
        if opts.rerun_zero_step_flake:
            report["rerun"] = rerun_flakes(runner, opts.repo, opts.pr, head, flakes,
                                           state_dir, report["observed_at"])
    return report


def render_human(report):
    info = report.get("pr_info") or {}
    lines = ["PR #%s %s" % (report.get("pr"), info.get("title") or ""),
             "  author: %s  branch: %s" % (info.get("author"), info.get("head_ref")),
             "  url: %s" % info.get("url"),
             "state: %s (exit %d)  head: %s" % (report["state"], EXIT_CODES[report["state"]],
                                                report.get("head_sha")),
             "action: %s" % report.get("action")]
    lines += ["  reason: %s" % r for r in report.get("reasons", [])]
    for t in report.get("threads", []):
        lines.append("  thread %s %s:%s by %s: %s %s" % (t["id"], t["path"], t["line"],
                                                       t["author"], t["excerpt"], t["url"]))
    for c in report.get("checks", []):
        lines.append("  %s %s [%s] %s %s" % (c["kind"], c.get("name"), c["verdict"],
                                            c.get("reason", ""), c.get("url") or ""))
    if "patch_id" in report:
        lines.append("patch-id: %s (%s)" % (report["patch_id"], report["patch_id_note"]))
    if "rerun" in report:
        lines.append("rerun: %s" % json.dumps(report["rerun"], ensure_ascii=False, sort_keys=True))
    lines.append("observed_at: %s" % report["observed_at"])
    return "\n".join(lines) + "\n"


def render(report, as_json):
    report = clean(report)
    if as_json:
        return json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    return render_human(report)


def emit(stream, text):
    buffer = getattr(stream, "buffer", None)
    if buffer is None:
        stream.write(text)
        stream.flush()
    else:
        stream.flush()
        buffer.write(text.encode("utf-8"))
        buffer.flush()


def parse_args(argv):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--repo", required=True, help="base repository OWNER/REPO")
    p.add_argument("--pr", required=True, type=int, help="pull request number")
    p.add_argument("--once", action="store_true", help="observe once (default)")
    p.add_argument("--interval", type=float, help="seconds between polls (needs --max-polls)")
    p.add_argument("--max-polls", type=int, help="poll bound while ci_pending or unknown")
    p.add_argument("--json", action="store_true", help="emit one JSON report")
    p.add_argument("--patch-id", action="store_true", help="add the aggregate PR patch-id")
    p.add_argument("--rerun-zero-step-flake", action="store_true",
                   help="rerun qualifying zero-step cancelled runs, once per (run_id, head_sha)")
    p.add_argument("--state-dir", help="rerun state directory (default %s)" % DEFAULT_STATE_DIR)
    return p.parse_args(argv)


def validate(opts):
    if not REPO_RE.fullmatch(opts.repo) or opts.repo.split("/", 1)[1] in (".", ".."):
        raise WatchError("--repo must be OWNER/REPO")
    if opts.pr <= 0:
        raise WatchError("--pr must be a positive integer")
    if (opts.interval is None) != (opts.max_polls is None):
        raise WatchError("--interval and --max-polls go together")
    if opts.interval is not None:
        if opts.once:
            raise WatchError("--once conflicts with --interval/--max-polls")
        if not opts.interval > 0 or opts.max_polls < 1:
            raise WatchError("--interval must be > 0 and --max-polls >= 1")


def main(argv=None, runner=None, out=None, err=None, sleep=time.sleep):
    out = out or sys.stdout
    err = err or sys.stderr
    runner = runner or run_command
    opts = parse_args(argv)
    state_dir = Path(os.path.expanduser(opts.state_dir or DEFAULT_STATE_DIR))
    try:
        validate(opts)
        polls = opts.max_polls or 1
        for poll in range(1, polls + 1):
            report = observe(opts, runner, state_dir)
            if report["state"] not in ("ci_pending", "unknown") or poll == polls:
                break
            emit(err, sanitize("poll %d/%d: %s\n" % (poll, polls, report["state"])))
            sleep(opts.interval)
        if opts.patch_id and report["state"] != "refused_fork":
            report["patch_id"], why = aggregate_patch_id(runner, opts.repo, opts.pr)
            report["patch_id_note"] = PATCH_ID_NOTE
            if why:
                report["patch_id_reason"] = why
    except WatchError as exc:
        report = finish(new_report(opts), "error", [str(exc)])
    emit(out, render(report, opts.json))
    return EXIT_CODES[report["state"]]


if __name__ == "__main__":
    sys.exit(main())
