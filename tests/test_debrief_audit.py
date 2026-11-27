import ast
import io
import json
import os
from pathlib import Path
import pwd
import random
import subprocess
import sys
import tempfile
import unicodedata
import unittest
from unittest import mock
from contextlib import redirect_stderr, redirect_stdout

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills/brood-debrief/scripts"
sys.path.insert(0, str(SCRIPTS))
import debrief_attention as T
import debrief_audit as A
import decisions_log as D

SEED, CASES = 20261124, 300  # sanitize: 300 strings, <= 16 code points
CANARY = "CANARY-7f3a"


def call(module, argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = module.main(argv)
    return code, out.getvalue(), err.getvalue()


def jsonl(path, records, pad=0):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records) + "x" * pad)


def use(tid, name, **inp):
    return {"message": {"content": [{"type": "tool_use", "id": tid, "name": name, "input": inp}]}}


def result(tid, error):
    return {"message": {"content": [
        {"type": "tool_result", "tool_use_id": tid, "is_error": error, "content": CANARY}]}}


def codex(kind, **payload):
    return {"type": "response_item", "payload": dict(payload, type=kind)}


def shell(cid, cmd):
    return codex("function_call", name="exec_command", call_id=cid,
                 arguments=json.dumps({"cmd": cmd}))


class Base(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.home = self.root / "home"
        env = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        self.ws_a, self.ws_b = self.root / "x/ws-a", self.root / "x/ws/a"  # one Claude slug
        self.ws_b.mkdir(parents=True)
        git = ["git", "-C", str(self.ws_a), "-c", "user.name=t", "-c", "user.email=t@t.invalid",
               "-c", "commit.gpgsign=false"]
        subprocess.run(["git", "init", "-q", str(self.ws_a)], check=True)
        subprocess.run(git + ["commit", "-q", "--allow-empty", "-m", "x"], check=True)
        sha = subprocess.run(git + ["rev-parse", "HEAD"], check=True, capture_output=True,
                             text=True).stdout.strip()
        self.log = self.root / "private/decisions.tsv"
        for ref in ("command:make lint-all; file:src/app.py; commit:%s; test:pytest -q" % sha,
                    "commit:%s; test:unittest failing" % ("0" * 40), "",
                    "by eye; command:ab; command:only-in-b; command:hidden-in-other;"
                    " file:src/b.py; commit:deadbeef; test:codex-ran-this",
                    "command:codex-ran-this; file:lib/patched.py; file:src/bad.py"):
            D.append(self.log, "r", "adopt", "decision", evidence_ref=ref)
        projects = self.home / ".claude/projects" / A.slug(str(self.ws_a))
        self.claude_b = projects / "sess-b.jsonl"
        jsonl(projects / "sess-a.jsonl", [
            {"type": "summary"}, {"cwd": str(self.ws_a), "message": {"content": CANARY}},
            use("t1", "Bash", command="make lint-all " + CANARY), result("t1", False),
            use("t2", "Edit", file_path=str(self.ws_a / "src/app.py"), new_string=CANARY),
            use("t3", "Bash", command="pytest -q"), result("t3", False),
            use("t4", "Edit", file_path="src/bad.py"), result("t4", True)])
        jsonl(self.claude_b, [{"cwd": str(self.ws_b)}, use("b1", "Bash", command="only-in-b"),
                              result("b1", False), use("b2", "Write", file_path="src/b.py")],
              pad=200000)
        day = self.home / ".codex/sessions/2026/11/24"
        jsonl(day / "rollout-a.jsonl", [
            {"type": "session_meta", "payload": {"cwd": str(self.ws_a)}},
            shell("c1", "codex-ran-this"), shell("c2", "python -m unittest failing"),
            codex("function_call_output", call_id="c2",
                  output="Chunk ID: 1\nProcess exited with code 1\nOutput:\n" + CANARY),
            codex("custom_tool_call", name="apply_patch", call_id="c3",
                  input="*** Begin Patch\n*** Update File: lib/patched.py\n+" + CANARY)])
        self.rollout_b = day / "rollout-b.jsonl"
        jsonl(self.rollout_b, [{"type": "session_meta", "payload": {"cwd": str(self.ws_b)}},
                               shell("d1", "hidden-in-other " + CANARY)], pad=200000)

    def audit(self):
        return call(A, ["--decisions", str(self.log), "--workspace", str(self.ws_a)])


class TestAudit(Base):
    def test_no_network_imports(self):
        bad = {"socket", "ssl", "http", "urllib", "ftplib", "smtplib", "asyncio"}
        for name in ("debrief_audit.py", "debrief_attention.py"):
            tree = ast.parse((SCRIPTS / name).read_text(encoding="utf-8"))
            found = {(n.module or "").split(".")[0] if isinstance(n, ast.ImportFrom)
                     else a.name.split(".")[0] for n in ast.walk(tree)
                     if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}
            self.assertTrue(found and not found & bad, (name, found))

    def test_claim_statuses_and_slug_collision(self):
        before = self.log.read_bytes()
        code, out, err = self.audit()
        self.assertEqual((code, self.log.read_bytes()), (0, before), err)
        report = json.loads(out)
        got = ["".join(c["status"][0] for c in row["claims"]) for row in report["rows"]]
        # supported/contradicted/unverifiable
        self.assertEqual(got, ["ssss", "cc", "u", "uuuuuuu", "ssu"])
        self.assertEqual(report["sessions"], ["claude:sess-a", "codex:rollout-a"])
        self.assertEqual(report["rejected_candidates"], 2)
        late = self.root / "late.jsonl"  # cwd on line 33
        jsonl(late, [{}] * 32 + [{"cwd": str(self.ws_a)}])
        self.assertFalse(A.claude_verified(late, str(self.ws_a), {}))
        self.assertEqual(report["rows"][0]["claims"][0]["matches"],
                         [{"session": "claude:sess-a", "line": 3}])

    def test_no_transcript_text_and_bounded_reads(self):
        opens, read, names = [], {}, {}
        real_open, real_read = os.open, os.read

        def rec_open(path, *a, **k):
            fd = real_open(path, *a, **k)
            names[fd] = os.fspath(path)
            opens.append(names[fd])
            return fd

        def rec_read(fd, n):
            data = real_read(fd, n)
            read[names.get(fd)] = read.get(names.get(fd), 0) + len(data)
            return data
        with mock.patch("os.open", rec_open), mock.patch("os.read", rec_read):
            out = self.audit()[1]
        self.assertTrue(out and CANARY not in out and "lint" not in out, out)
        real_home = pwd.getpwuid(os.getuid()).pw_dir
        for path in opens:
            for private in (".claude", ".codex"):
                self.assertFalse(path.startswith(os.path.join(real_home, private)), path)
        for path in (self.claude_b, self.rollout_b):
            self.assertEqual(opens.count(str(path)), 1)
            line1 = len(path.read_bytes().split(b"\n")[0]) + 1
            self.assertLessEqual(read[str(path)], line1 + A.CHUNK)


class TestAttention(Base):
    def setUp(self):
        super().setUp()
        self.audit_json = self.root / "audit.json"
        self.audit_json.write_text(self.audit()[1])
        self.pack = self.root / "pack.txt"
        with mock.patch("subprocess.run", side_effect=AssertionError), \
                mock.patch("subprocess.Popen", side_effect=AssertionError):
            code, _, err = call(T, ["pack", "--decisions", str(self.log), "--audit",
                                    str(self.audit_json), "--out", str(self.pack)])
        self.assertEqual(code, 0, err)

    def test_pack_format_offline(self):
        text = self.pack.read_text()
        self.assertEqual(T.check_pack(text), {1, 2, 3, 4, 5})
        self.assertTrue(text.startswith(T.MAGIC) and "<<<END-UNTRUSTED-DATA" in text)
        self.assertTrue(CANARY not in text and '"status": "contradicted"' in text)
        with self.assertRaises(T.AttentionError):
            T.check_pack(text.replace("adopt", "adopT"))
        D.append(self.log, "r", "adopt", "later")
        self.assertEqual(call(T, ["pack", "--decisions", str(self.log), "--audit",
                                  str(self.audit_json)])[0], 1)

    def test_sanitize_property(self):
        self.assertEqual(T.sanitize("\x1b[31mred\x1b[0m\u202e\tx\n\x1b]0;t\x07"), "red\tx\n")
        rng = random.Random(SEED)
        pool = ["\x1b[1;31m", "\x1b]8;;u\x07", "\t", "\n", "\u2028", "\u200b", "\x00", "a",
                "\u00e9"]
        for _ in range(CASES):
            out = T.sanitize("".join(rng.choice(pool) for _ in range(rng.randint(0, 16))))
            self.assertFalse([c for c in out if c not in "\t\n"
                              and unicodedata.category(c) in D.UNSAFE], repr(out))

    def test_validate(self):
        flag = {"row_seq": 1, "category": "missed_gap", "detail": "d"}
        ok = {"reviewer_model": "m", "status": "flags", "flags": [flag]}
        none = dict(ok, status="no_flags", flags=[])
        report = self.root / "report.txt"
        for text, want in [
                ("reviewed by m\n" + json.dumps(ok), 0),
                ("reviewed by m\nNo flags\n```json\n%s\n```" % json.dumps(none), 0),
                ("reviewed by m\n" + json.dumps(none), 1),
                ("reviewed by m\nNo flags\n" + json.dumps(dict(none, status="flags")), 1),
                ("reviewed by m\n" + json.dumps({"status": "flags", "flags": [flag]}), 1),
                ("reviewed by n\n" + json.dumps(ok), 1), (json.dumps(ok), 1),
                ("reviewed by m\n" + json.dumps(dict(ok, flags=[{"row_seq": "1"}])), 1),
                ("reviewed by m\n" + json.dumps(dict(ok, flags=[dict(flag, row_seq=9)])), 1)]:
            report.write_text(text)
            self.assertEqual(call(T, ["validate", "--report", str(report), "--pack",
                                      str(self.pack)])[0], want, text)


if __name__ == "__main__":
    unittest.main()
