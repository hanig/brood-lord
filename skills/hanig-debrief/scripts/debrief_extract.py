"""Read-only dossier parts. Roots: [{label, role, path}]; budgets: per_file_bytes,
files_per_root, total_bytes (positive integers <= contract caps).
All symlinks are omitted. Roots/entries sort by exact label/name. Markers match
untrimmed whole lines. Guidance-alias records share [root, 'guidance', digest]
cohort ids; other ids use root/locator or root/unit/attempt, never source text.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from debrief_schema import PER_FILE_BYTES, PER_ROOT_FILES, TOTAL_BYTES, POPULATIONS

MARKERS = ('STOP', 'REVIEW_FAIL', 'REVIEW_UNAVAILABLE', 'PROPOSED DECISION')
STATES = ('DONE', 'RUNNING', 'FAILED', 'PREEMPTED', 'INCOMPLETE', 'NEEDS_HUMAN')
COPY_MARKER = '.installed-by-multi-agent-skills'
SIDECARS = '.multi-agent-skills-provenance'
ATTEMPT = re.compile(r'[0-9a-f]{16}')

def build_cohort(population, members):
  """Members are identities, not receipt occurrences or content digests."""
  members = list(members)
  if population not in POPULATIONS or any(not _text(x) for x in members):
    raise ValueError('invalid cohort')
  ids = sorted(set(members))
  return {'population': population, 'members': ids, 'qualifying': ids[:]}

def _text(value):
  return (type(value) is str and bool(value) and not value.isspace()
      and not any(0xD800 <= ord(c) <= 0xDFFF for c in value))

def _identity(*parts):
  return json.dumps(parts, ensure_ascii=True, separators=(',', ':'))

def _pointer(*parts):
  return '/' + '/'.join(str(x).replace('~', '~0').replace('/', '~1') for x in parts)

def _pairs(pairs):
  out = {}
  for key, value in pairs:
    if key in out:
      raise ValueError('duplicate key')
    out[key] = value
  return out

def _json(text):
  def invalid(_):
    raise ValueError('nonfinite')
  return json.loads(text, object_pairs_hook=_pairs, parse_constant=invalid)

def _owner(parts):
  if len(parts) >= 3 and ATTEMPT.fullmatch(parts[-2]):
    return parts[-3], parts[-2]
  return None, None

def _structured(name, data):
  """Spines from swarm_state._unit_state and unit.cmd_allocate/cmd_check."""
  if type(data) is not dict or type(data.get('schema_version')) is not int:
    raise ValueError('unsupported')
  if data['schema_version'] != 1:
    raise ValueError('unsupported')
  rows = []
  if name == 'swarm-state.json':
    if type(data.get('units')) is not dict or 'halted' not in data:
      raise ValueError('unsupported')
    for uid, item in sorted(data['units'].items()):
      if (not _text(uid) or type(item) is not dict
      or item.get('state') not in (None, 'ALLOCATED', 'ABANDONED', 'SUPERSEDED') + STATES
      or 'state' not in item or type(item.get('attempts')) is not list):
        raise ValueError('unsupported')
      rows.append(('state', _pointer('units', uid, 'state'), uid, None))
      for i, attempt in enumerate(item['attempts']):
        if not _text(attempt):
          raise ValueError('unsupported')
        aid = attempt.split('/')[-1]
        if attempt.startswith('dry-attempt-'):
          aid = None
        elif not ATTEMPT.fullmatch(aid):
          raise ValueError('unsupported')
        rows.append(('attempt', _pointer('units', uid, 'attempts', i), uid, aid))
    return rows
  uid, aid = data.get('task_id'), data.get('attempt_id')
  if (not _text(uid) or not _text(aid) or not ATTEMPT.fullmatch(aid)
      or data.get('kind') not in ('code', 'slurm', 'pipeline')):
    raise ValueError('unsupported')
  if name == 'receipt.json':
    required = {'checked_at': str, 'notes': list, 'outputs': dict, 'basis': dict}
    if (data.get('state') not in STATES or type(data.get('exit_code')) is not int
      or data['exit_code'] != STATES.index(data['state'])
      or 'job_id' not in data
      or any(type(data.get(k)) is not t for k, t in required.items())):
      raise ValueError('unsupported')
    adapter = 'receipt'
  else:
    if (type(data.get('declared_outputs')) is not list
      or type(data.get('pinned_inputs')) is not dict
      or type(data.get('budget')) is not dict or 'command' not in data):
      raise ValueError('unsupported')
    adapter = 'attempt'
  return [(adapter, _pointer(key), uid, aid) for key in sorted(data)]

def _provenance(text, parts):
  values = _pairs(line.split('=', 1) for line in text.splitlines() if line)
  keys = {'schema', 'repo', 'origin', 'source_version', 'version', 'destination',
      'consumers', 'mode', 'installed_at', 'link_target', 'link_identity'}
  if (set(values) != keys or values['schema'] != '2'
      or values['repo'] != 'multi-agent-skills'
      or values['origin'] not in ('authored', 'vendored')
      or values['mode'] not in ('copy', 'link')
      or not values['source_version'] or values['version'] != values['source_version']
      or not os.path.isabs(values['destination']) or not values['installed_at']
      or values['consumers'] and any(not c for c in values['consumers'].split(','))):
    raise ValueError('unsupported provenance')
  dest = Path(values['destination'])
  skill = dest.name
  if parts[-1] == COPY_MARKER:
    if (len(parts) < 2 or parts[-2] != skill or values['mode'] != 'copy'
      or values['link_target'] or values['link_identity']):
      raise ValueError('unsupported provenance')
  else:
    digest = hashlib.sha256(os.fsencode(str(dest))).hexdigest()[:24]
    if (parts[-1] != skill + '-' + digest + '.provenance'
      or values['mode'] != 'link' or not os.path.isabs(values['link_target'])
      or not values['link_identity']):
      raise ValueError('unsupported provenance')
  return skill

def _text_rows(name, role, text):
  lines = text.splitlines()
  if name in ('CLAUDE.md', 'AGENTS.md') or 'pitfall' in name:
    return [('guidance', 1, max(1, len(lines)))]
  if name in ('evidence.md', 'SUMMARY.md'):
    return [('marker:' + line, i, i) for i, line in enumerate(lines, 1)
      if line in MARKERS]
  if name == 'PLAN.md':
    rows, needs = [], False
    for i, line in enumerate(lines, 1):
      if re.fullmatch(r'#{1,6} NEEDS YOU', line):
        needs = True
      elif line.startswith('#'):
        needs = False
      if (re.match(r'(?:\|[ \t]*)?D-[A-Za-z0-9._-]+[ \t]*\|', line)
      or re.match(r'[-*] NEEDS YOU(?:$|: )', line)
      or needs and re.match(r'[-*] \S', line)):
        rows.append(('decision', i, i))
    return rows
  if role == 'memory' and name.endswith('.md'):
    if not lines or lines[0] != '---' or '---' not in lines[1:]:
      raise ValueError('unsupported memory frontmatter')
    end = lines.index('---', 1)
    rows, seen, metadata = [], set(), False
    for i in range(1, end):
      line = lines[i]
      match = re.match(r'(name|description): \S', line)
      field = match.group(1) if match else None
      if line == 'metadata:':
        metadata = True
      elif line and not line.startswith(' '):
        metadata = False
      if metadata and re.match(r'  type: \S', line):
        field = 'metadata.type'
      if field:
        if field in seen:
          raise ValueError('duplicate frontmatter field')
        seen.add(field)
        last = i
        while last + 1 < end and lines[last + 1].startswith('    '):
          last += 1
        rows.append(('memory:' + field, i + 1, last + 1))
    if not {'name', 'description', 'metadata.type'} <= seen:
      raise ValueError('unsupported memory frontmatter')
    if end + 1 < len(lines):
      rows.append(('memory:body', end + 2, len(lines)))
    return rows
  raise ValueError('unsupported format')

def _fingerprint(st):
  return (st.st_dev, st.st_ino, st.st_mode, st.st_size, st.st_mtime_ns, st.st_ctime_ns)

def _read_file(parent, name, before, cap):
  fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
  with os.fdopen(fd, 'rb') as stream:
    if _fingerprint(os.fstat(stream.fileno())) != _fingerprint(before):
      return None
    data = stream.read(cap)
    after = os.fstat(stream.fileno())
  current = os.stat(name, dir_fd=parent, follow_symlinks=False)
  if (_fingerprint(before) != _fingerprint(after)
      or _fingerprint(before) != _fingerprint(current) or len(data) != before.st_size):
    return None
  return data

def _open_root(path):
  """Open every component without following links, including ancestors."""
  fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
  try:
    for part in path.parts[1:]:
      child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
      os.close(fd)
      fd = child
    return fd
  except BaseException:
    os.close(fd)
    raise

def collect(roots, budgets=None):
  """Return only evidence, cohorts, omissions and provenance; never write."""
  limits = {'per_file_bytes': PER_FILE_BYTES, 'files_per_root': PER_ROOT_FILES,
      'total_bytes': TOTAL_BYTES}
  for key, value in (budgets or {}).items():
    if key not in limits or type(value) is not int or not 0 < value <= limits[key]:
      raise ValueError('invalid budget')
    limits[key] = value
  labels = set()
  for root in roots:
    if (set(root) != {'label', 'role', 'path'} or not _text(root['label'])
      or root['label'] in labels
      or root['role'] not in ('project', 'memory', 'state', 'helpers')):
      raise ValueError('invalid root')
    labels.add(root['label'])
  result = {'evidence': {}, 'cohorts': {}, 'omissions': [], 'provenance': {}}
  members = {p: [] for p in POPULATIONS}
  consumed = 0
  aliases = set()

  def omit(label, locator, reason):
    result['omissions'].append(dict(source_root=label, locator=locator, reason=reason))

  def adapt(label, role, parts, data):
    locator, name = '/'.join(parts), parts[-1]
    digest = hashlib.sha256(data).hexdigest()
    uid, aid = _owner(parts)
    if role == 'helpers':
      rows = [('helper', {'line_range': [1, 1]}, None, None)]
    else:
      text = data.decode('utf-8')
      if name == COPY_MARKER or len(parts) > 1 and parts[-2] == SIDECARS:
        skill = _provenance(text, parts)
        if skill in result['provenance'] and result['provenance'][skill] != text:
          raise ValueError('unsupported conflicting provenance')
        result['provenance'][skill] = text
        rows = [('provenance', {'line_range': [1, len(text.splitlines())]}, None, None)]
      elif role == 'state' and name in ('swarm-state.json', 'unit.json', 'receipt.json'):
        rows = [(a, {'json_pointer': p}, u, t) for a, p, u, t in _structured(name, _json(text))]
      elif name == 'plan.json' and role in ('project', 'state'):
        plan = _json(text)
        if type(plan) is not dict or type(plan.get('units')) is not list:
          raise ValueError('unsupported plan')
        rows = [('plan', {'json_pointer': '/units'}, None, None)]
      else:
        rows = [(a, {'line_range': [s, e]}, uid, aid)
      for a, s, e in _text_rows(name, role, text)]
    if not rows:
      omit(label, locator, 'skipped: no matching observations')
      return
    if len(result['evidence']) + len(rows) > 999:
      omit(label, locator, 'over-budget: evidence ids')
      return
    doc = _identity(label, locator)
    if name in ('CLAUDE.md', 'AGENTS.md') and role != 'helpers':
      doc = _identity(label, 'guidance', digest)
      if doc in aliases:
        rows = [('guidance-alias', loc, u, a) for _, loc, u, a in rows]
      aliases.add(doc)
    for adapter, location, unit, attempt in rows:
      ref = 'E%03d' % (len(result['evidence']) + 1)
      result['evidence'][ref] = dict(source_root=label, locator=locator, sha256=digest,
      location=location, unit_id=unit, attempt_id=attempt,
      adapter={'name': adapter, 'version': '1'}, status='ok')
      if unit is not None:
        members['units'].append(_identity(label, unit))
      if attempt is not None:
        members['attempts'].append(_identity(label, unit, attempt))
      if adapter in ('state', 'attempt', 'receipt'):
        members['state_entries'].append(ref)
    members['helper_scripts' if role == 'helpers' else 'evidence_documents'].append(doc)

  for root in sorted(roots, key=lambda r: r['label']):
    label, role = root['label'], root['role']
    path = Path(root['path'])
    if (not path.is_absolute() or '..' in path.parts or '.git' in path.parts
      or path == Path(os.path.expanduser('~')) or path == Path('/')):
      omit(label, '.', 'skipped: forbidden root')
      continue
    try:
      root_fd = _open_root(path)
    except OSError:
      omit(label, '.', 'skipped: unreadable or symlink root')
      continue
    count, directories = 0, 0

    def walk(fd, prefix):
      nonlocal count, directories, consumed
      names = []
      with os.scandir(fd) as entries:
        for entry in entries:
          names.append(entry.name)
          if len(names) > PER_ROOT_FILES:
            omit(label, '/'.join(prefix) or '.', 'over-budget: directory entries')
            return
      for name in sorted(names):
        parts = prefix + [name]
        locator = '/'.join(parts)
        if count >= limits['files_per_root'] or consumed >= limits['total_bytes']:
          omit(label, '.', 'over-budget: uninspected remainder')
          return
        if name == '.git' or name.endswith('.jsonl'):
          omit(label, locator, 'skipped: excluded input')
          continue
        try:
          before = os.stat(name, dir_fd=fd, follow_symlinks=False)
          if stat.S_ISLNK(before.st_mode):
            omit(label, locator, 'skipped: symlink')
          elif stat.S_ISDIR(before.st_mode):
            directories += 1
            if directories > PER_ROOT_FILES or len(parts) > 64:
              omit(label, locator, 'over-budget: directories or depth')
              continue
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            try:
              walk(child, parts)
            finally:
              os.close(child)
          elif not stat.S_ISREG(before.st_mode):
            omit(label, locator, 'unsupported: file type')
          else:
            count += 1
            if before.st_size > limits['per_file_bytes']:
              omit(label, locator, 'over-budget: per-file bytes')
              continue
            if before.st_size > limits['total_bytes'] - consumed:
              omit(label, locator, 'over-budget: total bytes')
              continue
            known = (name in ('CLAUDE.md', 'AGENTS.md', 'PLAN.md', 'evidence.md',
      'SUMMARY.md', COPY_MARKER) or 'pitfall' in name
      or len(parts) > 1 and parts[-2] == SIDECARS
      or role == 'memory' and name.endswith('.md')
      or role == 'state' and name in ('swarm-state.json', 'unit.json', 'receipt.json')
      or role in ('project', 'state') and name == 'plan.json'
      or role == 'helpers' and not name.endswith('.json'))
            if not known:
              omit(label, locator, 'unsupported: format')
              continue
            consumed += before.st_size
            data = _read_file(fd, name, before, before.st_size)
            if data is None:
              omit(label, locator, 'unstable: changed during read')
            else:
              try:
                adapt(label, role, parts, data)
              except (ValueError, UnicodeError, RecursionError):
                omit(label, locator, 'unsupported: format or encoding')
            if before.st_size == limits['per_file_bytes']:
              omit(label, locator, 'limit reached: per-file bytes (retained if stable)')
        except OSError:
          omit(label, locator, 'skipped: unreadable or changed path')
    try:
      walk(root_fd, [])
    except OSError:
      omit(label, '.', 'skipped: unreadable directory')
    finally:
      os.close(root_fd)
    if count == limits['files_per_root'] or consumed == limits['total_bytes']:
      omit(label, '.', 'limit reached: files or total bytes')
  for population, ids in members.items():
    if ids:
      result['cohorts']['C%03d' % (len(result['cohorts']) + 1)] = build_cohort(population, ids)
  return result
