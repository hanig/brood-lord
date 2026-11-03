import ast
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills/hanig-debrief/scripts/debrief_file.py"
sys.path.insert(0, str(SCRIPT.parent))
import debrief_file as O
import debrief_filing as F
import debrief_packet as P
import debrief_schema as S


# The executable consumes scripted replies and records the actual process boundary.
# PATH is replaced, and its interpreter is absolute: no real gh can be reached.
FAKE_GH = r'''
import json, sys
from pathlib import Path
root = Path(__file__).parent
with (root / "calls").open("a") as stream:
    stream.write(json.dumps({"argv": sys.argv[1:], "stdin": sys.stdin.read()}) + "\n")
steps = json.loads((root / "steps").read_text())
if not steps:
    sys.exit(91)
step = steps.pop(0)
(root / "steps").write_text(json.dumps(steps))
if (step.get("stale_inactive") and sys.argv[1:3] == ["auth", "status"]
        and "--active" not in sys.argv):
    sys.exit(1)
if step.get("legacy_flags") and "--active" in sys.argv:
    sys.exit(1)
sys.stdout.write(step.get("stdout", ""))
sys.stderr.write(step.get("stderr", ""))
sys.exit(step.get("code", 0))
'''


def reply(value):
    return {"stdout": json.dumps(value)}


class TestDebriefFile(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.work = self.root / "work"
        self.work.mkdir()
        gh = self.bin / "gh"
        gh.write_text("#!" + sys.executable + "\n" + FAKE_GH)
        gh.chmod(0o700)
        self.env = patch.dict(os.environ, {"PATH": str(self.bin), "HOME": str(self.root)})
        self.env.start()
        self.addCleanup(self.env.stop)
        entry = S.load_catalog()["entries"][0]
        row = {key: entry[key] for key in ("skill", "what_broke", "mechanism", "proposed_change")}
        row.update(id="L01", catalog_entry=entry["id"], mechanism_basis="reported",
                   counts=[], evidence_refs=["E001"])
        self.packet = {"schema_version": S.PACKET_SCHEMA, "catalog_version": S.CATALOG_VERSION,
                       "packet_id": "12345678-1234-4234-8234-123456789abc",
                       "project": S.PROJECT, "learnings": [row]}
        self.title, self.body = P.render(self.packet)
        self.digest = P.approval_digest(self.packet, self.title)
        (self.work / O.PACKET).write_text(json.dumps(self.packet))

    def steps(self, *responses):
        (self.bin / "steps").write_text(json.dumps(list(responses)))

    def preflight(self):
        return [{}, reply({"name": "debrief"})]

    def issue(self, number=41, **changes):
        value = dict(number=number, title=self.title, body=self.body,
                     labels=[{"name": "debrief"}], state="open")
        value.update(changes)
        return value

    def calls(self):
        path = self.bin / "calls"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def state(self):
        return json.loads((self.work / F.RECORD).read_text())

    def status(self, value, number=None):
        record = self.state()
        record.update(status=value, number=number)
        (self.work / F.RECORD).write_text(json.dumps(record))

    def cli(self, command, *flags):
        output = io.StringIO()
        with redirect_stdout(output):
            code = O.main([command, "--workdir", str(self.work)] + list(flags))
        return code, output.getvalue()

    def approve(self):
        code, output = self.cli("approve", "--digest", self.digest, "--approver", "fixture owner")
        self.assertEqual(code, O.EXIT_PENDING)
        self.assertEqual(json.loads(output), {"status": "APPROVED", "number": None})

    def test_operator_command_capture(self):
        self.approve()
        self.steps(*self.preflight(), reply({"number": 41}), reply(self.issue()))
        real_run = subprocess.run
        with patch.object(O.subprocess, "run", wraps=real_run) as run:
            self.assertEqual(self.cli("file")[0], O.EXIT_FILED)
        self.assertEqual(len(run.call_args_list), 4)
        for call in run.call_args_list:
            self.assertIs(type(call.args[0]), list)
            self.assertIs(call.kwargs["shell"], False)
            self.assertEqual(call.kwargs["stdout"], subprocess.PIPE)
            self.assertEqual(call.kwargs["stderr"], subprocess.PIPE)
        calls = self.calls()
        self.assertEqual(O.REPOSITORY, "hanig/brood-lord")
        self.assertEqual(O.LABEL, "debrief")
        for index, call in enumerate(calls):
            self.assertNotIn(self.body, " ".join(call["argv"]))
            self.assertNotIn("fixture owner", json.dumps(call))
            self.assertEqual(call["argv"][call["argv"].index("--hostname") + 1], "github.com")
            if index == 0:
                self.assertEqual(call["argv"],
                                 ["api", "--hostname", "github.com", "--method", "GET", "user"])
            else:
                self.assertEqual(call["argv"][0], "api")
                endpoints = [v for v in call["argv"] if v.startswith("repos/")]
                self.assertEqual(len(endpoints), 1)
                self.assertTrue(endpoints[0].startswith("repos/hanig/brood-lord/"))
        post = calls[2]
        self.assertEqual(post["argv"][-2:], ["--input", "-"])
        self.assertEqual(json.loads(post["stdin"]),
                         {"title": self.title, "body": self.body, "labels": ["debrief"]})
        self.assertEqual(self.state()["status"], "FILED")
        self.assertEqual(self.cli("status"), (0, '{"status": "FILED", "number": 41}\n'))

    def test_find_closed_page_two_exact_title_and_no_pull_requests(self):
        self.approve()
        self.status("DELIVERY_UNCERTAIN")
        page1 = [self.issue(n, title=self.title + " suffix") for n in range(1, 101)]
        page1[0] = self.issue(1, pull_request={"url": "fixture"})
        self.steps(*self.preflight(), reply(page1),
                   reply([self.issue(state="closed")]), reply(self.issue(state="closed")))
        self.assertEqual(self.cli("file")[0], O.EXIT_FILED)
        pages = [call for call in self.calls() if any("?" in v for v in call["argv"])]
        self.assertEqual(len(pages), 2)
        for index, call in enumerate(pages, 1):
            endpoint = next(v for v in call["argv"] if "?" in v)
            self.assertIn("state=all", endpoint)
            self.assertIn("labels=debrief", endpoint)
            self.assertIn("page=" + str(index), endpoint)
            self.assertNotIn("search", endpoint)
        self.assertFalse(any("POST" in call["argv"] for call in self.calls()))

    def test_missing_label_refuses_without_create_and_keeps_approval(self):
        self.approve()
        self.steps({}, {"code": 1, "stderr": "fixture missing label"})
        self.assertEqual(self.cli("file")[0], O.EXIT_REFUSED)
        self.assertEqual(self.state()["status"], "APPROVED")
        self.assertFalse(any("POST" in call["argv"] for call in self.calls()))
        self.assertEqual(json.loads((self.work / O.RECEIPT).read_text()),
                         {"exit_code": 1, "message_class": "GH_LABEL_UNAVAILABLE"})

    def test_stale_inactive_account_does_not_block_active_credentials(self):
        self.approve()
        preflight = self.preflight()
        preflight[0]["stale_inactive"] = True
        self.steps(*preflight, reply({"number": 41}), reply(self.issue()))
        self.assertEqual(self.cli("file")[0], O.EXIT_FILED)

    def test_legacy_gh_needs_no_active_flag(self):
        self.approve()
        preflight = self.preflight()
        preflight[0]["legacy_flags"] = True
        self.steps(*preflight, reply({"number": 41}), reply(self.issue()))
        self.assertEqual(self.cli("file")[0], O.EXIT_FILED)

    def test_terminal_states_never_preflight_or_rewrite_receipt(self):
        self.approve()
        self.steps(*self.preflight(), reply({"number": 41}), reply(self.issue()))
        self.assertEqual(self.cli("file")[0], 0)
        receipt = (self.work / O.RECEIPT).read_bytes()
        calls = self.calls()
        for status, code in (("FILED", 0), ("CONFLICT", 5)):
            for failure in ("503", "rate limit", "unauthenticated", "missing"):
                with self.subTest(status=status, failure=failure):
                    self.status(status, 41)
                    self.steps(*([] if failure == "unauthenticated" else [{}]),
                               {"code": 1, "stderr": failure})
                    if failure == "missing":
                        (self.bin / "gh").rename(self.bin / "absent")
                    try:
                        result, output = self.cli("file")
                        self.assertEqual(result, code)
                        self.assertEqual(json.loads(output), {"status": status, "number": 41})
                        self.assertEqual(self.calls(), calls)
                        self.assertEqual((self.work / O.RECEIPT).read_bytes(), receipt)
                    finally:
                        if failure == "missing":
                            (self.bin / "absent").rename(self.bin / "gh")

    def test_launch_and_timeout_receipts_have_numeric_wrapper_codes(self):
        self.approve()
        sentinel = "synthetic private child output"
        errors = [(FileNotFoundError(sentinel), 127, "GH_MISSING"),
                  (PermissionError(sentinel), 126, "GH_EXEC_FAILED"),
                  (subprocess.TimeoutExpired(["gh"], 60, output=sentinel, stderr=sentinel),
                   124, "GH_TIMEOUT")]
        for error, code, name in errors:
            with self.subTest(name=name), patch.object(O.subprocess, "run", side_effect=error):
                self.assertNotEqual(self.cli("file")[0], 0)
                self.assertEqual(json.loads((self.work / O.RECEIPT).read_text()),
                                 {"exit_code": code, "message_class": name})

    def test_receipt_write_failure_keeps_single_json_stdout(self):
        self.approve()
        self.steps(*self.preflight(), reply({"number": 41}), reply(self.issue()))
        error_output = io.StringIO()
        with patch.object(O, "_receipt", side_effect=PermissionError("private path")), \
                patch("sys.stderr", error_output):
            code, output = self.cli("file")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output), {"status": "FILED", "number": 41})
        self.assertEqual(json.loads(error_output.getvalue()),
                         {"message_class": "GH_RECEIPT_WRITE_FAILED"})

    def test_gh_error_output_never_reaches_receipts_or_stdout(self):
        sentinel = "ghp_" + "syntheticSecret0123456789" * 2
        self.approve()
        failure = {"code": 19, "stdout": sentinel, "stderr": sentinel}
        for phase in ("auth", "label", "create", "get", "find"):
            with self.subTest(phase=phase):
                self.status("DELIVERY_UNCERTAIN" if phase == "find" else "APPROVED")
                responses = self.preflight()
                if phase == "auth":
                    responses = []
                elif phase == "label":
                    responses = responses[:1]
                elif phase == "get":
                    responses += [reply({"number": 41})]
                self.steps(*responses, failure)
                code, output = self.cli("file")
                self.assertNotEqual(code, 0)
                self.assertNotIn(sentinel, output)
                for path in self.work.iterdir():
                    self.assertNotIn(sentinel, path.read_text())
                receipt = json.loads((self.work / O.RECEIPT).read_text())
                self.assertEqual(set(receipt), {"exit_code", "message_class"})
                self.assertEqual(receipt["exit_code"], 19)
                self.assertIn(receipt["message_class"],
                              ("GH_API_FAILED", "GH_UNAUTHENTICATED", "GH_LABEL_UNAVAILABLE"))

    def test_dossier_never_opened_and_file_uses_frozen_packet(self):
        dossier = self.work / "dossier.json"
        dossier.write_text("private synthetic dossier")
        dossier.chmod(0)
        self.addCleanup(dossier.chmod, 0o600)
        path_open = Path.open
        def guarded_open(path, *args, **kwargs):
            self.assertNotEqual(path, dossier, "dossier was opened")
            return path_open(path, *args, **kwargs)
        with patch.object(Path, "open", guarded_open), patch.object(
                P, "load_dossier", side_effect=AssertionError("dossier was read")):
            self.approve()
            (self.work / O.PACKET).unlink()
            self.steps(*self.preflight(), reply({"number": 41}), reply(self.issue()))
            self.assertEqual(self.cli("file")[0], 0)
            self.assertEqual(self.cli("status")[0], 0)

    def test_each_exit_code_through_cli(self):
        self.assertEqual(self.cli("file")[0], 2)
        self.assertEqual(self.calls(), [])
        self.approve()
        self.assertEqual(self.cli("status")[0], 1)
        (self.bin / "gh").rename(self.bin / "absent")
        self.assertEqual(self.cli("file")[0], 6)
        self.assertEqual(self.state()["status"], "APPROVED")
        (self.bin / "absent").rename(self.bin / "gh")
        self.steps({"code": 4})
        self.assertEqual(self.cli("file")[0], 7)
        self.assertEqual(self.state()["status"], "APPROVED")
        self.steps(*self.preflight(), {"code": 1})
        self.assertEqual(self.cli("file")[0], 3)
        self.assertEqual(self.cli("status")[0], 3)
        self.steps(*self.preflight(), reply([self.issue(41), self.issue(42)]))
        self.assertEqual(self.cli("file")[0], 5)
        self.assertEqual(self.cli("status")[0], 5)
        self.status("APPROVED")
        self.steps(*self.preflight(), reply({"number": 41}), {"code": 1})
        self.assertEqual(self.cli("file")[0], 4)
        self.assertEqual(self.cli("status")[0], 4)
        self.steps(*self.preflight(), reply(self.issue()))
        self.assertEqual(self.cli("file")[0], 0)

    def test_readback_uses_raw_body_and_validates_label_and_identity(self):
        self.approve()
        for change in ({"body": "edited"}, {"labels": [{"name": "other"}]},
                       {"title": self.title + " "}, {"number": True},
                       {"number": 42}, {"pull_request": {}}, {"labels": ["debrief"]}):
            with self.subTest(change=change):
                self.status("APPROVED")
                self.steps(*self.preflight(), reply({"number": 41}), reply(self.issue(**change)))
                self.assertEqual(self.cli("file")[0], O.EXIT_READBACK_FAILED)
        self.status("APPROVED")
        self.steps(*self.preflight(), reply({"number": 41}),
                   reply(self.issue(body_html="different rendering", body_text="different text")))
        self.assertEqual(self.cli("file")[0], 0)

    def test_malformed_responses_fail_closed_without_raw_diagnostics(self):
        self.approve()
        for response in ("not-json", '{"number": 41, "number": 42}', 'null',
                         '{"number": true}', '{"number": "41"}'):
            with self.subTest(response=response):
                self.status("APPROVED")
                self.steps(*self.preflight(), {"stdout": response})
                self.assertEqual(self.cli("file")[0], O.EXIT_DELIVERY_UNCERTAIN)
                self.assertEqual(json.loads((self.work / O.RECEIPT).read_text()),
                                 {"exit_code": 0, "message_class": "GH_INVALID_RESPONSE"})

    def test_approval_refusal_is_offline_and_cli_has_no_destination_arguments(self):
        self.assertEqual(self.cli("approve", "--digest", self.digest.upper(),
                                  "--approver", "fixture owner")[0], 2)
        self.assertEqual(self.calls(), [])
        self.approve()
        for flag in ("--repository", "--label", "--title", "--body"):
            with self.subTest(flag=flag), patch("sys.stderr", io.StringIO()):
                with self.assertRaises(SystemExit) as stopped:
                    self.cli("file", flag, "arbitrary")
                self.assertEqual(stopped.exception.code, 2)
        self.assertEqual(self.calls(), [])

    def test_executable_entrypoint(self):
        self.approve()
        self.steps(*self.preflight(), reply({"number": 41}), reply(self.issue()))
        result = subprocess.run([sys.executable, str(SCRIPT), "file", "--workdir", str(self.work)],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"status": "FILED", "number": 41})


class TestDraftingBoundary(unittest.TestCase):
    def test_drafting_modules_never_import_operator(self):
        paths = list(SCRIPT.parent.glob("debrief_*.py"))
        self.assertTrue({"debrief_packet.py", "debrief_filing.py"} <= {p.name for p in paths})
        # Also scans debrief_extract and debrief_coverage when those units land.
        for path in paths:
            if path == SCRIPT:
                continue
            for node in ast.walk(ast.parse(path.read_text())):
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""] + [alias.name for alias in node.names]
                self.assertFalse(any("debrief_file" in name.split(".") for name in names),
                                 str(path))


if __name__ == "__main__":
    unittest.main()
