"""ARC-5038: accounting-only Slurm attempts retain their settle window."""

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'skills/hanig-swarm/scripts'))
import swarm as S
import unit as U

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scheduler_fixture import closed_bin, isolated_module_path
from scheduler_fixture import cleanup_module_path as tearDownModule


def setUpModule():
    isolated_module_path()


class TestAccountingOutage(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.bin = self.tmp / 'bin'
        self.env = mock.patch.dict(os.environ, {'PATH': closed_bin(self.bin)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.state_dir = str(self.tmp / 'state')
        self.assertTrue(S.acquire_lease(self.state_dir)[0])
        self.addCleanup(S.release_lease, self.state_dir)
        self.plan = {'name': 'outage', 'units': [
            {'id': 'u', 'kind': 'slurm', 'runtime': 'none',
             'command': 'true', 'outputs': ['out'], 'write_scopes': ['u/']}]}
        self.state = {'schema_version': 1, 'halted': None, 'units': {}}
        self.add_attempt('u', '42')

    def add_attempt(self, uid, job):
        attempt = self.tmp / 'runs' / uid / 'a1'
        attempt.mkdir(parents=True)
        self.submitted = U.now_iso()
        (attempt / U.UNIT).write_text(json.dumps({
            'schema_version': 1, 'task_id': uid, 'attempt_id': 'a1',
            'kind': 'slurm', 'job_id': job, 'bound_at': self.submitted,
            'created_at': self.submitted, 'declared_outputs': ['out']}))
        us = {'state': 'SUBMITTED', 'job_id': job, 'attempt_dir': str(attempt),
              'attempts': [str(attempt)], 'gpu_hours': 0}
        self.state['units'][uid] = us
        S._capture_artifact_basis(self.state, uid, str(attempt), {'outputs': ['out']})
        (attempt / 'out').write_text('produced output\n')
        return us

    def accounting(self, mode):
        executable = self.bin / 'sacct'
        if mode == 'absent':
            if executable.exists():
                executable.unlink()
            return
        body = {'failed': 'exit 1', 'missing': 'exit 0',
                'completed': "echo 'COMPLETED|0:0|%s|'" % self.submitted,
                'foreign': "echo 'COMPLETED|0:0|2000-01-01T00:00:00+0000|'"}[mode]
        executable.write_text('#!/bin/sh\n' + body + '\n')
        executable.chmod(0o755)

    def advance(self, now):
        with mock.patch.object(S.time, 'time', return_value=now):
            report, dispatched, halted = S.advance(
                self.plan, self.state, self.state_dir, str(self.tmp / 'runs'),
                False, max_new=0)
        self.assertEqual(dispatched, 0)
        self.assertIsNone(halted)
        # Reload every pass: outage/settle clocks must survive a new session.
        self.state = S.load_state(self.state_dir)
        return '\n'.join(report)

    def test_outage_probe_stays_nonterminal_past_settle(self):
        # Exhaustive finite cases: absent/failed sacct x four elapsed times.
        for mode in ('absent', 'failed'):
            with self.subTest(mode=mode):
                self.accounting(mode)
                self.state['units']['u'].pop('accounting_unavailable_since', None)
                for elapsed in (0, S.SETTLE_S, S.SETTLE_S + 1, 10 * S.SETTLE_S):
                    report = self.advance(10000 + elapsed)
                    us = self.state['units']['u']
                    self.assertEqual(us['state'], 'INCOMPLETE', report)
                    self.assertNotIn('incomplete_since', us)
                    self.assertEqual(us['accounting_unavailable_since'], 10000)
                    self.assertIn('u: accounting unavailable since', report)
                    self.assertIn('kept nonterminal', report)

    def test_recovery_missing_row_gets_full_window_even_after_prior_incomplete(self):
        self.accounting('missing')
        self.advance(10000)
        self.assertEqual(self.state['units']['u']['incomplete_since'], 10000)
        self.accounting('failed')
        self.advance(10000 + S.SETTLE_S + 1)
        self.assertNotIn('incomplete_since', self.state['units']['u'])
        self.accounting('missing')
        recovered = 20000
        for elapsed, expected in ((0, 'INCOMPLETE'), (S.SETTLE_S, 'INCOMPLETE'),
                                  (S.SETTLE_S + 1, 'FAILED_EVIDENCE')):
            report = self.advance(recovered + elapsed)
            us = self.state['units']['u']
            self.assertEqual(us['state'], expected, report)
            self.assertEqual(us['incomplete_since'], recovered)
            self.assertNotIn('accounting_unavailable_since', us)

    def test_recovery_requires_owned_terminal_ok_row(self):
        for mode, expected in (('absent', 'INCOMPLETE'), ('missing', 'INCOMPLETE'),
                               ('foreign', 'INCOMPLETE'), ('completed', 'DONE')):
            with self.subTest(mode=mode):
                self.accounting(mode)
                report = self.advance(10000)
                self.assertEqual(self.state['units']['u']['state'], expected, report)

    def test_cached_outage_reports_each_skipped_unit(self):
        self.plan['units'].append(dict(self.plan['units'][0], id='v', write_scopes=['v/']))
        self.add_attempt('v', '43')
        self.accounting('failed')
        for now in (10000, 20000):
            report = self.advance(now)
            for uid in ('u', 'v'):
                self.assertEqual(self.state['units'][uid]['state'], 'INCOMPLETE', report)
                self.assertIn(uid + ': accounting unavailable since', report)


if __name__ == '__main__':
    unittest.main()
