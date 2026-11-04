"""Synthetic, isolated adapter and mutation acceptance tests."""
import ast
import hashlib
import itertools
import stat
import json
import os
import random
import string
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
    self.put('PLAN.md', '|  D-1  | decision |\n| D-2.x_a | decision |\n'
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

  def test_marker_whole_lines_and_whitespace(self):
    self.put('evidence.md', 'STOP\n STOP\nSTOP \nREVIEW_FAIL\n REVIEW_FAIL\n'
  'REVIEW_UNAVAILABLE\nPROPOSED DECISION\nPROPOSED DECISION \n')
    got = self.collect()
    self.eq(self.locations(got),
  [span(i) for i in range(1, 9)])

  def test_marker_worker_shapes(self):
    lines = ['- REVIEW_FAIL in implementation round 1: three reviewers completed.',
      'REVIEW_FAIL (exit 1), all four reviewers completed',
      'STOP reason: contract conflict', '## REVIEW_FAIL and recovery boundary',
      '```text', 'the STOP rule', '```', '- PROPOSED DECISION: retry',
      'status: REVIEW_UNAVAILABLE; retry later']
    expected = [(i, token) for i, token in ((1, 'REVIEW_FAIL'),
      (2, 'REVIEW_FAIL'), (3, 'STOP'), (4, 'REVIEW_FAIL'), (6, 'STOP'),
      (8, 'PROPOSED DECISION'), (9, 'REVIEW_UNAVAILABLE'))]
    for name in ('evidence.md', 'SUMMARY.md'):
      self.put(name, '\n'.join(lines))
    for role in ('project', 'state', 'helpers'):
      with self.subTest(role=role):
        got = self.collect(role)
        self.eq([(r['locator'], r['location'], r['adapter']['name'])
          for r in got['evidence'].values()],
          [(name, span(i), 'marker:' + token) for name in ('SUMMARY.md', 'evidence.md')
           for i, token in expected])
        self.false(got['omissions'])

  def test_marker_helper_shebang_preserved(self):
    for name in ('evidence.md', 'SUMMARY.md'):
      for raw, marker in ((b'#!/bin/sh\necho hello\n', False),
                          (b'#!/bin/sh\n# STOP reason\n', True),
                          (b'#!/bin/sh\n# \xff\n', False)):
        with self.subTest(name=name, raw=raw):
          path = self.put(name, raw)
          got = self.collect('helpers')
          self.eq(len(self.records(got, 'helper')), 1)
          self.eq(self.locations(got, 'marker:STOP'), [span(2)] if marker else [])
          self.eq(len(self.members(got, 'helper_scripts')), 1)
          self.false(got['omissions'])
          path.unlink()

  def test_marker_boundary_refusals(self):
    lines = ['STOPPED', 'review_fail', 'XREVIEW_FAIL', 'REVIEW_FAILED',
      'PROPOSED  DECISION', '_STOP', '1STOP', 'aSTOP', 'STOP_', 'STOP9',
      'REVIEW_UNAVAILABLEz', 'PROPOSED\tDECISION', 'PROPOSED\nDECISION',
      'stop', 'review_unavailable', 'proposed decision']
    self.put('evidence.md', '\n'.join(lines))
    self.false(self.collect()['evidence'])

  def test_marker_unique_line_token(self):
    self.put('evidence.md', 'STOP STOP REVIEW_FAIL STOP REVIEW_FAIL\n'
      'REVIEW_FAIL/REVIEW_UNAVAILABLE/PROPOSED DECISION/STOP\nSTOP')
    got = self.collect()
    self.eq([(r['location'], r['adapter']['name']) for r in got['evidence'].values()],
      [(span(i), 'marker:' + token) for i, token in ((1, 'STOP'),
       (1, 'REVIEW_FAIL'), (2, 'REVIEW_FAIL'), (2, 'REVIEW_UNAVAILABLE'),
       (2, 'PROPOSED DECISION'), (2, 'STOP'), (3, 'STOP'))])

  def test_marker_no_excerpts(self):
    lines = ['PRIVATE_SYNTHETIC_A: STOP because PRIVATE_SYNTHETIC_B',
      'PRIVATE_SYNTHETIC_C: REVIEW_FAIL and REVIEW_UNAVAILABLE',
      'PRIVATE_SYNTHETIC_D: PROPOSED DECISION then STOP']
    for name in ('evidence.md', 'SUMMARY.md'):
      self.put(name, '\n'.join(lines))
    for role in ('project', 'state', 'helpers'):
      dossier = self.collect(role)
      self.eq(len(dossier['evidence']), 10)
      serialized = json.dumps(dossier)
      for line in lines:
        self.assertNotIn(line, serialized)
      self.assertNotIn('PRIVATE_SYNTHETIC', serialized)
      for r in dossier['evidence'].values():
        self.eq(set(r), set('source_root locator sha256 location unit_id attempt_id adapter status'.split()))

  def test_marker_seeded_oracle(self):
    # Fixed seed, 600 lines, 1..5 insertions, 0..16 prefix/suffix characters.
    # ASCII identifier boundaries are deliberate: Unicode letters are delimiters.
    rng = random.Random(20261103)
    identifiers = string.ascii_letters + string.digits + '_'
    alphabet = identifiers + ' -.:/()[]`\t\r\v\f\x85éΩ９'
    tokens = ('STOP', 'REVIEW_FAIL', 'REVIEW_UNAVAILABLE', 'PROPOSED DECISION')
    choices = tokens + ('STOPPED', 'review_fail', 'XREVIEW_FAIL',
      'REVIEW_FAILED', 'PROPOSED  DECISION')
    lines = []
    for _ in range(600):
      parts = []
      for _ in range(rng.randint(1, 5)):
        parts.extend((''.join(rng.choice(alphabet) for _ in range(rng.randint(0, 16))),
          rng.choice(choices),
          ''.join(rng.choice(alphabet) for _ in range(rng.randint(0, 16)))))
      lines.append(''.join(parts))
    expected = set()
    # Independent literal substring scan: no regex or production constants.
    for i, line in enumerate(lines, 1):
      for token in tokens:
        for start in range(len(line)):
          end = start + len(token)
          if (line[start:end] == token
              and (start == 0 or line[start - 1] not in identifiers)
              and (end == len(line) or line[end] not in identifiers)):
            expected.add((i, 'marker:' + token))
    self.true(expected)
    for name in ('evidence.md', 'SUMMARY.md'):
      self.put(name, '\n'.join(lines))
    for role in ('project', 'state', 'helpers'):
      got = self.collect(role)
      actual = [(r['locator'], r['location']['line_range'][0], r['adapter']['name'])
        for r in got['evidence'].values()]
      self.eq(len(actual), len(set(actual)))
      self.eq(set(actual), {(name, i, tag) for name in ('evidence.md', 'SUMMARY.md')
        for i, tag in expected})
      self.true(all(r['location']['line_range'][0] == r['location']['line_range'][1]
        for r in got['evidence'].values()))

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
  [span(1), span(2), span(3)])
      self.put('CLAUDE.md', 'a' + separator + 'b\n')
      self.eq(self.records(self.collect(), 'guidance')[0]['location'],
  span(1))

  def test_depth_budget(self):
    self.put('/'.join(['d'] * 65) + '/CLAUDE.md', 'guidance\n')
    self.eq(len(self.collect(max_depth=65)['evidence']), 1)
    got = self.collect(max_depth=2)
    self.false(got['evidence'])
    self.eq(got['omissions'], [dict(source_root='A', locator='d/d/d',
  reason='over-budget: max_depth'), dict(source_root='A', locator='d/d',
  reason='limit reached: max_depth')])

  def test_single_symlink_omission(self):
    (self.root / 'link.md').symlink_to('missing')
    self.eq(self.collect()['omissions'], [dict(source_root='A',
  locator='link.md', reason='skipped: symlink')])

  def test_round1_entry_budget(self):
    self.put('CLAUDE.md', 'guidance\n')
    for i in range(10001):
      (self.root / str(i)).mkdir()
    self.put('z/CLAUDE.md', 'late guidance\n')
    got = self.collect()
    self.eq(len(got['evidence']), 2)
    self.false(got['omissions'])

  def test_round1_memory_pitfall(self):
    for name in ('network-pitfalls.md', 'pitfall.txt', 'pitfall'):
      path = self.put(name, '---\nname: lesson\ndescription: lesson\n'
        'metadata:\n  type: feedback\n---\nBody.\n')
      got = self.collect('memory')
      self.eq({r['adapter']['name'] for r in got['evidence'].values()},
        {'guidance', 'memory:name', 'memory:description', 'memory:metadata.type', 'memory:body'})
      self.eq(self.locations(got, 'memory:body'), [span(7)])
      self.eq(self.locations(got, 'guidance'), [span(1, 7)])
      self.eq(len(self.members(got, 'evidence_documents')), 1)
      path.unlink()

  def test_round1_inline_metadata(self):
    self.put('lesson.md', '---\nname: lesson\ndescription: lesson\n'
      'metadata: {type: feedback}\n---\nBody.\n')
    got = self.collect('memory')
    self.false(got['evidence'])
    self.eq(len(got['omissions']), 1)
    self.eq(got['omissions'][0]['locator'], 'lesson.md')
    self.true(got['omissions'][0]['reason'].startswith('unsupported'))

  def test_memory_guidance_alias_keeps_memory(self):
    for name in ('CLAUDE.md', 'AGENTS.md'):
      self.put(name, '---\nname: lesson\ndescription: lesson\n'
        'metadata:\n  type: feedback\n---\nBody.\n')
    got = self.collect('memory')
    self.eq(len(self.records(got, 'memory:name')), 2)
    self.eq(len(self.records(got, 'guidance-alias')), 1)
    self.eq(len(self.members(got, 'evidence_documents')), 1)

  def test_round1_indented_table(self):
    self.put('PLAN.md', '| Decision | Text |\n| --- | --- |\n  | D-1 | choose |\n'
      'D-2 | no leading pipe |\n| D-3 | accepted |\n')
    self.eq(self.locations(self.collect(), 'decision'), [span(5)])

  def test_round1_incomplete_state(self):
    self.put('swarm-state.json', json.dumps(dict(schema_version=1, halted=None,
      units={'u': dict(state='DONE', attempts=[])})))
    got = self.collect('state')
    self.false(got['evidence'])
    self.true(any(o['reason'].startswith('unsupported') for o in got['omissions']))
    complete = dict(state='DONE', attempt_dir=None, attempts=[], gpu_hours=0)
    for key in complete:
      item = dict(complete)
      del item[key]
      self.put('swarm-state.json', json.dumps(dict(schema_version=1, halted=None,
        units={'a': complete, 'u': item})))
      got = self.collect('state')
      self.false(got['evidence'])
      self.true(got['omissions'][0]['reason'].startswith('unsupported'))

  def test_round1_plus_bullet(self):
    self.put('PLAN.md', '| D-1 | do X |\n## NEEDS YOU\n+ do Y\n- do Z\n')
    self.eq(self.locations(self.collect(), 'decision'), [span(i) for i in (1, 4)])

  def test_round1_separate_guidance(self):
    for name in ('CLAUDE.md', 'pkg/CLAUDE.md'):
      self.put(name, 'same\n')
    got = self.collect()
    self.eq(len(self.members(got, 'evidence_documents')), 2)
    self.false(self.records(got, 'guidance-alias'))
    self.put('pkg/AGENTS.md', 'same\n')
    got = self.collect()
    self.eq(len(self.members(got, 'evidence_documents')), 2)
    self.eq([r['locator'] for r in self.records(got, 'guidance-alias')], ['pkg/CLAUDE.md'])

  def test_round1_symlink_ancestor(self):
    self.put('CLAUDE.md', 'guidance\n')
    link = self.base / 'link'
    link.symlink_to(self.base, target_is_directory=True)
    got = X.collect([dict(label='A', role='project', path=link / 'project')])
    self.eq(len(got['evidence']), 1)
    self.eq(next(iter(got['evidence'].values()))['source_root'], 'A')
    self.put('outside/CLAUDE.md', 'outside', self.base)
    (self.root / 'escape').symlink_to(self.base / 'outside', target_is_directory=True)
    got = X.collect([dict(label='A', role='project', path=link / 'project')])
    self.eq(len(got['evidence']), 1)
    self.eq(got['omissions'], [dict(source_root='A', locator='escape', reason='skipped: symlink')])

  def test_resolved_excluded_roots(self):
    self.put('.git/CLAUDE.md', 'excluded')
    for target in (self.root / '.git', self.root):
      link = self.base / 'alias'
      if link.is_symlink():
        link.unlink()
      link.symlink_to(target, target_is_directory=True)
      with mock.patch.dict(os.environ, {'HOME': str(self.root)}):
        with mock.patch.object(X, '_open_root', side_effect=AssertionError('excluded read')):
          got = X.collect([dict(label='A', role='project', path=link)])
      self.false(got['evidence'])
      self.eq(got['omissions'][0]['reason'], 'skipped: forbidden root')

  def test_required_mutations(self):
    source = (SCRIPTS / 'debrief_extract.py').read_text()
    mutations = [
  ('memory scalar', [('_memory_scalar(value) and field not in seen', 'field not in seen')], 'test_d_multiline_memory'),
  ('memory indent', [('_need(width == indent)', '_need(width >= indent)')], 'test_e_memory_line_shapes'),
  ('memory ignored line', [("line = lines[i]\n      if line == 'metadata:':", "line = lines[i]\n      if line == 'unknown: value':\n        continue\n      if line == 'metadata:':")], 'test_e_memory_line_shapes'),
  ('relative', [("path = Path(os.path.realpath(root['path']))", "if not Path(root['path']).is_absolute():\n        omit(label, '.', 'skipped')\n        continue\n      path = Path(os.path.realpath(root['path']))")], 'test_d_relative_roots'),
  ('closing pipe', [(r'[ \t]*(?:\||$)', r'[ \t]*\|')], 'test_d_open_table_cell'),
  ('bullet spaces', [(r'[-*] [ \t]*\S', r'[-*] \S'),
    (r'[-*] [ \t]*NEEDS YOU', r'[-*] NEEDS YOU')], 'test_d_bullet_spacing'),
  ('helper selection', [('_helper_selected(name, data):', '_helper_selected(name, data):\n  return True')], 'test_d_helpers'),
  ('entries', [("visited >= max_entries", "visited >= PER_ROOT_FILES")], 'test_round1_entry_budget'),
  ('directories', [("visited = 0", "visited = 0; dirs = 0"),
    ("if len(parts) > max_depth:", "dirs += 1\n          if dirs > PER_ROOT_FILES or len(parts) > max_depth:")], 'test_round1_entry_budget'),
  ('memory selection', [("if guidance and role != 'memory':", 'if guidance:')], 'test_round1_memory_pitfall'),
  ('memory suffix', [("if guidance and role != 'memory':", "if guidance and not (role == 'memory' and name.endswith('.md')):")], 'test_round1_memory_pitfall'),
  ('state shape', [("  or not {'state', 'attempt_dir', 'attempts', 'gpu_hours'} <= item.keys()\n  or item['attempt_dir'] is not None and not _text(item['attempt_dir'])\n  or type(item['gpu_hours']) not in (int, float)\n", '')], 'test_round1_incomplete_state'),
  ('alias directory', [("_identity(label, parts[:-1], 'guidance', digest)", "_identity(label, 'guidance', digest)")], 'test_round1_separate_guidance'),
  ('alias memory', [("'guidance-alias' if tag == 'guidance' else tag", "'guidance-alias'")], 'test_memory_guidance_alias_keeps_memory'),
  ('root resolution', [("path = Path(os.path.realpath(root['path']))", "path = Path(root['path'])")], 'test_round1_symlink_ancestor'),
  ('resolved exclusions', [("if '.git' in path.parts or path == Path(os.path.realpath(os.path.expanduser('~'))) or path == Path('/'):", 'if False:')], 'test_resolved_excluded_roots'),
  ('table grammar', [(r'\|[ \t]*D-[A-Za-z0-9._-]+[ \t]*(?:\||$)', r'(?:\|[ \t]*)?D-[A-Za-z0-9._-]+[ \t]*\|')], 'test_round1_indented_table'),
  ('symlink', [('if stat.S_ISLNK(st.st_mode):', 'if False:'),
  ('follow_symlinks=False', 'follow_symlinks=True'),
  (' | os.O_NOFOLLOW', '')], 'test_boundary_symlink'),
  ('omission', [("omit(label, loc, 'over-budget: per-file bytes')", 'pass')],
  'test_boundary_caps'),
  ('receipts', [('ids = sorted(set(members))', 'ids = sorted(members)')], 'test_cohort_recount'),
  ('dry', [("aid = None", "raise ValueError('unsupported')")], 'test_state_source_coverage'),
  ('table', [(r'\|[ \t]*D-[A-Za-z0-9._-]+[ \t]*(?:\||$)', r'\| ?D-[A-Za-z0-9._-]+ ?\|')], 'test_source_coverage'),
  ('unsupported', [('if not known:', 'if False:')], 'test_exclusions'),
  ('whole-line markers', [("return [('marker:' + token, i, i) for i, line in enumerate(lines, 1)\n      for token in dict.fromkeys(MARKER_PATTERN.findall(line))]",
    "return [('marker:' + line, i, i) for i, line in enumerate(lines, 1) if line in MARKERS]")], 'test_marker_worker_shapes'),
  ('marker case', [("+ r')(?![A-Za-z0-9_])')", "+ r')(?![A-Za-z0-9_])', re.IGNORECASE)")], 'test_marker_boundary_refusals'),
  ('marker excerpt', [("'marker:' + token, i, i", "'marker:' + line, i, i")], 'test_marker_no_excerpts'),
  ('marker left boundary', [(r'(?<![A-Za-z0-9_])', '')], 'test_marker_boundary_refusals'),
  ('marker helper bypass', [("if role == 'helpers' and (name not in ('evidence.md', 'SUMMARY.md')\n        or data.startswith(b'#!')):", "if role == 'helpers':")], 'test_marker_worker_shapes'),
  ('marker helper preservation', [("or data.startswith(b'#!')):", "or False):")], 'test_marker_helper_shebang_preserved'),
  ('marker duplicates', [('dict.fromkeys(MARKER_PATTERN.findall(line))', 'MARKER_PATTERN.findall(line)')], 'test_marker_unique_line_token')]
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

  def test_d_relative_roots(self):
    self.put('CLAUDE.md', 'guidance')
    cwd = os.getcwd()
    try:
      os.chdir(self.base)
      for path in ('project', './project', 'project/../project'):
        self.eq(len(X.collect([dict(label='A', role='project', path=path)])['evidence']), 1)
    finally:
      os.chdir(cwd)

  def test_d_multiline_memory(self):
    for fields in ('name: |\n  title\ndescription: desc',
      'name: lesson\ndescription: desc\n  continuation',
      'name: &a lesson\ndescription: desc', 'name: [lesson]\ndescription: desc'):
      with self.subTest(fields=fields):
        self.put('lesson.md', '---\n' + fields + '\nmetadata:\n  type: feedback\n---\nbody\n')
        got = self.collect('memory')
        self.false(got['evidence'])
        self.true(got['omissions'][0]['reason'].startswith('unsupported'))

  def test_e_memory_line_shapes(self):
    prefix = '---\nname: lesson\ndescription: desc\nmetadata:\n  type: feedback\n'
    for line in ('unknown: value', '', '# comment', '  continuation',
      '    extra: value', ' extra: value', '\textra: value', '  extra:',
      'metadata:', 'description: duplicate', '  type: duplicate'):
      with self.subTest(line=line):
        self.put('lesson.md', prefix + line + '\n---\nbody\n')
        got = self.collect('memory')
        self.false(got['evidence'])
        self.false(got['cohorts'])
        self.eq(got['omissions'], [dict(source_root='A', locator='lesson.md',
          reason='unsupported: format or encoding')])
    for value in ('|', '>+', '&anchor value', '*alias', '[feedback]', '{a: b}',
      '"unclosed', "'unclosed", '!tag value'):
      for key in ('name', 'description', '  type', '  extra'):
        with self.subTest(key=key, value=value):
          fields = {'name': 'lesson', 'description': 'desc', 'metadata': '',
            '  type': 'feedback', '  extra': 'value'}
          fields[key] = value
          self.put('lesson.md', '---\n' + ''.join(k + ':' + (' ' + v if v else '')
            + '\n' for k, v in fields.items()) + '---\nbody\n')
          got = self.collect('memory')
          self.false(got['evidence'])
          self.true(got['omissions'][0]['reason'].startswith('unsupported'))

  def test_e_memory_single_line_citations(self):
    self.put('lesson.md', '---\nname: "[literal]"\ndescription: \'it\'\'s literal\'\n'
      'metadata:\n    type: feedback\n    extra: "&literal"\n---\nbody\n')
    got = self.collect('memory')
    self.eq(self.locations(got), [span(2), span(3), span(5), span(8)])
    self.false(got['omissions'])

  def test_d_open_table_cell(self):
    self.put('PLAN.md', '| ID | Text |\n| --- | --- |\n| D-1\n| D-2  \n')
    self.eq(self.locations(self.collect(), 'decision'), [span(3), span(4)])

  def test_e_review_memory_names(self):
    for name in ('evidence.md', 'SUMMARY.md', 'PLAN.md'):
      path = self.put(name, '---\nname: lesson\ndescription: desc\nmetadata:\n  type: feedback\n---\nSTOP\n| D-1\n')
      got = self.collect('memory')
      self.eq(len(self.records(got, 'memory:name')), 1)
      self.eq(self.locations(got, 'memory:body'), [span(7, 8)])
      self.true(self.records(got, 'decision' if name == 'PLAN.md' else 'marker:STOP'))
      path.unlink()

  def test_e_review_owner(self):
    self.put('CLAUDE.md', 'guidance')
    self.put(' /' + 'a' * 16 + '/evidence.md', 'STOP\n')
    try:
      got = self.collect()
    except ValueError as e:
      self.fail(str(e))
    self.eq(len(got['evidence']), 1)
    self.true(got['omissions'][0]['reason'].startswith('unsupported'))
    with mock.patch.object(X.os, 'scandir') as scan:
      scan.return_value.__enter__.return_value = [types.SimpleNamespace(name='\udcff')]
      self.eq(self.collect()['omissions'][0]['locator'], '.')

  def test_reserved_memory_keeps_independent_project_observations(self):
    for name in ('evidence.md', 'SUMMARY.md', 'PLAN.md'):
      for prefix in ('', '---\nname: "\\uD800"\n---\n',
                     '---\nname: valid\nunknown: invalid\n---\n'):
        with self.subTest(name=name, prefix=prefix):
          raw = prefix + 'STOP\n| D-1 | decide |\n'
          path = self.put(name, raw)
          got = self.collect('memory')
          adapter = 'decision' if name == 'PLAN.md' else 'marker:STOP'
          self.eq(len(self.records(got, adapter)), 1)
          self.false(any(r['adapter']['name'].startswith('memory:')
                         for r in got['evidence'].values()))
          self.true(any('unsupported' in o['reason'] for o in got['omissions']))
          self.eq(path.read_text(), raw)
          path.unlink()

  def test_e_review_surrogate_owner(self):
    # Synthetic directory entry: portable even on filesystems refusing these bytes.
    with mock.patch.object(X.os, 'scandir') as scan:
      scan.return_value.__enter__.return_value = [types.SimpleNamespace(name='\udcff')]
      got = self.collect()
    self.false(got['evidence'])
    self.eq(got['omissions'][0]['locator'], '.')
    self.true(got['omissions'][0]['reason'].startswith('unsupported'))

  def test_e_review_unicode(self):
    for value in (r'"\U00110000"', r'"\uD800"', r'"\U0000DFFF"'):
      self.put('lesson.md', '---\nname: ' + value + '\ndescription: desc\nmetadata:\n  type: feedback\n---\nbody')
      got = self.collect('memory')
      self.false(got['evidence'])
      self.true(got['omissions'][0]['reason'].startswith('unsupported'))

  def test_e_review_nonheading(self):
    self.put('PLAN.md', '## NEEDS YOU\n#not-a-heading\n- approve\n## Done\n- no\n')
    self.eq(self.locations(self.collect(), 'decision'), [span(3)])

  def test_e_review_depth(self):
    self.put('d/CLAUDE.md', 'guidance')
    got = self.collect(max_depth=1)
    self.eq(len(got['evidence']), 1)
    self.true(any('max_depth' in o['reason'] for o in got['omissions']))

  def test_d_bullet_spacing(self):
    self.put('PLAN.md', '*  NEEDS YOU: decide\n## NEEDS YOU\n-  approve\n* \tdecide\n')
    self.eq(self.locations(self.collect(), 'decision'), [span(i) for i in (1, 3, 4)])

  def test_d_helpers(self):
    for name in ('README.md', 'data.csv', 'NOTES', 'state.json'):
      self.put(name, 'documentation')
    self.put('run.sh', 'echo hello')
    self.put('run', '#!/bin/sh\necho hello')
    got = self.collect('helpers')
    self.eq([r['locator'] for r in self.records(got, 'helper')], ['run', 'run.sh'])
    self.eq(len(self.members(got, 'helper_scripts')), 2)
    self.eq({o['locator'] for o in got['omissions']}, {'README.md', 'data.csv', 'NOTES', 'state.json'})
    self.true(all(o['reason'].startswith('unsupported') for o in got['omissions']))


  def test_x_helpers_selected_cap(self):
    self.put('000.txt', 'ordinary text')
    self.put('001.not-a-script', 'ordinary text')
    self.put('PLAN.md', 'not a helper')
    self.put('z.sh', '#!/bin/sh\nexit 0')
    got = self.collect('helpers', files_per_root=1)
    self.eq([r['locator'] for r in got['evidence'].values()], ['z.sh'])
    self.false(any('over-budget' in o['reason'] for o in got['omissions']))
    self.put('zzz', '#!/bin/sh\nexit 0')
    got = self.collect('helpers', files_per_root=2)
    self.eq([r['locator'] for r in got['evidence'].values()], ['z.sh', 'zzz'])
    self.false(any('over-budget' in o['reason'] for o in got['omissions']))

  def test_x_operator_exclusion_overlap(self):
    self.put('.git/PLAN.md', '| D-1 | hidden |')
    self.put('notes.jsonl', '{}')
    got = X.collect([dict(label='A', role='project', path=self.root)],
      exclusions=[('project', '.git'), ('project', '*.jsonl')])
    self.eq(sorted((o['locator'], o['reason']) for o in got['omissions']),
      [('.git', 'skipped: excluded by operator'), ('notes.jsonl', 'skipped: excluded by operator')])
    self.false(got['evidence'])

  def test_x_selected_file_cap(self):
    self.put('000.bin', 'unsupported')
    self.put('PLAN.md', '| D-1 | decision |')
    got = self.collect(files_per_root=1)
    self.eq([r['locator'] for r in got['evidence'].values()], ['PLAN.md'])
    self.false(any('over-budget' in o['reason'] for o in got['omissions']))
    self.put('z/PLAN.md', '| D-2 | next |')
    got = self.collect(files_per_root=1)
    self.true(any(o['reason'] == 'over-budget: uninspected remainder (max-files)'
      for o in got['omissions']))

  def test_x_relative_exclusions_prune(self):
    self.put('.swarm/PLAN.md', '| D-1 | hidden |')
    self.put('PLAN.md', '| D-2 | visible |')
    self.put('skip.bin', 'skip')
    roots = [dict(label='A', role='project', path=self.root)]
    original = X.os.open
    def guarded(name, *args, **kwargs):
      self.assertNotEqual(name, '.swarm', 'excluded directory was opened')
      return original(name, *args, **kwargs)
    with mock.patch.object(X.os, 'open', side_effect=guarded):
      got = X.collect(roots, exclusions=[('project', '.swarm'), ('project', 'skip.*'),
                                        ('memory', 'PLAN.md')])
    self.eq([r['locator'] for r in got['evidence'].values()], ['PLAN.md'])
    self.eq(sorted((o['locator'], o['reason']) for o in got['omissions']),
      [('.swarm', 'skipped: excluded by operator'), ('skip.bin', 'skipped: excluded by operator')])
    # Absolute patterns do not match root-relative paths.
    got = X.collect(roots, exclusions=[('project', str(self.root / 'PLAN.md'))])
    self.eq(len(got['evidence']), 2)

  def test_x_entry_cap_and_per_root_reset(self):
    for i in range(5):
      self.put(str(i) + '.bin', 'unselected')
    roots = [dict(label=l, role='project', path=self.root) for l in ('A', 'B')]
    got = X.collect(roots, {'entries_per_root': 3})
    for label in ('A', 'B'):
      omissions = [o for o in got['omissions'] if o['source_root'] == label]
      self.eq(len(omissions), 4)
      self.eq(omissions[-1]['reason'], 'over-budget: uninspected remainder (max-entries)')
    got = X.collect(roots, {'entries_per_root': 5})
    self.false(any('over-budget' in o['reason'] for o in got['omissions']))

  def test_x_bfs_exhaustive(self):
    # Exhaustive ordered trees, depth <= 2, 0..3 entries per directory.
    # 1 + 81 + 81**2 + 81**3 = 538084 trees; no random seed.
    shapes = [shape for n in range(4) for shape in itertools.product('fdh', repeat=n)]
    variants = [('f', ())] + [(kind, shape) for kind in 'dh' for shape in shapes]
    names = ('z', 'a', 'm')  # deliberately not sorted; h denotes dot-directory
    def entries(shape):
      return [(('.' if kind == 'h' else '') + names[i], kind)
              for i, kind in enumerate(shape)]
    tree, opened = {}, []
    def scan(fd):
      opened.append(fd)
      return Scan(tree[fd])
    class Scan:
      def __init__(self, children):
        self.children = children
      def __enter__(self):
        return (types.SimpleNamespace(name=name) for name, kind in reversed(self.children))
      def __exit__(self, *args):
        pass
    def info(name, dir_fd, **kwargs):
      kind = dict(tree[dir_fd])[name]
      return types.SimpleNamespace(st_mode=stat.S_IFREG if kind == 'f' else stat.S_IFDIR)
    fake = types.SimpleNamespace(dup=lambda fd: fd, close=lambda fd: None,
      open=lambda name, flags, dir_fd: dir_fd + (name,), scandir=scan, stat=info,
      O_RDONLY=0, O_DIRECTORY=0, O_NOFOLLOW=0)
    def unexpected(*args):
      self.fail(str(args))
    total = 0
    with mock.patch.object(X, 'os', fake):
      for size in range(4):
        for arrangement in itertools.product(variants, repeat=size):
          tree.clear()
          tree[()] = entries([kind for kind, children in arrangement])
          for (name, kind), (_, children) in zip(tree[()], arrangement):
            if kind != 'f':
              tree[(name,)] = entries(children)
              for child, child_kind in tree[(name,)]:
                if child_kind != 'f':
                  tree[(name, child)] = []
          # Independent level-by-level oracle partitions files and directory
          # classes explicitly, without the implementation's category key.
          level, expected_files, expected_dirs = [()], [], []
          while level:
            following = []
            for directory in level:
              expected_dirs.append(directory)
              children = tree[directory]
              expected_files.extend(directory + (name,) for name in sorted(
                name for name, kind in children if kind == 'f'))
              for kind in ('d', 'h'):
                following.extend(directory + (name,) for name in sorted(
                  name for name, tag in children if tag == kind))
            level = following
          opened.clear()
          got = [tuple(parts) for fd, parts, st in X._walk((), 100, 128, [], unexpected)]
          self.eq(got, expected_files)
          self.eq(opened, expected_dirs)
          total += 1
    self.eq(total, 538084)

  def test_x_traversal_mutations(self):
    source = (SCRIPTS / 'debrief_extract.py').read_text()
    mutations = [
      ('helper count', 'if not _helper_selected(name, data):', 'if False:',
       'test_x_helpers_selected_cap'),
      ('exclusion overlap', "if any(fnmatch.fnmatchcase(loc, pattern) for pattern in patterns):",
       "if name != '.git' and not name.endswith('.jsonl') and any(fnmatch.fnmatchcase(loc, pattern) for pattern in patterns):",
       'test_x_operator_exclusion_overlap'),
      ('lexical sort', 'key=lambda item: (item[0], item[1])',
       'key=lambda item: item[1]', 'test_x_bfs_exhaustive'),
      ('unselected count', "if not known:\n          omit", "if not known:\n          count += 1\n          omit",
       'test_x_selected_file_cap'),
      ('absolute exclusion', 'fnmatch.fnmatchcase(loc, pattern)',
       "fnmatch.fnmatchcase('/absolute/' + loc, pattern)", 'test_x_relative_exclusions_prune')]
    for label, before, after, test in mutations:
      with self.subTest(mutation=label):
        self.assertIn(before, source)
        mutant = types.ModuleType('debrief_extract_mutant')
        exec(compile(source.replace(before, after), '<synthetic mutation>', 'exec'), mutant.__dict__)
        case = TestExtract(test)
        case.setUp()
        try:
          with mock.patch.dict(globals(), {'X': mutant}):
            with self.assertRaises(AssertionError):
              getattr(case, test)()
        finally:
          case.doCleanups()

if __name__ == '__main__':
  unittest.main()
