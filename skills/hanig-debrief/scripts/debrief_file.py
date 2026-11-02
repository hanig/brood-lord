"""Operator-only GitHub adapter; drafting modules must never import this module.

approve reads packet.json; file uses D4's frozen filing.json. Neither reads the
private dossier. gh-receipt.json is diagnostic only, never filing authority.
Exit codes: FILED=0, pending (including APPROVED)=1, refusal=2,
DELIVERY_UNCERTAIN=3, READBACK_FAILED=4, CONFLICT=5, missing gh=6,
unauthenticated gh=7. Only FILED denotes confirmed delivery.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile

import debrief_filing as F
import debrief_packet as P
import debrief_schema as S


REPOSITORY = S.REPOSITORY
LABEL = S.LABEL
PACKET = "packet.json"
RECEIPT = "gh-receipt.json"
EXIT_FILED, EXIT_PENDING, EXIT_REFUSED = 0, 1, 2
EXIT_DELIVERY_UNCERTAIN, EXIT_READBACK_FAILED, EXIT_CONFLICT = 3, 4, 5
EXIT_GH_MISSING, EXIT_GH_UNAUTHENTICATED = 6, 7
STATE_EXITS = {"FILED": EXIT_FILED, "DELIVERY_UNCERTAIN": EXIT_DELIVERY_UNCERTAIN,
               "READBACK_FAILED": EXIT_READBACK_FAILED, "CONFLICT": EXIT_CONFLICT}


class GhError(Exception):
    """Contains only a fixed message class, never command output."""


class GhTransport:
    def __init__(self):
        self.ready = False
        self.receipt = {"exit_code": 0, "message_class": "GH_OK"}

    def _fail(self, message_class, exit_code=None):
        self.receipt = {"exit_code": exit_code, "message_class": message_class}
        raise GhError(message_class)

    def _run(self, args, payload=None, failure="GH_API_FAILED"):
        # All child output stays captured, including gh authentication diagnostics.
        try:
            result = subprocess.run(
                ["gh"] + args, input="" if payload is None else json.dumps(payload),
                text=True, encoding="utf-8", stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, timeout=60, check=False, shell=False)
        except FileNotFoundError:
            self._fail("GH_MISSING")
        except subprocess.TimeoutExpired:
            self._fail("GH_TIMEOUT")
        except (OSError, UnicodeError):
            self._fail("GH_EXEC_FAILED")
        if result.returncode != 0:
            self._fail(failure, result.returncode)
        return result.stdout

    def _api(self, endpoint, method="GET", payload=None, failure="GH_API_FAILED"):
        args = ["api", "--hostname", "github.com", "--method", method,
                "--header", "Accept: application/vnd.github.raw+json",
                "repos/" + REPOSITORY + "/" + endpoint]
        if payload is not None:
            args += ["--input", "-"]
        raw = self._run(args, payload, failure)
        try:
            return json.loads(raw, object_pairs_hook=S._pairs,
                              parse_constant=S._constant)
        except (ValueError, RecursionError):
            self._fail("GH_INVALID_RESPONSE", 0)

    def prepare(self):
        """Refuse missing authentication/label before D4 records a POST intent."""
        if not self.ready:
            self._run(["auth", "status", "--active", "--hostname", "github.com"],
                      failure="GH_UNAUTHENTICATED")
            label = self._api("labels/" + LABEL, failure="GH_LABEL_UNAVAILABLE")
            if type(label) is not dict or label.get("name") != LABEL:
                self._fail("GH_LABEL_UNAVAILABLE", 0)
            self.ready = True

    def create(self, title, body, label):
        if label != LABEL:
            self._fail("GH_LABEL_REFUSED")
        self.prepare()
        issue = self._api("issues", "POST",
                          {"title": title, "body": body, "labels": [LABEL]})
        if type(issue) is not dict or not F._number(issue.get("number")):
            self._fail("GH_INVALID_RESPONSE", 0)
        return issue["number"]

    def get(self, number):
        if not F._number(number):
            self._fail("GH_NUMBER_REFUSED")
        self.prepare()
        issue = self._api("issues/" + str(number))
        if (type(issue) is not dict or type(issue.get("number")) is not int
                or issue["number"] != number or "pull_request" in issue
                or type(issue.get("labels")) is not list):
            self._fail("GH_INVALID_RESPONSE", 0)
        labels = issue["labels"]
        if any(type(label) is not dict or type(label.get("name")) is not str
               for label in labels):
            self._fail("GH_INVALID_RESPONSE", 0)
        return {"title": issue.get("title"), "body": issue.get("body"),
                "state": issue.get("state"), "labels": [v["name"] for v in labels]}

    def find(self, packet_id):
        self.prepare()
        matches, page = [], 1
        while True:
            issues = self._api("issues?state=all&labels=" + LABEL
                               + "&per_page=100&sort=created&direction=asc&page=" + str(page))
            if type(issues) is not list:
                self._fail("GH_INVALID_RESPONSE", 0)
            for issue in issues:
                if type(issue) is not dict:
                    self._fail("GH_INVALID_RESPONSE", 0)
                if "pull_request" not in issue and issue.get("title") == S.TITLE_PREFIX + packet_id:
                    if not F._number(issue.get("number")):
                        self._fail("GH_INVALID_RESPONSE", 0)
                    matches.append(issue["number"])
            if len(issues) < 100:
                return matches
            page += 1


def _receipt(workdir, receipt):
    """Atomically replace private diagnostics; never append captured output."""
    root = Path(workdir)
    fd, name = tempfile.mkstemp(prefix=".gh-receipt-", dir=str(root))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(receipt, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, str(root / RECEIPT))
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _summary(record):
    print(json.dumps({"status": record["status"], "number": record["number"]}))
    return STATE_EXITS.get(record["status"], EXIT_PENDING)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("approve", "file", "status"):
        command = commands.add_parser(name, allow_abbrev=False)
        command.add_argument("--workdir", required=True)
        if name == "approve":
            command.add_argument("--digest", required=True)
            command.add_argument("--approver", required=True)
    args = parser.parse_args(argv)
    transport = None
    try:
        if args.command == "approve":
            return _summary(F.approve(args.workdir, P.load_packet(Path(args.workdir) / PACKET),
                                      args.digest, args.approver))
        with F._locked(args.workdir) as root:
            record = F._read_state(root) if args.command == "status" else F._read(root)
        if args.command == "status":
            return _summary(record)
        # Validate approval before preflight; D4 revalidates under its own lock.
        transport = GhTransport()
        transport.prepare()
        return _summary(F.file(args.workdir, transport))
    except GhError:
        message = transport.receipt["message_class"]
        print(json.dumps({"status": "REFUSED", "message_class": message}))
        return {"GH_MISSING": EXIT_GH_MISSING,
                "GH_UNAUTHENTICATED": EXIT_GH_UNAUTHENTICATED}.get(message, EXIT_REFUSED)
    except (S.SchemaError, OSError, ValueError, RecursionError):
        print(json.dumps({"status": "REFUSED", "message_class": "LOCAL_INPUT_REFUSED"}))
        return EXIT_REFUSED
    finally:
        if transport is not None:
            try:
                _receipt(args.workdir, transport.receipt)
            except OSError:
                # Filing state is already durable; diagnostic failure is not a retry instruction.
                print(json.dumps({"message_class": "GH_RECEIPT_WRITE_FAILED"}))


if __name__ == "__main__":
    raise SystemExit(main())
