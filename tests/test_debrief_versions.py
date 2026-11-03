"""Finite boundary tables; all generated observations are synthetic."""
import copy
import contextlib
import io
import itertools
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests.test_debrief_packet import dossier, P, S
import debrief
import debrief_coverage as C
import debrief_extract as E


class TestDebriefVersions(unittest.TestCase):
    def test_identifier_tables(self):
        # Exhaustive Cartesian product: 2 versions x 2 prefixes x 10 values
        # x widths 0..8, plus explicit malformed literals. No random seed.
        numbers = (0, 1, 99, 100, 999, 1000, 123456, 999999, 1000000, -1)
        for version, prefix, n, width in itertools.product((1, 2), 'EC', numbers, range(9)):
            literal = prefix + str(n).zfill(width)
            valid = (1 <= n <= (999 if version == 1 else 999999)
                     and literal == prefix + str(n).zfill(3))
            with self.subTest(version=version, literal=literal):
                self.check_id(version, prefix, literal, valid)
        for version, prefix, tail in itertools.product((1, 2), 'EC',
                ('0001', '000', '01', '1000000', '001\n', '００１', '001 ', '+001')):
            self.check_id(version, prefix, prefix + tail, False)

    def check_id(self, version, prefix, literal, valid):
        d = dossier()
        p = P.compile_packet(d, S.load_catalog())
        d['schema_version'] = 'debrief_dossier.v%d' % version
        p['schema_version'] = 'debrief_packet.v%d' % version
        cand, row = d['candidates'][0], p['learnings'][0]
        if prefix == 'E':
            d['evidence'] = {literal: d['evidence']['E001']}
            cand['evidence_refs'] = row['evidence_refs'] = [literal]
            cand.update(basis='reproduced', reproduction=dict(receipt_ref=literal,
                tested_version='fixture', probe='fixture', observed='fixture'))
        else:
            d['cohorts'] = {literal: d['cohorts']['C001']}
            cand['counts'][0]['cohort_ref'] = row['counts'][0]['cohort_ref'] = literal
        for value, validate in ((d, P.validate_dossier), (p, P.validate_packet)):
            if valid:
                self.assertEqual(validate(value), value)
            else:
                with self.assertRaises(S.SchemaError):
                    validate(value)
        if valid:
            self.assertEqual(P.recount(d, p), p)

    def test_upgrade_lossless_and_downgrade_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            work.chmod(0o700)
            d = dossier()
            d['schema_version'] = 'debrief_dossier.v1'
            path = work / 'dossier.json'
            path.write_text(json.dumps(d))
            packet = work / 'packet.json'
            packet.write_text('untouched existing packet')
            self.assertEqual(debrief.main(['upgrade', '--workdir', tmp]), 0)
            expected = copy.deepcopy(d)
            expected['schema_version'] = 'debrief_dossier.v2'
            self.assertEqual(S.load_json(path), expected)
            self.assertEqual(packet.read_text(), 'untouched existing packet')
            self.assertEqual(debrief.main(['upgrade', '--workdir', tmp]), 0)
            original = path.read_bytes()
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(debrief.main(['downgrade', '--workdir', tmp]), 1)
            self.assertIn('downgrade: refused', err.getvalue())
            self.assertEqual(path.read_bytes(), original)

    def test_mixed_versions_refused(self):
        d = dossier()
        p = P.compile_packet(d, S.load_catalog())
        d['schema_version'] = 'debrief_dossier.v1'
        with self.assertRaisesRegex(S.SchemaError, 'mixed-version.*upgrade'):
            P.compile_packet(d, S.load_catalog())
        with self.assertRaisesRegex(S.SchemaError, 'mixed-version'):
            P.recount(d, p)
        p['schema_version'] = 'debrief_packet.v1'
        self.assertEqual(P.recount(d, p), p)
        self.assertEqual(P.parse_body(P.render(p)[1]), p)
        d['schema_version'] = 'debrief_dossier.v2'
        with self.assertRaisesRegex(S.SchemaError, 'mixed-version'):
            P.recount(d, p)

    def test_capacity_guard_at_v2_bound(self):
        self.assertEqual(S.MAX_IDENTIFIERS, 999999)
        # Scale only the bound to exercise inclusive retention and refusal
        # through the real collector, without allocating a million records.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ('a', 'b', 'c'):
                (root / name).mkdir()
                (root / name / 'evidence.md').write_text('STOP\n')
            with patch.object(S, 'MAX_IDENTIFIERS', 2):
                got = E.collect([dict(label='fixture', role='project', path=tmp)])
            self.assertEqual(list(got['evidence']), ['E001', 'E002'])
            self.assertTrue(any(o['reason'] == 'over-budget: evidence ids' for o in got['omissions']))

    def test_group_allocates_c1000_only_in_v2(self):
        d = dossier()
        cohorts = {'C%03d' % i: dict(population='attempts', members=[str(i)],
                    qualifying=[str(i)]) for i in range(1, 1000)}
        candidates = []
        for i in (1, 2):
            c = copy.deepcopy(d['candidates'][0])
            c['counts'][0].update(cohort_ref='C%03d' % i, numerator=1, denominator=1)
            candidates.append(c)
        with self.assertRaisesRegex(S.SchemaError, 'cohort ids exhausted'):
            C.group(candidates, copy.deepcopy(cohorts), schema=S.DOSSIER_V1)
        result = C.group(candidates, cohorts)
        self.assertEqual(result[0]['counts'][0]['cohort_ref'], 'C1000')
        d.update(cohorts=cohorts, candidates=result)
        P.validate_dossier(d)
        P.recount(d, P.compile_packet(d, S.load_catalog()))


if __name__ == '__main__':
    unittest.main()
