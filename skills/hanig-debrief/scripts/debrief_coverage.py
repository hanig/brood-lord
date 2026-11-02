"""Private review operations; no project code is loaded or executed.

Candidates use debrief_dossier.v1. Skill comes from the installed catalog.
Candidate IDs are zero-based list positions, stable while the dossier retains its
observations. group returns copies, leaving those observations intact; pass the
run's cohorts to recount counts. New union cohorts are added without overwriting
existing cohorts. classify records its reason and returns a disposition; pass
provenance=dossier['provenance'] to provide installed marker contents.

The authorized session calls record_review/record_reproduction explicitly. These
are local editing APIs, not authentication of the caller or of a receipt's claims.
"""
import copy
import os
from pathlib import Path
import re
import subprocess

import debrief_packet as P
import debrief_schema as S


def group(candidates, cohorts=None, catalog=None):
    """Group one run by exact catalog, skill and basis; recount distinct members.

    Conflicting review decisions remain unknown pending review; any rejected
    observation keeps its group rejected. Original rationales and receipt refs
    survive in the source dossier. Never infer a reproduction from a signature.
    """
    catalog, entries = P._cat(catalog)
    S.need(type(candidates) is list, 'candidates', 'expected list')
    source = {} if cohorts is None else cohorts
    # Validate cohort membership independently of possibly stale count totals.
    P.validate_dossier(dict(schema_version=S.DOSSIER_SCHEMA, evidence={},
        cohorts=source, candidates=[], omissions=[], provenance={}), catalog)
    pending = copy.deepcopy(source)
    groups, index = [], {}
    for candidate in candidates:
        entry_id = candidate['catalog_entry']
        S.need(entry_id is None or entry_id in entries, 'candidate', 'unknown entry')
        key = (entry_id, entries[entry_id]['skill'] if entry_id else None,
               candidate['basis'])
        if entry_id is None or key not in index:
            index[key] = len(groups)
            groups.append([])
        groups[index[key]].append(candidate)
    result = []
    for observations in groups:
        merged = copy.deepcopy(observations[0])
        merged['evidence_refs'] = sorted({ref for c in observations for ref in c['evidence_refs']})
        merged['rationale'] = '\n\n'.join(dict.fromkeys(c['rationale'] for c in observations))
        dispositions = {c['coverage_disposition'] for c in observations}
        if len(dispositions) > 1:
            merged['coverage_disposition'] = 'unknown'
            merged['rationale'] += '\n\nGrouped review decisions differ; review required.'
        statuses = {c['status'] for c in observations}
        if 'rejected' in statuses:
            merged['status'] = 'rejected'
        elif 'unrepresentable' in statuses:
            merged['status'] = 'unrepresentable'
        elif merged['coverage_disposition'] == 'covered':
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
                ref = next(('C%03d' % i for i in range(1, 1000) if 'C%03d' % i not in pending), None)
                S.need(ref is not None, 'cohorts', 'cohort ids exhausted')
                pending[ref] = cohort
            merged['counts'].append(dict(measure=measure, population=population,
                scope=S.COUNT_SCOPE, numerator=len(qualifying), denominator=len(members), cohort_ref=ref))
        P._counts(merged['counts'], entries.get(merged['catalog_entry']), 'counts', pending)
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
            or type(commit) is not str or re.fullmatch(S.COMMIT_SHA, commit) is None):
        return None
    return commit


def classify(candidate, registry, checkout, *, provenance=None, catalog=None):
    """Classify an exact mapping; explicit extends/conflicts decisions take priority.

    Missing provenance/checkout is unknown even for an always mapping. A known
    non-ancestor is unknown (the snapshot maps it, but applicability is unproved).
    Documentation and registry notes are never searched for similarity.
    """
    catalog, entries = P._cat(catalog)
    S.validate_coverage(registry, catalog)

    def finish(disposition, reason):
        candidate['coverage_disposition'] = disposition
        candidate['rationale'] += '\n\n' + reason
        return disposition

    if candidate['coverage_disposition'] in ('extends', 'conflicts'):
        return candidate['coverage_disposition']
    entry = entries.get(candidate['catalog_entry'])
    mappings = [r for r in registry['entries'] if entry and r['catalog_entry'] == entry['id']]
    if not mappings:
        return finish('not_found_in_snapshot', 'No exact catalog mapping in the installed snapshot.')
    commit = _commit(provenance, entry['skill'])
    if commit is None or checkout is None or not Path(checkout).is_dir():
        return finish('unknown', 'Installed commit or explicit checkout is unavailable.')
    # Ambient Git overrides must not redirect the explicitly supplied checkout.
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env['GIT_NO_REPLACE_OBJECTS'] = '1'
    for mapping in mappings:
        applies = mapping['applicability']
        if applies.get('always') is True:
            return finish('covered', 'Exact registry mapping applies unconditionally.')
        try:
            checked = subprocess.run(
                ['git', '-C', str(checkout), 'merge-base', '--is-ancestor', applies['fixed_in'], commit],
                env=env, timeout=10, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if checked.returncode == 0:
            return finish('covered', 'Exact registry fix is an ancestor of the installed commit.')
    return finish('unknown', 'No exact mapping has established applicability at the installed commit.')


def _candidate(dossier, candidate_id):
    P.validate_dossier(dossier)
    S.need(type(candidate_id) is int and 0 <= candidate_id < len(dossier['candidates']),
           'candidate_id', 'expected zero-based candidate position')
    return dossier['candidates'][candidate_id]


def record_review(dossier, candidate_id, disposition, rationale):
    """Record the authorized session's explicit extension/conflict judgment."""
    candidate = _candidate(dossier, candidate_id)
    S.need(disposition in ('extends', 'conflicts'), 'disposition', 'expected explicit review decision')
    S.text(rationale, 'rationale')
    candidate['coverage_disposition'] = disposition
    candidate['rationale'] += '\n\n' + rationale
    return candidate


def select(dossier):
    """Mark eligible candidates without deleting any private observations."""
    P.validate_dossier(dossier)
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


def record_reproduction(dossier, candidate_id, receipt_ref, tested_version, probe, observed):
    """Attach a cited receipt; recording is explicit, never an extraction side effect."""
    candidate = _candidate(dossier, candidate_id)
    S.match(receipt_ref, S.EVIDENCE_ID, 'receipt_ref')
    S.need(receipt_ref in dossier['evidence'], 'receipt_ref', 'unresolved evidence record')
    for key, value in (('tested_version', tested_version), ('probe', probe), ('observed', observed)):
        S.text(value, key)
    candidate['reproduction'] = dict(receipt_ref=receipt_ref, tested_version=tested_version,
                                     probe=probe, observed=observed)
    candidate['evidence_refs'] = sorted(set(candidate['evidence_refs']) | {receipt_ref})
    candidate['basis'] = 'reproduced'
    return candidate
