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

def span(start, end=None):
  return {'line_range': [start, start if end is None else end]}

class TestExtract(unittest.TestCase):
  eq = unittest.TestCase.assertEqual
  true = unittest.TestCase.assertTrue
  false = unittest.TestCase.assertFalse

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

  def records(self, got, adapter):
    return [r for r in got['evidence'].values() if r['adapter']['name'] == adapter]

  def members(self, got, population):
    return next(c['members'] for c in got['cohorts'].values() if c['population'] == population)

  def locations(self, got, adapter=None):
    rows = self.records(got, adapter) if adapter else got['evidence'].values()
    return [r['location'] for r in rows]

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
    self.eq(len(alias), 1)
    self.eq(alias[0]['locator'], 'CLAUDE.md')
    self.eq(self.locations(got, 'decision'),
  [span(i) for i in (1, 2, 5)])
    stop = self.records(got, 'marker:STOP')
    self.eq(len(stop), 2)
    self.true(all(r['unit_id'] == 'u' and r['attempt_id'] == 'a' * 16 for r in stop))
    self.eq(len(self.members(got, 'evidence_documents')), 5)
    want = set('source_root locator sha256 location unit_id attempt_id adapter status'.split())
    for r in got['evidence'].values():
      self.eq(set(r), want)
      self.eq(r['sha256'], hashlib.sha256(
  (self.root / r['locator']).read_bytes()).hexdigest())
      self.eq(r['adapter']['version'], '1')
    self.eq(got, self.collect())
    self.assertNotIn('mechanism_basis', json.dumps(got))

  def test_state_source_coverage(self):
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
    self.true(self.records(got, 'state'))
    self.eq(len(self.members(got, 'attempts')), 1)
    self.eq(self.records(got, 'state')[0]['location'],
  {'json_pointer': '/units/u~1~0/state'})
    self.assertIn({'json_pointer': '/units/u~1~0/attempts/0'},
  self.locations(got, 'attempt'))
    self.eq({r['location']['json_pointer'] for r in self.records(got, 'receipt')},
  {'/' + k for k in self.receipt()})
    self.eq(len(self.members(got, 'units')), 1)
    for value in ('{"schema_version":2}', '{"schema_version":1,"schema_version":1}',
  json.dumps(dict(self.receipt(), notes='wrong'))):
      self.put('receipt.json', value)
      self.true(any(o['locator'] == 'receipt.json' and
  o['reason'].startswith('unsupported') for o in self.collect('state')['omissions']))

  def test_memory_source_coverage(self):
    self.put('lesson.md', '---\nname: lesson\ndescription: description\n'
  'metadata:\n  type: feedback\n---\nbody\nsecond line\n')
    got = self.collect('memory')
    self.eq([(r['adapter']['name'], r['location']) for r in got['evidence'].values()],
  [('memory:name', span(2)),
  ('memory:description', span(3)),
  ('memory:metadata.type', span(5)),
  ('memory:body', span(7, 8))])
    self.put('bad.py', b'raise RuntimeError("MUST NEVER EXECUTE")\n\xff')
    got = self.collect('helpers')
    helper = next(r for r in got['evidence'].values() if r['locator'] == 'bad.py')
    self.eq(helper['adapter']['name'], 'helper')
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
    self.eq(got['provenance'], {'hanig-one': copy, 'hanig-two': link})
    self.true(any(o['locator'].startswith('hanig-bad') for o in got['omissions']))

  def test_boundary_symlink(self):
    outside = self.base / 'outside'
    outside.mkdir(exist_ok=True)
    self.put('CLAUDE.md', 'outside', outside)
    for name, target in [('CLAUDE.md', outside / 'CLAUDE.md'), ('escape', outside)]:
      path = self.root / name
      if path.is_symlink():
        path.unlink()
      path.symlink_to(target)
    got = self.collect()
    self.false(got['evidence'])
    self.eq({o['locator'] for o in got['omissions']}, {'CLAUDE.md', 'escape'})

  def test_boundary_caps(self):
    self.put('CLAUDE.md', '12345')
    got = self.collect(per_file_bytes=4)
    self.false(got['evidence'])
    self.true(any(o['locator'] == 'CLAUDE.md' and 'per-file' in o['reason']
  for o in got['omissions']))
    self.put('AGENTS.md', 'tiny')
    got = self.collect(files_per_root=1)
    self.eq(len(got['evidence']), 1)
    self.true(any(o['locator'] == '.' and 'budget' in o['reason'] for o in got['omissions']))
    got = self.collect(total_bytes=4)
    self.eq(len(got['evidence']), 1)
    self.true(any('limit reached' in o['reason'] for o in got['omissions']))

  def test_boundary_unstable(self):
    path = self.put('CLAUDE.md', 'first')
    original = X._read_file
    def changing(*args):
      path.write_text('changed')
      return original(*args)
    with mock.patch.object(X, '_read_file', side_effect=changing):
      got = self.collect()
    self.false(got['evidence'])
    self.true(any(o['reason'].startswith('unstable') for o in got['omissions']))

  def test_large_plan_read_only(self):
    data = b'{"units":[]}'
    self.put('plan.json', data + b' ' * (8 * 1024 * 1024 - len(data)))
    original = os.open
    def read_only(path, flags, *args, **kwargs):
      self.false(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC))
      return original(path, flags, *args, **kwargs)
    with mock.patch.object(X.os, 'open', side_effect=read_only):
      got = self.collect()
    self.eq(self.records(got, 'plan')[0]['location'], {'json_pointer': '/units'})
    self.true(any('limit reached' in o['reason'] for o in got['omissions']))

  def test_exclusions(self):
    self.put('.git/CLAUDE.md', 'never read')
    self.put('transcript.jsonl', 'never read')
    self.put('unknown.bin', b'\xff')
    with mock.patch.object(X, '_read_file', side_effect=AssertionError('excluded read')):
      got = self.collect()
    self.false(got['evidence'])
    self.eq({o['locator'] for o in got['omissions']}, {'.git', 'transcript.jsonl', 'unknown.bin'})
    with mock.patch.dict(os.environ, {'HOME': str(self.root)}):
      with mock.patch.object(X, '_open_root', side_effect=AssertionError('root opened')):
        self.eq(self.collect()['omissions'][0]['locator'], '.')
    with self.assertRaises(ValueError):
      self.collect(per_file_bytes=True)

  def test_cohort_recount(self):
    for name, unit in [('r1', 'u'), ('r2', 'u'), ('r3', 'v')]:
      self.put(name + '/receipt.json', json.dumps(self.receipt(unit)))
    got = self.collect('state')
    self.eq(self.members(got, 'units'), ['u', 'v'])
    self.eq(len(self.members(got, 'evidence_documents')), 3)
    self.put('u/' + 'a' * 16 + '/evidence.md', 'STOP')
    self.put('v/' + 'a' * 16 + '/evidence.md', 'STOP')
    got = self.collect('state')
    self.eq(len(self.members(got, 'units')), 2)
    self.eq(len(self.members(got, 'evidence_documents')), 5)

  def test_marker_exact_untrimmed(self):
    self.put('evidence.md', 'STOP\n STOP\nSTOP \nREVIEW_FAIL\n REVIEW_FAIL\n'
  'REVIEW_UNAVAILABLE\nPROPOSED DECISION\nPROPOSED DECISION \n')
    got = self.collect()
    self.eq(self.locations(got),
  [span(i) for i in (1, 4, 6, 7)])

  def test_no_network_or_execution_imports(self):
    tree = ast.parse((SCRIPTS / 'debrief_extract.py').read_text())
    imports = set()
    for node in ast.walk(tree):
      if isinstance(node, ast.Import):
        imports.update(a.name.split('.')[0] for a in node.names)
      elif isinstance(node, ast.ImportFrom):
        imports.add(node.module.split('.')[0])
    self.false(imports & {'socket', 'ssl', 'http', 'urllib', 'ftplib',
  'smtplib', 'asyncio', 'subprocess', 'importlib', 'runpy'})
    self.false(any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
  and n.func.id in ('exec', 'eval', '__import__') for n in ast.walk(tree)))

  def test_duplicate_roots(self):
    self.put('receipt.json', json.dumps(self.receipt()))
    got = X.collect([dict(label=l, role='state', path=self.root) for l in ('A', 'B')])
    self.eq(len(self.members(got, 'units')), 1)
    self.eq(len(self.members(got, 'attempts')), 1)

  def test_fenced_rows(self):
    self.put('PLAN.md', '````text\n| D-123 | example |\n```\n- NEEDS YOU: example\n````\n'
  '  ~~~\n| D-2 | example |\n  ~~~\n| D-3 | actual |\n')
    self.eq(self.records(self.collect(), 'decision')[0]['location'],
  span(9))
    self.eq(len(self.collect()['evidence']), 1)

  def test_memory_indent(self):
    self.put('lesson.md', '---\nname: lesson\ndescription: desc\nmetadata:\n'
  '    other: value\n    type: feedback\n---\nbody\n')
    self.eq(len(self.collect('memory')['evidence']), 4)

  def test_physical_lines(self):
    for separator in ('\r', '\v', '\f', '\x1c', '\x1d', '\x1e', '\x85', '\u2028', '\u2029'):
      self.put('evidence.md', 'prefix' + separator + 'STOP\nSTOP\nSTOP\r\n')
      self.eq(self.locations(self.collect(), 'marker:STOP'),
  [span(2)])
      self.put('CLAUDE.md', 'a' + separator + 'b\n')
      self.eq(self.records(self.collect(), 'guidance')[0]['location'],
  span(1))

  def test_depth_budget(self):
    self.put('/'.join(['d'] * 65) + '/CLAUDE.md', 'guidance\n')
    self.eq(len(self.collect(max_depth=65)['evidence']), 1)
    got = self.collect(max_depth=2)
    self.false(got['evidence'])
    self.eq(got['omissions'], [dict(source_root='A', locator='d/d/d',
  reason='over-budget: directories or max_depth')])

  def test_single_symlink_omission(self):
    (self.root / 'link.md').symlink_to('missing')
    self.eq(self.collect()['omissions'], [dict(source_root='A',
  locator='link.md', reason='skipped: symlink')])

  def test_round1_entry_budget(self):
    self.put('CLAUDE.md', 'guidance\n')
    for i in range(10001):
      (self.root / str(i)).mkdir()
    self.eq(len(self.collect()['evidence']), 1)

  def test_round1_memory_pitfall(self):
    self.put('network-pitfalls.md', '---\nname: lesson\ndescription: lesson\n'
      'metadata:\n  type: feedback\n---\nBody.\n')
    got = self.collect('memory')
    self.eq({r['adapter']['name'] for r in got['evidence'].values()},
      {'guidance', 'memory:name', 'memory:description', 'memory:metadata.type', 'memory:body'})

  def test_round1_inline_metadata(self):
    self.put('lesson.md', '---\nname: lesson\ndescription: lesson\n'
      'metadata: {type: feedback}\n---\nBody.\n')
    self.eq(len(self.collect('memory')['evidence']), 4)

  def test_round1_indented_table(self):
    self.put('PLAN.md', '| Decision | Text |\n| --- | --- |\n  | D-1 | choose |\n')
    self.eq(self.locations(self.collect(), 'decision'), [span(3)])

  def test_round1_incomplete_state(self):
    self.put('swarm-state.json', json.dumps(dict(schema_version=1, halted=None,
      units={'u': dict(state='DONE', attempts=[])})))
    got = self.collect('state')
    self.false(got['evidence'])
    self.true(any(o['reason'].startswith('unsupported') for o in got['omissions']))

  def test_round1_plus_bullet(self):
    self.put('PLAN.md', '| D-1 | do X |\n## NEEDS YOU\n+ do Y\n- do Z\n')
    self.eq(self.locations(self.collect(), 'decision'), [span(i) for i in (1, 3, 4)])

  def test_round1_separate_guidance(self):
    for name in ('CLAUDE.md', 'pkg/CLAUDE.md'):
      self.put(name, 'same\n')
    got = self.collect()
    self.eq(len(self.members(got, 'evidence_documents')), 2)
    self.false(self.records(got, 'guidance-alias'))

  def test_round1_symlink_ancestor(self):
    self.put('CLAUDE.md', 'guidance\n')
    link = self.base / 'link'
    link.symlink_to(self.base, target_is_directory=True)
    got = X.collect([dict(label='A', role='project', path=link / 'project')])
    self.eq(len(got['evidence']), 1)

  def test_required_mutations(self):
    source = (SCRIPTS / 'debrief_extract.py').read_text()
    mutations = [
  ('symlink', [('if stat.S_ISLNK(st.st_mode):', 'if False:'),
  ('follow_symlinks=False', 'follow_symlinks=True'),
  (' | os.O_NOFOLLOW', '')], 'test_boundary_symlink'),
  ('omission', [("omit(label, loc, 'over-budget: per-file bytes')", 'pass')],
  'test_boundary_caps'),
  ('receipts', [('ids = sorted(set(members))', 'ids = sorted(members)')], 'test_cohort_recount'),
  ('dry', [("aid = None", "raise ValueError('unsupported')")], 'test_state_source_coverage'),
  ('table', [(r'(?:\|[ \t]*)?D-[A-Za-z0-9._-]+[ \t]*\|', r'\| ?D-[A-Za-z0-9._-]+ ?\|')], 'test_source_coverage'),
  ('unsupported', [('if not known:', 'if False:')], 'test_exclusions'),
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
