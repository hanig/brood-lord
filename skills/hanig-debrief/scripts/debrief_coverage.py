"""Private versioned dossier edits. IDs are zero-based candidate positions.
group returns copies and adds union cohorts. classify takes installed marker
contents via provenance=dossier['provenance']. Review and reproduction records
are explicit operator edits, not authentication.
"""
import copy
import os
from pathlib import Path
import re
import subprocess

import debrief_packet as P
import debrief_schema as S


def group(candidates, cohorts=None, catalog=None, *, schema=S.DOSSIER_SCHEMA):
  """Group exact count pairs; preserve excluded observations and recount."""
  catalog, entries = P._cat(catalog)
  S.need(type(candidates) is list, 'candidates', 'expected list')
  _, cid, limit = S.identifier_grammar(schema, "dossier")
  source = {} if cohorts is None else cohorts
  # Validate cohort membership independently of possibly stale count totals.
  P.validate_dossier(dict(schema_version=schema, evidence={},
    cohorts=source, candidates=[], omissions=[], provenance={}), catalog)
  pending = copy.deepcopy(source)
  groups, index = [], {}
  for candidate in candidates:
    entry_id = candidate['catalog_entry']
    S.need(entry_id is None or entry_id in entries, 'candidate', 'unknown entry')
    key = (entry_id, entries[entry_id]['skill'] if entry_id else None,
       candidate['basis'],
       frozenset((c['measure'], c['population']) for c in candidate['counts']))
    if (entry_id is None or candidate['status'] in ('rejected', 'unrepresentable')
        or candidate['coverage_disposition'] in ('extends', 'conflicts')
        or candidate['reproduction'] is not None):
      groups.append([candidate])
      continue
    if key not in index:
      index[key] = len(groups)
      groups.append([])
    groups[index[key]].append(candidate)
  result = []
  for observations in groups:
    merged = copy.deepcopy(observations[0])
    if (merged['catalog_entry'] is None or merged['status'] in ('rejected', 'unrepresentable')
        or merged['coverage_disposition'] in ('extends', 'conflicts')
        or merged['reproduction'] is not None):
      result.append(merged)
      continue
    merged['evidence_refs'] = sorted({ref for c in observations for ref in c['evidence_refs']})
    merged['rationale'] = '\n\n'.join(dict.fromkeys(c['rationale'] for c in observations))
    dispositions = {c['coverage_disposition'] for c in observations}
    if len(dispositions) > 1:
      merged['coverage_disposition'] = 'unknown'
      merged['rationale'] += '\n\nGrouped snapshot classifications differ; classification required.'
    if merged['coverage_disposition'] == 'covered':
      merged['status'] = 'covered'
    else:
      merged['status'] = 'selected'
    measures = {}
    for c in observations:
      for count in c['counts']:
        ref = count['cohort_ref']
        S.need(ref in source, 'count', 'unresolved cohort')
        cohort = source[ref]
        S.need(cohort['population'] == count['population'], 'count', 'population mismatch')
        measure = count['measure']
        if measure not in measures:
          measures[measure] = (count['population'], set(), set())
        population, members, qualifying = measures[measure]
        S.need(population == count['population'], 'count', 'different populations')
        members.update(cohort['members'])
        qualifying.update(cohort['qualifying'])
    merged['counts'] = []
    for measure, (population, members, qualifying) in measures.items():
      cohort = dict(population=population, members=sorted(members), qualifying=sorted(qualifying))
      ref = next((r for r, v in pending.items() if v == cohort), None)
      if ref is None:
        ref = next(('C%03d' % i for i in range(1, limit + 1) if 'C%03d' % i not in pending), None)
        S.need(ref is not None, 'cohorts', 'cohort ids exhausted')
        pending[ref] = cohort
      merged['counts'].append(dict(measure=measure, population=population,
        scope=S.COUNT_SCOPE, numerator=len(qualifying), denominator=len(members), cohort_ref=ref))
    P._counts(merged['counts'], entries.get(merged['catalog_entry']), 'counts', pending, pattern=cid)
    result.append(merged)
  if cohorts is not None:
    cohorts.update(pending)
  return result


def _commit(provenance, skill):
  marker = provenance.get(skill) if type(provenance) is dict else None
  if type(marker) is not str:
    return None
  fields = {}
  for line in marker.split('\n'):
    if not line:
      continue
    key, sep, value = line.partition('=')
    if not sep or key in fields:
      return None
    fields[key] = value
  commit = fields.get('source_version')
  if (fields.get('schema') != '2' or fields.get('repo') != 'multi-agent-skills'
      or fields.get('origin') != 'authored' or fields.get('version') != commit
      or type(commit) is not str or re.fullmatch(r'[0-9a-f]{40}', commit) is None):
    return None
  return commit


def classify(candidate, registry, checkout, *, provenance=None, catalog=None):
  """Return snapshot coverage without changing the candidate or reading reviews.

  Absent mappings are not_found_in_snapshot. Always mappings are unconditional;
  fixed_in requires known provenance and ancestry in an explicit checkout.
  """
  catalog, entries = P._cat(catalog)
  S.validate_coverage(registry, catalog)

  entry_id = candidate['catalog_entry']
  S.need(entry_id is None or entry_id in entries,
         'candidate.catalog_entry', 'unknown catalog entry')
  entry = entries.get(entry_id)
  mappings = [r for r in registry['entries'] if entry and r['catalog_entry'] == entry['id']]
  if not mappings:
    return 'not_found_in_snapshot'
  if any(r['applicability'].get('always') is True for r in mappings):
    return 'covered'
  commit = _commit(provenance, entry['skill'])
  if commit is None or checkout is None or not Path(checkout).is_dir():
    return 'unknown'
  # Ambient Git overrides must not redirect the explicitly supplied checkout.
  env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
  env['GIT_NO_REPLACE_OBJECTS'] = '1'
  for mapping in mappings:
    applies = mapping['applicability']
    fixed = applies.get('fixed_in', commit)
    if type(fixed) is not str or re.fullmatch(r'[0-9a-f]{40}', fixed) is None:
      continue
    try:
      checked = subprocess.run(
        ['git', '-C', str(checkout), 'merge-base', '--is-ancestor', fixed + '^{commit}', commit + '^{commit}'],
        env=env, timeout=10, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    except (OSError, subprocess.TimeoutExpired):
      continue
    if checked.returncode == 0:
      return 'covered'
  return 'unknown'


def _candidate(dossier, candidate_id, catalog=None):
  P.validate_dossier(dossier, catalog)
  S.need(type(candidate_id) is int and 0 <= candidate_id < len(dossier['candidates']),
     'candidate_id', 'expected zero-based candidate position')
  return dossier['candidates'][candidate_id]


def record_review(dossier, candidate_id, disposition, rationale, catalog=None):
  """Record a post-grouping decision directly, retaining its private reason."""
  candidate = _candidate(dossier, candidate_id, catalog)
  S.need(disposition in ('extends', 'conflicts'), 'disposition', 'expected explicit review decision')
  S.text(rationale, 'rationale')
  candidate['coverage_disposition'] = disposition
  candidate['rationale'] += '\n\n' + rationale
  return candidate


def select(dossier, catalog=None):
  """Mark eligible candidates without deleting any private observations."""
  P.validate_dossier(dossier, catalog)
  for candidate in dossier['candidates']:
    if candidate['status'] in ('rejected', 'unrepresentable'):
      continue
    if candidate['catalog_entry'] is None:
      candidate['status'] = 'unrepresentable'
    elif candidate['coverage_disposition'] == 'covered':
      candidate['status'] = 'covered'
    else:
      candidate['status'] = 'selected'
  return dossier


def record_reproduction(dossier, candidate_id, receipt_ref, tested_version, probe, observed, catalog=None):
  """Attach a cited receipt; recording is explicit, never an extraction side effect."""
  candidate = _candidate(dossier, candidate_id, catalog)
  eid, _, _ = S.identifier_grammar(dossier['schema_version'], 'dossier')
  S.match(receipt_ref, eid, 'receipt_ref')
  S.need(receipt_ref in dossier['evidence'], 'receipt_ref', 'unresolved evidence record')
  for key, value in (('tested_version', tested_version), ('probe', probe), ('observed', observed)):
    S.text(value, key)
  candidate['reproduction'] = dict(receipt_ref=receipt_ref, tested_version=tested_version,
                  probe=probe, observed=observed)
  candidate['evidence_refs'] = sorted(set(candidate['evidence_refs']) | {receipt_ref})
  candidate['basis'] = 'reproduced'
  return candidate
