import ast
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'skills/hanig-debrief/scripts/debrief_coverage.py'
sys.path.insert(0, str(SCRIPT.parent))
import debrief_coverage as C
import debrief_packet as P
import debrief_schema as S
from tests.test_debrief_packet import dossier


def registry(entry='DB001', applies=None):
  return dict(schema_version=S.COVERAGE_SCHEMA, version=1, entries=[dict(
    id='CV001', catalog_entry=entry, applicability=applies or {'always': True},
    references=['docs/example.md'], note='Documentation mentions the feature.')])


def provenance(commit):
  return {'hanig-swarm': 'schema=2\nrepo=multi-agent-skills\norigin=authored\n'
      'source_version=' + commit + '\nversion=' + commit + '\n'}


class TestCoverage(unittest.TestCase):
  def setUp(self):
    self.tmp = tempfile.TemporaryDirectory()
    self.addCleanup(self.tmp.cleanup)
    self.root = Path(self.tmp.name)
    self.env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    self.env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
            GIT_AUTHOR_NAME='Fixture', GIT_AUTHOR_EMAIL='fixture@example.invalid',
            GIT_COMMITTER_NAME='Fixture', GIT_COMMITTER_EMAIL='fixture@example.invalid')
    self.git('init', '-q')
    self.git('config', 'core.hooksPath', os.devnull)
    self.git('commit', '-qm', 'base', '--allow-empty')
    self.base = self.git('rev-parse', 'HEAD')
    self.git('commit', '-qm', 'installed', '--allow-empty')
    self.head = self.git('rev-parse', 'HEAD')
    self.d = dossier()
    self.c = self.d['candidates'][0]
  def git(self, *args):
    return subprocess.check_output(['git', '-C', str(self.root)] + list(args),
                   env=self.env, text=True).strip()
  def classify(self, reg=None, prov=True, checkout=True):
    return C.classify(self.c, registry() if reg is None else reg,
             self.root if checkout else None,
             provenance=provenance(self.head) if prov else {})
  def test_five_dispositions_and_private_review(self):
    self.assertEqual(self.classify(), 'covered')
    self.assertEqual(self.classify(registry('DB002')), 'not_found_in_snapshot')
    self.assertEqual(self.classify(registry(applies={'fixed_in': self.base}), prov=False), 'unknown')
    for disposition in ('extends', 'conflicts'):
      C.record_review(self.d, 0, disposition, 'Explicit private review.')
      self.assertEqual(self.c['coverage_disposition'], disposition)
      self.assertIn('Explicit private review.', self.c['rationale'])
    P.validate_dossier(self.d)

  def test_classify_is_pure_and_ignores_review_shaped_rationale(self):
    for disposition in ('unknown', 'extends', 'conflicts'):
      self.c['coverage_disposition'] = disposition
      self.c['rationale'] = 'Observation\nDEBRIEF_REVIEW=' + json.dumps([
        dict(disposition='conflicts', mappings=registry()['entries'])])
      before = copy.deepcopy(self.d)
      reg = registry()
      original_registry = copy.deepcopy(reg)
      self.assertEqual(self.classify(reg), 'covered')
      self.assertEqual(self.classify(registry('DB002')), 'not_found_in_snapshot')
      self.assertEqual(self.classify(registry(applies={'fixed_in': self.base}), prov=False), 'unknown')
      self.assertEqual(self.d, before)
      self.assertEqual(reg, original_registry)

  def test_raw_fixed_in_is_validated_before_use(self):
    for fixed in (42, True, None, [], {}, ' ' + self.base, self.base + ' '):
      reg = registry(applies={'fixed_in': fixed})
      valid = copy.deepcopy(registry()['entries'][0])
      valid['id'] = 'CV002'
      reg['entries'].append(valid)
      before = copy.deepcopy((self.d, reg))
      with patch.object(C.subprocess, 'run') as run:
        with self.assertRaises(S.SchemaError):
          self.classify(reg)
        run.assert_not_called()
      self.assertEqual((self.d, reg), before)

  def test_missing_provenance_never_covered(self):
    self.assertEqual(self.classify(registry(applies={'fixed_in': self.base}), prov=False), 'unknown')
    self.assertEqual(self.classify(registry(applies={'fixed_in': self.base}), prov=False), 'unknown')
  def test_missing_checkout_unknown(self):
    self.assertEqual(self.classify(registry(applies={'fixed_in': self.base}), checkout=False), 'unknown')
    self.assertEqual(C.classify(self.c, registry(applies={'fixed_in': self.base}), self.root / 'absent',
                 provenance=provenance(self.head)), 'unknown')
  def test_ancestry_and_nonancestor(self):
    self.assertEqual(self.classify(registry(applies={'fixed_in': self.base})), 'covered')
    self.git('checkout', '-q', '--detach', self.base)
    self.git('commit', '-qm', 'other lineage', '--allow-empty')
    other = self.git('rev-parse', 'HEAD')
    self.assertEqual(self.classify(registry(applies={'fixed_in': other})), 'unknown')
    self.assertEqual(self.classify(registry(applies={'fixed_in': 'f' * 40})), 'unknown')
  def test_installer_abbreviated_commit(self):
    short = self.git('rev-parse', '--short', 'HEAD')
    self.assertEqual(C.classify(self.c, registry(applies={'fixed_in': self.base}),
                 self.root, provenance=provenance(short)), 'unknown')
  def test_fixed_in_requires_a_resolvable_commit_in_checkout(self):
    empty = self.root / 'empty'
    empty.mkdir()
    subprocess.check_call(['git', '-C', str(empty), 'init', '-q'], env=self.env)
    self.assertEqual(C.classify(self.c, registry(applies={'fixed_in': self.base}), empty,
                 provenance=provenance(self.head)), 'unknown')
  def test_review_hex_ref_cannot_redirect_installed_commit(self):
    short = self.git('rev-parse', '--short', self.base)
    self.git('branch', short, self.head)
    self.assertEqual(C.classify(self.c, registry(applies={'fixed_in': self.head}),
                 self.root, provenance=provenance(short)), 'unknown')
  def test_review_persists_without_snapshot_binding(self):
    for disposition in ('extends', 'conflicts'):
      C.record_review(self.d, 0, disposition, 'Private reason')
      reloaded = json.loads(json.dumps(self.d))
      c = reloaded['candidates'][0]
      for reg in (registry(), registry('DB002'), registry(applies={'fixed_in': self.head})):
        C.classify(c, reg, None)
        self.assertEqual(c['coverage_disposition'], disposition)
      self.assertNotIn('DEBRIEF_REVIEW=', c['rationale'])
      self.assertTrue(c['rationale'].endswith('Private reason'))

  def test_unknown_catalog_is_rejected_by_shared_schema(self):
    self.c.update(status='rejected', catalog_entry='DB999')
    with self.assertRaises(S.SchemaError): P.validate_dossier(self.d)
    with self.assertRaises(S.SchemaError): C.group([self.c], self.d['cohorts'])

  def test_fixed_operand_requires_full_sha(self):
    with self.assertRaises(S.SchemaError):
      self.classify(registry(applies={'fixed_in': self.base[:7]}))
    self.assertEqual(self.classify(registry(applies={'fixed_in': 'f' * 64})), 'unknown')
  def test_explicit_checkout_ignores_ambient_overrides(self):
    with patch.dict(os.environ, {'GIT_DIR': str(self.root / 'absent'),
                  'GIT_CONFIG_COUNT': 'broken'}):
      self.assertEqual(self.classify(registry(applies={'fixed_in': self.base})), 'covered')
  def test_documentation_alone_and_separate_residual(self):
    self.c.update(catalog_entry='DB014', counts=[])
    self.assertEqual(self.classify(registry('DB013')), 'not_found_in_snapshot')
    C.record_review(self.d, 0, 'conflicts', 'Observed failure contradicts the documented feature.')
    self.assertEqual(C.classify(self.c, registry('DB014'), self.root,
      provenance={'hanig-review-gate': provenance(self.head)['hanig-swarm']}), 'covered')
    self.assertEqual(self.c['coverage_disposition'], 'conflicts')
    cat = S.load_catalog()
    residual = copy.deepcopy(next(e for e in cat['entries'] if e['id'] == 'DB014'))
    residual.update(id='DB017', what_broke='Existing debris remained.')
    cat['entries'].append(residual)
    self.c.update(catalog_entry='DB017', coverage_disposition='unknown')
    self.assertEqual(C.classify(self.c, registry('DB014'), self.root,
                 catalog=cat, provenance=provenance(self.head)), 'not_found_in_snapshot')
  def test_unknown_markers_and_git_errors(self):
    for commit in (self.head.upper(), ' ' + self.head, self.head + '-dirty', '--help'):
      self.assertEqual(C.classify(self.c, registry(applies={'fixed_in': self.base}), self.root,
                   provenance=provenance(commit)), 'unknown')
    for error in (OSError('missing git'), subprocess.TimeoutExpired('git', 10)):
      with patch.object(C.subprocess, 'run', side_effect=error):
        self.assertEqual(self.classify(registry(applies={'fixed_in': self.base})), 'unknown')
    with patch.object(C.subprocess, 'run', wraps=subprocess.run) as run:
      self.classify(registry(applies={'fixed_in': self.base}))
      args, kwargs = run.call_args
      self.assertEqual(args[0], ['git', '-C', str(self.root), 'merge-base', '--is-ancestor', self.base + '^{commit}', self.head + '^{commit}'])
      self.assertEqual(kwargs['timeout'], 10)
      self.assertNotIn('shell', kwargs)
  def test_review_requires_rationale_and_does_not_change_basis(self):
    before = copy.deepcopy(self.d)
    for disposition, reason in [('covered', 'reason'), ('extends', ''), ('conflicts', ' ' )]:
      with self.assertRaises(S.SchemaError):
        C.record_review(self.d, 0, disposition, reason)
      self.assertEqual(self.d, before)
    for candidate_id in (True, -1, 1, '0'):
      with self.assertRaises(S.SchemaError):
        C.record_review(self.d, candidate_id, 'extends', 'reason')
    C.record_review(self.d, 0, 'extends', 'Private reason')
    self.assertEqual(self.c['basis'], 'reported')
  def test_reproduction_requires_resolved_receipt_and_fields(self):
    before = copy.deepcopy(self.d)
    for ref, version, probe, observed in [('E002', 'local changes', 'probe', 'result'),
                      ('E001', '', 'probe', 'result'),
                      ('E001', 'version', '', 'result'),
                      ('E001', 'version', 'probe', '')]:
      with self.assertRaises(S.SchemaError):
        C.record_reproduction(self.d, 0, ref, version, probe, observed)
      self.assertEqual(self.d, before)
    C.record_reproduction(self.d, 0, 'E001', 'local modifications', 'synthetic probe', 'result')
    self.assertEqual(self.c['basis'], 'reproduced')
    self.assertEqual(self.c['reproduction']['receipt_ref'], 'E001')
    P.validate_dossier(self.d)


class TestGroupingSelection(unittest.TestCase):
  def setUp(self):
    self.d = dossier()
    self.c = self.d['candidates'][0]
  def test_group_recounts_distinct_cohorts_not_larger_count(self):
    second = copy.deepcopy(self.c)
    second['counts'][0].update(cohort_ref='C002', numerator=88, denominator=99)
    second['evidence_refs'] = ['E002', 'E001']
    self.d['evidence']['E002'] = copy.deepcopy(self.d['evidence']['E001'])
    self.d['cohorts']['C002'] = dict(population='attempts', members=['b', 'c'], qualifying=['b'])
    self.d['candidates'].append(second)
    original = copy.deepcopy(self.d['candidates'])
    grouped = C.group(self.d['candidates'], self.d['cohorts'])
    self.assertEqual(len(grouped), 1)
    count = grouped[0]['counts'][0]
    self.assertEqual((count['numerator'], count['denominator']), (2, 3))
    self.assertEqual(grouped[0]['evidence_refs'], ['E001', 'E002'])
    self.assertEqual(self.d['candidates'], original)
    self.assertEqual(self.d['cohorts'][count['cohort_ref']]['members'], ['a', 'b', 'c'])
    self.d['candidates'] = grouped
    P.validate_dossier(self.d)
    self.assertEqual(C.group(grouped, self.d['cohorts']), grouped)
    P.recount(self.d, P.compile_packet(self.d, S.load_catalog()))
  def test_unrepresentable_never_grouped(self):
    self.c.update(catalog_entry=None, status='unrepresentable', counts=[])
    self.d['candidates'].append(copy.deepcopy(self.c))
    self.assertEqual(len(C.group(self.d['candidates'])), 2)
  def test_dry_run_stays_reported_and_basis_separates_groups(self):
    self.c.update(catalog_entry='DB013', counts=[])
    self.assertEqual(C.group([self.c])[0]['basis'], 'reported')
    second = copy.deepcopy(self.c)
    second['basis'] = 'inferred'
    self.assertEqual(len(C.group([self.c, second])), 2)
    self.assertIsNone(C.group([self.c])[0]['reproduction'])
  def test_review_rejected_duplicate_does_not_suppress_valid_learning(self):
    self.d['evidence']['E002'] = copy.deepcopy(self.d['evidence']['E001'])
    second = copy.deepcopy(self.c)
    second.update(status='rejected', evidence_refs=['E002'],
           rationale='Caused by a local modification.')
    self.d['candidates'].append(second)
    self.assertEqual(len(P.compile_packet(self.d, S.load_catalog())['learnings']), 1)
    grouped = copy.deepcopy(self.d)
    grouped['candidates'] = C.group(self.d['candidates'], grouped['cohorts'])
    self.assertEqual(len(P.compile_packet(grouped, S.load_catalog())['learnings']), 1)
  def test_review_same_measure_populations_contract(self):
    cat = S.load_catalog()
    cat['entries'][0]['measures'].append(dict(measure='affected', population='units'))
    second = copy.deepcopy(self.c)
    second['counts'][0].update(population='units', cohort_ref='C002',
                 numerator=1, denominator=1)
    self.d['cohorts']['C002'] = dict(population='units', members=['u'], qualifying=['u'])
    self.d['candidates'].append(second)
    P.validate_dossier(self.d, cat)
    grouped = copy.deepcopy(self.d)
    grouped['candidates'] = C.group(self.d['candidates'], grouped['cohorts'], cat)
    self.assertEqual(len(grouped['candidates']), 2)
    P.validate_dossier(grouped, cat)
    self.assertEqual({(c['measure'], c['population'])
             for candidate in grouped['candidates'] for c in candidate['counts']},
            {('affected', 'units'), ('affected', 'attempts')})
  def test_review_null_catalog_group_is_not_selected(self):
    self.c.update(catalog_entry=None, counts=[], status='covered',
           coverage_disposition='unknown')
    P.validate_dossier(self.d)
    self.assertEqual(C.group([self.c])[0], self.c)
  def test_missing_cohort_refuses_without_mutating(self):
    before = copy.deepcopy(self.d)
    with self.assertRaises(S.SchemaError):
      C.group([self.c])
    self.assertEqual(self.d, before)
  def test_different_populations_never_added(self):
    cat = S.load_catalog()
    cat['entries'][0]['measures'].append(dict(measure='affected', population='units'))
    second = copy.deepcopy(self.c)
    second['counts'][0].update(population='units', cohort_ref='C002')
    self.d['cohorts']['C002'] = dict(population='units', members=['a', 'b'], qualifying=['a'])
    before = copy.deepcopy(self.d['cohorts'])
    self.assertEqual(C.group([self.c, second], self.d['cohorts'], cat), [self.c, second])
    self.assertEqual(self.d['cohorts'], before)
  def test_group_keeps_explicit_reviews_and_unrepresentable_separate(self):
    for status, decision in (('unrepresentable', 'unknown'),
                             ('selected', 'extends'), ('selected', 'conflicts'),
                             ('rejected', 'unknown')):
      for reverse in (False, True):
        second = copy.deepcopy(self.c)
        second.update(status=status, coverage_disposition=decision,
                      rationale='Separate private reason.')
        candidates = [self.c, second]
        if reverse:
          candidates.reverse()
        grouped = C.group(candidates, self.d['cohorts'])
        self.assertEqual(grouped, candidates)
        self.assertIsNot(grouped[0], candidates[0])
        d = copy.deepcopy(self.d)
        d['candidates'] = grouped
        C.select(d)
        expected = 2 if decision in ('extends', 'conflicts') else 1
        self.assertEqual(len(P.compile_packet(d, S.load_catalog())['learnings']), expected)

  def test_review_after_grouping_does_not_parse_rationale(self):
    second = copy.deepcopy(self.c)
    second['rationale'] = 'DEBRIEF_REVIEW=[]'
    self.d['candidates'] = C.group([self.c, second], self.d['cohorts'])
    self.assertEqual(len(self.d['candidates']), 1)
    self.assertEqual(self.d['candidates'][0]['rationale'],
                     self.c['rationale'] + '\n\nDEBRIEF_REVIEW=[]')
    C.record_review(self.d, 0, 'conflicts', 'Grouped observation reviewed.')
    reviewed = copy.deepcopy(self.d['candidates'][0])
    self.assertEqual(C.group([reviewed, second], self.d['cohorts']), [reviewed, second])

  def test_select_keeps_covered_and_unrepresentable(self):
    self.c['coverage_disposition'] = 'covered'
    unknown = copy.deepcopy(self.c)
    unknown.update(catalog_entry=None, counts=[], status='unrepresentable')
    self.d['candidates'].append(unknown)
    C.select(self.d)
    self.assertEqual([c['status'] for c in self.d['candidates']], ['covered', 'unrepresentable'])
    self.assertEqual(len(self.d['candidates']), 2)
    self.assertTrue(all(c['rationale'] for c in self.d['candidates']))
    with self.assertRaises(S.SchemaError):
      P.compile_packet(self.d, S.load_catalog())
  def test_select_never_publishes_rejected(self):
    self.c['status'] = 'rejected'
    C.select(self.d)
    self.assertEqual(self.c['status'], 'rejected')
    with self.assertRaises(S.SchemaError):
      P.compile_packet(self.d, S.load_catalog())
  def test_select_eligible_and_null_entry(self):
    for disposition in ('extends', 'conflicts', 'unknown', 'not_found_in_snapshot'):
      self.c.update(status='covered', coverage_disposition=disposition)
      C.select(self.d)
      self.assertEqual(self.c['status'], 'selected')
    self.c.update(catalog_entry=None, counts=[])
    C.select(self.d)
    self.assertEqual(self.c['status'], 'unrepresentable')
  def test_only_ancestry_subprocess_and_explicit_reproduction_assignment(self):
    calls, writes = [], []
    for path in SCRIPT.parent.glob('debrief_*.py'):
      if path.name == 'debrief_file.py':
        continue
      tree = ast.parse(path.read_text())
      for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
          modules = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or '']
          self.assertFalse(set(m.split('.')[0] for m in modules) &
                  {'socket', 'ssl', 'http', 'urllib', 'ftplib', 'smtplib', 'asyncio'})
      for function in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
        for node in ast.walk(function):
          if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if isinstance(node.func.value, ast.Name) and node.func.value.id == 'subprocess':
              calls.append((path.name, function.name, node))
          if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and node.value.value == 'reproduced':
            writes.append((path.name, function.name))
    self.assertEqual([(p, f) for p, f, _ in calls], [('debrief_coverage.py', 'classify')])
    call = calls[0][2]
    self.assertIsInstance(call.args[0], ast.List)
    self.assertEqual(call.args[0].elts[0].value, 'git')
    self.assertEqual(call.args[0].elts[3].value, 'merge-base')
    self.assertEqual(call.args[0].elts[4].value, '--is-ancestor')
    self.assertNotIn('shell', [k.arg for k in call.keywords])
    self.assertEqual(writes, [('debrief_coverage.py', 'record_reproduction')])


class TestRoundOneRegressions(unittest.TestCase):
  def test_explicit_catalog_survives_selection_and_edits(self):
    cat = S.load_catalog()
    cat['entries'][0]['measures'].append(dict(measure='affected', population='units'))
    d = dossier()
    d['cohorts']['C001']['population'] = 'units'
    d['candidates'][0]['counts'][0]['population'] = 'units'
    P.validate_dossier(d, cat)
    d['candidates'] = C.group(d['candidates'], d['cohorts'], cat)
    for operation in (lambda: C.select(d, cat),
                      lambda: C.record_review(d, 0, 'extends', 'private', cat),
                      lambda: C.record_reproduction(d, 0, 'E001', 'v', 'p', 'r', cat)):
      with self.subTest(operation=operation):
        operation()

  def test_group_retains_all_reproduction_details(self):
    d = dossier()
    d['evidence']['E002'] = copy.deepcopy(d['evidence']['E001'])
    d['candidates'].append(copy.deepcopy(d['candidates'][0]))
    for i in range(2):
      C.record_reproduction(d, i, 'E00%d' % (i+1),
                           'version-%d' % i, 'probe-%d' % i, 'result-%d' % i)
    d['candidates'] = C.group(d['candidates'], d['cohorts'])
    self.assertEqual(len(d['candidates']), 2)
    rendered = json.dumps(d)
    for value in ('version-1', 'probe-1', 'result-1'):
      self.assertIn(value, rendered)

  def test_classify_refuses_unknown_catalog(self):
    c = dossier()['candidates'][0]
    c['catalog_entry'] = 'DB999'
    with self.assertRaises(S.SchemaError):
      C.classify(c, registry(), None)

  def test_always_without_git_provenance_or_checkout(self):
    with patch.object(C.subprocess, 'run', side_effect=AssertionError('git consulted')):
      for checkout in (None,):
        self.assertEqual(C.classify(dossier()['candidates'][0], registry(), checkout),
                         'covered')


if __name__ == '__main__':
  unittest.main()
