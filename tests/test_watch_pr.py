"""Offline fixture tests for watch_pr.py (gh is stubbed; git patch-id is real).

Live GitHub step-detail behavior on real cancelled runs is fixture-verified,
not live-exercised. The sanitizer check enumerates range(0x110000)
exhaustively (no random sampling, no seed).
"""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unicodedata
import unittest
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "skills/hanig-orchestrate/scripts/watch_pr.py"
sys.path.insert(0, str(SCRIPT.parent))
import watch_pr as W  # noqa: E402

REPO = "acme/widgets"
HEAD = "a" * 40
OLD = "b" * 40
NEW = "c" * 40
# Literal copy of the task's category list: the test must not read the script's own set.
BANNED = {"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"}
MUTATING_PR_VERBS = {"merge", "review", "edit", "comment", "close", "reopen", "ready", "checkout"}


def pr_fixture(**over):
    pr = {"number": 7, "title": "Add thing", "author": {"login": "dev"},
          "url": "https://github.com/acme/widgets/pull/7", "state": "OPEN",
          "headRefName": "feature", "headRefOid": HEAD,
          "headRepository": {"name": "widgets"}, "headRepositoryOwner": {"login": "acme"},
          "isCrossRepository": False, "mergeable": "MERGEABLE"}
    pr.update(over)
    return pr


def wf_run(rid, conclusion="success", status="completed", wid=1,
           created="2026-11-24T10:00:00Z", head=HEAD, name="CI"):
    return {"id": rid, "name": name, "workflow_id": wid, "status": status,
            "conclusion": conclusion, "created_at": created, "head_sha": head,
            "html_url": "https://github.com/acme/widgets/actions/runs/%d" % rid}


def job(jid, steps=(), conclusion="cancelled", head=HEAD, status="completed"):
    return {"id": jid, "name": "test", "status": status, "conclusion": conclusion,
            "head_sha": head, "steps": list(steps)}


def check(cid, conclusion="success", app="github-actions", status="completed", head=HEAD,
          name="test"):
    return {"id": cid, "name": name, "status": status, "conclusion": conclusion,
            "head_sha": head, "app": {"slug": app},
            "details_url": "https://github.com/acme/widgets/runs/%d" % cid}


def thread(tid, resolved=False, body="please fix"):
    return {"id": tid, "isResolved": resolved, "isOutdated": False, "path": "a.py", "line": 3,
            "comments": {"nodes": [{"author": {"login": "rev"}, "body": body,
                                    "url": "https://github.com/acme/widgets/pull/7#r1"}]}}


def flake_fixture(**over):
    fx = {"pr": pr_fixture(), "runs": [wf_run(101, "cancelled")],
          "jobs": {"101": [job(9001)]}, "checks": [check(9001, "cancelled")], "statuses": []}
    fx.update(over)
    return fx


def green_fixture(**over):
    fx = {"pr": pr_fixture(), "runs": [wf_run(100)], "jobs": {},
          "checks": [check(8000)], "statuses": [{"context": "lint", "state": "success"}]}
    fx.update(over)
    return fx


def _listing(key, items, query):
    page = int(query.get("page", "1"))
    return {"total_count": len(items), key: items if page == 1 else []}


def route(fx, argv):
    """Answer one gh argv from fixture ``fx``; shared by FakeRunner and the gh stub."""
    args = argv[1:]
    ok = lambda doc: (0, json.dumps(doc).encode(), b"")  # noqa: E731
    if args[:2] == ["pr", "view"]:
        fields = args[args.index("--json") + 1]
        if fields == "headRefOid":
            return ok({"headRefOid": fx.get("head_after", fx["pr"].get("headRefOid"))})
        if fx.get("pr_view_error"):
            return 1, b"", fx["pr_view_error"].encode("utf-8", "surrogatepass")
        return ok(fx["pr"])
    if args[:2] == ["pr", "diff"]:
        return fx.get("diff_rc", 0), fx.get("diff", "").encode(), b"diff failed"
    if args[:2] == ["api", "graphql"]:
        pages = fx.get("thread_pages", [[]])
        cursor = [a for a in args if a.startswith("cursor=")]
        i = int(cursor[0].split("=", 1)[1]) if cursor else 0
        return ok({"data": {"repository": {"pullRequest": {"reviewThreads": {
            "pageInfo": {"hasNextPage": i + 1 < len(pages), "endCursor": str(i + 1)},
            "nodes": pages[i]}}}}})
    if args[:3] == ["api", "-X", "POST"]:
        return ok({})
    if args[0] == "api":
        path, _, qs = args[1].partition("?")
        query = dict(p.split("=", 1) for p in qs.split("&") if p)
        parts = path.split("/")
        if path.endswith("/actions/runs"):
            assert query["head_sha"] == fx["pr"]["headRefOid"]
            return ok(_listing("workflow_runs", fx.get("runs", []), query))
        if parts[-1] == "jobs":
            jobs = fx.get("jobs", {}).get(parts[-2])
            if jobs is None:
                return 1, b"", b"HTTP 404: Not Found"
            return ok(_listing("jobs", jobs, query))
        if parts[-1] == "check-runs":
            return ok(_listing("check_runs", fx.get("checks", []), query))
        if parts[-1] == "status":
            return ok(_listing("statuses", fx.get("statuses", []), query))
    return 99, b"", ("unrouted gh call %r" % (args,)).encode()


def is_mutating(argv):
    if argv[0] != "gh":
        return False
    args = argv[1:]
    for flag in ("-X", "--method"):
        if flag in args and args[args.index(flag) + 1].upper() != "GET":
            return True
    if any("rerun" in a or "mutation" in a for a in args):
        return True
    return args[:1] == ["pr"] and len(args) > 1 and args[1] in MUTATING_PR_VERBS


class FakeRunner(object):
    def __init__(self, fx):
        self.fx = fx
        self.calls = []

    def __call__(self, argv, input_bytes=None, max_bytes=None, cwd=None):
        self.calls.append(list(argv))
        max_bytes = W.API_BYTE_CAP if max_bytes is None else max_bytes
        if argv[0] == "git":  # real git patch-id, in the scratch cwd chosen by the script
            return W.run_command(argv, input_bytes=input_bytes, max_bytes=max_bytes, cwd=cwd)
        rc, out, err = route(self.fx, argv)
        if len(out) > max_bytes:
            return W.CommandResult(rc, b"", err, truncated=True)
        return W.CommandResult(rc, out, err)

    @property
    def gh(self):
        return [c for c in self.calls if c[0] == "gh"]

    @property
    def mutating(self):
        return [c for c in self.calls if is_mutating(c)]


def invoke(fx, *extra, as_json=True, runner=None):
    runner = runner or FakeRunner(fx)
    out, err = io.StringIO(), io.StringIO()
    argv = ["--repo", REPO, "--pr", "7"] + (["--json"] if as_json else []) + list(extra)
    code = W.main(argv, runner=runner, out=out, err=err, sleep=lambda s: None)
    text = out.getvalue()
    return code, (json.loads(text) if as_json else text), runner


class TestForkRefusal(unittest.TestCase):
    CASES = {
        "other owner": {"headRepositoryOwner": {"login": "mallory"}},
        "other name": {"headRepository": {"name": "widgets-fork"}},
        "null head repo": {"headRepository": None},
        "null head owner": {"headRepositoryOwner": None},
        "missing head repo": {"headRepository": "__delete__"},
        "missing owner login": {"headRepositoryOwner": {}},
        "cross repository flag": {"isCrossRepository": True},
    }

    def test_fork_and_null_head_refused_with_a_single_gh_call(self):
        for label, over in self.CASES.items():
            with self.subTest(label):
                pr = pr_fixture(**over)
                for k in [k for k, v in over.items() if v == "__delete__"]:
                    del pr[k]
                # Every later blocker is armed: a missed refusal would reach it.
                fx = flake_fixture(pr=dict(pr, mergeable="CONFLICTING"),
                                   thread_pages=[[thread("T1")]], diff="x")
                code, report, runner = invoke(fx, "--patch-id", "--rerun-zero-step-flake",
                                              "--state-dir", tempfile.mkdtemp())
                self.assertEqual((code, report["state"]), (16, "refused_fork"))
                self.assertEqual(len(runner.calls), 1)
                self.assertEqual(runner.calls[0][:3], ["gh", "pr", "view"])
                self.assertNotIn("patch_id", report)
                self.assertTrue(report["action_required"])

    def test_same_repo_head_is_not_refused(self):
        code, report, _ = invoke(green_fixture())
        self.assertEqual((code, report["state"]), (0, "ready"))


class TestOrdering(unittest.TestCase):
    def test_conflicts_stop_before_threads_and_ci(self):
        fx = flake_fixture(pr=pr_fixture(mergeable="CONFLICTING"), thread_pages=[[thread("T1")]])
        code, report, runner = invoke(fx)
        self.assertEqual((code, report["state"]), (11, "conflicts"))
        self.assertEqual(len(runner.gh), 1)

    def test_unknown_or_missing_mergeable_is_never_clean(self):
        for value in ("UNKNOWN", None, "__delete__", "SOMETHING"):
            with self.subTest(value):
                pr = pr_fixture(mergeable=value)
                if value == "__delete__":
                    del pr["mergeable"]
                code, report, runner = invoke(green_fixture(pr=pr))
                self.assertEqual((code, report["state"]), (15, "unknown"))
                self.assertEqual(len(runner.gh), 1)

    def test_closed_pr_is_unknown(self):
        code, report, _ = invoke(green_fixture(pr=pr_fixture(state="MERGED")))
        self.assertEqual((code, report["state"]), (15, "unknown"))

    def test_open_threads_stop_before_ci(self):
        fx = flake_fixture(runs=[wf_run(5, "failure")],
                           thread_pages=[[thread("T0", True), thread("T1", body="nit")]])
        code, report, runner = invoke(fx)
        self.assertEqual((code, report["state"]), (12, "threads_open"))
        self.assertEqual([t["id"] for t in report["threads"]], ["T1"])
        self.assertEqual(report["threads"][0]["excerpt"], "nit")
        self.assertFalse([c for c in runner.gh if c[1] == "api" and c[2] != "graphql"])

    def test_open_thread_on_second_page_and_missing_flag(self):
        fx = green_fixture(thread_pages=[[thread("T0", True)], [thread("T9", True)],
                                         [{"id": "T2", "path": "b.py"}]])
        code, report, _ = invoke(fx)
        self.assertEqual((code, report["state"]), (12, "threads_open"))
        self.assertEqual([t["id"] for t in report["threads"]], ["T2"])

    def test_resolved_threads_reach_ci(self):
        code, report, _ = invoke(green_fixture(thread_pages=[[thread("T0", True)]]))
        self.assertEqual((code, report["state"]), (0, "ready"))

    def test_api_error_is_exit_two_with_schema(self):
        fx = green_fixture(pr_view_error="HTTP 502")
        code, report, _ = invoke(fx)
        self.assertEqual((code, report["state"]), (2, "error"))
        for key in ("schema_version", "observed_at", "state", "head_sha", "action_required"):
            self.assertIn(key, report)

    def test_config_errors_exit_two(self):
        for extra in (["--interval", "5"], ["--once", "--interval", "1", "--max-polls", "2"]):
            with self.subTest(extra):
                code, report, runner = invoke(green_fixture(), *extra)
                self.assertEqual((code, report["state"]), (2, "error"))
                self.assertEqual(runner.calls, [])
        runner = FakeRunner({})
        code = W.main(["--repo", "bad", "--pr", "7", "--json"], runner=runner,
                      out=io.StringIO(), err=io.StringIO())
        self.assertEqual((code, runner.calls), (2, []))

    def test_polling_stops_at_max_polls_while_pending(self):
        fx = green_fixture(runs=[wf_run(1, None, status="in_progress")])
        code, report, runner = invoke(fx, "--interval", "1", "--max-polls", "3")
        self.assertEqual((code, report["state"]), (10, "ci_pending"))
        self.assertEqual(sum(1 for c in runner.gh if c[1:3] == ["pr", "view"]), 3)


class TestCiClassification(unittest.TestCase):
    def assertCi(self, fx, state, reason=None):
        code, report, runner = invoke(fx)
        self.assertEqual((report["state"], code), (state, W.EXIT_CODES[state]), report)
        if reason:
            self.assertIn(reason, [c.get("reason") for c in report["checks"]], report)
        self.assertEqual(runner.mutating, [])
        return report

    def test_ready_and_schema(self):
        report = self.assertCi(green_fixture(), "ready")
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(report["head_sha"], HEAD)
        self.assertFalse(report["action_required"])
        self.assertRegex(report["observed_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")

    def test_no_checks_is_pending(self):
        self.assertCi(green_fixture(runs=[], checks=[], statuses=[]), "ci_pending")

    def test_cancelled_empty_steps_is_zero_step_flake(self):
        report = self.assertCi(flake_fixture(), "ci_zero_step_flake")
        self.assertEqual(report["zero_step_runs"], [101])
        self.assertTrue(report["action_required"])

    def test_stale_sha_cancellation_is_ignored(self):
        fx = green_fixture(runs=[wf_run(100), wf_run(90, "cancelled", head=OLD)],
                           jobs={"90": [job(1, head=OLD, steps=[{"n": 1}])]},
                           checks=[check(8000), check(1, "cancelled", head=OLD)])
        self.assertCi(fx, "ready")
        fx = flake_fixture(runs=[wf_run(101, "cancelled"), wf_run(90, "failure", head=OLD)])
        self.assertCi(fx, "ci_zero_step_flake")

    def test_newer_non_cancelled_same_workflow_disqualifies(self):
        for newer in (wf_run(102, "success", created="2026-11-24T11:00:00Z"),
                      wf_run(102, None, status="in_progress", created="2026-11-24T11:00:00Z")):
            with self.subTest(newer["status"]):
                fx = flake_fixture(runs=[wf_run(101, "cancelled"), newer])
                self.assertCi(fx, "ci_failed", "cancelled_superseded_by_newer_run")
        # An older run of the same workflow, or a newer run of another, does not.
        fx = flake_fixture(runs=[wf_run(101, "cancelled"),
                                 wf_run(99, "success", created="2026-11-24T09:00:00Z"),
                                 wf_run(102, "success", wid=2, created="2026-11-24T11:00:00Z")])
        self.assertCi(fx, "ci_zero_step_flake")

    def test_failure_and_startup_failure_with_empty_steps_are_not_flakes(self):
        for conclusion in ("failure", "startup_failure", "timed_out"):
            with self.subTest(conclusion):
                fx = flake_fixture(runs=[wf_run(101, conclusion)], checks=[])
                self.assertCi(fx, "ci_failed", "zero_step_failure_unclassified")
                fx = flake_fixture(runs=[wf_run(101, conclusion)], jobs={}, checks=[])
                self.assertCi(fx, "ci_failed", conclusion)

    def test_started_steps_are_not_a_flake(self):
        fx = flake_fixture(jobs={"101": [job(9001, steps=[{"name": "Set up job"}])]})
        self.assertCi(fx, "ci_failed", "started_steps")
        fx = flake_fixture(jobs={"101": [job(9001), job(9002, steps=[{"name": "x"}],
                                                        conclusion="skipped")]})
        self.assertCi(fx, "ci_failed", "started_steps")

    def test_missing_job_data_is_not_a_flake(self):
        bad = {"absent": {}, "empty": {"101": []},
               "no steps key": {"101": [{k: v for k, v in job(9001).items() if k != "steps"}]},
               "null steps": {"101": [dict(job(9001), steps=None)]}}
        for label, jobs in bad.items():
            with self.subTest(label):
                self.assertCi(flake_fixture(jobs=jobs), "ci_failed", "job_data_missing")

    def test_job_on_other_head_or_not_cancelled_is_not_a_flake(self):
        self.assertCi(flake_fixture(jobs={"101": [job(9001, head=OLD)]}), "ci_failed",
                      "job_head_mismatch")
        self.assertCi(flake_fixture(jobs={"101": [job(9001, conclusion="failure")]}),
                      "ci_failed", "job_not_cancelled")

    def test_non_actions_contexts_block_the_flake(self):
        fx = flake_fixture(statuses=[{"context": "ext", "state": "failure"}])
        self.assertCi(fx, "ci_failed", "non_actions_status_failure")
        fx = flake_fixture(checks=[check(9001, "cancelled"), check(5, "failure", app="circleci")])
        self.assertCi(fx, "ci_failed", "non_actions_check_failure")
        fx = flake_fixture(checks=[check(9001, "cancelled"), check(5, "cancelled", app="circleci")])
        self.assertCi(fx, "ci_failed", "non_actions_check_cancelled")
        self.assertCi(flake_fixture(statuses=[{"context": "ext", "state": "pending"}]),
                      "ci_pending")

    def test_in_progress_run_blocks_the_flake(self):
        fx = flake_fixture(runs=[wf_run(101, "cancelled"),
                                 wf_run(50, None, status="queued", wid=2)])
        self.assertCi(fx, "ci_pending")

    def test_uncovered_actions_cancellation_fails(self):
        fx = green_fixture(checks=[check(8000), check(7777, "cancelled")])
        self.assertCi(fx, "ci_failed", "cancelled")

    def test_completed_without_conclusion_is_not_ready(self):
        self.assertCi(green_fixture(runs=[wf_run(100, None)]), "ci_failed", "conclusion_missing")


class TestRerun(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = os.path.join(self.tmp.name, "state")

    def tearDown(self):
        self.tmp.cleanup()

    def test_default_runs_make_no_mutating_call(self):
        fixtures = [flake_fixture(), green_fixture(), flake_fixture(diff="x"),
                    flake_fixture(runs=[wf_run(1, "failure")]),
                    green_fixture(thread_pages=[[thread("T")]])]
        for fx in fixtures:
            for extra in ([], ["--patch-id"], ["--state-dir", self.state]):
                code, report, runner = invoke(fx, *extra)
                self.assertEqual(runner.mutating, [])
                self.assertNotIn("rerun", report)
        self.assertFalse(os.path.exists(self.state))

    def test_rerun_once_per_run_and_head_across_invocations(self):
        code, report, runner = invoke(flake_fixture(), "--rerun-zero-step-flake",
                                      "--state-dir", self.state)
        self.assertEqual(code, 14)
        self.assertEqual(runner.mutating, [["gh", "api", "-X", "POST",
                                            "repos/acme/widgets/actions/runs/101/rerun-failed-jobs"]])
        self.assertEqual(report["rerun"]["runs"], [{"run_id": 101, "result": "requested"}])
        saved = json.loads(Path(self.state, W.STATE_FILE).read_text())
        self.assertEqual(saved["reruns"]["101:" + HEAD]["result"], "requested")
        code, report, runner = invoke(flake_fixture(), "--rerun-zero-step-flake",
                                      "--state-dir", self.state)
        self.assertEqual(runner.mutating, [])
        self.assertEqual(report["rerun"]["runs"], [{"run_id": 101, "result": "already_rerun"}])
        # A new head is a new key.
        fx = flake_fixture(pr=pr_fixture(headRefOid=NEW),
                           jobs={"101": [job(9001, head=NEW)]},
                           runs=[wf_run(101, "cancelled", head=NEW)],
                           checks=[check(9001, "cancelled", head=NEW)])
        code, report, runner = invoke(fx, "--rerun-zero-step-flake", "--state-dir", self.state)
        self.assertEqual(len(runner.mutating), 1)

    def test_head_move_aborts_without_rerun_or_state(self):
        fx = flake_fixture(head_after=NEW)
        code, report, runner = invoke(fx, "--rerun-zero-step-flake", "--state-dir", self.state)
        self.assertEqual(report["rerun"]["status"], "aborted")
        self.assertEqual(report["rerun"]["reason"], "head_moved")
        self.assertEqual(runner.mutating, [])
        self.assertFalse(os.path.exists(self.state))

    def test_flag_on_non_flake_state_makes_no_extra_call(self):
        fx = flake_fixture(runs=[wf_run(101, "failure")])
        code, report, runner = invoke(fx, "--rerun-zero-step-flake", "--state-dir", self.state)
        self.assertEqual(code, 13)
        self.assertEqual(runner.mutating, [])
        self.assertEqual(sum(1 for c in runner.gh if c[1:3] == ["pr", "view"]), 1)

    def test_state_dir_inside_git_worktree_is_refused(self):
        os.makedirs(os.path.join(self.tmp.name, "repo", ".git"))
        inside = os.path.join(self.tmp.name, "repo", "state")
        code, report, runner = invoke(flake_fixture(), "--rerun-zero-step-flake",
                                      "--state-dir", inside)
        self.assertEqual(code, 2)
        self.assertEqual(runner.mutating, [])

    def test_corrupt_state_file_refuses_rerun(self):
        os.makedirs(self.state)
        Path(self.state, W.STATE_FILE).write_text("{not json")
        code, report, runner = invoke(flake_fixture(), "--rerun-zero-step-flake",
                                      "--state-dir", self.state)
        self.assertEqual(code, 2)
        self.assertEqual(runner.mutating, [])

    def test_cap_holds_across_concurrent_fresh_processes(self):
        bindir = os.path.join(self.tmp.name, "bin")
        os.makedirs(bindir)
        stub = os.path.join(bindir, "gh")
        Path(stub).write_text(
            "#!%s\nimport importlib.util, json, os, sys\n"
            "spec = importlib.util.spec_from_file_location('watch_pr_fixture', %r)\n"
            "mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)\n"
            "fx = json.load(open(os.environ['WATCH_FIXTURE']))\n"
            "with open(os.environ['WATCH_LOG'], 'a') as log:\n"
            "    log.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "rc, out, err = mod.route(fx, ['gh'] + sys.argv[1:])\n"
            "sys.stdout.buffer.write(out); sys.stderr.buffer.write(err); sys.exit(rc)\n"
            % (sys.executable, str(Path(__file__).resolve())))
        os.chmod(stub, 0o755)
        fixture = os.path.join(self.tmp.name, "fx.json")
        Path(fixture).write_text(json.dumps(flake_fixture()))
        log = os.path.join(self.tmp.name, "calls.log")
        env = {"PATH": bindir, "HOME": self.tmp.name, "WATCH_FIXTURE": fixture, "WATCH_LOG": log}
        argv = [sys.executable, str(SCRIPT), "--repo", REPO, "--pr", "7", "--json",
                "--rerun-zero-step-flake", "--state-dir", self.state]
        procs = [subprocess.Popen(argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                 for _ in range(4)]
        outs = [p.communicate(timeout=120) for p in procs]
        self.assertEqual([p.returncode for p in procs], [14] * 4, [o[1] for o in outs])
        later = subprocess.run(argv, env=env, stdout=subprocess.PIPE, timeout=120)
        self.assertEqual(later.returncode, 14)
        results = sorted(json.loads(o[0])["rerun"]["runs"][0]["result"] for o in outs)
        self.assertEqual(results, ["already_rerun"] * 3 + ["requested"])
        self.assertEqual(json.loads(later.stdout)["rerun"]["runs"][0]["result"], "already_rerun")
        calls = [json.loads(l) for l in Path(log).read_text().splitlines()]
        self.assertEqual(sum(1 for c in calls if is_mutating(["gh"] + c)), 1)


DIFF = """diff --git a/f.txt b/f.txt
index 1111111..2222222 100644
--- a/f.txt
+++ b/f.txt
@@ -%d,3 +%d,4 @@ ctx
 one
 two
+%s
 three
"""


class TestPatchId(unittest.TestCase):
    def patch_id(self, **fx):
        code, report, _ = invoke(green_fixture(**fx), "--patch-id")
        self.assertEqual(report["patch_id_note"],
                         "aggregate delta identity only; never certifies CI validity")
        return report["patch_id"]

    def test_restack_equal_content_change_unequal_error_unavailable(self):
        base = self.patch_id(diff=DIFF % (10, 10, "added"))
        restacked = self.patch_id(diff=(DIFF % (42, 42, "added")).replace("1111111..2222222",
                                                                         "3333333..4444444"))
        changed = self.patch_id(diff=DIFF % (10, 10, "different"))
        self.assertRegex(base, r"^[0-9a-f]{40}$")
        self.assertEqual(base, restacked)
        self.assertNotEqual(base, changed)
        self.assertEqual(self.patch_id(diff=DIFF % (1, 1, "x"), diff_rc=1), "unavailable")
        self.assertEqual(self.patch_id(diff=""), "unavailable")
        with mock.patch.object(W, "DIFF_BYTE_CAP", 20):
            self.assertEqual(self.patch_id(diff=DIFF % (1, 1, "x")), "unavailable")

    def test_patch_id_uses_plain_diff(self):
        code, report, runner = invoke(green_fixture(diff=DIFF % (1, 1, "x")), "--patch-id")
        diff_calls = [c for c in runner.gh if c[1:3] == ["pr", "diff"]]
        self.assertEqual(len(diff_calls), 1)
        self.assertNotIn("--patch", diff_calls[0])


def banned_chars():
    return "".join(chr(c) for c in range(0x110000)
                   if chr(c) not in "\t\n" and unicodedata.category(chr(c)) in BANNED)


class TestSanitizer(unittest.TestCase):
    def test_exhaustive_code_points(self):
        # A trailing character would be consumed by ESC/CSI/OSC introducers as a
        # sequence body; test_ansi_sequences covers that context.
        wrong = []
        for cp in range(0x110000):
            ch = chr(cp)
            keep = ch in "\t\n" or unicodedata.category(ch) not in BANNED
            for text, kept in ((ch, ch), ("a" + ch, "a" + ch)):
                if W.sanitize(text) != (kept if keep else kept[:-1]):
                    wrong.append(cp)
        self.assertEqual(wrong, [])

    def test_ansi_sequences(self):
        cases = {"\x1b[31mred\x1b[0m": "red", "\x1b]8;;http://x\x07link\x1b]8;;\x07": "link",
                 "\x1b]0;title\x1b\\text": "text", "\x9b31mc1": "c1", "\x1b(Bplain": "plain"}
        for raw, want in cases.items():
            self.assertEqual(W.sanitize(raw), want)

    def assertClean(self, text):
        bad = sorted({hex(ord(c)) for c in text
                      if c not in "\t\n" and unicodedata.category(c) in BANNED})
        self.assertEqual(bad, [])
        self.assertNotIn("\x1b", text)

    def walk(self, value):
        if isinstance(value, dict):
            for k, v in value.items():
                self.assertClean(k)
                self.walk(v)
        elif isinstance(value, list):
            for v in value:
                self.walk(v)
        elif isinstance(value, str):
            self.assertClean(value)

    def test_every_echoed_field_in_human_and_json_output(self):
        dirty = banned_chars() + "\x1b[31m\x1b]0;t\x07"
        tag = lambda name: "<%s>%s</%s>" % (name, dirty, name)  # noqa: E731
        pr = pr_fixture(title=tag("title"), author={"login": tag("author")},
                        headRefName=tag("branch"), url=tag("url"))
        node = {"id": tag("tid"), "isResolved": False, "isOutdated": False, "path": tag("path"),
                "line": 1, "comments": {"nodes": [{"author": {"login": tag("reviewer")},
                                                   "body": tag("body"), "url": tag("turl")}]}}
        run = dict(wf_run(5, "failure"), name=tag("run"), html_url=tag("runurl"))
        chk = dict(check(6, "failure", app="circleci"), name=tag("check"), details_url=tag("curl"))
        status = {"context": tag("ctx"), "state": "error", "target_url": tag("surl"),
                  "description": tag("desc")}
        scenarios = {
            "threads_open": green_fixture(pr=pr, thread_pages=[[node]]),
            "ci_failed": green_fixture(pr=pr, runs=[run], checks=[chk], statuses=[status]),
            "refused_fork": green_fixture(pr=dict(pr, headRepositoryOwner={"login": tag("owner")})),
            "error": green_fixture(pr_view_error=tag("stderr")),
        }
        # The JSON-escaped payload exceeds the production API cap; lift it so the
        # payload reaches the renderer instead of becoming a truncation error.
        cap = mock.patch.object(W, "API_BYTE_CAP", 1 << 30)
        cap.start()
        self.addCleanup(cap.stop)
        for state, fx in scenarios.items():
            for as_json in (True, False):
                with self.subTest(state=state, json=as_json):
                    code, out, _ = invoke(fx, as_json=as_json)
                    text = json.dumps(out, ensure_ascii=False) if as_json else out
                    self.assertClean(text)
                    if as_json:
                        self.assertEqual(out["state"], state)
                        self.walk(out)
                    self.assertEqual(code, W.EXIT_CODES[state])
                    for name in {"threads_open": ("title", "author", "branch", "url", "tid",
                                                  "path", "reviewer", "body", "turl"),
                                 "ci_failed": ("run", "runurl", "check", "curl", "ctx", "surl"),
                                 "refused_fork": ("owner",), "error": ()}[state]:
                        self.assertIn("<%s></%s>" % (name, name), text)
                    if state == "error":  # gh stderr is echoed as a bounded prefix
                        self.assertIn("<stderr>", text)


if __name__ == "__main__":
    unittest.main()
