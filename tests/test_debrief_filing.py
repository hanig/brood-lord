import copy
import io
import json
import multiprocessing
import os
from pathlib import Path
import stat
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills/hanig-debrief/scripts/debrief_filing.py"
sys.path.insert(0, str(SCRIPT.parent))
import debrief_filing as F
import debrief_packet as P
import debrief_schema as S


def packet():
    entry = S.load_catalog()["entries"][0]
    row = {k: entry[k] for k in ("skill", "what_broke", "mechanism", "proposed_change")}
    row.update(id="L01", catalog_entry=entry["id"], mechanism_basis="reported",
               counts=[], evidence_refs=["E001"])
    return {"schema_version": S.PACKET_SCHEMA, "catalog_version": S.CATALOG_VERSION,
            "packet_id": "12345678-1234-4234-8234-123456789abc", "project": S.PROJECT,
            "learnings": [row]}


class Crash(BaseException):
    pass


class FakeTransport:
    def __init__(self):
        self.calls = []
        self.issues = {}

    def create(self, title, body, label):
        self.calls.append(("create", title, body, label))
        number = len(self.issues) + 1
        self.issues[number] = dict(title=title, body=body, labels=[label], state="open")
        return number

    def get(self, number):
        self.calls.append(("get", number))
        return self.issues[number]

    def find(self, packet_id, state="all"):
        self.calls.append(("find", packet_id, state))
        return [n for n, issue in self.issues.items() if packet_id in issue["body"]
                and (state == "all" or issue["state"] == state)]


class DiskTransport:
    """Synthetic shared remote for separate processes, with append-only call log."""
    def __init__(self, root):
        self.root = Path(root)

    def create(self, title, body, label):
        with (self.root / "calls").open("a") as stream:
            stream.write("create\n")
        time.sleep(0.15)
        (self.root / "issue").write_text(json.dumps(dict(
            title=title, body=body, labels=[label], state="open")))
        return 1

    def get(self, number):
        return json.loads((self.root / "issue").read_text())

    def find(self, packet_id):
        return [1] if (self.root / "issue").exists() else []


def concurrent_file(workdir, remote, ready, start):
    ready.put(True)
    if not start.wait(10):
        raise RuntimeError("fixture start timed out")
    result = F.file(workdir, DiskTransport(remote))
    if result["status"] != "FILED":
        raise RuntimeError("fixture did not file")


class TestDebriefFiling(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.work = self.root / "filing"
        self.work.mkdir()
        self.packet = packet()
        self.digest = P.approval_digest(self.packet, P.render(self.packet)[0])
        self.transport = FakeTransport()

    def approve(self):
        return F.approve(self.work, self.packet, self.digest, "fixture approver")

    def state(self):
        return json.loads((self.work / F.RECORD).read_text())

    def put(self, record):
        (self.work / F.RECORD).write_text(json.dumps(record))

    def creates(self):
        return sum(call[0] == "create" for call in self.transport.calls)

    def test_approval_binding_matrix(self):
        for approver in ("", " \n\t", None, False):
            with self.subTest(approver=approver), self.assertRaises(S.SchemaError):
                F.approve(self.work, self.packet, self.digest, approver)
        with self.assertRaises(S.SchemaError):
            F.file(self.work, self.transport)
        with self.assertRaises(S.SchemaError):
            F.approve(self.work, self.packet, "0" * 64, "fixture approver")
        self.assertFalse((self.work / F.RECORD).exists())
        original = self.approve()
        changes = [("approver", " "), ("digest", "0" * 64), ("title", "changed"),
                   ("label", "changed"), ("repository", "fixture/other"),
                   ("schema_version", "other"), ("packet_schema_version", "other"),
                   ("catalog_version", 2), ("renderer_version", 2),
                   ("catalog_version", True), ("renderer_version", True),
                   ("packet_id", "changed")]
        for field, value in changes:
            record = copy.deepcopy(original)
            record["approval"][field] = value
            self.put(record)
            with self.subTest(field=field, value=value), self.assertRaises(S.SchemaError):
                F.file(self.work, self.transport)
        record = copy.deepcopy(original)
        record["packet"]["learnings"][0]["mechanism_basis"] = "inferred"
        self.put(record)
        with self.assertRaises(S.SchemaError):
            F.file(self.work, self.transport)
        self.put(original)
        for name, value in (("TITLE_PREFIX", "other: "), ("REPOSITORY", "fixture/other"),
                            ("LABEL", "other"), ("RENDERER_VERSION", 2),
                            ("PACKET_SCHEMA", "other"), ("CATALOG_VERSION", 2)):
            with self.subTest(constant=name), patch.object(S, name, value):
                with self.assertRaises(S.SchemaError):
                    F.file(self.work, self.transport)
        self.assertEqual(self.transport.calls, [])

    def test_atomic_private_receipt_and_frozen_packet(self):
        events = []
        fsync, replace = os.fsync, os.replace
        def sync(fd):
            events.append("fsync")
            return fsync(fd)
        def rename(src, dst):
            events.append("replace")
            self.assertEqual(events, ["fsync", "replace"])
            return replace(src, dst)
        with patch.object(F.os, "fsync", sync), patch.object(F.os, "replace", rename):
            original = self.approve()
        self.assertEqual(events, ["fsync", "replace", "fsync"])
        self.assertEqual(stat.S_IMODE((self.work / F.RECORD).stat().st_mode), 0o600)
        self.assertEqual(original["approval"]["approver"], "fixture approver")
        self.assertRegex(original["approval"]["approved_at"], r"T\d\d:\d\d:\d\d\.\d{6}Z$")
        with patch.object(F.os, "replace", side_effect=OSError("fixture failure")):
            with self.assertRaises(OSError):
                self.approve()
        self.assertEqual(self.state(), original)
        self.assertEqual(list(self.work.iterdir()), [self.work / F.RECORD])
        self.packet["learnings"].clear()
        with patch.object(P, "load_dossier", side_effect=AssertionError("dossier read")):
            self.assertEqual(F.file(self.work, self.transport)["status"], "FILED")
        self.assertNotIn("fixture approver", str(self.transport.calls))

    def test_filing_crash_matrix(self):
        for point in ("before_create", "after_create", "before_readback"):
            with self.subTest(point=point):
                self.work = self.root / point
                self.transport = FakeTransport()
                self.approve()
                original_save = F._save
                def save(root, record, status, number=None):
                    if status == "POST_IN_FLIGHT" and number is not None and point == "after_create":
                        raise Crash()
                    result = original_save(root, record, status, number)
                    if status == "POST_IN_FLIGHT" and number is None and point == "before_create":
                        raise Crash()
                    return result
                def get(number):
                    raise Crash()
                with patch.object(F, "_save", save):
                    if point == "before_readback":
                        with patch.object(self.transport, "get", get), self.assertRaises(Crash):
                            F.file(self.work, self.transport)
                    else:
                        with self.assertRaises(Crash):
                            F.file(self.work, self.transport)
                self.assertEqual(self.state()["status"], "POST_IN_FLIGHT")
                for _ in range(2):
                    result = F.file(self.work, self.transport)
                    self.assertEqual(result["status"],
                                     "DELIVERY_UNCERTAIN" if point == "before_create" else "FILED")
                self.assertEqual(self.creates(), 0 if point == "before_create" else 1)
                self.assertTrue(any(c[0] == "find" for c in self.transport.calls))

    def test_intent_is_durable_at_create_and_ambiguous_timeout(self):
        self.approve()
        create = self.transport.create
        def timeout(title, body, label):
            self.assertEqual(self.state()["status"], "POST_IN_FLIGHT")
            create(title, body, label)
            raise TimeoutError("synthetic remote response lost")
        with patch.object(self.transport, "create", timeout):
            self.assertEqual(F.file(self.work, self.transport)["status"], "DELIVERY_UNCERTAIN")
        self.assertEqual(F.file(self.work, self.transport)["status"], "FILED")
        self.assertEqual(self.creates(), 1)

    def uncertain(self):
        self.approve()
        with patch.object(self.transport, "create", side_effect=TimeoutError):
            F.file(self.work, self.transport)

    def test_reconcile_closed_and_conflicts(self):
        for scenario in ("closed", "two", "edited", "none", "unavailable"):
            with self.subTest(scenario=scenario):
                self.work = self.root / scenario
                self.transport = FakeTransport()
                self.uncertain()
                if scenario not in ("none", "unavailable"):
                    title, body = P.render(self.packet)
                    n = self.transport.create(title, body, S.LABEL)
                    self.transport.issues[n]["state"] = "closed"
                    if scenario == "two":
                        self.transport.create(title, body, S.LABEL)
                    if scenario == "edited":
                        self.transport.issues[n]["body"] += "edited"
                self.transport.calls.clear()
                if scenario == "unavailable":
                    with patch.object(self.transport, "find", side_effect=TimeoutError):
                        result = F.file(self.work, self.transport)
                else:
                    result = F.file(self.work, self.transport)
                expected = {"closed": "FILED", "two": "CONFLICT", "edited": "CONFLICT",
                            "none": "DELIVERY_UNCERTAIN", "unavailable": "DELIVERY_UNCERTAIN"}
                self.assertEqual(result["status"], expected[scenario])
                self.assertEqual(self.creates(), 0)
                with self.assertRaises(S.SchemaError):
                    self.approve()

    def test_readback_equivalence(self):
        for variant in ("crlf", "indent", "append", "second", "value", "title", "label"):
            with self.subTest(variant=variant):
                self.work = self.root / variant
                self.transport = FakeTransport()
                self.approve()
                get = self.transport.get
                def altered(number):
                    issue = copy.deepcopy(get(number))
                    if variant == "crlf":
                        issue["body"] = issue["body"].replace("\n", "\r\n")
                    elif variant == "indent":
                        issue["body"] = S.PREAMBLE + "\n\n```json\n" + json.dumps(
                            self.packet, indent=4) + "\n```"
                    elif variant == "append":
                        issue["body"] += "appended text"
                    elif variant == "second":
                        issue["body"] += "\n```json\n{}\n```"
                    elif variant == "value":
                        issue["body"] = issue["body"].replace('"reported"', '"inferred"')
                    elif variant == "title":
                        issue["title"] += " "
                    else:
                        issue["labels"] = ["Debrief"]
                    return issue
                with patch.object(self.transport, "get", altered):
                    result = F.file(self.work, self.transport)
                    self.assertEqual(result["status"],
                                     "FILED" if variant in ("crlf", "indent") else "READBACK_FAILED")
                    self.assertEqual(result["number"], 1)
                    again = F.file(self.work, self.transport)
                    self.assertEqual(again["status"],
                                     "FILED" if variant in ("crlf", "indent") else "CONFLICT")
                self.assertEqual(self.creates(), 1)

    def test_unavailable_readback_retries_known_number(self):
        self.approve()
        with patch.object(self.transport, "get", side_effect=TimeoutError):
            for _ in range(2):
                result = F.file(self.work, self.transport)
                self.assertEqual((result["status"], result["number"]), ("READBACK_FAILED", 1))
        self.assertEqual(F.file(self.work, self.transport)["status"], "FILED")
        self.assertEqual(self.creates(), 1)

    def test_concurrent_processes_create_once(self):
        self.approve()
        remote = self.root / "remote"
        remote.mkdir()
        ctx = multiprocessing.get_context("spawn")
        ready, start = ctx.Queue(), ctx.Event()
        processes = [ctx.Process(target=concurrent_file,
                     args=(str(self.work), str(remote), ready, start)) for _ in range(2)]
        try:
            for process in processes:
                process.start()
            for _ in processes:
                self.assertTrue(ready.get(timeout=10))
            start.set()
            for process in processes:
                process.join(15)
                self.assertEqual(process.exitcode, 0)
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join(5)
            ready.close()
            ready.join_thread()
        self.assertEqual((remote / "calls").read_text().splitlines(), ["create"])
        self.assertEqual(self.state()["status"], "FILED")

    def test_required_mutations(self):
        source = SCRIPT.read_text()
        cases = [
            ("skip intent persistence", '        _save(root, record, "POST_IN_FLIGHT")\n', '',
             "test_intent_is_durable_at_create_and_ambiguous_timeout"),
            ("retry create in flight", '        if status in ("POST_IN_FLIGHT", "DELIVERY_UNCERTAIN"):',
             '        if status == "DELIVERY_UNCERTAIN":', "test_filing_crash_matrix"),
            ("drop approval digest", 'digest == binding["digest"]', 'True',
             "test_approval_binding_matrix"),
            ("drop stale digest", 'type(approval[key]) is type(value) and approval[key] == value',
             'key == "digest" or (type(approval[key]) is type(value) and approval[key] == value)',
             "test_approval_binding_matrix"),
            ("find open only", 'transport.find(record["packet"]["packet_id"])',
             'transport.find(record["packet"]["packet_id"], state="open")',
             "test_reconcile_closed_and_conflicts"),
            ("skip readback", 'return _readback(root, record, transport, "READBACK_FAILED")',
             'return _save(root, record, "FILED")', "test_readback_equivalence"),
        ]
        for name, old, new, test in cases:
            with self.subTest(mutation=name):
                self.assertEqual(source.count(old), 1)
                mutant = types.ModuleType("debrief_filing_mutant")
                exec(compile(source.replace(old, new), str(SCRIPT), "exec"), mutant.__dict__)
                output = io.StringIO()
                with patch(__name__ + ".F", mutant):
                    result = unittest.TextTestRunner(stream=output).run(
                        unittest.TestSuite([TestDebriefFiling(test)]))
                self.assertFalse(result.wasSuccessful(), name + " survived: " + output.getvalue())
                self.assertTrue(result.failures, name + " must cause an assertion failure")


if __name__ == "__main__":
    unittest.main()
