"""Synthetic command-line workflow and private/public boundary regressions."""
import ast
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'skills/hanig-debrief/scripts/debrief.py'
sys.path.insert(0, str(SCRIPT.parent))
import debrief_packet as P
import debrief_schema as S
import debrief_triage as T
from tests.test_debrief_packet import dossier


class TestDebriefCLI(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.work = self.root / 'private'
        self.project = self.root / 'project'
        self.project.mkdir()
        (self.project / 'evidence.md').write_text('STOP\n')
        (self.project / 'unknown.bin').write_bytes(b'synthetic')
        self.env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}

    def cli(self, command, *args, work=None, code=0):
        result = subprocess.run([sys.executable, str(SCRIPT), command,
                                 '--workdir', str(work or self.work)] + list(args),
                                env=self.env, text=True, capture_output=True)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return result.stdout

    def collect(self):
        self.cli('collect', '--root', 'project=' + str(self.project))

    def populate(self):
        self.collect()
        path = self.work / 'dossier.json'
        d = P.load_dossier(path)
        c = dossier()['candidates'][0]
        c.update(counts=[], rationale='Synthetic source assertion.')
        d['candidates'] = [c]
        for status in ('covered', 'unrepresentable', 'rejected'):
            other = copy.deepcopy(c)
            other.update(status=status, rationale='Private ' + status)
            if status == 'unrepresentable':
                other['catalog_entry'] = None
            if status == 'covered':
                other['coverage_disposition'] = 'covered'
            d['candidates'].append(other)
        path.write_text(json.dumps(d))
        return d

    def test_workflow_exact_render_and_private_summary(self):
        self.populate()
        self.cli('classify', '--checkout', str(self.project))
        # Snapshot has no coverage: explicitly retain a synthetic covered item.
        path = self.work / 'dossier.json'
        d = P.load_dossier(path)
        covered = copy.deepcopy(d['candidates'][0])
        covered.update(status='covered', coverage_disposition='covered', rationale='Private covered')
        d['candidates'].append(covered)
        path.write_text(json.dumps(d))
        self.cli('compile')
        packet = P.load_packet(self.work / 'packet.json')
        title, body = P.render(packet)
        shown = self.cli('show')
        public, private = shown.split('PRIVATE SUMMARY (never upload or paste)\n')
        self.assertEqual(public, 'TITLE\n' + title + '\nBODY\n' + body +
                         '\nAPPROVAL DIGEST\n' + P.approval_digest(packet, title) + '\n')
        summary = json.loads(private)
        self.assertEqual(summary['omissions'], d['omissions'])
        self.assertEqual(summary['candidates'], [
            dict(id=i, status=c['status'], reason=c['rationale'])
            for i, c in enumerate(d['candidates']) if c['status'] != 'selected'])
        self.assertEqual({c['status'] for c in summary['candidates']},
                         {'covered', 'unrepresentable', 'rejected'})
        self.assertNotIn('Private covered', public)
        self.assertEqual(self.cli('recount'), 'recount valid\n')

    def test_private_modes_and_collect_preserves_existing_work(self):
        self.collect()
        self.assertEqual(self.work.stat().st_mode & 0o777, 0o700)
        path = self.work / 'dossier.json'
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        original = path.read_bytes()
        self.cli('collect', '--root', 'project=' + str(self.project), code=1)
        self.assertEqual(path.read_bytes(), original)

    def test_refuses_git_worktree_and_symlink_alias_before_creation(self):
        subprocess.run(['git', 'init', '-q', str(self.project)], check=True, env=self.env)
        alias = self.root / 'alias'
        alias.symlink_to(self.project, target_is_directory=True)
        for parent in (self.project, alias):
            target = parent / 'nested' / 'private'
            self.cli('collect', '--root', 'project=' + str(self.project), work=target, code=1)
            self.assertFalse(target.exists())
        linked = self.root / 'linked'
        linked.mkdir()
        (linked / '.git').write_text('gitdir: synthetic\n')
        self.cli('collect', '--root', 'project=' + str(self.project), work=linked, code=1)
        self.assertFalse((linked / 'dossier.json').exists())

    def test_explicit_workdir_required(self):
        result = subprocess.run([sys.executable, str(SCRIPT), 'show'], capture_output=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn(b'--workdir', result.stderr)

    def test_review_and_reproduction_survive_reclassification(self):
        self.populate()
        self.cli('classify', '--checkout', str(self.project))
        reason, probe, observed = (self.root / n for n in ('reason', 'probe', 'observed'))
        for path in (reason, probe, observed):
            path.write_text('Synthetic ' + path.name)
        self.cli('review', '--candidate', '0', '--disposition', 'extends',
                 '--rationale-file', str(reason))
        self.cli('reproduce', '--candidate', '0', '--receipt-ref', 'E001',
                 '--tested-version', 'synthetic-version', '--probe-file', str(probe),
                 '--observed-file', str(observed))
        self.cli('classify', '--checkout', str(self.project))
        c = P.load_dossier(self.work / 'dossier.json')['candidates'][0]
        self.assertEqual(c['coverage_disposition'], 'extends')
        self.assertEqual(c['basis'], 'reproduced')
        self.assertEqual(c['reproduction'], dict(receipt_ref='E001',
            tested_version='synthetic-version', probe='Synthetic probe', observed='Synthetic observed'))
        self.assertIn('Synthetic reason', c['rationale'])

    def test_no_network_or_operator_import(self):
        forbidden = {'socket', 'ssl', 'http', 'urllib', 'ftplib', 'smtplib',
                     'asyncio', 'subprocess', 'debrief_file'}
        for node in ast.walk(ast.parse(SCRIPT.read_text())):
            if isinstance(node, ast.Import):
                names = [alias.name.split('.')[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [(node.module or '').split('.')[0]]
            else:
                continue
            self.assertFalse(forbidden.intersection(names), names)

    def test_worked_example_validates_and_recounts(self):
        example = SCRIPT.parent.parent / 'references/example'
        d = P.load_dossier(example / 'dossier.json')
        packet = P.load_packet(example / 'packet.json')
        P.recount(d, packet)
        T.validate(example / (packet['packet_id'] + '.json'), example / 'packet.json',
                   [example / 'plan.json'])


if __name__ == '__main__':
    unittest.main()
