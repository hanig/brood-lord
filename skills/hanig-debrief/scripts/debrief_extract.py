"""collect([{label, role, path}], budgets).
Caps: per_file_bytes, files_per_root, total_bytes; max_depth defaults/caps at
128 levels; every hit omitted. Roots resolve once; descendant symlinks are omitted.
Only LF delimits lines; CR, including CRLF, is content (no marker match).
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

def _need(ok):
  if not ok:
    raise ValueError('unsupported')

def build_cohort(population, members):
  members = list(members)
  _need(not (population not in POPULATIONS or any(not _text(x) for x in members)))
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
    _need(key not in out)
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
  _need(not (type(data) is not dict or type(data.get('schema_version')) is not int))
  _need(data['schema_version'] == 1)
  rows = []
  if name == 'swarm-state.json':
    _need(not (type(data.get('units')) is not dict or 'halted' not in data))
    for uid, item in sorted(data['units'].items()):
      _need(not (not _text(uid) or type(item) is not dict
  or not {'state', 'attempt_dir', 'attempts', 'gpu_hours'} <= item.keys()
  or item['attempt_dir'] is not None and not _text(item['attempt_dir'])
  or type(item['gpu_hours']) not in (int, float)
  or item.get('state') not in (None, 'ALLOCATED', 'ABANDONED', 'SUPERSEDED') + STATES
  or 'state' not in item or type(item.get('attempts')) is not list))
      rows.append(('state', _pointer('units', uid, 'state'), uid, None))
      for i, attempt in enumerate(item['attempts']):
        _need(_text(attempt))
        aid = attempt.split('/')[-1]
        if attempt.startswith('dry-attempt-'):
          aid = None
        else:
          _need(ATTEMPT.fullmatch(aid))
        rows.append(('attempt', _pointer('units', uid, 'attempts', i), uid, aid))
    return rows
  uid, aid = data.get('task_id'), data.get('attempt_id')
  _need(not (not _text(uid) or not _text(aid) or not ATTEMPT.fullmatch(aid)
  or data.get('kind') not in ('code', 'slurm', 'pipeline')))
  if name == 'receipt.json':
    required = {'checked_at': str, 'notes': list, 'outputs': dict, 'basis': dict}
    _need(not (data.get('state') not in STATES or type(data.get('exit_code')) is not int
  or data['exit_code'] != STATES.index(data['state'])
  or 'job_id' not in data
  or any(type(data.get(k)) is not t for k, t in required.items())))
    tag = 'receipt'
  else:
    _need(not (type(data.get('declared_outputs')) is not list
  or type(data.get('pinned_inputs')) is not dict
  or type(data.get('budget')) is not dict or 'command' not in data))
    tag = 'attempt'
  return [(tag, _pointer(key), uid, aid) for key in sorted(data)]

def _provenance(text, parts):
  v = _pairs(line.split('=', 1) for line in _lines(text) if line)
  keys = {'schema', 'repo', 'origin', 'source_version', 'version', 'destination',
  'consumers', 'mode', 'installed_at', 'link_target', 'link_identity'}
  _need(not (set(v) != keys or v['schema'] != '2'
  or v['repo'] != 'multi-agent-skills'
  or v['origin'] not in ('authored', 'vendored')
  or v['mode'] not in ('copy', 'link')
  or not v['source_version'] or v['version'] != v['source_version']
  or not os.path.isabs(v['destination']) or not v['installed_at']
  or v['consumers'] and any(not c for c in v['consumers'].split(','))))
  dest = Path(v['destination'])
  skill = dest.name
  if parts[-1] == COPY_MARKER:
    _need(not (len(parts) < 2 or parts[-2] != skill or v['mode'] != 'copy'
  or v['link_target'] or v['link_identity']))
  else:
    digest = hashlib.sha256(os.fsencode(str(dest))).hexdigest()[:24]
    _need(not (parts[-1] != skill + '-' + digest + '.provenance'
  or v['mode'] != 'link' or not os.path.isabs(v['link_target'])
  or not v['link_identity']))
  return skill

def _lines(text):
  return text[:-1].split('\n') if text.endswith('\n') else text.split('\n')

def _memory_scalar(value):
  # This is the bounded frontmatter grammar, not a YAML loader. Quotes must
  # close on this line; unquoted YAML collection/property/block indicators
  # cannot stand in for a scalar. Apply this to unobserved metadata keys too.
  if '\r' in value:
    return False
  if value.startswith("'"):
    return re.fullmatch(r"'(?:[^']|'')*'", value) is not None
  if value.startswith('"'):
    return re.fullmatch(r'"(?:[^"\\]|\\(?:[0abtnvfre /"\\N_LP]|x[0-9a-fA-F]{2}|u[0-9a-fA-F]{4}|U[0-9a-fA-F]{8}))*"', value) is not None
  return (bool(value) and not value[0].isspace()
    and value[0] not in '|>&*[]{}!#%@`'
    and not re.search(r':(?:\s|$)|(?:^|\s)#|^[-?](?:\s|$)', value))

def _text_rows(name, role, text):
  lines = _lines(text)
  guidance = name in ('CLAUDE.md', 'AGENTS.md') or 'pitfall' in name
  if guidance and role != 'memory':
    return [('guidance', 1, max(1, len(lines)))]
  if name in ('evidence.md', 'SUMMARY.md'):
    return [('marker:' + line, i, i) for i, line in enumerate(lines, 1)
  if line in MARKERS]
  if name == 'PLAN.md':
    rows, needs, fence = [], False, None
    for i, line in enumerate(lines, 1):
      if fence:
        if re.fullmatch(r' {0,3}' + fence[0] + '{' + str(len(fence)) + r',}[ \t]*', line):
          fence = None
        continue
      opening = re.match(r' {0,3}(`{3,}|~{3,})(.*)$', line)
      if opening and (opening[1][0] != '`' or '`' not in opening[2]):
        fence = opening[1]
        continue
      if re.fullmatch(r'#{1,6} NEEDS YOU', line):
        needs = True
      elif line.startswith('#'):
        needs = False
      if (re.match(r'\|[ \t]*D-[A-Za-z0-9._-]+[ \t]*(?:\||$)', line)
  or re.match(r'[-*] [ \t]*NEEDS YOU(?:$|: )', line)
  or needs and re.match(r'[-*] [ \t]*\S', line)):
        rows.append(('decision', i, i))
    return rows
  if role == 'memory' and (name.endswith('.md') or guidance):
    _need(not (not lines or lines[0] != '---' or '---' not in lines[1:]))
    end = lines.index('---', 1)
    rows, seen, metadata, indent = [], set(), False, None
    for i in range(1, end):
      line = lines[i]
      if line == 'metadata:':
        _need('metadata' not in seen)
        seen.add('metadata')
        metadata = True
        continue
      top = re.fullmatch(r'(name|description): (.+)', line)
      nested = re.fullmatch(r'( +)([A-Za-z_][A-Za-z0-9_.-]*): (.+)', line)
      _need(top is not None or metadata and nested is not None)
      if top:
        metadata = False
        field, value = top.groups()
      else:
        width = len(nested[1])
        if indent is None:
          indent = width
        _need(width == indent)
        field, value = 'metadata.' + nested[2], nested[3]
      _need(_memory_scalar(value) and field not in seen)
      seen.add(field)
      if field in ('name', 'description', 'metadata.type'):
        rows.append(('memory:' + field, i + 1, i + 1))
    _need({'name', 'description', 'metadata.type'} <= seen)
    if end + 1 < len(lines):
      rows.append(('memory:body', end + 2, len(lines)))
    if guidance:
      rows.append(('guidance', 1, max(1, len(lines))))
    return rows
  raise ValueError('unsupported format')

def _stamp(st):
  return (st.st_dev, st.st_ino, st.st_mode, st.st_size, st.st_mtime_ns, st.st_ctime_ns)

def _read_file(parent, name, st, cap):
  fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
  with os.fdopen(fd, 'rb') as stream:
    if _stamp(os.fstat(stream.fileno())) != _stamp(st):
      return None
    data = stream.read(cap)
    after = os.fstat(stream.fileno())
  current = os.stat(name, dir_fd=parent, follow_symlinks=False)
  if (_stamp(st) != _stamp(after)
  or _stamp(st) != _stamp(current) or len(data) != st.st_size):
    return None
  return data

def _open_root(path):
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
  caps = {'per_file_bytes': PER_FILE_BYTES, 'files_per_root': PER_ROOT_FILES,
  'total_bytes': TOTAL_BYTES, 'max_depth': 128}
  for key, value in (budgets or {}).items():
    _need(not (key not in caps or type(value) is not int or not 0 < value <= caps[key]))
    caps[key] = value
  labels = set()
  for root in roots:
    _need(not (set(root) != {'label', 'role', 'path'} or not _text(root['label'])
  or root['label'] in labels
  or root['role'] not in ('project', 'memory', 'state', 'helpers')))
    labels.add(root['label'])
  out = {'evidence': {}, 'cohorts': {}, 'omissions': [], 'provenance': {}}
  members = {p: [] for p in POPULATIONS}
  used = 0
  aliases = set()

  def omit(label, loc, reason):
    out['omissions'].append(dict(source_root=label, locator=loc, reason=reason))

  def adapt(label, role, parts, data):
    loc, name = '/'.join(parts), parts[-1]
    digest = hashlib.sha256(data).hexdigest()
    uid, aid = _owner(parts)
    if role == 'helpers':
      _need(Path(name).suffix in ('.py', '.sh', '.bash', '.zsh', '.fish', '.csh',
        '.ksh', '.pl', '.rb', '.js', '.mjs', '.cjs', '.ts', '.lua', '.awk', '.r', '.R', '.ps1')
        or data.startswith(b'#!'))
      rows = [('helper', {'line_range': [1, 1]}, None, None)]
    else:
      text = data.decode('utf-8')
      if name == COPY_MARKER or len(parts) > 1 and parts[-2] == SIDECARS:
        skill = _provenance(text, parts)
        _need(not (skill in out['provenance'] and out['provenance'][skill] != text))
        out['provenance'][skill] = text
        rows = [('provenance', {'line_range': [1, len(_lines(text))]}, None, None)]
      elif role == 'state' and name in ('swarm-state.json', 'unit.json', 'receipt.json'):
        rows = [(a, {'json_pointer': p}, u, t) for a, p, u, t in _structured(name, _json(text))]
      elif name == 'plan.json' and role in ('project', 'state'):
        plan = _json(text)
        _need(not (type(plan) is not dict or type(plan.get('units')) is not list))
        rows = [('plan', {'json_pointer': '/units'}, None, None)]
      else:
        rows = [(a, {'line_range': [s, e]}, uid, aid)
  for a, s, e in _text_rows(name, role, text)]
    if not rows:
      omit(label, loc, 'skipped: no matching observations')
      return
    if len(out['evidence']) + len(rows) > 999:
      omit(label, loc, 'over-budget: evidence ids')
      return
    doc = _identity(label, loc)
    if name in ('CLAUDE.md', 'AGENTS.md') and role != 'helpers':
      doc = _identity(label, parts[:-1], 'guidance', digest)
      if doc in aliases:
        rows = [('guidance-alias' if tag == 'guidance' else tag, loc, u, a)
  for tag, loc, u, a in rows]
      aliases.add(doc)
    for tag, location, unit, attempt in rows:
      ref = 'E%03d' % (len(out['evidence']) + 1)
      out['evidence'][ref] = dict(source_root=label, locator=loc, sha256=digest,
  location=location, unit_id=unit, attempt_id=attempt,
  adapter={'name': tag, 'version': '1'}, status='ok')
      if unit is not None:
        members['units'].append(unit)
      if attempt is not None:
        members['attempts'].append(_identity(unit, attempt))
      if tag in ('state', 'attempt', 'receipt'):
        members['state_entries'].append(ref)
    members['helper_scripts' if role == 'helpers' else 'evidence_documents'].append(doc)

  for root in sorted(roots, key=lambda r: r['label']):
    label, role = root['label'], root['role']
    try:
      path = Path(os.path.realpath(root['path']))
      if '.git' in path.parts or path == Path(os.path.realpath(os.path.expanduser('~'))) or path == Path('/'):
        omit(label, '.', 'skipped: forbidden root')
        continue
      root_fd = _open_root(path)
    except OSError:
      omit(label, '.', 'skipped: unreadable or symlink root')
      continue
    count = 0

    def walk(fd, prefix):
      nonlocal count, used
      names = []
      with os.scandir(fd) as entries:
        for entry in entries:
          names.append(entry.name)
      for name in sorted(names):
        parts = prefix + [name]
        loc = '/'.join(parts)
        if count >= caps['files_per_root'] or used >= caps['total_bytes']:
          omit(label, '.', 'over-budget: uninspected remainder')
          return
        if name == '.git' or name.endswith('.jsonl'):
          omit(label, loc, 'skipped: excluded input')
          continue
        try:
          st = os.stat(name, dir_fd=fd, follow_symlinks=False)
          if stat.S_ISLNK(st.st_mode):
            omit(label, loc, 'skipped: symlink')
          elif stat.S_ISDIR(st.st_mode):
            if len(parts) > caps['max_depth']:
              omit(label, loc, 'over-budget: max_depth')
              continue
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            try:
              walk(child, parts)
            finally:
              os.close(child)
          elif not stat.S_ISREG(st.st_mode):
            omit(label, loc, 'unsupported: file type')
          else:
            count += 1
            if st.st_size > caps['per_file_bytes']:
              omit(label, loc, 'over-budget: per-file bytes')
              continue
            if st.st_size > caps['total_bytes'] - used:
              omit(label, loc, 'over-budget: total bytes')
              continue
            known = (name in ('CLAUDE.md', 'AGENTS.md', 'PLAN.md', 'evidence.md',
  'SUMMARY.md', COPY_MARKER) or 'pitfall' in name
  or len(parts) > 1 and parts[-2] == SIDECARS
  or role == 'memory' and name.endswith('.md')
  or role == 'state' and name in ('swarm-state.json', 'unit.json', 'receipt.json')
  or role in ('project', 'state') and name == 'plan.json'
  or role == 'helpers')
            if not known:
              omit(label, loc, 'unsupported: format')
              continue
            used += st.st_size
            data = _read_file(fd, name, st, st.st_size)
            if data is None:
              omit(label, loc, 'unstable: changed during read')
            else:
              try:
                adapt(label, role, parts, data)
              except (ValueError, UnicodeError, RecursionError):
                omit(label, loc, 'unsupported: format or encoding')
            if st.st_size == caps['per_file_bytes']:
              omit(label, loc, 'limit reached: per-file bytes (retained if stable)')
        except OSError:
          omit(label, loc, 'skipped: unreadable or changed path')
    try:
      walk(root_fd, [])
    except OSError:
      omit(label, '.', 'skipped: unreadable directory')
    finally:
      os.close(root_fd)
    if count == caps['files_per_root'] or used == caps['total_bytes']:
      omit(label, '.', 'limit reached: files or total bytes')
  for population, ids in members.items():
    if ids:
      out['cohorts']['C%03d' % (len(out['cohorts']) + 1)] = build_cohort(population, ids)
  return out
