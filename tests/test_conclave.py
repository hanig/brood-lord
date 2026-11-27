"""Offline tests for brood-conclave: ranking, effort clamping, packets, parsing.

    python3 -m unittest tests.test_conclave
"""

import contextlib
import functools
import io
import json
import os
import shlex
import threading
import time
import sys
import tempfile
import unittest
from pathlib import Path

from tests.watchdog_guard import guarded_main

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "brood-conclave"
sys.path.insert(0, str(SKILL / "scripts"))

import allow as A  # noqa: E402
import conclave as C  # noqa: E402

DAY = 86400
T0 = 1790000000  # an arbitrary epoch second; only the order matters


def model(ident, out_per_m, days, efforts=("low", "medium", "high"),
          modalities=("text",)):
    return {"id": ident, "name": ident, "created": T0 + days * DAY,
            "pricing": {"completion": str(out_per_m / 1e6)},
            "architecture": {"output_modalities": list(modalities)},
            "reasoning": {"supported_efforts": list(efforts)}}


GOOGLE = {"vendor": "google",
          "lines": ["gemini-argon", "gemini-pro"],
          "lower_lines": ["gemini-flash"], "variants": []}
ANTHROPIC = {"vendor": "anthropic",
             "lines": ["claude-fable", "claude-opus"],
             "lower_lines": ["claude-sonnet", "claude-haiku"], "variants": []}
OPENAI = {"vendor": "openai", "lines": ["gpt-astra"],
          "lower_lines": ["gpt-sol"], "variants": ["pro"]}


def pick(catalog, family, ratio=0.5):
    rows = C.candidates(catalog, family, "2026-01-01")
    return C.select(rows, family, ratio)


class ParseName(unittest.TestCase):
    def test_version_in_middle_and_end(self):
        self.assertEqual(C.parse_name("gpt-6.1-sol")[:2], ("gpt-sol", (6, 1)))
        self.assertEqual(C.parse_name("claude-fable-5.1")[:2],
                         ("claude-fable", (5, 1)))
        self.assertEqual(C.parse_name("claude-opus-4-5-20251101"),
                         ("claude-opus", (4, 5), {"snapshot"}))

    def test_variants_and_preview(self):
        line, version, flags = C.parse_name("gpt-6-astra-pro", ["pro"])
        self.assertEqual((line, flags), ("gpt-astra", {"pro"}))
        line, version, flags = C.parse_name("gemini-3.1-pro-preview")
        self.assertEqual((line, version, flags),
                         ("gemini-pro", (3, 1), {"preview"}))

    def test_unversioned_line(self):
        self.assertEqual(C.parse_name("muse-wonder")[:2], ("muse-wonder", ()))


class Select(unittest.TestCase):
    def test_newest_version_of_top_line_beats_pricier_old_models(self):
        catalog = [model("anthropic/claude-opus-4.1", 75, 0),
                   model("anthropic/claude-opus-5.5", 20, 300),
                   model("anthropic/claude-fable-5", 50, 200),
                   model("anthropic/claude-fable-5.1", 50, 280)]
        chosen, status, _, _ = pick(catalog, ANTHROPIC)
        self.assertEqual((chosen["id"], status),
                         ("anthropic/claude-fable-5.1", "OK"))

    def test_new_version_inside_a_line_is_taken_without_edits(self):
        family = dict(ANTHROPIC, lines=["claude-opus"])
        catalog = [model("anthropic/claude-opus-5.5", 20, 300),
                   model("anthropic/claude-opus-6", 20, 400)]
        self.assertEqual(pick(catalog, family)[0]["id"],
                         "anthropic/claude-opus-6")

    def test_unknown_flagship_line_is_adopted(self):
        catalog = [model("google/gemini-4-argon", 10, 300),
                   model("google/gemini-5-carbon", 12, 400)]
        chosen, status, why, _ = pick(catalog, GOOGLE)
        self.assertEqual((chosen["id"], status),
                         ("google/gemini-5-carbon", "NEW_LINE"))
        self.assertIn("gemini-carbon", why)

    def test_cheap_unknown_line_is_reported_not_adopted(self):
        catalog = [model("google/gemini-4-argon", 10, 300),
                   model("google/gemini-5-mercury", 2, 400)]
        chosen, status, _, unranked = pick(catalog, GOOGLE)
        self.assertEqual((chosen["id"], status), ("google/gemini-4-argon", "OK"))
        self.assertEqual([r["id"] for r in unranked],
                         ["google/gemini-5-mercury"])

    def test_lower_line_never_adopted(self):
        catalog = [model("google/gemini-4-argon", 10, 300),
                   model("google/gemini-4-flash", 9, 400)]
        chosen, _, _, unranked = pick(catalog, GOOGLE)
        self.assertEqual(chosen["id"], "google/gemini-4-argon")
        self.assertEqual(unranked, [])

    def test_variants_images_and_aliases_excluded(self):
        catalog = [model("openai/gpt-6-astra", 50, 300),
                   model("openai/gpt-6-astra-pro", 50, 300),
                   model("openai/gpt-6-astra:batch", 25, 300),
                   model("google/gemini-3-pro-image", 12, 350,
                         modalities=("text", "image"))]
        self.assertEqual(pick(catalog, OPENAI)[0]["id"], "openai/gpt-6-astra")
        rows = C.candidates(catalog, GOOGLE, "2026-01-01")
        self.assertEqual(rows, [])

    def test_release_beats_preview_of_same_version(self):
        catalog = [model("google/gemini-5-argon-preview", 10, 300),
                   model("google/gemini-5-argon", 10, 290)]
        self.assertEqual(pick(catalog, GOOGLE)[0]["id"],
                         "google/gemini-5-argon")


class NativeUpgrade(unittest.TestCase):
    def test_agy_lists_newer_version_first(self):
        catalog = [model("google/gemini-4-argon", 10, 300)]
        chosen = pick(catalog, GOOGLE)[0]
        native = {"codex": {}, "agy": ["gemini-4-argon-high",
                                       "gemini-5-argon-high",
                                       "gemini-4-flash-high"]}
        best, notes = C.native_upgrade(chosen, GOOGLE, native)
        self.assertEqual(best["id"], "google/gemini-5-argon")
        self.assertTrue(best["native_only"])
        route, reason = C.plan_route("pi", best, native, "xhigh")
        self.assertIsNone(route)

    def test_unknown_native_line_is_only_noted(self):
        catalog = [model("google/gemini-4-argon", 10, 300)]
        chosen = pick(catalog, GOOGLE)[0]
        native = {"codex": {}, "agy": ["gemini-5-carbon-high"]}
        best, notes = C.native_upgrade(chosen, GOOGLE, native)
        self.assertIs(best, chosen)
        self.assertIn("gemini-5-carbon", notes[0])


class Direct(unittest.TestCase):
    def test_direct_route_maps_dots_and_needs_a_key(self):
        chosen = {"id": "anthropic/claude-fable-5.1", "efforts": ["high",
                                                                 "xhigh"]}
        native = {"opencode": {"anthropic/claude-fable-5-1"}}
        saved = C.api_keys
        try:
            C.api_keys = lambda: {"ANTHROPIC_API_KEY": "k"}
            route, _ = C.plan_direct(chosen, native, "max")
            self.assertEqual((route["model"], route["effort"]),
                             ("anthropic/claude-fable-5-1", "xhigh"))
            C.api_keys = lambda: {}
            route, reason = C.plan_direct(chosen, native, "xhigh")
            self.assertIsNone(route)
            self.assertIn("ANTHROPIC_API_KEY", reason)
        finally:
            C.api_keys = saved
        route, reason = C.plan_direct({"id": "meta/muse-wonder",
                                       "efforts": []}, native, "xhigh")
        self.assertIsNone(route)


class Effort(unittest.TestCase):
    def test_clamp(self):
        self.assertEqual(C.clamp("xhigh", ["low", "medium", "high"]), "high")
        self.assertEqual(C.clamp("xhigh", ["high", "xhigh", "max"]), "xhigh")
        self.assertEqual(C.clamp("low", ["high"]), "high")
        self.assertEqual(C.clamp("xhigh", []), "xhigh")

    def test_agy_tier_follows_effort(self):
        route = {"harness": "agy", "model": "g-high", "effort": "high",
                 "tiers": {"low": "g-low", "high": "g-high"},
                 "supported": ["low", "high"]}
        self.assertEqual(C.with_effort(route, "medium")["model"], "g-low")
        self.assertEqual(C.with_effort(route, "max")["model"], "g-high")


class Argv(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.packet = self.tmp / "packet.md"
        self.saved_pi_has = C._pi_has
        C._pi_has = lambda flag: self.pi_flags_known

    pi_flags_known = True

    def tearDown(self):
        C._pi_has = self.saved_pi_has

    def test_old_pi_gets_no_unknown_flag(self):
        self.pi_flags_known = False
        argv, _, _ = self.argv({"harness": "pi", "model": "openrouter/x/y",
                                "effort": "xhigh"})
        self.assertNotIn("--no-mcp", argv)
        self.pi_flags_known = True
        argv, _, _ = self.argv({"harness": "pi", "model": "openrouter/x/y",
                                "effort": "xhigh"})
        self.assertIn("--no-mcp", argv)

    def argv(self, route, images=()):
        return C.build_argv(route, "do the task", self.packet, list(images),
                            self.tmp, self.tmp / "answer.md")

    def test_every_harness_turns_search_on(self):
        argv, env, stdin = self.argv({"harness": "codex", "model": "m",
                                      "effort": "xhigh"})
        self.assertEqual(argv[:3], ["codex", "--search", "exec"])
        self.assertIn('model_reasoning_effort="xhigh"', argv)
        self.assertEqual(stdin, "do the task")
        argv, env, _ = self.argv({"harness": "pi",
                                  "model": "openrouter/x/y:online",
                                  "effort": "xhigh"})
        self.assertTrue(argv[argv.index("--model") + 1].endswith(":online"))
        argv, env, _ = self.argv({"harness": "opencode",
                                  "model": "openrouter/x/y", "effort": "xhigh"})
        self.assertEqual(env["OPENCODE_ENABLE_EXA"], "1")
        self.assertEqual(json.loads(env["OPENCODE_CONFIG_CONTENT"]),
                         {"permission": {"edit": "deny", "bash": "deny"}})
        argv, env, _ = self.argv({"harness": "claude", "model": "m",
                                  "effort": "xhigh"})
        self.assertIn("WebSearch", argv[argv.index("--allowedTools") + 1])
        argv, env, _ = self.argv({"harness": "agy", "model": "m",
                                  "effort": "high"})
        self.assertTrue(argv[-1].startswith("--print=do the task"))


class Posts(unittest.TestCase):
    POST = ("Answer text, see https://example.org/a.\n\n```json\n"
            '{"bottom_line": "b", "claims": [{"id": 1, "claim": "c", '
            '"confidence": 70}]}\n```\n')

    def test_extract_and_strip(self):
        self.assertEqual(C.extract_json(self.POST)["bottom_line"], "b")
        self.assertNotIn("```", C.strip_json(self.POST))
        self.assertIsNone(C.extract_json("no json here"))

    def test_scrub_keeps_substance(self):
        text = ("As GPT-6 Astra, I think so. I am Claude. Firms such as Google "
                "and Anthropic compete.")
        out = C.scrub(text)
        self.assertNotIn("Astra", out)
        self.assertNotIn("I am Claude", out)
        self.assertIn("such as Google and Anthropic", out)

    def test_stance(self):
        self.assertEqual(C.stance_of({"stance": "Agree"}), "agree")
        self.assertEqual(C.stance_of({"stance": "against"}), "disagree")
        self.assertEqual(C.stance_of({"stance": "maybe"}), "uncertain")


class Packets(unittest.TestCase):
    SEATS = ["openai", "google", "anthropic", "meta"]

    def test_labels_hide_self_and_shuffle_per_reader(self):
        labels = C.assign_labels("b1", 1, self.SEATS)
        for reader, mine in labels.items():
            self.assertNotIn(reader, mine)
            self.assertEqual(sorted(mine.values()),
                             ["Panelist A", "Panelist B", "Panelist C"])
        self.assertEqual(labels, C.assign_labels("b1", 1, self.SEATS))
        orders = {tuple(sorted(m, key=m.get)) for m in labels.values()}
        self.assertGreater(len(orders), 1)

    def test_red_team_rotates(self):
        self.assertEqual([C.red_team_seat(k, self.SEATS) for k in (1, 2, 5)],
                         ["openai", "google", "openai"])

    def test_packet_never_shows_real_seat_names(self):
        labels = C.assign_labels("b1", 1, self.SEATS)
        posts = {s: {"text": "Post by %s. As Gemini 4 Argon, yes.\n\n```json\n"
                             '{"claims": []}\n```' % s.upper(), "data": {}}
                 for s in self.SEATS}
        cruxes = [{"id": "C1", "statement": "X holds.", "for": ["openai"],
                   "against": ["google", "meta"], "uncertain": []}]
        packet = C.crossexam_packet("openai", 1, "BRIEF", cruxes, posts,
                                    labels, "openai")
        self.assertIn("Agree: you.", packet)
        self.assertIn("Devil's advocate", packet)
        others = packet.split("anonymized)")[1]
        self.assertNotIn("Gemini 4 Argon", others)
        self.assertIn("As a panelist, yes.", others)
        for seat in ("GOOGLE", "ANTHROPIC", "META"):
            self.assertIn("Post by %s" % seat, others)  # body kept
        self.assertNotIn("Post by OPENAI", others)       # own post not repeated
        self.assertNotIn("$", packet)                    # every slot filled

FAKE_LOR = r"""#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
log = Path(os.environ["FAKE_LOR_LOG"])
with log.open("a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\n")
noun = sys.argv[1]
verb = sys.argv[2] if len(sys.argv) > 2 else ""
args = sys.argv[3:]
def flag(name):
    return args[args.index(name) + 1]
if (noun, verb) == ("model", "list"):
    print(json.dumps({"models": [
        {"canonical_model_id": os.environ["FAKE_LOR_GOOD"]}]}))
elif (noun, verb) == ("session", "start"):
    model = flag("--canonical-model-id")
    if model != os.environ["FAKE_LOR_GOOD"]:
        print("invalid_argument: The provider states no context window for "
              + model, file=sys.stderr)
        sys.exit(1)
    task = Path(flag("--task")[1:]).read_text()
    Path(os.environ["FAKE_LOR_TASK"]).write_text(task)
    print(json.dumps({"session_id": "s-1"}))
elif (noun, verb) == ("session", "inspect"):
    def ev(e):
        return json.dumps({"event": e})
    reply = os.environ.get("FAKE_LOR_REPLY", "TAG v1.2.3 URL https://x")
    print(ev({"message": {"role": "MESSAGE_ROLE_USER",
                          "content": [{"text": "task"}]}}))
    print(ev({"model_response_id": "r1", "message": {
        "role": "MESSAGE_ROLE_ASSISTANT",
        "content": [{"text": "searching first"}]}}))
    print(ev({"model_response_id": "r1", "metrics": {
        "cost": {"amount": "0.25"}}}))
    print(ev({"tool_result": {"content": [{"text": "results"}]}}))
    print(ev({"model_response_id": "r2", "message": {
        "role": "MESSAGE_ROLE_ASSISTANT",
        "content": [{"text": reply[:len(reply) // 2]},
                    {"text": reply[len(reply) // 2:]}]}}))
    print(ev({"model_response_id": "r2", "metrics": {
        "cost": {"amount": "0.5"}}}))
    print(ev({"finish": {"reason": "FINISH_REASON_STOP"}}))
elif (noun, verb) == ("session", "stop"):
    pass
"""


class LorRoute(unittest.TestCase):
    def test_candidates_map_dots_and_explicit_ids(self):
        config = {"lor_ids": {"meta/muse-wonder": "meta/muse-wonder-1.0"}}
        self.assertEqual(
            C.lor_candidates("anthropic/claude-opus-5.5", config, {}),
            ["anthropic/claude-opus-5.5", "anthropic/claude-opus-5-5"])
        self.assertEqual(
            C.lor_candidates("anthropic/claude-opus-5.5", config, {},
                             known_models=["anthropic/claude-opus-5-5"]),
            ["anthropic/claude-opus-5-5", "anthropic/claude-opus-5.5"])
        self.assertEqual(
            C.lor_candidates("meta/muse-wonder", config, {}),
            ["meta/muse-wonder-1.0", "meta/muse-wonder"])
        self.assertEqual(   # the id that worked last time goes first
            C.lor_candidates("openai/gpt-6-astra", {},
                             {"openai/gpt-6-astra": "openai/gpt-6-astra-x"}),
            ["openai/gpt-6-astra-x", "openai/gpt-6-astra"])

    def test_resolve_lor_mode_precedence(self):
        self.assertTrue(C.resolve_lor_mode(None, None, None, {}))
        self.assertTrue(C.resolve_lor_mode(None, {}, {}, {}))
        self.assertFalse(C.resolve_lor_mode(None, {}, {"lor": False}, {}))
        self.assertTrue(C.resolve_lor_mode(None, {}, {"lor": True}, {}))
        self.assertTrue(C.resolve_lor_mode(None, {}, {"lor": False},
                                           {"CONCLAVE_LOR": "1"}))
        self.assertFalse(C.resolve_lor_mode(None, {}, {"lor": True},
                                            {"CONCLAVE_LOR": "0"}))
        self.assertTrue(C.resolve_lor_mode(None, {"lor": True}, {"lor": False},
                                           {"CONCLAVE_LOR": "0"}))
        self.assertFalse(C.resolve_lor_mode(None, {"lor": False},
                                            {"lor": True},
                                            {"CONCLAVE_LOR": "1"}))
        self.assertTrue(C.resolve_lor_mode(True, {"lor": False},
                                           {"lor": False}, {}))
        self.assertFalse(C.resolve_lor_mode(False, {"lor": True},
                                            {"lor": True},
                                            {"CONCLAVE_LOR": "1"}))

    def test_select_routes_lor_only_fails_closed_and_synthesizes(self):
        seat = {
            "model": "anthropic/claude-opus-5.5",
            "routes": [
                {"harness": "claude", "model": "claude-opus-5-5",
                 "supported": ["high", "xhigh"], "effort": "xhigh"},
                {"harness": "pi",
                 "model": "openrouter/anthropic/claude-opus-5.5:online",
                 "supported": ["high", "xhigh"], "effort": "xhigh"},
            ],
        }
        saved = os.environ.get("LOR_SESSION_ID")
        real_which = C.shutil.which
        C.shutil.which = lambda name: "/bin/" + name
        os.environ["LOR_SESSION_ID"] = "parent"
        try:
            routes = C.select_routes(
                seat, "xhigh", lor_only=True,
                native={"lor": ["anthropic/claude-opus-5-5"]})
            self.assertEqual([r["harness"] for r in routes], ["lor"])
            self.assertEqual(routes[0]["model"], "anthropic/claude-opus-5-5")
            # Explicit per-seat override still works even under lor_only.
            override = C.select_routes(
                seat, "xhigh", prefer="pi", lor_only=True,
                native={"lor": ["anthropic/claude-opus-5-5"]})
            self.assertEqual(override[0]["harness"], "pi")
        finally:
            C.shutil.which = real_which
            if saved is None:
                os.environ.pop("LOR_SESSION_ID", None)
            else:
                os.environ["LOR_SESSION_ID"] = saved

    def test_route_only_inside_a_lor_session(self):
        pick_row = {"id": "google/gemini-4-argon", "efforts": ["low", "high"],
                    "native_only": True}
        saved = os.environ.pop("LOR_SESSION_ID", None)
        real_which = C.shutil.which
        C.shutil.which = lambda name: "/bin/" + name
        try:
            route, why = C.plan_route("lor", pick_row, {}, "xhigh")
            self.assertIsNone(route)
            self.assertIn("LOR_SESSION_ID", why)
            os.environ["LOR_SESSION_ID"] = "parent"
            route, why = C.plan_route("lor", pick_row, {}, "xhigh")
            self.assertEqual(route["harness"], "lor")
            self.assertEqual(route["effort"], "high")       # clamped
            self.assertEqual(route["catalog_id"], "google/gemini-4-argon")
        finally:
            C.shutil.which = real_which
            os.environ.pop("LOR_SESSION_ID", None)
            if saved is not None:
                os.environ["LOR_SESSION_ID"] = saved

    def test_final_text_is_the_last_response_only(self):
        def ev(e):
            return {"event": e}
        events = [
            ev({"message": {"role": "MESSAGE_ROLE_USER", "content": []}}),
            ev({"model_response_id": "a", "message": {
                "role": "MESSAGE_ROLE_ASSISTANT",
                "content": [{"text": "thinking out loud"}]}}),
            ev({"model_response_id": "b", "message": {
                "role": "MESSAGE_ROLE_ASSISTANT",
                "content": [{"text": "the answer"}]}})]
        self.assertEqual(C.lor_final_text(events)[0], False)  # not finished
        events.append(ev({"finish": {"reason": "FINISH_REASON_STOP"}}))
        self.assertEqual(C.lor_final_text(events),
                         (True, "FINISH_REASON_STOP", "the answer"))

    def test_a_yield_is_not_the_end_and_a_later_wake_is_ignored(self):
        def ev(e):
            return {"event": e}

        def say(rid, text):
            return ev({"model_response_id": rid, "message": {
                "role": "MESSAGE_ROLE_ASSISTANT", "content": [{"text": text}]}})
        events = [
            ev({"message": {"role": "MESSAGE_ROLE_USER", "content": []}}),
            say("a", "checking the web"),
            ev({"finish": {"reason": "FINISH_REASON_YIELDED"}}),
            ev({"message": {"role": "MESSAGE_ROLE_USER",
                            "content": [{"text": "[Background tool ...]"}]}}),
            say("b", "the real answer"),
            ev({"finish": {"reason": "FINISH_REASON_STOP"}}),
            ev({"message": {"role": "MESSAGE_ROLE_USER",
                            "content": [{"text": "[Background tool ...]"}]}}),
            say("c", "an invented cross-examination"),
            ev({"finish": {"reason": "FINISH_REASON_STOP"}})]
        self.assertEqual(C.lor_final_text(events[:3]), (False, "", ""))
        self.assertEqual(C.lor_final_text(events),
                         (True, "FINISH_REASON_STOP", "the real answer"))

    def test_task_carries_search_tools_isolation_and_attachments(self):
        task = C.lor_task(
            "PACKET", ["/tmp/fig1.png"], board_dir="/tmp/board",
            attachments=["/tmp/fig1.png", "/tmp/table.csv"])
        self.assertTrue(task.startswith("PACKET"))
        self.assertIn("lor web search", task)
        self.assertIn("lor web fetch", task)
        self.assertIn("shell and file tools", task)
        self.assertIn("/tmp/board", task)
        self.assertIn("/tmp/fig1.png", task)
        self.assertIn("/tmp/table.csv", task)
        plain = C.lor_task("PACKET", [])
        self.assertNotIn("Figures", plain)
        self.assertNotIn("Other attached files", plain)

    def test_start_argv_inherits_tools_and_sets_workdir(self):
        argv = C.lor_start_argv("openai/gpt-6.2-astra", "xhigh", "conclave r0",
                                Path("/tmp/work/task.md"), Path("/tmp/work"))
        self.assertEqual(argv[:3], ["lor", "session", "start"])
        self.assertNotIn("--tools", argv)
        self.assertEqual(argv[argv.index("--working-directory") + 1],
                         "/tmp/work")
        self.assertEqual(argv[argv.index("--effort") + 1], "xhigh")

    def test_run_route_end_to_end_with_a_fake_lor(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "bin").mkdir()
            fake = tmp / "bin" / "lor"
            fake.write_text(FAKE_LOR)
            fake.chmod(0o755)
            env = {"PATH": "%s:%s" % (tmp / "bin", os.environ["PATH"]),
                   "FAKE_LOR_LOG": str(tmp / "calls.jsonl"),
                   "FAKE_LOR_GOOD": "anthropic/claude-opus-5-5",
                   "FAKE_LOR_TASK": str(tmp / "task.md"),
                   "XDG_STATE_HOME": str(tmp / "state"),
                   "LOR_SESSION_ID": "parent"}
            saved = {k: os.environ.get(k) for k in env}
            os.environ.update(env)
            poll = C.LOR_POLL_SECONDS
            C.LOR_POLL_SECONDS = 0
            try:
                route = {"harness": "lor",
                         "model": "anthropic/claude-opus-5.5",
                         "candidates": ["anthropic/claude-opus-5.5",
                                        "anthropic/claude-opus-5-5"],
                         "catalog_id": "anthropic/claude-opus-5.5",
                         "supported": ["high"], "effort": "high"}
                ok, text, detail, _ = C.run_route(
                    route, "PACKET", [], tmp / "board" / "r0" / "work" /
                    "anthropic", 60, tmp / "log.txt", min_chars=1)
                self.assertTrue(ok, detail)
                self.assertEqual(text, "TAG v1.2.3 URL https://x")
                calls = [json.loads(l) for l in
                         (tmp / "calls.jsonl").read_text().splitlines()]
                self.assertEqual([c[:2] for c in calls], [
                    ["session", "start"], ["session", "start"],
                    ["session", "inspect"], ["session", "stop"]])
                self.assertNotIn("--tools", calls[0])
                self.assertIn("--working-directory", calls[0])
                self.assertNotIn("--if-idle", calls[-1])
                self.assertIn("lor web search", (tmp / "task.md").read_text())
                self.assertEqual(json.loads(
                    (tmp / "state" / "hanig-conclave" / "lor_ids.json")
                    .read_text()), {"anthropic/claude-opus-5.5":
                                    "anthropic/claude-opus-5-5"})
                meta = json.loads((tmp / "board" / "r0" / "work" /
                                   "anthropic" / "session.json").read_text())
                self.assertEqual(meta["cost_usd"], 0.75)
            finally:
                C.LOR_POLL_SECONDS = poll
                for k, v in saved.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v

    def test_board_lor_flag_persists_across_round_and_ratify(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "bin").mkdir()
            fake = tmp / "bin" / "lor"
            fake.write_text(FAKE_LOR)
            fake.chmod(0o755)
            state = tmp / "state" / "hanig-conclave"
            state.mkdir(parents=True)
            # Cached roster with only non-lor harness to prove --lor synthesizes
            # lor and never falls back to claude/pi.
            (state / "roster.json").write_text(json.dumps({
                "schema": 1, "resolved_at": C.utc_now(), "effort": "high",
                "seats": {
                    "anthropic": {
                        "model": "anthropic/claude-opus-5.5",
                        "name": "Claude Opus 5.5", "status": "OK",
                        "routes": [{"harness": "claude",
                                    "model": "claude-opus-5-5",
                                    "supported": ["high"], "effort": "high"}],
                    }
                },
            }))
            brief = tmp / "brief.md"
            brief.write_text("Pick A or B.")
            board = tmp / "board"
            long_reply = (
                "Detailed analysis with https://example.org/evidence " +
                ("word " * 50) +
                "\n\n```json\n"
                '{"bottom_line": "Pick A.", '
                '"claims": [{"id": 1, "claim": "A wins", "confidence": 80}], '
                '"vote": "approve", "reservations": [], "edits": []}\n```\n')
            env = {"PATH": "%s:%s" % (tmp / "bin", os.environ["PATH"]),
                   "FAKE_LOR_LOG": str(tmp / "calls.jsonl"),
                   "FAKE_LOR_GOOD": "anthropic/claude-opus-5-5",
                   "FAKE_LOR_TASK": str(tmp / "task.md"),
                   "FAKE_LOR_REPLY": long_reply,
                   "XDG_STATE_HOME": str(tmp / "state"),
                   "LOR_SESSION_ID": "parent"}
            saved = {k: os.environ.get(k) for k in env}
            # The default under test must not depend on the caller's shell.
            saved["CONCLAVE_LOR"] = os.environ.pop("CONCLAVE_LOR", None)
            os.environ.update(env)
            poll = C.LOR_POLL_SECONDS
            C.LOR_POLL_SECONDS = 0
            try:
                # Default (no --lor flag) uses Lor subagents.
                guarded_main(functools.partial(C.main, ["new", str(board),
                                                        "--brief", str(brief)]))
                meta = json.loads((board / "board.json").read_text())
                self.assertTrue(meta["lor"])
                # Explicit --no-lor persists opt-out on a board.
                no_lor_board = tmp / "board-no-lor"
                guarded_main(functools.partial(C.main, ["new", str(no_lor_board),
                                                        "--brief", str(brief),
                                                        "--no-lor"]))
                self.assertFalse(json.loads(
                    (no_lor_board / "board.json").read_text())["lor"])
                guarded_main(functools.partial(C.main, ["round", str(board), "0",
                                                        "--dry-run"]))
                guarded_main(functools.partial(C.main, ["round", str(board), "0"]))
                r0_meta = json.loads((board / "r0" / "anthropic.meta.json")
                                     .read_text())
                self.assertEqual(r0_meta["harness"], "lor")
                self.assertEqual(r0_meta["lor_session_id"], "s-1")
                self.assertEqual(r0_meta["cost_usd"], 0.75)
                (board / "statement.md").write_text("We agree on A.")
                guarded_main(functools.partial(C.main, ["ratify", str(board)]))
                v_meta = json.loads(
                    (board / "votes" / "p1" / "anthropic.meta.json")
                    .read_text())
                self.assertEqual(v_meta["harness"], "lor")
                tally = (board / "votes" / "p1" / "tally.md").read_text()
                self.assertIn("| approve |", tally)
            finally:
                C.LOR_POLL_SECONDS = poll
                for k, v in saved.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v

    def test_lor_flag_dies_outside_lor_session(self):
        saved = os.environ.pop("LOR_SESSION_ID", None)
        try:
            with self.assertRaises(SystemExit) as ctx:
                C.ensure_lor_available()
            self.assertEqual(ctx.exception.code, 2)
        finally:
            if saved is not None:
                os.environ["LOR_SESSION_ID"] = saved



class StateDir(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name) / "state"
        saved = os.environ.get("XDG_STATE_HOME")
        os.environ["XDG_STATE_HOME"] = str(self.base)

        def restore():
            if saved is None:
                os.environ.pop("XDG_STATE_HOME", None)
            else:
                os.environ["XDG_STATE_HOME"] = saved
        self.addCleanup(restore)
        self.addCleanup(self.tmp.cleanup)

    def test_live_state_keeps_its_legacy_name(self):
        # Rename convention: skills are brood-*, live state stays hanig-*,
        # so a cached roster, doctor result and lor ids survive the rename.
        self.assertEqual(C.state_dir(), self.base / "hanig-conclave")
        self.assertTrue((self.base / "hanig-conclave").is_dir())
        self.assertFalse((self.base / "brood-conclave").exists())


class AllowRules(unittest.TestCase):
    def test_rules_name_the_installed_copy_not_an_agent_store(self):
        script = "/opt/skills/brood-conclave/scripts/conclave.py"
        rules = A.rules(script, home="/home/someone")
        self.assertEqual(rules[0], "Bash(python3 %s:*)" % script)
        self.assertFalse(any("hanig-conclave" in r for r in rules))
        beside = os.path.join(os.path.dirname(os.path.abspath(A.__file__)),
                              "conclave.py")
        self.assertEqual(A.rules()[0], "Bash(python3 %s:*)" % beside)

    def test_home_spelling_and_symlinked_skill(self):
        with tempfile.TemporaryDirectory() as raw:
            home = os.path.realpath(raw)
            real = Path(home) / "src" / "brood-conclave" / "scripts"
            real.mkdir(parents=True)
            (real / "conclave.py").write_text("")
            link = Path(home) / "skills" / "brood-conclave"
            link.parent.mkdir()
            link.symlink_to(real.parent, target_is_directory=True)
            script = str(link / "scripts" / "conclave.py")
            self.assertEqual(A.rules(script, home=home), [
                "Bash(python3 %s:*)" % script,
                "Bash(python3 %s:*)" % (real / "conclave.py"),
                "Bash(python3 ~/skills/brood-conclave/scripts/conclave.py:*)",
                "Bash(python3 ~/src/brood-conclave/scripts/conclave.py:*)"])

    def test_remove_also_takes_out_legacy_rules(self):
        with tempfile.TemporaryDirectory() as raw:
            settings = Path(raw) / "settings.json"
            legacy = A.legacy_rules()
            settings.write_text(json.dumps(
                {"permissions": {"allow": ["Bash(ls:*)"] + legacy}}))
            argv = sys.argv
            sys.argv = ["allow.py", "--settings", str(settings), "--remove"]
            try:
                with open(os.devnull, "w") as quiet:
                    out = sys.stdout
                    sys.stdout = quiet
                    try:
                        A.main()
                    finally:
                        sys.stdout = out
            finally:
                sys.argv = argv
            self.assertEqual(json.loads(settings.read_text()),
                             {"permissions": {"allow": ["Bash(ls:*)"]}})



class RememberLorId(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        saved = os.environ.get("XDG_STATE_HOME")
        os.environ["XDG_STATE_HOME"] = self.tmp.name

        def restore():
            if saved is None:
                os.environ.pop("XDG_STATE_HOME", None)
            else:
                os.environ["XDG_STATE_HOME"] = saved
        self.addCleanup(restore)

    def test_concurrent_seats_keep_every_mapping(self):
        # Widen the read-to-write window so an unserialized merge loses
        # updates and a shared temp name collides on every run.
        real_read = C.read_json

        def slow_read(*a, **k):
            data = real_read(*a, **k)
            time.sleep(0.002)
            return data
        C.read_json = slow_read
        self.addCleanup(setattr, C, "read_json", real_read)
        n_threads, per_thread = 8, 5
        barrier = threading.Barrier(n_threads)
        errors = []

        def worker(t):
            try:
                barrier.wait()
                for i in range(per_thread):
                    C.remember_lor_id("cat/%d-%d" % (t, i), "id-%d-%d" % (t, i))
            except Exception as exc:  # collected and asserted below
                errors.append(exc)
        threads = [threading.Thread(target=worker, args=(t,))
                   for t in range(n_threads)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        self.assertEqual(errors, [])
        path = C.state_dir() / "lor_ids.json"
        self.assertEqual(json.loads(path.read_text()), {
            "cat/%d-%d" % (t, i): "id-%d-%d" % (t, i)
            for t in range(n_threads) for i in range(per_thread)})
        self.assertEqual(sorted(p.name for p in path.parent.iterdir()),
                         ["lor_ids.json"])


def _board(tmp, seats):
    board = Path(tmp) / "board"
    board.mkdir()
    (board / "board.json").write_text(json.dumps(
        {"seats": seats, "max_rounds": 3, "effort": "high"}))
    (board / "roster.json").write_text(json.dumps(
        {"seats": {s: {"model": "m/%s" % s} for s in seats}}))
    return board


class SettledNeedsEverySeat(unittest.TestCase):
    SEATS = ["a", "b", "c", "d"]

    def test_unit(self):
        self.assertFalse(C.settled({"a": "agree"}, self.SEATS))
        self.assertFalse(C.settled({}, self.SEATS))
        self.assertTrue(C.settled({s: "agree" for s in self.SEATS},
                                  self.SEATS))
        self.assertTrue(C.settled({s: "disagree" for s in self.SEATS},
                                  self.SEATS))
        self.assertFalse(C.settled({s: "uncertain" for s in self.SEATS},
                                   self.SEATS))
        split = dict({s: "agree" for s in self.SEATS}, d="disagree")
        self.assertFalse(C.settled(split, self.SEATS))

    def test_one_agree_and_three_missing_stays_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            board = _board(tmp, self.SEATS)
            crux = {"id": "C1", "statement": "A beats B", "for": [],
                    "against": [], "uncertain": []}
            (board / "r1-cruxes.json").write_text(json.dumps(
                {"round": 1, "cruxes": [crux]}))
            (board / "r1").mkdir()
            # Seat a answered; b failed (no post); c and d posted but left
            # C1 out.
            (board / "r1" / "a.json").write_text(json.dumps({"positions": [
                {"crux": "C1", "stance": "agree", "confidence": 90}]}))
            for seat in ("c", "d"):
                (board / "r1" / (seat + ".json")).write_text(
                    json.dumps({"positions": []}))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                C.main(["status", str(board)])
            self.assertNotIn("SETTLED", out.getvalue())
            self.assertIn("3 missing", out.getvalue())
            with contextlib.redirect_stdout(io.StringIO()):
                C.main(["cruxes", str(board)])
            carried = json.loads((board / "r2-cruxes.json").read_text())
            self.assertEqual([c["id"] for c in carried["cruxes"]], ["C1"])
            self.assertEqual(carried["cruxes"][0]["for"], ["a"])


class TallyNeedsEveryVote(unittest.TestCase):
    def tally(self, board):
        out = io.StringIO()
        code = None
        with contextlib.redirect_stdout(out):
            try:
                C.main(["tally", str(board)])
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue()

    def vote(self, board, seat, vote=None, failed=False):
        d = board / "votes" / "p1"
        d.mkdir(parents=True, exist_ok=True)
        meta = {"seat": seat, "model": "m/%s" % seat}
        if failed:
            meta["failed"] = True
        (d / (seat + ".meta.json")).write_text(json.dumps(meta))
        if vote is not None:
            (d / (seat + ".json")).write_text(json.dumps({"vote": vote}))

    def test_all_approve_recommends_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            board = _board(tmp, ["a", "b"])
            self.vote(board, "a", "approve")
            self.vote(board, "b", "approve_with_reservations")
            code, out = self.tally(board)
            self.assertIsNone(code)
            self.assertIn("export the transcript", out)

    def test_failed_missing_and_unparsed_votes_block_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            board = _board(tmp, ["a", "b", "c", "d"])
            self.vote(board, "a", "approve")
            self.vote(board, "b", failed=True)
            self.vote(board, "c")  # answered, no JSON vote
            # d never wrote anything (its thread raised).
            code, out = self.tally(board)
            self.assertEqual(code, 3)
            self.assertNotIn("export the transcript", out)
            self.assertIn("not ratified", out)
            self.assertIn("b (failed)", out)
            self.assertIn("c (unparsed)", out)
            self.assertIn("d (missing)", out)
            self.assertIn("--seats b,c,d --pass 1", out)
            tally = (board / "votes" / "p1" / "tally.md").read_text()
            self.assertIn("| b | m/b | failed |", tally)

    def test_invalid_vote_string_is_not_a_vote(self):
        with tempfile.TemporaryDirectory() as tmp:
            board = _board(tmp, ["a", "b"])
            self.vote(board, "a", "approve")
            self.vote(board, "b", "maybe")
            code, out = self.tally(board)
            self.assertEqual(code, 3)
            self.assertIn("b (maybe)", out)


class AllowHardening(unittest.TestCase):
    def run_main(self, argv, script=None):
        saved_argv, saved_script = sys.argv, A.default_script
        sys.argv = ["allow.py"] + argv
        if script:
            A.default_script = lambda: script
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                A.main()
        finally:
            sys.argv, A.default_script = saved_argv, saved_script
        return out.getvalue()

    def test_non_object_settings_fail_cleanly(self):
        for body in ("null", "[]", '"text"', "3",
                     '{"permissions": []}',
                     '{"permissions": {"allow": "Bash(ls:*)"}}'):
            with tempfile.TemporaryDirectory() as raw:
                settings = Path(raw) / "settings.json"
                settings.write_text(body)
                with self.assertRaises(SystemExit) as ctx:
                    self.run_main(["--settings", str(settings)])
                self.assertIsInstance(ctx.exception.code, str, body)
                self.assertIn("fix it first", ctx.exception.code)
                self.assertEqual(settings.read_text(), body)
                self.assertEqual(os.listdir(raw), ["settings.json"])

    def test_paths_with_spaces_are_shell_quoted(self):
        with tempfile.TemporaryDirectory() as raw:
            script = os.path.join(os.path.realpath(raw), "my skills",
                                  "conclave.py")
            rules = A.rules(script, home="/nowhere")
            self.assertIn("Bash(python3 %s:*)" % shlex.quote(script), rules)
            self.assertIn("Bash(python3 %s:*)" % script, rules)
            settings = Path(raw) / "settings.json"
            out = self.run_main(["--settings", str(settings), "--dry-run"],
                                script=script)
            line = [ln for ln in out.splitlines() if "call conclave.py" in ln][0]
            command = line.split("`")[1]
            self.assertEqual(shlex.split(command), ["python3", script, "..."])

    def test_home_form_keeps_tilde_unquoted(self):
        with tempfile.TemporaryDirectory() as raw:
            home = os.path.realpath(raw)
            script = os.path.join(home, "my skills", "conclave.py")
            self.assertIn("Bash(python3 ~/'my skills/conclave.py':*)",
                          A.rules(script, home=home))


class Packaging(unittest.TestCase):
    def test_skill_is_auto_triggerable_and_portable(self):
        text = (SKILL / "SKILL.md").read_text()
        front = text.split("\n---\n", 1)[0]
        self.assertIn("\nname: brood-conclave\n", front)
        self.assertNotIn("disable-model-invocation", front)
        self.assertFalse((SKILL / "agents").exists())
        self.assertNotIn("~/.claude/skills", text)
        self.assertIn('"$BROOD_CONCLAVE_DIR/scripts/conclave.py"', text)

    def test_lor_is_the_default_and_templates_ship(self):
        config = json.loads((SKILL / "families.json").read_text())
        self.assertIs(config["lor"], True)
        self.assertTrue(C.resolve_lor_mode(None, {}, config, {}))
        for name in ("round0.md", "crossexam.md", "ratify.md", "redteam.md"):
            self.assertTrue((SKILL / "templates" / name).is_file(), name)


if __name__ == "__main__":
    unittest.main()
