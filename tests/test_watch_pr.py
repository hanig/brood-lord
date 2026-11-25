"""Offline watch_pr.py tests: gh is a fixture (live step details are not exercised),
git patch-id is real, and the sanitizer check enumerates range(0x110000)."""
import io
import json
from pathlib import Path
import sys
import unicodedata
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/hanig-orchestrate/scripts"))
import watch_pr as W  # noqa: E402

HEAD, OLD = "a" * 40, "b" * 40
BANNED = {"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"}  # the task's list, not the script's

def pr(**over):
  return dict({"number": 7, "title": "T", "author": {"login": "dev"}, "url": "u", "state": "OPEN",
               "headRefName": "f", "headRefOid": HEAD, "headRepository": {"name": "w"},
               "headRepositoryOwner": {"login": "acme"}, "isCrossRepository": False,
               "mergeable": "MERGEABLE"}, **over)

def run(rid, conclusion="success", status="completed", wid=1, created="T10", head=HEAD):
  return {"id": rid, "name": "CI", "workflow_id": wid, "status": status, "conclusion": conclusion,
          "created_at": created, "head_sha": head, "html_url": "r"}

def job(jid=9, steps=(), conclusion="cancelled", head=HEAD):
  return {"id": jid, "status": "completed", "conclusion": conclusion, "head_sha": head,
          "steps": list(steps)}

def check(cid, conclusion="success", app="github-actions", status="completed"):
  return {"id": cid, "name": "t", "status": status, "conclusion": conclusion, "head_sha": HEAD,
          "app": {"slug": app}, "details_url": "d"}

def thread(tid, resolved=False):
  return {"id": tid, "isResolved": resolved, "path": "a.py", "line": 3,
          "comments": {"nodes": [{"author": {"login": "rev"}, "body": "nit", "url": "c"}]}}

def fx(**over):
  """Zero-step flake by default: one cancelled run whose only job has no steps."""
  return dict({"pr": pr(), "runs": [run(101, "cancelled")], "jobs": {"101": [job()]},
               "checks": [check(9, "cancelled")], "statuses": []}, **over)

GREEN = dict(runs=[run(100)], jobs={}, checks=[check(8)], statuses=[{"context": "l", "state": "success"}])

def route(f, argv):
  args, ok = argv[1:], lambda d: (0, json.dumps(d).encode(), b"", False)
  if args[:2] == ["pr", "view"]:
    return (1, b"", f["err"].encode("utf-8", "surrogatepass"), False) if "err" in f else ok(f["pr"])
  if args[:2] == ["pr", "diff"]:
    return f.get("diff_rc", 0), f.get("diff", "").encode(), b"", False
  if args[:2] == ["api", "graphql"]:
    pages = f.get("threads", [[]])
    i = int(([a[2:] for a in args if a.startswith("c=")] or ["0"])[0])
    return ok({"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": pages[i],
               "pageInfo": {"hasNextPage": i + 1 < len(pages), "endCursor": str(i + 1)}}}}}})
  path, _, qs = args[1].partition("?")
  parts, first = path.split("/"), qs.endswith("&page=1")
  key, items = {"runs": ("workflow_runs", f["runs"]), "check-runs": ("check_runs", f["checks"]),
                "status": ("statuses", f["statuses"])}.get(parts[-1], ("jobs", None))
  if key == "jobs":
    items = f["jobs"].get(parts[-2])
    if items is None:
      return 1, b"", b"HTTP 404", False
  return ok({"total_count": len(items), key: items if first else []})

class Fake(object):
  def __init__(self, f):
    self.f, self.calls = f, []

  def __call__(self, argv, input_bytes=None, max_bytes=None, cwd=None):
    self.calls.append(argv)
    if argv[0] == "git":
      return W.run_command(argv, input_bytes, max_bytes, cwd)
    rc, out, err, _ = route(self.f, argv)
    cap = W.API_BYTE_CAP if max_bytes is None else max_bytes
    return (rc, b"", err, True) if len(out) > cap else (rc, out, err, False)

  def mutating(self):  # read-only means pr view, pr diff, GET api and query-only GraphQL
    return [c for c in self.calls if c[0] == "gh" and c[1:3] not in (["pr", "view"], ["pr", "diff"])
            and (c[1] != "api" or {"-X", "--method"} & set(c) or "mutation" in str(c))]

def invoke(f, *extra, as_json=True):
  fake, out = Fake(f), io.StringIO()
  code = W.main(["--repo", "acme/w", "--pr", "7"] + ["--json"] * as_json + list(extra),
                runner=fake, out=out, err=io.StringIO(), sleep=lambda s: None)
  return code, (json.loads(out.getvalue()) if as_json else out.getvalue()), fake

class TestOrder(unittest.TestCase):
  def test_fork_and_null_head_refused_with_one_call(self):
    for over in ({"headRepositoryOwner": {"login": "evil"}}, {"headRepository": {"name": "x"}},
                 {"headRepository": None}, {"headRepositoryOwner": None},
                 {"headRepositoryOwner": {}}, {"isCrossRepository": True}, {"isCrossRepository": None}):
      with self.subTest(over):  # every later blocker is armed
        code, rep, fake = invoke(fx(pr=pr(mergeable="CONFLICTING", **over), threads=[[thread("T")]]),
                                 "--patch-id")
        self.assertEqual((code, rep["state"], len(fake.calls)), (16, "refused_fork", 1))
        self.assertNotIn("patch_id", rep)
    p = pr()
    del p["headRepository"]
    self.assertEqual(len(invoke(fx(pr=p))[2].calls), 1)

  def test_conflicts_and_unknown_stop_after_pr_view(self):
    for mergeable, code in (("CONFLICTING", 11), ("UNKNOWN", 15), (None, 15), ("X", 15)):
      c, rep, fake = invoke(fx(pr=pr(mergeable=mergeable), threads=[[thread("T")]]))
      self.assertEqual((c, len(fake.calls)), (code, 1), mergeable)
    self.assertEqual(invoke(fx(pr=pr(state="MERGED")))[0], 15)

  def test_threads_stop_before_ci(self):
    code, rep, fake = invoke(fx(runs=[run(5, "failure")], threads=[[thread("A", True), thread("B")]]))
    self.assertEqual((code, [t["id"] for t in rep["threads"]]), (12, ["B"]))
    self.assertEqual([c[1:3] for c in fake.calls], [["pr", "view"], ["api", "graphql"]])
    code, rep, _ = invoke(fx(threads=[[thread("A", True)], [{"id": "C"}]], **GREEN))
    self.assertEqual((code, [t["id"] for t in rep["threads"]]), (12, ["C"]))  # page 2, no flag
    self.assertEqual(invoke(fx(threads=[[thread("A", True)]], **GREEN))[0], 0)

  def test_schema_errors_and_polling(self):
    code, rep, _ = invoke(fx(**GREEN))
    self.assertEqual((code, rep["state"], rep["schema_version"], rep["head_sha"]), (0, "ready", 1, HEAD))
    self.assertFalse(rep["action_required"])
    self.assertRegex(rep["observed_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
    code, rep, _ = invoke(fx(err="HTTP 502"))
    self.assertEqual((code, rep["state"], rep["head_sha"], rep["action_required"]), (2, "error", None, True))
    for extra in (["--interval", "1"], ["--once", "--interval", "1", "--max-polls", "2"]):
      code, rep, fake = invoke(fx(), *extra)
      self.assertEqual((code, fake.calls), (2, []))
    code, rep, fake = invoke(fx(**dict(GREEN, runs=[run(1, None, "queued")])), "--interval", "1",
                             "--max-polls", "3")
    self.assertEqual((code, len(fake.calls)), (10, 3 * 5))

F, P, Z = "ci_failed", "ci_pending", "ci_zero_step_flake"
CI_CASES = [  # (label, fixture overrides, state, a reason that must be listed)
  ("green", GREEN, "ready", None), ("none", dict(runs=[], checks=[], statuses=[]), P, None),
  ("zero-step", {}, Z, None),
  ("stale cancel", dict(GREEN, runs=[run(100), run(90, "cancelled", head=OLD)]), "ready", None),
  ("stale failure", dict(runs=[run(101, "cancelled"), run(90, "failure", head=OLD)]), Z, None),
  ("newer ok", dict(runs=[run(101, "cancelled"), run(102, created="T11")]), F,
   "cancelled_superseded_by_newer_run"),
  ("newer queued", dict(runs=[run(101, "cancelled"), run(102, None, "queued", created="T11")]), F,
   "cancelled_superseded_by_newer_run"),
  ("older/other", dict(runs=[run(101, "cancelled"), run(99, created="T09"), run(102, wid=2)]), Z, None),
  ("failure", dict(runs=[run(101, "failure")], checks=[]), F, "zero_step_failure_unclassified"),
  ("startup", dict(runs=[run(101, "startup_failure")], checks=[]), F, "zero_step_failure_unclassified"),
  ("timeout", dict(runs=[run(101, "timed_out")], checks=[]), F, "zero_step_failure_unclassified"),
  ("startup, no jobs", dict(runs=[run(101, "startup_failure")], jobs={}, checks=[]), F,
   "startup_failure"),
  ("steps", dict(jobs={"101": [job(steps=[{"name": "Set up job"}])]}), F, "started_steps"),
  ("no payload", dict(jobs={}), F, "job_data_missing"),
  ("no jobs", dict(jobs={"101": []}), F, "job_data_missing"),
  ("no steps key", dict(jobs={"101": [{"id": 9, "conclusion": "cancelled", "head_sha": HEAD}]}), F,
   "job_data_missing"),
  ("null steps", dict(jobs={"101": [dict(job(), steps=None)]}), F, "job_data_missing"),
  ("old job", dict(jobs={"101": [job(head=OLD)]}), F, "job_head_mismatch"),
  ("job failed", dict(jobs={"101": [job(conclusion="failure")]}), F, "job_not_cancelled"),
  ("status", dict(statuses=[{"context": "x", "state": "failure"}]), F, "non_actions_status_failure"),
  ("other app", dict(checks=[check(9, "cancelled"), check(9, "cancelled", "ci")]), F,
   "non_actions_check_cancelled"),
  ("status pending", dict(statuses=[{"context": "x", "state": "pending"}]), P, None),
  ("in progress", dict(runs=[run(101, "cancelled"), run(5, None, "in_progress", wid=2)]), P, None),
  ("uncovered", dict(GREEN, checks=[check(77, "cancelled")]), F, "cancelled"),
  ("no conclusion", dict(GREEN, runs=[run(100, None)]), F, "conclusion_missing"),
]

class TestCi(unittest.TestCase):
  def test_classification_without_mutating_calls(self):
    for label, over, state, reason in CI_CASES:
      with self.subTest(label):
        code, rep, fake = invoke(fx(**over), "--patch-id")
        self.assertEqual((rep["state"], code), (state, W.EXIT_CODES[state]), rep)
        if reason:
          self.assertIn(reason, [c.get("reason") for c in rep["checks"]])
        self.assertEqual(fake.mutating(), [])
        self.assertEqual(rep.get("zero_step_runs"), [101] if state == "ci_zero_step_flake" else None)

DIFF = "diff --git a/f b/f\nindex %s 100644\n--- a/f\n+++ b/f\n@@ -%d,2 +%d,3 @@\n one\n+%s\n two\n"

class TestPatchId(unittest.TestCase):
  def pid(self, diff, **over):
    code, rep, fake = invoke(fx(diff=diff, **dict(GREEN, **over)), "--patch-id")
    self.assertEqual(rep["patch_id_note"], "aggregate delta identity only; never certifies CI validity")
    self.assertNotIn("--patch", [a for c in fake.calls for a in c])
    return rep["patch_id"]

  def test_restack_equal_change_unequal_errors_unavailable(self):
    base = self.pid(DIFF % ("1111111..2222222", 3, 3, "x"))
    self.assertRegex(base, "^[0-9a-f]{40}$")
    self.assertEqual(base, self.pid(DIFF % ("3333333..4444444", 40, 40, "x")))
    self.assertNotEqual(base, self.pid(DIFF % ("1111111..2222222", 3, 3, "y")))
    self.assertEqual(self.pid(DIFF % ("1..2", 3, 3, "x"), diff_rc=1), "unavailable")
    self.assertEqual(self.pid(""), "unavailable")
    with mock.patch.object(W, "DIFF_BYTE_CAP", 20):
      self.assertEqual(self.pid(DIFF % ("1..2", 3, 3, "x")), "unavailable")

class TestSanitizer(unittest.TestCase):
  def test_exhaustive_code_points(self):
    # A trailing character could be a sequence body after ESC/CSI/OSC; test_ansi covers that.
    wrong = [cp for cp in range(0x110000) for text in (chr(cp), "a" + chr(cp))
             if W.sanitize(text) != (text if chr(cp) in "\t\n"
                                     or unicodedata.category(chr(cp)) not in BANNED else text[:-1])]
    self.assertEqual(wrong, [])

  def test_ansi(self):
    for raw, want in {"\x1b[31mred\x1b[0m": "red", "\x1b]8;;h\x07l\x1b]8;;\x07": "l",
                      "\x1b]0;t\x1b\\x": "x", "\x9b31mc": "c", "\x1b(Bp": "p"}.items():
      self.assertEqual(W.sanitize(raw), want)

  def test_every_echoed_field_in_human_and_json_output(self):
    t = "<x>%s\x1b[31m\x1b]0;t\x07</x>" % "".join(
      chr(c) for c in range(0x110000) if chr(c) not in "\t\n" and unicodedata.category(chr(c)) in BANNED)
    p = pr(title=t, author={"login": t}, headRefName=t, url=t)
    node = {"id": t, "path": t, "comments": {"nodes": [{"author": {"login": t}, "body": t, "url": t}]}}
    ci = dict(runs=[dict(run(5, "failure"), name=t, html_url=t)], statuses=[
      {"context": t, "state": "error", "target_url": t}], checks=[dict(check(6, "x", "ci"), name=t, details_url=t)])
    cases = [(12, fx(pr=p, threads=[[node]]), 9), (13, fx(pr=p, **ci), 13),
             (16, fx(pr=dict(p, headRepositoryOwner={"login": t})), 5), (2, fx(err=t), 0)]
    with mock.patch.object(W, "API_BYTE_CAP", 1 << 30):  # the escaped payload exceeds 16 MiB
      for want, f, fields in cases:
        for as_json in (True, False):
          code, out, _ = invoke(f, as_json=as_json)
          text = json.dumps(out, ensure_ascii=False) if as_json else out
          self.assertEqual(code, want)
          self.assertEqual({c for c in text if c not in "\t\n" and unicodedata.category(c) in BANNED}, set())
          self.assertGreaterEqual(text.count("<x></x>"), fields, (want, as_json))
          self.assertIn("<x>", text)

if __name__ == "__main__":
  unittest.main()
