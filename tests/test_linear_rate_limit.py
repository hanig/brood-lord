"""Offline rate-limit contract; exhaustive request positions, no live tracker."""
import itertools
import json
import unittest
from unittest import mock

from tests import test_linear_drain as D
from tests import test_linear_file as F
from tests import test_linear_issue as I

API, LS = D.API, D.LS


class TestTransport(unittest.TestCase):
    def test_signals_and_headers(self):
        # Exhaustive product: 3 signals x 3 headers = 9 cases.
        for status, header in itertools.product((429, 400, 200), ({},
                {'Retry-After': '60'}, {'X-RateLimit-Requests-Reset': '60000'})):
            with self.subTest(status=status, header=header):
                body = b'not JSON' if status == 429 else json.dumps({'errors': [
                    {'message': 'refusal', 'extensions': {'code': 'RATELIMITED'}}]}).encode()
                client = API.Client('synthetic-key')
                with mock.patch.object(API, 'transport', return_value=(status, body, header)), \
                        mock.patch.object(API.time, 'time', return_value=0):
                    with self.assertRaises(API.LinearRateLimited) as raised:
                        client.request('query {}')
                self.assertIsInstance(raised.exception, API.LinearError)
                self.assertEqual(raised.exception.reset_at,
                                 '1970-01-01T00:01:00+00:00' if header else None)
                self.assertEqual(client.requests, 1)
                self.assertNotIn('synthetic-key', str(raised.exception))

    def test_dates_bad_headers_and_other_errors(self):
        self.assertEqual(API.reset_time({'Retry-After': 'Thu, 01 Jan 1970 00:01:00 GMT'}),
                         '1970-01-01T00:01:00+00:00')
        for value in ('bad', 'NaN', 'Infinity', '1e9999'):
            self.assertIsNone(API.reset_time({'X-RateLimit-Requests-Reset': value}))
        with mock.patch.object(API, 'transport', return_value=(400, b'{"errors":[{"message":"other"}]}')):
            with self.assertRaises(API.LinearError) as raised:
                API.Client('key').query('query {}')
        self.assertNotIsInstance(raised.exception, API.LinearRateLimited)

    def test_network_seam_exposes_response_headers(self):
        response = mock.MagicMock(status=429)
        response.headers = {'Retry-After': '60'}
        response.read.return_value = b'refused'
        response.__enter__.return_value = response
        with mock.patch.object(API.urllib.request, 'urlopen', return_value=response):
            self.assertEqual(API.transport(b'{}', {}), (429, b'refused', response.headers))
        error = API.urllib.error.HTTPError('https://example.invalid', 429, 'limited',
                                         response.headers, None)
        with mock.patch.object(API.urllib.request, 'urlopen', side_effect=error):
            self.assertEqual(API.transport(b'{}', {})[2], response.headers)


class TestCommands(unittest.TestCase):
    def fixture(self, kind):
        case = {'drain': D.DrainCase, 'file': F.FileCase, 'issue': I.IssueCase}[kind]('runTest')
        case.setUp()
        self.addCleanup(case.doCleanups)
        return case

    def exercise(self, case, call, position, remaining=None):
        transport = case.fake
        calls = []
        def limited(body, headers, timeout=None):
            calls.append(json.loads(body)['query'])
            if len(calls) == position:
                return 429, b'{}', {'X-RateLimit-Requests-Reset': '60000'}
            return transport(body, headers, timeout)
        with mock.patch.object(API, 'transport', limited), \
                mock.patch('time.sleep', side_effect=AssertionError('must not sleep')):
            self.assertEqual(call(), API.RATE_LIMIT_EXIT, case.stdout + case.stderr)
        self.assertEqual(len(calls), position, case.stdout + case.stderr)
        self.assertIn('Linear rate limit; reset 1970-01-01T00:01:00+00:00;', case.stderr)
        self.assertIn('%d requests' % position, case.stderr)
        self.assertIn('requests: %d' % position, case.stderr)
        self.assertIn('INCOMPLETE', case.stderr)
        if remaining is not None:
            self.assertIn('%d items unreached' % remaining, case.stderr)
        self.assertNotIn('CONFIRMED', case.stdout)
        return calls

    def test_drain_every_request_position(self):
        # Enumerate every request position in a successful two-issue drain.
        base = self.fixture('drain')
        base.fake.add('2')
        base.intent(key='a')
        base.intent(key='b', tracker='ARC-2')
        self.assertEqual(base.drain(), 0, base.stdout + base.stderr)
        bound = len(base.fake.calls)
        self.assertGreater(bound, 5)
        base.doCleanups()
        for position in range(1, bound + 1):
            with self.subTest(position=position):
                case = self.fixture('drain')
                case.fake.add('2')
                case.intent(key='a')
                case.intent(key='b', tracker='ARC-2')
                calls = self.exercise(case, case.drain, position)
                if not any('IntentCommentCreate' in q for q in calls[:-1]):
                    self.assertEqual(case.receipts(), [])
                if sum('IntentCommentCreate' in q for q in calls) <= 1:
                    self.assertFalse(any(r.get('key') == 'b' for r in case.receipts()))
                case.doCleanups()

    def test_file_every_request_position_and_open_operation(self):
        # Enumerate all requests of a three-issue filing, including final audit.
        base = self.fixture('file')
        self.assertEqual(base.file(), 0, base.stdout + base.stderr)
        bound = len(base.fake.calls)
        base.doCleanups()
        for position in range(1, bound + 1):
            with self.subTest(position=position):
                case = self.fixture('file')
                self.exercise(case, case.file, position)
                for path in case.records():
                    self.assertFalse(I.LI.confirmed(path))
                case.doCleanups()

    def test_remaining_and_non_rate_error_continues(self):
        case = self.fixture('drain')
        case.fake.add('2')
        case.intent(key='a')
        case.intent(key='b', tracker='ARC-2')
        # First binding read then independent issue lookups.
        self.exercise(case, case.drain, 2, remaining=1)
        case.fake.fail = 'OperationIdentifiers'
        self.assertEqual(case.drain(), 3, case.stdout + case.stderr)
        self.assertNotIn('Linear rate limit', case.stderr)
        self.assertGreater(len(case.fake.calls), 2)

    def test_other_command_stops(self):
        # Bind and audit are each one snapshot. Issue edit/new each one target.
        for command in ('bind', 'audit', 'new', 'edit', 'issue-replay', 'file-replay'):
            with self.subTest(command=command):
                case = self.fixture('file' if command == 'file-replay' else 'issue')
                if command == 'bind':
                    call = lambda: case.cli('bind', '--project', 'project', '--repository', 'owner/repo')
                elif command == 'audit':
                    call = lambda: case.cli('audit', '--binding', str(case.binding))
                elif command == 'file-replay':
                    self.assertEqual(case.file(), 0, case.stdout + case.stderr)
                    call = case.replay_file
                elif command == 'issue-replay':
                    self.assertEqual(case.issue('new', '--independent', 'reason'), 0, case.stdout + case.stderr)
                    op = case.records()[0].stem
                    call = lambda: case.issue('replay', op)
                elif command == 'edit':
                    call = lambda: case.issue('edit', 'ARC-1', '--title', 'changed', '--independent', 'reason')
                else:
                    call = lambda: case.issue('new', '--independent', 'reason')
                self.exercise(case, call, 1)
                case.doCleanups()


if __name__ == '__main__':
    unittest.main()
