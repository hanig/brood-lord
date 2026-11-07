"""Raw archive contract; randomized oracle uses seed 601106, 120 cases."""; import hashlib; import json; import os; from pathlib import Path; import random; import stat; import subprocess; import sys; import tempfile; import time; import unittest; from unittest import mock; sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/hanig-swarm/scripts')); import usage_raw as U

def event(n, **overrides):
 counters = {key: n for key in U.COUNTERS}; counters.update(overrides); return (json.dumps({'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {'total_token_usage': counters}}}) + '\n').encode()

class TestRawUsage(unittest.TestCase):

 def setUp(self):
  temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup); self.root = Path(temporary.name); self.state = self.root / 'state'; self.state.mkdir(); self.home = self.root / 'home'; (self.home / 'sessions' / 'nested').mkdir(parents=True); self.rollout = self.home / 'sessions' / 'nested' / 'rollout-a.jsonl'; self.rollout.write_bytes(event(3))

 def capture(self, final=True, attempt='a'):
  (U.finish if final else U.capture)(self.state, attempt, self.home); return self.state / 'usage-raw' / attempt

 def usage(self):
  return U.attempt_usage(self.state, 'a')

 def unknown(self, reason=None):
  result = self.usage(); self.assertEqual(result['status'], 'unknown', result)
  if reason:
   self.assertEqual(result['reason'], reason)

 def test_idempotent_append_only_and_private(self):
  target = self.capture(); before = {p.name: p.read_bytes() for p in target.iterdir()}; self.capture(); self.assertEqual(before, {p.name: p.read_bytes() for p in target.iterdir()}); self.rollout.write_bytes(event(3) + event(8)); self.capture()
  for name, data in before.items():
   if name == 'manifest.jsonl':
    self.assertTrue((target / name).read_bytes().startswith(data))
   else:
    self.assertEqual((target / name).read_bytes(), data)
  self.assertEqual(self.usage()['totals']['total_tokens'], 8)
  for path in target.iterdir():
   self.assertEqual(stat.S_IMODE(path.stat().st_mode), 384)

 def test_concurrent_append_digest_matches_copied_bytes(self):
  parent = os.getpid(); read = os.read; appended = False

  def append_during_copy(fd, count):
   nonlocal appended; chunk = read(fd, count)
   if os.getpid() != parent and chunk and (not appended):
    appended = True
    with self.rollout.open('ab') as stream:
     stream.write(event(4))
   return chunk
  with mock.patch.object(U.os, 'read', side_effect=append_during_copy):
   target = self.capture()
  row = U._rows(target / 'manifest.jsonl')[0]; data = (target / row['blob']).read_bytes(); self.assertEqual(row['bytes'], len(data)); self.assertEqual(row['sha256'], hashlib.sha256(data).hexdigest())

 def test_fifo_symlink_and_symlink_directory_fault_without_read(self):
  sessions = self.home / 'sessions'; os.mkfifo(str(sessions / 'rollout-pipe.jsonl')); (sessions / 'rollout-link.jsonl').symlink_to(self.rollout); (sessions / 'alias').symlink_to(sessions / 'nested', target_is_directory=True); started = time.monotonic(); target = self.capture(); self.assertLess(time.monotonic() - started, 2); self.assertEqual(sum(('fault' in r for r in U._rows(target / 'manifest.jsonl'))), 3); self.unknown('capture_fault')

 def test_missing_home_and_bounded_stalled_read_and_cap(self):
  U.finish(self.state, 'gone', self.root / 'absent'); self.assertEqual(U._rows(self.state / 'usage-raw/gone/manifest.jsonl')[0]['fault'], 'no_home'); parent = os.getpid(); read = os.read

  def stalled(*args):
   if os.getpid() != parent:
    time.sleep(10)
   return read(*args)
  with mock.patch.object(U.os, 'read', side_effect=stalled):
   started = time.monotonic()
   with self.assertRaisesRegex(ValueError, 'timeout'):
    U._read(self.rollout, seconds=0.1)
   self.assertLess(time.monotonic() - started, 1)
  with self.assertRaisesRegex(ValueError, 'byte_cap'):
   U._read(self.rollout, cap=1)
  with mock.patch.object(U, '_read', side_effect=ValueError('timeout')):
   self.capture()
  self.unknown('capture_fault')

 def test_final_required(self):
  self.capture(final=False); self.unknown('incomplete')

 def test_prefix_required_for_same_relpath(self):
  self.capture(); self.rollout.write_bytes(event(1)); self.capture(); self.unknown('rewritten')

 def test_negative_counter_required(self):
  self.rollout.write_bytes(event(2, input_tokens=-1)); self.capture(); self.unknown('invalid_counters')

 def test_parser_rule_fixtures(self):
  cases = [(b'', 'no_token_count'), (b'{}\n', 'no_token_count'), (event(0), None), (event(3) + event(2), 'decreased'), (event(3) + b'{bad}\n', 'invalid_json'), (event(3) + b'{partial', None), (event(3) + b'{"token_count"', 'invalid_json'), (event(2, input_tokens=None), 'invalid_counters'), (event(2, input_tokens=True), 'invalid_counters'), (event(2, input_tokens=1.5), 'invalid_counters'), (b'{"payload":{"type":"token_count","info":null}}\n', 'invalid_counters'), (b'{"payload":{"type":"token_count","info":{"total_token_usage":{}}}}\n', 'invalid_counters'), (event(2) + b'{"payload":{"type":"token_count","usage":{}}}\n', 'invalid_counters'), (b'{"type":"turn_context"}\n' + event(2), None)]
  for index, (data, reason) in enumerate(cases):
   with self.subTest(index=index):
    self.rollout.write_bytes(data); self.capture(attempt=str(index)); result = U.attempt_usage(self.state, str(index)); self.assertEqual(result['status'], 'unknown' if reason else 'ok', result)
    if reason:
     self.assertEqual(result['reason'], reason)
    else:
     self.assertEqual(result['totals'], {k: 0 if data == event(0) else 2 if data.endswith(event(2)) else 3 for k in U.COUNTERS})

 def test_missing_corrupt_manifest_missing_blob_digest_and_multiple(self):
  target = self.capture(); manifest = target / 'manifest.jsonl'; original = manifest.read_bytes(); manifest.unlink(); self.unknown(); manifest.write_bytes(b'{bad}\n'); self.unknown(); manifest.write_bytes(b'{"type":"final"}\n'); self.unknown('corrupt_manifest'); manifest.write_bytes(original); blob = target / U._rows(manifest)[0]['blob']; data = blob.read_bytes(); blob.unlink(); self.unknown(); blob.write_bytes(b'x' * len(data)); self.unknown('corrupt_blob'); blob.write_bytes(data); self.rollout.unlink(); (self.rollout.parent / 'rollout-b.jsonl').write_bytes(event(1)); self.capture(); self.unknown('multiple_rollouts')

 def test_randomized_oracle_and_attempt_isolation(self):
  rng = random.Random(601106); self.capture(attempt='control')
  for index in range(120):
   values = sorted((rng.randrange(20) for _ in range(rng.randrange(7)))); data = b'{}\n'.join((event(n) for n in values)); mode = rng.randrange(8); self.rollout.write_bytes(data); attempt = str(index); self.capture(attempt=attempt, final=mode != 0); target = self.state / 'usage-raw' / attempt; expected = values[-1] if values else None
   if mode == 0:
    expected = None
   elif mode in (1, 2, 3):
    self.rollout.write_bytes(data[:len(data) // 2] if mode == 1 else event(-1) if mode == 2 else event(99)); self.capture(attempt=attempt); expected = 99 if mode == 3 and (not data) else None
   elif mode == 4:
    (target / 'manifest.jsonl').unlink(); expected = None
   elif mode == 5:
    (target / U._rows(target / 'manifest.jsonl')[0]['blob']).unlink(); expected = None
   elif mode == 6:
    self.rollout.write_bytes(data + b'not json\n'); self.capture(attempt=attempt); expected = None
   result = U.attempt_usage(self.state, attempt)
   if result['status'] == 'ok':
    self.assertIsNotNone(expected, (index, mode, result)); self.assertEqual(result['totals'], {k: expected for k in U.COUNTERS})
   self.assertEqual(U.attempt_usage(self.state, 'control')['totals']['total_tokens'], 3)

 def test_report_pure_unknown_counts_finish_dates_and_filters(self):
  self.capture(); state = {'units': {'u': {'attempts': ['/run/a', '/run/b', 'dry-attempt-/run/dry'], 'attempt_codex_homes': {'a': {'finished_at': 1793925000}, 'b': {'finished_at': 1793925000}}}}}; before = {str(p): (p.stat().st_mtime_ns, p.read_bytes()) for p in self.state.rglob('*') if p.is_file()}; result = U.report(self.state, state); self.assertEqual((result['measured_count'], result['unknown_count']), (1, 1)); self.assertEqual(result['totals']['total_tokens'], 3); self.assertEqual(U.report(self.state, state, unit='other')['totals'], None); self.assertEqual(U.report(self.state, state, since='2099-01-01')['measured_count'], 0); self.assertEqual(before, {str(p): (p.stat().st_mtime_ns, p.read_bytes()) for p in self.state.rglob('*') if p.is_file()})

 def test_usage_cli_reads_preserved_copy_without_home(self):
  target = self.capture(); import shutil; shutil.rmtree(self.home); (self.state / 'swarm-state.json').write_text(json.dumps({'units': {'u': {'attempts': ['/run/a'], 'attempt_codex_homes': {'a': {'finished_at': 1793925000}}}}})); before = {str(p): p.read_bytes() for p in self.state.rglob('*') if p.is_file()}; result = subprocess.run([sys.executable, '-B', str(Path(U.__file__).with_name('swarm.py')), 'usage', '--state-dir', str(self.state), '--json'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30); self.assertEqual(result.returncode, 0, result.stderr); self.assertEqual(json.loads(result.stdout)['totals']['total_tokens'], 3); self.assertEqual(before, {str(p): p.read_bytes() for p in self.state.rglob('*') if p.is_file()})

 def test_reverted_snapshot_is_unknown(self):
  self.capture(final=False); self.rollout.write_bytes(event(3) + event(8)); self.capture(final=False); self.rollout.write_bytes(event(3)); self.capture(); self.unknown('rewritten')

 def test_disappeared_source_is_unknown(self):
  self.capture(final=False); self.rollout.write_bytes(event(3) + event(8)); self.rollout.unlink(); self.capture(); self.unknown('capture_fault')

 def test_nonstandard_json_constants(self):
  import itertools
  for i, (constant, nested) in enumerate(itertools.product((b'NaN', b'Infinity', b'-Infinity'), (False, True))):
   data = b'{"type":"other","x":' + constant + b'}' if nested else constant; self.rollout.write_bytes(event(3) + data + b'\n'); self.capture(attempt=str(i)); self.assertEqual(U.attempt_usage(self.state, str(i))['status'], 'unknown')

 def test_capture_bounds_manifest_and_metadata_stalls(self):
  self.capture(final=False); real_open, real_stat, real_walk, real_fdopen = (os.open, Path.lstat, os.walk, os.fdopen)

  def opened(path, flags, *args, **kw):
   if str(path).endswith('manifest.jsonl') and flags & os.O_ACCMODE == os.O_RDONLY:
    time.sleep(0.3)
   return real_open(path, flags, *args, **kw)

  def stated(path, *args, **kw):
   if path == self.rollout:
    time.sleep(0.3)
   return real_stat(path, *args, **kw)

  def walked(*args, **kw):
   time.sleep(0.3); return real_walk(*args, **kw)

  def stream(fd, mode, *args, **kw):
   value = real_fdopen(fd, mode, *args, **kw)
   if mode != 'r':
    return value

   class Slow:

    def __enter__(self):
     return self

    def __exit__(self, *exc):
     value.close()

    def __iter__(self):
     time.sleep(0.3); return iter(value)
   return Slow()
  for target, name, fn in ((U.os, 'open', opened), (Path, 'lstat', stated), (U.os, 'walk', walked), (U.os, 'fdopen', stream)):
   with self.subTest(name=name):
    with mock.patch.object(U, 'READ_SECONDS', 0.05), mock.patch.object(target, name, new=fn):
     with self.assertRaisesRegex(ValueError, 'timeout'):
      U.capture(self.state, 'a', self.home)
    self.unknown('capture_fault')

 def test_unavailable_timezone_preserves_unknown_without_failing(self):
  from zoneinfo import ZoneInfoNotFoundError; self.capture(); state = {'units': {'u': {'attempts': ['a'], 'attempt_codex_homes': {'a': {'finished_at': 1793925000}}}}}
  with mock.patch('zoneinfo.ZoneInfo', side_effect=ZoneInfoNotFoundError('fixture')):
   result = U.report(self.state, state, tz='Europe/Berlin'); self.assertEqual(result['unknown_count'], 1); self.assertEqual(result['unknown'][0]['reason'], 'timezone_unavailable'); self.assertIsNone(result['totals']); self.assertEqual(U.report(self.state, state)['measured_count'], 1)
if __name__ == '__main__':
 unittest.main()
