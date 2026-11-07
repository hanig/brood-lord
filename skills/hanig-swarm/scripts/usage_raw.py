"""Append-only local rollout capture and pure, conservative usage reporting.

One coordinator owns the archive. Agent-authored transcripts attest usage;
none of this module's data confers execution or closure authority.
"""
import datetime as dt; import hashlib; import json; import os; from pathlib import Path; import re; import select; import signal; import stat; import tempfile; import time; COUNTERS = ('input_tokens', 'cached_input_tokens', 'output_tokens', 'reasoning_output_tokens', 'total_tokens', 'cache_write_input_tokens'); READ_SECONDS = 5.0; BYTE_CAP = 32 * 1024 * 1024

def archive(state_dir, attempt):
 if not isinstance(attempt, str) or not re.fullmatch('[A-Za-z0-9_-]+', attempt):
  raise ValueError('invalid usage attempt id')
 root = Path(state_dir) / 'usage-raw'; target = root / attempt
 for directory in (root, target):
  try:
   directory.mkdir(mode=448, parents=True); os.chmod(str(directory), 448); _sync_dir(directory.parent)
  except FileExistsError:
   pass
  if not stat.S_ISDIR(directory.lstat().st_mode):
   raise ValueError('usage archive must be a real directory')
 return target

def _regular(path):
 fd = os.open(str(path), os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
 if not stat.S_ISREG(os.fstat(fd).st_mode):
  os.close(fd); raise ValueError('non_regular')
 return fd

def _read_worker(path, writer):
 status = 1
 try:
  if callable(path):
   os.setsid(); path(); status = 0; return
  fd = _regular(path)
  try:
   while True:
    chunk = os.read(fd, 65536)
    if not chunk:
     status = 0
     break
    while chunk:
     chunk = chunk[os.write(writer, chunk):]
  finally:
   os.close(fd)
 except Exception:
  pass
 finally:
  os._exit(status)

def _bounded(path, seconds=READ_SECONDS, cap=BYTE_CAP):
 """Bound regular-file reads in a child, including a stalled filesystem.

    The parent reads only a pipe. It never waits unboundedly to reap a child
    that is stuck in kernel I/O, and has no multiprocessing exit-time join.
    """
 reader, writer = os.pipe()
 try:
  pid = os.fork()
 except BaseException:
  os.close(reader); os.close(writer); raise
 if pid == 0:
  os.close(reader); _read_worker(path, writer)
 os.close(writer); deadline = time.monotonic() + seconds; chunks, size, reaped = ([], 0, False)
 try:
  while True:
   remaining = deadline - time.monotonic()
   if remaining <= 0 or not select.select([reader], [], [], remaining)[0]:
    raise ValueError('timeout')
   chunk = os.read(reader, 65536)
   if not chunk:
    while time.monotonic() < deadline:
     child, status = os.waitpid(pid, os.WNOHANG)
     if child:
      reaped = True
      if status != 0:
       raise ValueError('read_error')
      return b''.join(chunks)
     time.sleep(0.001)
    raise ValueError('timeout')
   size += len(chunk)
   if size > cap:
    raise ValueError('byte_cap')
   chunks.append(chunk)
 finally:
  os.close(reader)
  if not reaped:
   try:
    if callable(path):
     try:
      os.killpg(pid, signal.SIGKILL)
     except ProcessLookupError:
      pass
    os.kill(pid, signal.SIGKILL)
   except ProcessLookupError:
    pass
   until = time.monotonic() + 0.05
   while time.monotonic() < until:
    if os.waitpid(pid, os.WNOHANG)[0]:
     break
    time.sleep(0.001)

def _read(path, seconds=READ_SECONDS, cap=BYTE_CAP):
 return _bounded(path, seconds, cap)

def capture(state_dir, attempt_id, codex_home):
 try:
  _bounded(lambda: _capture(state_dir, attempt_id, codex_home), READ_SECONDS)
 except Exception:
  try:
   _bounded(lambda: append(archive(state_dir, attempt_id), {'fault': 'capture_error'}), READ_SECONDS)
  except Exception:
   pass
  raise

def _invalid_constant(value):
 raise ValueError('invalid_json')

def _rows(path):
 fd = _regular(path)
 with os.fdopen(fd, 'r', encoding='utf-8') as stream:
  return [json.loads(line, parse_constant=_invalid_constant) for line in stream]

def _sync_dir(path):
 fd = os.open(str(path), os.O_RDONLY)
 try:
  os.fsync(fd)
 finally:
  os.close(fd)

def append(target, row):
 row = dict(row, captured_at=dt.datetime.now(dt.timezone.utc).isoformat()); fd = os.open(str(target / 'manifest.jsonl'), os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 384)
 with os.fdopen(fd, 'ab') as stream:
  if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
   raise ValueError('manifest must be regular')
  os.fchmod(stream.fileno(), 384); stream.write((json.dumps(row, sort_keys=True) + '\n').encode()); stream.flush(); os.fsync(stream.fileno())
 _sync_dir(target)

def _capture(state_dir, attempt_id, codex_home):
 target = archive(state_dir, attempt_id); manifest = target / 'manifest.jsonl'; rows = _rows(manifest) if manifest.exists() else []; known = {(r.get('relpath'), r.get('sha256')) for r in rows}; latest = {r['relpath']: r['sha256'] for r in rows if 'sha256' in r}; seen = set()

 def fault(relpath, reason):
  if not any((r.get('relpath') == relpath and r.get('fault') == reason for r in rows)):
   append(target, {'relpath': relpath, 'fault': reason})
 home = Path(codex_home)
 if not home.exists():
  fault('', 'no_home'); return
 if not stat.S_ISDIR(os.stat(str(home), follow_symlinks=False).st_mode):
  fault('', 'non_regular_home'); return
 sessions = home / 'sessions'
 try:
  session_mode = os.stat(str(sessions), follow_symlinks=False).st_mode
 except FileNotFoundError:
  for path in latest:
   fault(path, 'disappeared')
  return
 except OSError:
  fault('sessions', 'enumeration_error'); return
 if not stat.S_ISDIR(session_mode):
  fault('sessions', 'non_regular'); return

 def walk_error(error):
  fault('sessions', 'enumeration_error')
 for directory, dirs, files in os.walk(str(sessions), followlinks=False, onerror=walk_error):
  for name in list(dirs):
   path = Path(directory) / name
   if path.is_symlink():
    dirs.remove(name); fault(str(path.relative_to(home)), 'symlink')
  for name in files:
   if not (name.startswith('rollout-') and name.endswith('.jsonl')):
    continue
   path = Path(directory) / name; relpath = str(path.relative_to(home)); seen.add(relpath)
   try:
    if not stat.S_ISREG(path.lstat().st_mode):
     raise ValueError('non_regular')
    data = _read(path)
   except (OSError, ValueError, EOFError) as exc:
    fault(relpath, str(exc) if isinstance(exc, ValueError) else 'read_error')
    continue
   digest = hashlib.sha256(data).hexdigest()
   if (relpath, digest) in known:
    if latest.get(relpath) != digest:
     fault(relpath, 'rewritten')
    continue
   number = len(list(target.iterdir()))
   while (target / ('%d-%s' % (number, digest))).exists():
    number += 1
   blob = target / ('%d-%s' % (number, digest)); fd, temporary = tempfile.mkstemp(prefix='.capture-', dir=str(target))
   try:
    with os.fdopen(fd, 'wb') as stream:
     os.fchmod(stream.fileno(), 384); stream.write(data); stream.flush(); os.fsync(stream.fileno())
    os.rename(temporary, blob); _sync_dir(target); append(target, {'relpath': relpath, 'bytes': len(data), 'sha256': digest, 'blob': blob.name})
   finally:
    if os.path.exists(temporary):
     os.unlink(temporary)
 for path in latest.keys() - seen:
  fault(path, 'disappeared')

def finish(state_dir, attempt, home):
 capture(state_dir, attempt, home); _bounded(lambda: _finalize(state_dir, attempt), READ_SECONDS)

def _finalize(state_dir, attempt):
 target = archive(state_dir, attempt); rows = _rows(target / 'manifest.jsonl') if (target / 'manifest.jsonl').exists() else []
 if not rows or rows[-1].get('type') != 'final':
  append(target, {'type': 'final'})

def _tokens(data):
 previous = None; lines = data.splitlines(keepends=True)
 for index, line in enumerate(lines):
  try:
   event = json.loads(line, parse_constant=_invalid_constant)
  except (ValueError, UnicodeError):
   if index == len(lines) - 1 and (not line.endswith(b'\n')) and (b'token_count' not in line):
    continue
   raise ValueError('invalid_json')
  if not isinstance(event, dict):
   continue
  payload = event.get('payload')
  if not isinstance(payload, dict) or payload.get('type') != 'token_count':
   continue
  info = payload.get('info'); counters = info.get('total_token_usage') if isinstance(info, dict) else None
  if not isinstance(counters, dict) or set(counters) != set(COUNTERS):
   raise ValueError('invalid_counters')
  if any((type(counters[k]) is not int or counters[k] < 0 for k in COUNTERS)):
   raise ValueError('invalid_counters')
  if previous is not None and any((counters[k] < previous[k] for k in COUNTERS)):
   raise ValueError('decreased')
  previous = counters
 if previous is None:
  raise ValueError('no_token_count')
 return previous

def attempt_usage(state_dir, attempt):
 """Read only. Faults and malformed evidence fail closed for this attempt."""
 try:
  if not isinstance(attempt, str) or not re.fullmatch('[A-Za-z0-9_-]+', attempt):
   raise ValueError('invalid_attempt')
  target = Path(state_dir) / 'usage-raw' / attempt; rows = _rows(target / 'manifest.jsonl')
  if not rows or any((not isinstance(r, dict) or not isinstance(r.get('captured_at'), str) for r in rows)):
   raise ValueError('corrupt_manifest')
  if len({r.get('relpath') for r in rows if 'sha256' in r and isinstance(r.get('relpath'), str)}) > 1:
   raise ValueError('multiple_rollouts')
  if any((r.get('fault') == 'rewritten' for r in rows)):
   raise ValueError('rewritten')
  if any(('fault' in r or r.get('type') == 'capture_abandoned' for r in rows)):
   raise ValueError('capture_fault')
  if rows[-1].get('type') != 'final':
   raise ValueError('incomplete')
  files = {}
  for row in rows:
   if row.get('type') == 'final':
    if set(row) != {'type', 'captured_at'}:
     raise ValueError('corrupt_manifest')
    continue
   if set(row) != {'relpath', 'blob', 'bytes', 'sha256', 'captured_at'}:
    raise ValueError('corrupt_manifest')
   relpath, blob = (row['relpath'], row['blob'])
   if not isinstance(relpath, str) or not re.fullmatch('[0-9]+-[0-9a-f]{64}', blob):
    raise ValueError('corrupt_manifest')
   fd = _regular(target / blob)
   with os.fdopen(fd, 'rb') as stream:
    data = stream.read()
   if type(row['bytes']) is not int or len(data) != row['bytes'] or hashlib.sha256(data).hexdigest() != row['sha256'] or (not blob.endswith('-' + row['sha256'])):
    raise ValueError('corrupt_blob')
   if relpath in files and (not data.startswith(files[relpath])):
    raise ValueError('rewritten')
   files[relpath] = data
  if len(files) > 1:
   raise ValueError('multiple_rollouts')
  if not files:
   raise ValueError('no_token_count')
  return {'status': 'ok', 'totals': _tokens(next(iter(files.values())))}
 except (OSError, ValueError, KeyError, TypeError, UnicodeError, RecursionError):
  import sys; exc = sys.exc_info()[1]; reason = str(exc) if type(exc) is ValueError else 'corrupt_or_missing_archive'; return {'status': 'unknown', 'reason': reason}

def report(state_dir, state, unit=None, since=None, tz='UTC'):
 if tz == 'UTC':
  zone = dt.timezone.utc
 else:
  from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
  try:
   zone = ZoneInfo(tz)
  except ZoneInfoNotFoundError:
   zone = None
 from swarm_preflight import DRY_PREFIX; cutoff = dt.date.fromisoformat(since) if since else None; measured, unknown, days = ([], [], {}); totals = {key: 0 for key in COUNTERS}
 for uid, us in (state.get('units') or {}).items():
  if unit is not None and unit != uid:
   continue
  attempts = {Path(p).name for p in us.get('attempts', []) if not str(p).startswith(DRY_PREFIX)}; attempts.update((us.get('attempt_codex_homes') or {}).keys())
  for attempt in sorted(attempts):
   result = attempt_usage(state_dir, attempt); meta = (us.get('attempt_codex_homes') or {}).get(attempt) or {}; finished = meta.get('finished_at'); day = dt.datetime.fromtimestamp(finished, zone).date() if zone is not None and isinstance(finished, (int, float)) else None
   if cutoff and day is not None and (day < cutoff):
    continue
   if day is None:
    result = {'status': 'unknown', 'reason': 'missing_finish_time' if zone else 'timezone_unavailable'}
   row = dict(result, unit=uid, attempt=attempt, day=day.isoformat() if day else None)
   if result['status'] == 'unknown':
    unknown.append(row)
    continue
   measured.append(row); daily = days.setdefault(row['day'], {k: 0 for k in COUNTERS})
   for key in COUNTERS:
    daily[key] += row['totals'][key]; totals[key] += row['totals'][key]
 return {'measured': measured, 'unknown': unknown, 'measured_count': len(measured), 'unknown_count': len(unknown), 'totals': totals if measured else None, 'days': days, 'timezone': tz, 'evidence': 'agent-attested'}
