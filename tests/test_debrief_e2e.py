"""One synthetic tree through collection, publication, and offline triage."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tests.test_debrief_file import FAKE_GH, reply

SCRIPTS = Path(__file__).resolve().parents[1] / 'skills/hanig-debrief/scripts'
sys.path.insert(0, str(SCRIPTS))
import debrief_packet as P
import debrief_schema as S
import debrief_triage as T


class TestDebriefE2E(unittest.TestCase):
    def test_synthetic_project_to_filed_and_triaged(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project, work, binary = (root / name for name in ('project', 'work', 'bin'))
            project.mkdir()
            binary.mkdir()
            sentinel = 'SYNTHETIC_PRIVATE_PROJECT_SENTINEL'
            (project / 'evidence.md').write_text(('- STOP reason: ' + sentinel + '\n') * 1001)
            gh = binary / 'gh'
            gh.write_text('#!' + sys.executable + '\n' + FAKE_GH)
            gh.chmod(0o700)
            env = dict(os.environ, PATH=str(binary), HOME=str(root))
            env = {k: v for k, v in env.items() if not k.startswith(('GIT_', 'PYTHON'))}

            def cli(script, *args, code=0):
                result = subprocess.run([sys.executable, str(SCRIPTS / script)] + list(args),
                    cwd=root, env=env, capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, code, result.stdout + result.stderr)
                return result.stdout

            cli('debrief.py', 'collect', '--workdir', str(work),
                '--root', 'project=' + str(project))
            path = work / 'dossier.json'
            dossier = P.load_dossier(path)
            self.assertEqual(dossier['schema_version'], 'debrief_dossier.v2')
            self.assertNotIn(sentinel, json.dumps(dossier))
            self.assertEqual(list(dossier['evidence']), ['E' + str(i).zfill(3) for i in range(1, 1002)])
            self.assertFalse(any(o['reason'] == 'over-budget: evidence ids' for o in dossier['omissions']))
            evidence = 'E1001'
            self.assertEqual(dossier['evidence'][evidence]['adapter']['name'], 'marker:STOP')
            self.assertEqual(dossier['evidence'][evidence]['location'], {'line_range': [1001, 1001]})
            # Catalog assignment is the private operator step, not inferred by collect.
            dossier['candidates'] = [dict(catalog_entry='DB001', status='selected',
                basis='reported', rationale=sentinel, evidence_refs=[evidence], counts=[],
                reproduction=None, coverage_disposition='unknown')]
            path.write_text(json.dumps(dossier))
            cli('debrief.py', 'classify', '--workdir', str(work), '--checkout', str(project))
            cli('debrief.py', 'compile', '--workdir', str(work))
            packet = P.load_packet(work / 'packet.json')
            self.assertEqual(len(packet['learnings']), 1)
            self.assertEqual(packet['schema_version'], 'debrief_packet.v2')
            self.assertEqual(packet['learnings'][0]['evidence_refs'], ['E1001'])
            shown = cli('debrief.py', 'show', '--workdir', str(work))
            self.assertIn('E1001', shown)
            title, body = P.render(packet)
            # Exit 1 means APPROVED/pending; only confirmed FILED returns 0.
            approved = cli('debrief_file.py', 'approve', '--workdir', str(work),
                '--digest', P.approval_digest(packet, title), '--approver', 'fixture owner', code=1)
            self.assertEqual(json.loads(approved), dict(status='APPROVED', number=None))
            frozen = json.loads((work / 'filing.json').read_text())
            self.assertEqual(frozen['status'], 'APPROVED')
            self.assertEqual(frozen['packet'], packet)
            self.assertEqual(frozen['approval']['digest'], P.approval_digest(packet, title))
            self.assertEqual(frozen['approval']['approver'], 'fixture owner')
            self.assertFalse((binary / 'calls').exists())
            issue = dict(number=41, title=title, body=body,
                         labels=[{'name': 'debrief'}], state='open')
            (binary / 'steps').write_text(json.dumps([
                {}, reply({'name': 'debrief'}), reply({'number': 41}), reply(issue)]))
            result = cli('debrief_file.py', 'file', '--workdir', str(work))
            self.assertEqual(json.loads(result), dict(status='FILED', number=41))
            state = json.loads((work / 'filing.json').read_text())
            self.assertEqual(state['status'], 'FILED')
            calls = [json.loads(line) for line in (binary / 'calls').read_text().splitlines()]
            creates = [c for c in calls if 'POST' in c['argv']]
            self.assertEqual(len(creates), 1)
            for call in creates:
                payload = json.loads(call['stdin'])
                self.assertEqual(payload, dict(title=title, body=body, labels=['debrief']))
                self.assertNotIn(sentinel, payload['title'] + payload['body'])
            # A real fake-gh GET consumed the read-back reply after the POST.
            self.assertIn('GET', calls[-1]['argv'])
            self.assertIn('repos/hanig/brood-lord/issues/41', calls[-1]['argv'])
            self.assertEqual(json.loads((binary / 'steps').read_text()), [])
            cli('debrief_file.py', 'file', '--workdir', str(work))
            self.assertEqual(len((binary / 'calls').read_text().splitlines()), len(calls))

            plan = root / 'plan.json'
            plan.write_text(json.dumps(dict(name='synthetic', units=[dict(id='fix-a')])))
            record = root / 'debrief/triage' / (packet['packet_id'] + '.json')
            record.parent.mkdir(parents=True)
            record.write_text(json.dumps(dict(schema_version=S.TRIAGE_SCHEMA,
                packet_id=packet['packet_id'], packet_digest=T.packet_digest(packet),
                issue=dict(repository=S.REPOSITORY, number=41), dispositions={
                    row['id']: dict(disposition='accept', unit_refs=['fix-a'])
                    for row in packet['learnings']})))
            self.assertEqual(cli('debrief_triage.py', 'validate', '--record', str(record),
                '--packet', str(work / 'packet.json'), '--plan', str(plan)), 'triage valid\n')


if __name__ == '__main__':
    unittest.main()
