"""Synthetic, isolated adapter and mutation acceptance tests."""
import ast
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / 'skills/hanig-debrief/scripts'
sys.path.insert(0, str(SCRIPTS))
import debrief_extract as X
import debrief_packet as P

class TestExtract(unittest.TestCase):
  def setUp(self):
    self.tmp = tempfile.TemporaryDirectory()
    self.addCleanup(self.tmp.cleanup)
    self.base = Path(self.tmp.name).resolve()
    self.root = self.base / 'project'
    self.root.mkdir()

  def put(self, locator, content, root=None):
    path = (root or self.root) / locator
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content if isinstance(content, bytes) else content.encode())
    return path

  def collect(self, role='project', **budgets):
    got = X.collect([dict(label='A', role=role, path=self.root)], budgets)
    P.validate_dossier(dict(got, schema_version='debrief_dossier.v1', candidates=[]))
    return got

  def records(self, result, adapter):
    return [r for r in result['evidence'].values() if r['adapter']['name'] == adapter]

  def cohort(self, result, population):
    return next(c for c in result['cohorts'].values() if c['population'] == population)

  def receipt(self, unit='u', attempt='a' * 16):
    return dict(schema_version=1, task_id=unit, attempt_id=attempt,
      kind='code', checked_at='time', job_id=None, state='DONE',
      exit_code=0, notes=[], outputs={}, basis={})

  def test_source_coverage(self):
    self.put('CLAUDE.md', 'guidance\n')
    self.put('AGENTS.md', 'guidance\n')
    self.put('old-pitfall.md', 'lesson\n')
    self.put('PLAN.md', '|  D-1  | decision |\nD-2.x_a | decision |\n'
      '| d-3 | no |\n## NEEDS YOU\n- decide\n## Done\n- no\n')
    self.put('u/' + 'a' * 16 + '/evidence.md', 'STOP\nREVIEW_FAIL\n'
      'REVIEW_UNAVAILABLE\nPROPOSED DECISION\n')
    self.put('u/' + 'a' * 16 + '/SUMMARY.md', 'STOP\n')
    got = self.collect()
    alias = self.records(got, 'guidance-alias')
    self.assertEqual(len(alias), 1)
    self.assertEqual(alias[0]['locator'], 'CLAUDE.md')
    self.assertEqual([r['location'] for r in self.records(got, 'decision')],
      [{'line_range': [i, i]} for i in (1, 2, 5)])
    stop = self.records(got, 'marker:STOP')
    self.assertEqual(len(stop), 2)
    self.assertTrue(all(r['unit_id'] == 'u' and r['attempt_id'] == 'a' * 16 for r in stop))
    self.assertEqual(len(self.cohort(got, 'evidence_documents')['members']), 5)
    expected = {'source_root', 'locator', 'sha256', 'location', 'unit_id',
      'attempt_id', 'adapter', 'status'}
    for record in got['evidence'].values():
      self.assertEqual(set(record), expected)
      self.assertEqual(record['sha256'], hashlib.sha256(
      (self.root / record['locator']).read_bytes()).hexdigest())
      self.assertEqual(record['adapter']['version'], '1')
    self.assertEqual(got, self.collect())
    self.assertNotIn('mechanism_basis', json.dumps(got))

  def test_state_and_receipt_source_coverage(self):
    aid = 'a' * 16
    state = dict(schema_version=1, halted=None, units={
      'u/~': dict(state='RUNNING', attempt_dir='/synthetic/u/' + aid,
      attempts=['/synthetic/u/' + aid, 'dry-attempt-/synthetic/u/' + aid], gpu_hours=0)})
    self.put('swarm-state.json', json.dumps(state))
    self.put('receipt.json', json.dumps(self.receipt('u/~')))
    self.put('unit.json', json.dumps(dict(schema_version=1, task_id='u/~',
      attempt_id=aid, kind='code', command='never execute',
      declared_outputs=['x'], pinned_inputs={}, budget={})))
    got = self.collect('state')
    self.assertTrue(self.records(got, 'state'))
    self.assertEqual(len(self.cohort(got, 'attempts')['members']), 1)
    self.assertEqual(self.records(got, 'state')[0]['location'],
      {'json_pointer': '/units/u~1~0/state'})
    self.assertIn({'json_pointer': '/units/u~1~0/attempts/0'},
      [r['location'] for r in self.records(got, 'attempt')])
    self.assertEqual({r['location']['json_pointer'] for r in self.records(got, 'receipt')},
      {'/' + k for k in self.receipt()})
    self.assertEqual(len(self.cohort(got, 'units')['members']), 1)
    # Wrong version, duplicate keys and malformed known fields are unsupported.
    for value in ('{"schema_version":2}', '{"schema_version":1,"schema_version":1}',
      json.dumps(dict(self.receipt(), notes='wrong'))):
      self.put('receipt.json', value)
      self.assertTrue(any(o['locator'] == 'receipt.json' and
      o['reason'].startswith('unsupported') for o in self.collect('state')['omissions']))

  def test_memory_and_helper_source_coverage(self):
    self.put('lesson.md', '---\nname: lesson\ndescription: description\n'
      'metadata:\n  type: feedback\n---\nbody\nsecond line\n')
    got = self.collect('memory')
    self.assertEqual([(r['adapter']['name'], r['location']) for r in got['evidence'].values()],
      [('memory:name', {'line_range': [2, 2]}),
      ('memory:description', {'line_range': [3, 3]}),
      ('memory:metadata.type', {'line_range': [5, 5]}),
      ('memory:body', {'line_range': [7, 8]})])
    self.put('bad.py', b'raise RuntimeError("MUST NEVER EXECUTE")\n\xff')
    got = self.collect('helpers')
    helper = next(r for r in got['evidence'].values() if r['locator'] == 'bad.py')
    self.assertEqual(helper['adapter']['name'], 'helper')
    self.assertNotIn('size', helper)
    self.assertNotIn('MUST NEVER EXECUTE', json.dumps(got))

  def test_provenance_source_coverage(self):
    def marker(skill, mode):
      dest = self.root / skill
      data = dict(schema='2', repo='multi-agent-skills', origin='authored',
      source_version='version', version='version', destination=str(dest),
      consumers='claude', mode=mode, installed_at='time',
      link_target='/not-opened' if mode == 'link' else '',
      link_identity='1:2' if mode == 'link' else '')
      return ''.join(k + '=' + v + '\n' for k, v in data.items())
    copy = marker('hanig-one', 'copy')
    link = marker('hanig-two', 'link')
    self.put('hanig-one/' + X.COPY_MARKER, copy)
    digest = hashlib.sha256(os.fsencode(str(self.root / 'hanig-two'))).hexdigest()[:24]
    self.put(X.SIDECARS + '/hanig-two-' + digest + '.provenance', link)
    self.put('hanig-bad/' + X.COPY_MARKER, 'nonsense')
    got = self.collect()
    self.assertEqual(got['provenance'], {'hanig-one': copy, 'hanig-two': link})
    self.assertTrue(any(o['locator'].startswith('hanig-bad') for o in got['omissions']))

  def test_collection_boundary_symlink(self):
    outside = self.base / 'outside'
    outside.mkdir(exist_ok=True)
    self.put('CLAUDE.md', 'outside', outside)
    for name, target in [('CLAUDE.md', outside / 'CLAUDE.md'), ('escape', outside)]:
      path = self.root / name
      if path.is_symlink():
        path.unlink()
      path.symlink_to(target)
    got = self.collect()
    self.assertFalse(got['evidence'])
    self.assertEqual({o['locator'] for o in got['omissions']}, {'CLAUDE.md', 'escape'})

  def test_collection_boundary_caps(self):
    self.put('CLAUDE.md', '12345')
    got = self.collect(per_file_bytes=4)
    self.assertFalse(got['evidence'])
    self.assertTrue(any(o['locator'] == 'CLAUDE.md' and 'per-file' in o['reason']
      for o in got['omissions']))
    self.put('AGENTS.md', 'tiny')
    got = self.collect(files_per_root=1)
    self.assertEqual(len(got['evidence']), 1)
    self.assertTrue(any(o['locator'] == '.' and 'budget' in o['reason'] for o in got['omissions']))
    got = self.collect(total_bytes=4)
    self.assertEqual(len(got['evidence']), 1)
    self.assertTrue(any('limit reached' in o['reason'] for o in got['omissions']))

  def test_collection_boundary_unstable(self):
    path = self.put('CLAUDE.md', 'first')
    original = X._read_file
    def changing(*args):
      path.write_text('changed')
      return original(*args)
    with mock.patch.object(X, '_read_file', side_effect=changing):
      got = self.collect()
    self.assertFalse(got['evidence'])
    self.assertTrue(any(o['reason'].startswith('unstable') for o in got['omissions']))

  def test_collection_boundary_large_plan_and_no_writes(self):
    data = b'{"units":[]}'
    self.put('plan.json', data + b' ' * (8 * 1024 * 1024 - len(data)))
    original = os.open
    def read_only(path, flags, *args, **kwargs):
      self.assertFalse(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC))
      return original(path, flags, *args, **kwargs)
    with mock.patch.object(X.os, 'open', side_effect=read_only):
      got = self.collect()
    self.assertEqual(self.records(got, 'plan')[0]['location'], {'json_pointer': '/units'})
    self.assertTrue(any('limit reached' in o['reason'] for o in got['omissions']))

  def test_exclusions_and_invalid_roots(self):
    self.put('.git/CLAUDE.md', 'never read')
    self.put('transcript.jsonl', 'never read')
    self.put('unknown.bin', b'\xff')
    with mock.patch.object(X, '_read_file', side_effect=AssertionError('excluded read')):
      got = self.collect()
    self.assertFalse(got['evidence'])
    self.assertEqual({o['locator'] for o in got['omissions']}, {'.git', 'transcript.jsonl', 'unknown.bin'})
    with mock.patch.dict(os.environ, {'HOME': str(self.root)}):
      with mock.patch.object(X, '_open_root', side_effect=AssertionError('root opened')):
        self.assertEqual(self.collect()['omissions'][0]['locator'], '.')
    with self.assertRaises(ValueError):
      self.collect(per_file_bytes=True)

  def test_cohort_recount(self):
    for name, unit in [('r1', 'u'), ('r2', 'u'), ('r3', 'v')]:
      self.put(name + '/receipt.json', json.dumps(self.receipt(unit)))
    got = self.collect('state')
    self.assertEqual(self.cohort(got, 'units')['members'], ['["A","u"]', '["A","v"]'])
    self.assertEqual(len(self.cohort(got, 'evidence_documents')['members']), 3)
    self.put('u/' + 'a' * 16 + '/evidence.md', 'STOP')
    self.put('v/' + 'a' * 16 + '/evidence.md', 'STOP')
    got = self.collect('state')
    self.assertEqual(len(self.cohort(got, 'units')['members']), 2)
    self.assertEqual(len(self.cohort(got, 'evidence_documents')['members']), 5)

  def test_marker_exact_untrimmed(self):
    self.put('evidence.md', 'STOP\n STOP\nSTOP \nREVIEW_FAIL\n REVIEW_FAIL\n'
      'REVIEW_UNAVAILABLE\nPROPOSED DECISION\nPROPOSED DECISION \n')
    got = self.collect()
    self.assertEqual([r['location'] for r in got['evidence'].values()],
      [{'line_range': [i, i]} for i in (1, 4, 6, 7)])

  def test_no_network_or_execution_imports(self):
    tree = ast.parse((SCRIPTS / 'debrief_extract.py').read_text())
    imports = set()
    for node in ast.walk(tree):
      if isinstance(node, ast.Import):
        imports.update(a.name.split('.')[0] for a in node.names)
      elif isinstance(node, ast.ImportFrom):
        imports.add(node.module.split('.')[0])
    self.assertFalse(imports & {'socket', 'ssl', 'http', 'urllib', 'ftplib',
      'smtplib', 'asyncio', 'subprocess', 'importlib', 'runpy'})
    self.assertFalse(any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
      and n.func.id in ('exec', 'eval', '__import__') for n in ast.walk(tree)))

  def test_required_mutations(self):
    source = (SCRIPTS / 'debrief_extract.py').read_text()
    mutations = [
      ('symlink', [('if stat.S_ISLNK(before.st_mode):', 'if False:'),
      ('follow_symlinks=False', 'follow_symlinks=True'),
      (' | os.O_NOFOLLOW', '')], 'test_collection_boundary_symlink'),
      ('omission', [("omit(label, locator, 'over-budget: per-file bytes')", 'pass')],
      'test_collection_boundary_caps'),
      ('receipts', [('ids = sorted(set(members))', 'ids = sorted(members)')], 'test_cohort_recount'),
      ('dry', [("aid = None", "raise ValueError('unsupported')")], 'test_state_and_receipt_source_coverage'),
      ('table', [(r'(?:\|[ \t]*)?D-[A-Za-z0-9._-]+[ \t]*\|', r'\| ?D-[A-Za-z0-9._-]+ ?\|')], 'test_source_coverage'),
      ('unsupported', [('if not known:', 'if False:')], 'test_exclusions_and_invalid_roots'),
      ('strip', [('if line in MARKERS', 'if line.strip() in MARKERS')], 'test_marker_exact_untrimmed')]
    for name, replacements, test in mutations:
      with self.subTest(mutation=name):
        changed = source
        for before, after in replacements:
          self.assertIn(before, changed)
          changed = changed.replace(before, after)
        mutant = types.ModuleType('debrief_extract_mutant')
        exec(compile(changed, '<synthetic mutation>', 'exec'), mutant.__dict__)
        case = TestExtract(test)
        case.setUp()
        try:
          with mock.patch.dict(globals(), {'X': mutant}):
            with self.assertRaises((AssertionError, P.S.SchemaError)):
              getattr(case, test)()
        finally:
          case.doCleanups()

if __name__ == '__main__':
  unittest.main()
