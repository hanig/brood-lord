"""Fail-closed Lor subagent transport; offline, through a fake `lor` CLI."""

import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/hanig-review-gate/scripts"
sys.path.insert(0, str(SCRIPTS))
import committee as C  # noqa: E402
import review as R  # noqa: E402
L = R._lor
FAKE = '''#!%s
import json, os, subprocess, sys, time
d = os.environ["FAKE_LOR"]; mode = open(d + "/mode").read(); verb = sys.argv[2]
stop_fail, mode = mode.endswith("stop-fail"), mode.split("+")[0]
open(d + "/calls", "a").write(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd(), "ls": os.listdir("."),
    "env": sorted(os.environ), "stdin": sys.stdin.read() if verb == "start" else ""}) + "\\n")
BLANK = ("timeout", "drop", "pass", "error", "cancel", "empty")
ev = lambda e: print(json.dumps({"recorded": {"event": e}}), flush=True)
if verb == "list":
    if mode == "list-hang": time.sleep(60)
    if mode == "list-fail": sys.exit(1)
    if mode == "list-malformed": print("{ not JSON")
    else: print(json.dumps({"models": [{"canonical_model_id": "unrelated"}]
         + ([] if mode == "list-absent" else [{"canonical_model_id": "fern-0.1"}])}))
if verb == "start" and mode == "start-fail": sys.exit("refused " + "x" * 290 + os.environ["LOR_SESSION_TOKEN"])
if verb == "stop" and stop_fail: sys.exit("stop refused")
if verb == "start" and mode != "start-fail":
    print('{\\n  "session_id": "0d6f1c2e-1111-4222-8333-944455556666"\\n}', flush=True)
    time.sleep(60 if mode == "start-hang" else 0)
if verb == "watch":
    kid = subprocess.Popen(["sleep", "60"], stdout=subprocess.DEVNULL)
    open(d + "/pids", "w").write("%%d %%d" %% (os.getpid(), kid.pid))
    if mode == "timeout": time.sleep(60)
    ev({"tool_call": {"name": "pass"}} if mode == "pass" else {"model_response_id": "r1",
       "message": {"role": "MESSAGE_ROLE_ASSISTANT", "content": [{"text": "" if mode in BLANK else "OK"}]},
       "provider_metadata_json": json.dumps({"providerModelId": "served-x"})})
    ev({"metrics": {"usage": {"input_tokens": "7", "output_tokens": "5", "reasoning_tokens": "3"}}})
    if mode == "drop": sys.exit(0)
    if mode == "error": ev({"error": {"message": "router down"}})
    if mode == "no-newline":
        sys.stdout.write(json.dumps({"event": {"finish": {"reason": "FINISH_REASON_STOP"}}})); sys.exit(0)
    ev({"finish": {"reason": "FINISH_REASON_CANCELLED" if mode == "cancel" else "FINISH_REASON_STOP"}})
    time.sleep(60)
if verb == "start" and mode == "sid-fail": sys.exit(1)
'''


def seats():
    return {r["name"]: r for r in R.load_reviewers()}


class TestLor(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.d = Path(tmp.name).resolve()
        (self.d / "lor").write_text(FAKE % sys.executable)
        (self.d / "lor").chmod(0o755)
        self.mode("ok")
        env = {k: v for k, v in os.environ.items() if not k.startswith(("LOR_", "HANIG_REVIEW_NO_LOR"))
               and not k.endswith("_API_KEY")}
        env.update(FAKE_LOR=str(self.d), PATH=f"{self.d}{os.pathsep}{os.environ['PATH']}",
                   LOR_SESSION_ID="parent", LOR_SESSION_TOKEN="lor-sess-secret", GH_TOKEN="gh-x",
                   LOR_ROUTER_TOKEN="lor-router-secret", LOR_SESSION_REFRESH_PROOF="lor-proof-secret",
                   OPENAI_API_KEY="sk-paid", HANIG_ROUTING_SNAPSHOTS="off")
        for patcher in (mock.patch.dict(os.environ, env, clear=True),
                        mock.patch.object(R, "_post", side_effect=AssertionError("paid API"))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def mode(self, mode):
        (self.d / "mode").write_text(mode)
        (self.d / "calls").unlink(missing_ok=True)

    def calls(self):
        path = self.d / "calls"
        return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []

    def assert_stopped_and_reaped(self):
        self.assertEqual([c["argv"][1] for c in self.calls()], ["start", "watch", "stop"])
        self.assertNotIn("OPENAI_API_KEY", self.calls()[0]["env"])
        for pid in map(int, (self.d / "pids").read_text().split()):
            for _ in range(50):
                try:
                    os.kill(pid, 0)
                    time.sleep(0.05)
                except ProcessLookupError:
                    break
            else:
                self.fail(f"watch group member {pid} survived")

    def test_env_session_availability_and_parse(self):
        env = L.build_lor_child_env({"OPENAI_API_KEY": "a", "GH_TOKEN": "b", "X_SESSION_TOKEN": "c",
                                     "AWS_REGION": "d", "LOR_SESSION_TOKEN": "e", "HOME": "/h", "PATH": "::r"})
        self.assertEqual(env, {"LOR_SESSION_TOKEN": "e", "HOME": "/h", "PATH": os.path.abspath("r")})
        for sid, opt, want in (("s", "", True), ("s", "1", False), ("s", "ON", False), ("", "", False)):
            self.assertIs(L.lor_in_session({"LOR_SESSION_ID": sid, L.OPT_OUT: opt}), want)
        self.assertIsNone(L.lor_availability())
        self.assertIn("LOR_SESSION_ID", L.lor_availability({"PATH": str(self.d)}))
        self.assertIn("not found", L.lor_availability({"LOR_SESSION_ID": "s", "PATH": "/nonexistent"}))
        msg = {"role": "MESSAGE_ROLE_ASSISTANT", "content": [{"text": "a"}, {"text": "b"}]}
        events = [{"event": {"message": msg, "provider_metadata_json": '{"providerModelId": "m"}'}},
                  {"recorded": {"event": {"metrics": {"usage": {"input_tokens": "2", "reasoning_tokens": "4"}}}}},
                  {"recorded": {"event": {"metrics": {"usage": {"input_tokens": "3", "output_tokens": "x"}}}}},
                  {"recorded": {"event": {"finish": {"reason": "FINISH_REASON_STOP"}}}},
                  {"recorded": {"event": {"error": "after finish"}}}]
        got = L.parse_lor_events(events, "sid")
        self.assertEqual([got[k] for k in ("text", "served_model", "in_tokens", "out_tokens",
                                           "reasoning_tokens", "status", "response_id", "error")],
                         ["ab", "m", 5, None, 4, "completed", "sid", None])
        tail = L.parse_lor_events(events[:1] + [{"event": {"message": {"role": msg["role"]}}}])
        self.assertEqual((tail["text"], tail["served_model"], tail["status"]), ("", None, "incomplete"))

    def test_multi_turn_zero_tool_subagent_is_always_stopped(self):
        msgs = [{"role": "user", "content": "q1"}, {"role": "assistant", "content": "a1"},
                {"role": "user", "content": "q2"}]
        astra = seats()["astra"]
        result, err = R.lor_messages(astra, msgs, "SYS", 20)
        self.assertEqual((err, result["text"], result["served_model"], result["in_tokens"], result["status"]),
                         (None, "OK", "served-x", 7, "completed"))
        start = self.calls()[0]
        argv = start["argv"]
        for flag, value in (("--canonical-model-id", astra["lor_model"]), ("--tools", "pass"),
                            ("--effort", astra["effort"])):
            self.assertEqual(argv[argv.index(flag) + 1], value)
        workdir = Path(argv[argv.index("--working-directory") + 1]).resolve()
        self.assertEqual(workdir, Path(start["cwd"]))
        self.assertEqual((argv.count("--tools"), start["ls"], workdir.exists()), (1, [], False))
        self.assertTrue(start["stdin"].startswith("SYS\n\n" + L.NO_TOOLS))
        self.assertRegex(start["stdin"], r'<turn_[0-9a-f]{8} role="assistant">\na1\n</turn_')
        self.assertIn("LOR_SESSION_TOKEN", start["env"])
        self.assertNotIn("GH_TOKEN", start["env"])
        self.assert_stopped_and_reaped()
        self.mode("no-newline")
        self.assertEqual(R.lor_messages(astra, msgs, "SYS", 20)[0]["text"], "OK")
        self.assert_stopped_and_reaped()
        for mode, want in (("timeout", "timed out"), ("drop", "watch ended"), ("pass", "no content"),
                           ("error", "router down"), ("cancel", "CANCELLED"), ("empty", "no content")):
            with self.subTest(mode=mode):
                self.mode(mode)
                result = R.run_one(astra, "review", 1.5)
                self.assertIn(want, result["error"])
                self.assert_stopped_and_reaped()

    def test_start_failure_and_secret_scrubbing(self):
        self.mode("start-fail")
        self.assertEqual(R.call_lor(seats()["gemini-neon"], "p", 10),
                         (None, "lor session start failed: refused " + "x" * 290 + "<LOR_SESSION_TOKEN redacted>"))
        self.assertEqual(len(self.calls()), 1)
        for mode, verbs, want in (("sid-fail", ["start", "stop"], "start failed"),
                                  ("start-hang", ["start", "stop"], "start timed out"),
                                  ("timeout", ["start", "watch", "stop"], "timed out"),
                                  ("error", ["start", "watch", "stop"], "router down"),
                                  ("cancel", ["start", "watch", "stop"], "CANCELLED"),
                                  ("drop", ["start", "watch", "stop"], "watch ended")):
            for stop in ("", "+stop-fail"):  # not cleanly finished: a failed stop is reported
                with self.subTest(mode=mode + stop):
                    self.mode(mode + stop)
                    result, err = R.call_lor(seats()["astra"], "p", 2)
                    self.assertEqual((result, [c["argv"][1] for c in self.calls()]), (None, verbs))
                    self.assertIn(want, err)
                    self.assertEqual("lor session stop failed" in err and "stop refused" in err, bool(stop))
        self.mode("stop-fail")  # clean FINISH_REASON_STOP: a failed stop keeps the verdict
        result, err = R.call_lor(seats()["astra"], "p", 20)
        self.assertEqual((err, result and result["text"], [c["argv"][1] for c in self.calls()]),
                         (None, "OK", ["start", "watch", "stop"]))
        for var in L.LOR_SECRET_ENV:
            self.assertEqual(R.redact(f"x {os.environ[var]}"), f"x <{var} redacted>")
        with mock.patch.object(R.subprocess, "Popen", side_effect=OSError) as popen, \
                self.assertRaises(OSError):
            R._run_journal_append([], "{}\n")
        self.assertFalse(set(R.SECRET_ENV) & set(popen.call_args[1]["env"]))

    def test_watch_survives_descriptors_beyond_fd_setsize(self):
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        if hard != resource.RLIM_INFINITY and hard < 1200:
            self.skipTest("host cannot hold 1100 descriptors")
        resource.setrlimit(resource.RLIMIT_NOFILE, (max(soft, 1200) if hard == resource.RLIM_INFINITY
                                                    else min(max(soft, 1200), hard), hard))
        self.addCleanup(resource.setrlimit, resource.RLIMIT_NOFILE, (soft, hard))
        held = [os.open(os.devnull, os.O_RDONLY) for _ in range(1100)]
        self.addCleanup(lambda: [os.close(fd) for fd in held])
        self.assertEqual(R.call_lor(seats()["astra"], "p", 20)[0]["text"], "OK")

    def test_routing_inside_outside_and_resolved(self):
        lor_seats = {n for n, r in seats().items() if r.get("lor_model")}
        self.assertEqual(len(lor_seats), 8)
        for name, rev in seats().items():
            want = ("lor", rev["lor_model"]) if name in lor_seats else (rev["provider"], rev["model"])
            self.assertEqual((R.effective_provider(rev), R.effective_model(rev)), want)
            resolved = dict(rev, _resolved_from=rev["model"])
            self.assertEqual(R.effective_provider(resolved), rev["provider"])
            if name in lor_seats and rev.get("enabled", True):
                self.assertIsNone(R.availability(rev))
        # 2026-11-26: the paid-API-only seat is disabled, not merely unrouted.
        self.assertEqual(R.availability(seats()["kimi-k2.7-code"]), "disabled in reviewers.json")
        out = io.StringIO()
        with redirect_stdout(out), mock.patch.dict(os.environ, {"HANIG_REVIEW_SLURM": "off"}), \
                mock.patch.object(R._subs, "claude_availability", return_value=None), \
                mock.patch.object(R._subs, "codex_availability", return_value=None):
            R.cmd_list(R.load_reviewers(), probe=False)
        header, *body = out.getvalue().splitlines()
        self.assertEqual(header.split()[:4], ["REVIEWER", "PROVIDER", "ROUTE", "MODEL"])
        rows = {line.split()[0]: line.split()[1:3] for line in body if line.strip()
                and not line.split()[0].isdigit() and not line.startswith("No reviewer")}
        self.assertEqual({n for n, (p, _r) in rows.items() if p == "lor"}, lor_seats | {"fern-0.1"})
        self.assertEqual({n for n, (_p, r) in rows.items() if r == "lor"}, lor_seats | {"fern-0.1"})
        self.assertEqual([c["argv"] for c in self.calls()], [["model", "list"]])
        self.mode("ok")
        self.assertEqual((rows["terra"], rows["sonnet-5.5"]),
                         (["codex", "cli_local"], ["claude", "cli_local"]))
        explicit = {"name": "x", "provider": "lor", "model": "m"}
        # Fail closed: outside a Lor session a lor_model seat stays `lor` and is
        # blocked with an actionable reason; a paid key is never used for it.
        with mock.patch.dict(os.environ, {"LOR_SESSION_ID": ""}, OPENAI_API_KEY="sk-paid"):
            for rev in (seats()["astra"], explicit):
                self.assertEqual((R.effective_provider(rev), R.seat_route(rev)), ("lor", "lor"))
                why = R.availability(rev)
                self.assertIn(f"lor routing required for {rev['name']}: LOR_SESSION_ID unset", why)
                self.assertIn(f"{L.OPT_OUT}=1", why)
                self.assertIn("lor routing required", R.call_lor(rev, "p", 5)[1])
            result = R.run_one(seats()["astra"], "review", 5)
            self.assertEqual((result["ok"], result["provider"], result["route"]), (False, "lor", "lor"))
        self.assertEqual(self.calls(), [])
        with mock.patch.dict(os.environ, {L.OPT_OUT: "1"}, OPENAI_API_KEY=""):
            self.assertEqual((R.effective_provider(seats()["astra"]), R.seat_route(seats()["astra"])),
                             ("openai", "api"))
            self.assertEqual(R.availability(seats()["astra"]), "OPENAI_API_KEY not set")
            self.assertIn("lor provider disabled", R.availability(explicit))
            self.assertIn("lor provider disabled", R.call_lor(explicit, "p", 5)[1])

    def invoke_review(self, *flags, plan=False, code=0):
        path = self.d / "review.py"
        path.write_text("x = 1\n")
        argv = [str(SCRIPTS / "review.py"), "--json", "--file", str(path)]
        if plan:
            argv += ["--kind", "plan"]
        else:
            argv += ["--kind", "implementation", "--round", "1", "--claim", R.HONEST_RUN_CLAIM]
        out, err = io.StringIO(), io.StringIO()
        calls = []

        def reply(rev, *_args, **_kwargs):
            calls.append(rev)
            return {"text": json.dumps(dict(verdict="upheld", findings=[],
                claims=[dict(claim_index=0, status="supported", claim=R.HONEST_RUN_CLAIM,
                             why="No execution paths were removed.")],
                notes="Checked."))}, None

        def plan_reply(rev, *_args, **_kwargs):
            calls.append(rev)
            return json.dumps(dict(verdict="upheld", findings=[], claims=[],
                                   notes="Checked.", own_findings=[], peer_responses=[])), None, {}

        env = {"HANIG_REVIEW_SLURM": "off", "XDG_STATE_HOME": str(self.d / "state")}
        with mock.patch.object(sys, "argv", argv + list(flags)), redirect_stdout(out), \
                redirect_stderr(err), mock.patch.dict(os.environ, env), \
                mock.patch.object(R._subs, "claude_availability", return_value=None), \
                mock.patch.object(R._subs, "codex_availability", return_value=None), \
                mock.patch.dict(R.PROVIDERS, {p: reply for p in R.PROVIDERS}), \
                mock.patch.object(C, "ask_member", side_effect=plan_reply), \
                mock.patch.object(R, "reserve_review_round"), \
                mock.patch.object(R, "prior_evidence", return_value=None), \
                mock.patch.object(R, "release_round"), self.assertRaises(SystemExit) as exit_:
            R.main()
        self.assertEqual(exit_.exception.code, code, out.getvalue() + err.getvalue())
        report = json.loads(out.getvalue())
        self.assertTrue(report["journal"]["written"], err.getvalue())
        journal = json.loads(Path(report["journal"]["path"]).read_text())
        self.assertEqual(journal["seat_routes"], report["seat_routes"])
        return report, calls

    def assert_route(self, report, seat, *, provider="lor", route="lor", status="completed"):
        model = "moonshotai/kimi-k3" if seat == "kimi-k3" else seat
        self.assertIn(seat, report["seat_routes"])
        self.assertEqual(report["seat_routes"][seat],
                         dict(provider=provider, model=model, route=route, status=status))
        self.assertNotIn("fern-0.1" if seat == "kimi-k3" else "kimi-k3", report["seat_routes"])

    def test_profiles_and_escalation_select_advertised_replacement(self):
        for flags, plan in (((), False), (("--profile", "standard"), False),
                            (("--profile", "deep"), False), ((), True),
                            (("--profile", "fast", "--escalate"), False)):
            with self.subTest(flags=flags, plan=plan):
                self.mode("ok")
                report, calls = self.invoke_review(*flags, plan=plan)
                self.assert_route(report, "fern-0.1")
                fern = [r for r in calls if r["name"] == "fern-0.1"]
                self.assertEqual(len(fern), 2 if plan else 1)
                self.assertEqual(fern[0]["effort"], "xhigh")
                self.assertEqual(fern[0]["_lor_replaced"], "kimi-k3")
                self.assertEqual(fern[0]["profiles"], ["plan", "standard", "deep"])
                self.assertEqual([c["argv"] for c in self.calls()], [["model", "list"]])
                if "--escalate" in flags:
                    self.assertEqual(report["tiers_run"], ["fast", "standard", "deep"])

    def test_missing_or_broken_model_list_keeps_kimi_on_lor(self):
        discover = L.lor_available_models
        for mode in ("list-absent", "list-fail", "list-malformed", "list-hang"):
            with self.subTest(mode=mode), mock.patch.object(L, "lor_available_models",
                    side_effect=lambda env: discover(env, timeout=0.3)):
                self.mode(mode)
                report, _ = self.invoke_review("--profile", "standard")
                self.assert_route(report, "kimi-k3")
                [call] = self.calls()
                self.assertEqual(call["argv"], ["model", "list"])
                self.assertNotEqual(Path(call["cwd"]), Path.cwd())
                self.assertNotIn("OPENAI_API_KEY", call["env"])
                self.assertIn("LOR_SESSION_TOKEN", call["env"])

    def test_outside_lor_fails_closed_but_explicit_opt_out_uses_api(self):
        for env, provider, route, status in (
                ({"LOR_SESSION_ID": ""}, "lor", "lor", "unavailable"),
                ({L.OPT_OUT: "1"}, "openrouter", "api", "completed")):
            with self.subTest(env=env), mock.patch.dict(os.environ, env | {"OPENROUTER_API_KEY": "fake"}):
                report, calls = self.invoke_review("--profile", "standard")
                self.assert_route(report, "kimi-k3", provider=provider, route=route, status=status)
                self.assertEqual(any(r["name"] == "kimi-k3" for r in calls), status == "completed")
                self.assertEqual(self.calls(), [])
                if status == "unavailable":
                    [entry] = report["unavailable"]
                    self.assertIn("LOR_SESSION_ID unset", entry["reason"])

    def test_explicit_seats_skip_discovery_and_author_keeps_kimi(self):
        for seat in ("kimi-k3", "fern-0.1"):
            with self.subTest(seat=seat):
                report, _ = self.invoke_review("--only", seat, "--quorum", "1",
                                               "--allow-single-reviewer", "offline test")
                self.assert_route(report, seat)
                self.assertEqual(self.calls(), [])
        report, _ = self.invoke_review("--author", "lor/fern-0.1")
        self.assert_route(report, "kimi-k3")
        self.assertEqual([c["argv"] for c in self.calls()], [["model", "list"]])

    def test_profiles_without_target_do_not_query_model_list(self):
        report, _ = self.invoke_review("--profile", "fast")
        self.assertEqual(set(report["seat_routes"]), {"luna", "haiku-5.5"})
        self.assertEqual(self.calls(), [])
        roster = seats()
        selected = [roster[name] for name in ("kimi-k3", "fern-0.1")]
        self.assertEqual(L.resolve_lor_substitutions(
            selected, roster.values(), available_models=frozenset({"fern-0.1"})), selected)

    def test_list_shows_both_and_only_checks_the_replacement_in_catalog(self):
        self.mode("list-absent")
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["review.py", "--list", "--profile", "standard"]), \
                mock.patch.object(R, "probe_liveness", return_value=None) as probe, \
                redirect_stdout(out), self.assertRaises(SystemExit) as exit_:
            R.main()
        self.assertEqual(exit_.exception.code, 0)
        self.assertRegex(out.getvalue(), r"fern-0\.1.*UNAVAILABLE \(not in lor model list\)")
        self.assertRegex(out.getvalue(), r"kimi-k3.*ready")
        self.assertNotIn("fern-0.1", [call.args[0]["name"] for call in probe.call_args_list])
        self.assertEqual([c["argv"] for c in self.calls()], [["model", "list"]])

    def test_plan_and_committee_reserves_substitute_when_eligible(self):
        roster = seats()
        kimi = roster["kimi-k3"]
        kimi["profiles"].remove("plan")
        for seat in roster.values():
            for profile in ("plan", "committee"):
                seat.get("_reserve_order", {}).pop(profile, None)
        kimi["_reserve_order"].update(plan=0, committee=0)
        roster["sonnet-5.5"]["profiles"].append("plan")
        with mock.patch.object(R, "load_reviewers", return_value=list(roster.values())):
            report, calls = self.invoke_review("--author", "claude/claude-sonnet-5-5", plan=True)
            self.assert_route(report, "fern-0.1")
            [fern, second_fern] = [r for r in calls if r["name"] == "fern-0.1"]
            self.assertEqual(fern, second_fern)
            self.assertEqual(fern["_reserve_order"], kimi["_reserve_order"])
            self.assertIsNot(fern["_reserve_order"], kimi["_reserve_order"])
            self.assertEqual(roster["fern-0.1"]["profiles"], [])
            for extra, expected in (([], "fern-0.1"), (["lor/fern-0.1"], "kimi-k3")):
                authors = ["openrouter/google/gemini-4-neon", *extra]
                chosen = C.pick_members([], authors)
                self.assertEqual({r["name"] for r in chosen}, {"sonnet-5.5", "deepseek-v4-pro", expected})

    def test_tiebreak_reserve_substitutes_unless_fern_already_voted(self):
        primary, error = C.tiebreaker()
        self.assertIsNone(error)
        args = SimpleNamespace(name="split", timeout=1)
        for voters, expected in (([primary], "fern-0.1"),
                                  ([primary, seats()["fern-0.1"]], "kimi-k3")):
            with self.subTest(expected=expected), mock.patch.object(C, "save_session"), \
                    mock.patch.object(C, "decision_reply", return_value=(None, "offline")) as ask, \
                    mock.patch.object(C, "finish_decision"):
                session = {"members": {"first": {"voters": voters}}}
                C.tiebreak(args, session, {"author": []})
                self.assertEqual(ask.call_args.args[0]["name"], expected)
                self.assertEqual(session["tiebreak_selection"]["substituted"], [expected])

    def test_committee_routes_lor_and_contrasts_effective_providers(self):
        history = [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}]
        self.assertEqual(C.ask_member(seats()["gemini-neon"], history, "p2", 20),
                         ("OK", None, {"in_tokens": 7, "out_tokens": 5}))
        self.assertIn("<conversation_history_", self.calls()[0]["stdin"])
        self.mode("timeout")
        self.assertIn("timed out", C.ask_member(seats()["gemini-neon"], [], "p", 1)[1])
        for profile in ("committee", "planning", "plan"):
            for env in ({}, {"LOR_SESSION_ID": ""}, {L.OPT_OUT: "1"}):
                with self.subTest(profile=profile, env=env), mock.patch.dict(os.environ, env):
                    panels = [[r for r in R.load_reviewers() if R.in_profile(r, profile)]]
                    if profile != "plan":
                        panels += [C.pick_members([], authors=[a], profile=profile) for a in
                                   ("claude/claude-sonnet-5-5", "codex/gpt-6.1-terra", "openai/none")]
                    for panel in panels:
                        self.assertGreaterEqual(len(C.transports(panel)), 2)
                        self.assertGreaterEqual(len(R._panels.panel_makers(panel)), 2)
                    # Only an explicit opt-out leaves Lor: a missing session blocks.
                    self.assertEqual("lor" in C.transports(panels[0]), L.OPT_OUT not in env)
        with mock.patch("sys.stderr", io.StringIO()), self.assertRaisesRegex(SystemExit, "provider 'lor'"):
            C.pick_members(["gemini-neon,muse-wonder"])
        data = json.loads(R.CONFIG.read_text())
        for extra, want in (({"lor_model": " "}, "'lor_model'"),
                            ({"lor_replaces": " "}, "'lor_replaces'"),
                            ({"lor_replaces": None}, "'lor_replaces'"),
                            ({"provider": "lor", "effort": "huge"}, "huge")):
            revs = [dict(data["reviewers"][0], **extra)] + data["reviewers"][1:]
            (self.d / "r.json").write_text(json.dumps(dict(data, reviewers=revs)))
            with mock.patch.object(R, "CONFIG", self.d / "r.json"), \
                    mock.patch("sys.stderr", io.StringIO()) as err, self.assertRaises(SystemExit):
                R.load_reviewers()
            self.assertIn(want, err.getvalue())


if __name__ == "__main__":
    unittest.main()
