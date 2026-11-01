"""U8 exit-review regressions: locale keys, caller locators and fixture storage."""
import errno
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'skills/hanig-swarm/scripts'))
import coordinator_paths as paths
from tests.fixtures.d5.ready import ready_installation, missing_installation, readiness


class TestU8ExitRegressions(unittest.TestCase):
    def test_locale_regressions_accept_unicode_tmpdir(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / '\u4e2d'
            directory.mkdir()
            prefix = 'tests.test_d5_exit_regressions.TestU8ExitRegressions.'
            result = subprocess.run([sys.executable, '-B', '-m', 'unittest',
                prefix + 'test_surrogateescaped_cwd_keeps_default_paths_usable',
                prefix + 'test_distinct_euc_jp_contexts_keep_separate_defaults'],
                cwd=ROOT, env=dict(os.environ, TMPDIR=str(directory)),
                capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, (result.stdout, result.stderr))

    def test_locale_tmpdir_fixture_accepts_ascii_filesystem_encoding(self):
        # Recovery-only reproducer: the fixture chooses an unencodable name
        # even when the incoming TMPDIR is unset. No fix is applied here.
        mkdir = os.mkdir
        def ascii_mkdir(path, *args, **kwargs):
            value = os.fspath(path)
            if isinstance(value, str):
                value.encode('ascii', 'surrogateescape')
            return mkdir(path, *args, **kwargs)
        with mock.patch.dict(os.environ):
            os.environ.pop('TMPDIR', None)
            with mock.patch.object(os, 'mkdir', ascii_mkdir):
                self.test_locale_regressions_accept_unicode_tmpdir()

    def test_explicit_tilde_state_is_contained(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            installed = ready_installation(root / 'installed')
            state = Path(installed['state_dir'])
            relative_home = os.path.relpath(state, Path.home().resolve())
            locator = '~/' + str(relative_home)
            self.assertEqual(paths.resolve_paths(state_dir=locator, cwd=root)[0], state)
            runs = root / 'runs'
            command = [sys.executable, '-B', str(ROOT / 'skills/hanig-swarm/scripts/unit.py'), 'allocate', '--state-dir', locator, '--root', str(runs), '--task', 'synthetic', '--kind', 'pipeline', '--output', 'out']
            absolute = list(command)
            absolute[absolute.index('--state-dir') + 1] = str(state)
            control = subprocess.run(absolute, cwd=root, capture_output=True, text=True, timeout=30)
            self.assertNotEqual(control.returncode, 0, (control.stdout, control.stderr))
            self.assertIn('D5 active:', control.stderr)
            self.assertFalse(runs.exists())
            result = subprocess.run(command, cwd=root, capture_output=True, text=True, timeout=30)
            self.assertNotEqual(result.returncode, 0, (result.stdout, result.stderr))
            self.assertIn('D5 active:', result.stderr)
            self.assertFalse(runs.exists())

    def test_surrogateescaped_cwd_keeps_default_paths_usable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            context = root / 'raw-\udcff'
            expected = hashlib.sha256(os.fsencode(context)).hexdigest()[:12]
            with mock.patch.object(os, 'getcwd', return_value=str(context)), \
                    mock.patch.dict(os.environ, {'XDG_STATE_HOME': str(root / 'xdg')}):
                state, runs, worktrees = paths.resolve_paths(need_root=True)
            self.assertEqual(state.parent.name, 'raw-' + expected)
            self.assertEqual(runs, state.parent / 'runs')
            self.assertEqual(worktrees, [])

    def test_other_direct_entries_expand_tilde_before_classification(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            home = root / 'home'
            installed = ready_installation(home)
            state = Path(installed['state_dir'])
            stage = root / 'stage'
            # No stage/plan exists: an inactive route cannot reach real work.
            commands = [
                ('hanig-swarm', 'unit', ['bind', str(stage), '--job-id', '7']),
                ('hanig-swarm', 'unit', ['check', str(stage)]),
                ('hanig-swarm', 'remote_verify', ['--worker', str(stage)]),
                ('hanig-swarm', 'remote_verify', ['--supervise', str(stage)]),
                ('hanig-orchestrate', 'merge_unit', [str(root / 'plan.json'),
                    '--unit', 'synthetic', '--pr', '1', '--approver', 'fixture']),
                ('hanig-swarm', 'swarm', ['run', str(root / 'plan.json')]),
            ]
            before = {p.relative_to(home): p.read_bytes() for p in home.rglob('*') if p.is_file()}
            for bundle, tool, arguments in commands:
                for locator in (str(state), '~/state'):
                    with self.subTest(tool=tool, arguments=arguments, locator=locator):
                        result = subprocess.run([sys.executable, '-B',
                            str(ROOT / 'skills' / bundle / 'scripts' / (tool + '.py')),
                            *arguments, '--state-dir', locator], cwd=root,
                            env=dict(os.environ, HOME=str(home)), capture_output=True,
                            text=True, timeout=30)
                        self.assertNotEqual(result.returncode, 0, (result.stdout, result.stderr))
                        self.assertIn('D5 active:', result.stderr)
                        self.assertFalse(stage.exists())
                        self.assertEqual({p.relative_to(home): p.read_bytes()
                            for p in home.rglob('*') if p.is_file()}, before)

    def test_distinct_euc_jp_contexts_keep_separate_defaults(self):
        # Synthetic EUC-JP boundary; distinct native names must not share defaults.
        # No filesystem writes or dependence on the host filesystem encoding.
        parent = Path('/tmp/project-parent')
        contexts = [parent / bytes.fromhex(value).decode('euc_jp', 'surrogateescape')
                    for value in ('a1a1', 'e38080')]
        self.assertNotEqual(*contexts)
        native_os = types.SimpleNamespace(**vars(os))
        native_os.fsencode = lambda value: os.fspath(value).encode('euc_jp', 'surrogateescape')
        with mock.patch.object(paths, 'os', native_os), \
                mock.patch.object(paths, '_external_base', return_value=Path('/tmp/project-state')), \
                mock.patch.object(paths, 'project_context', side_effect=contexts):
            first, second = paths.default_paths(), paths.default_paths()
        self.assertNotEqual(first[0], second[0])
        self.assertNotEqual(first[1], second[1])

    def assert_witness_roundtrip(self, entry, fail_body):
        directory = Path(entry['state_dir'])
        names = (readiness.MARKER, readiness.SENTINEL)
        before = {name: (directory / name).read_bytes() for name in names}
        entries = sorted(p.name for p in directory.iterdir())
        # An old fixed timestamp makes late cleanup detectable even on a
        # filesystem whose timestamp resolution cannot separate adjacent calls.
        os.utime(directory, ns=(1234567890000000000, 1234567890000000000))
        original = directory.stat()
        def consume():
            with missing_installation(entry):
                self.assertFalse(any((directory / name).exists() for name in names))
                if fail_body:
                    raise RuntimeError('synthetic consumer failure')
        try:
            if fail_body:
                with self.assertRaisesRegex(RuntimeError, 'synthetic consumer failure'):
                    consume()
            else:
                consume()
        finally:
            self.assertEqual({name: (directory / name).read_bytes() for name in names}, before)
            self.assertEqual(sorted(p.name for p in directory.iterdir()), entries)
            after = directory.stat()
            self.assertEqual((after.st_ino, after.st_mtime_ns),
                             (original.st_ino, original.st_mtime_ns))
            self.assertEqual(readiness.state(directory), readiness.READY)

    def test_missing_installation_state_is_mountpoint(self):
        with tempfile.TemporaryDirectory() as temporary:
            entry = ready_installation(Path(temporary).resolve())
            directory = Path(entry['state_dir'])
            original_rename = Path.rename
            def volume(path):
                path = Path(path).absolute()
                return 1 if path == directory or directory in path.parents else 2
            def rename(source, target):
                if volume(source) != volume(target):
                    raise OSError(errno.EXDEV, 'synthetic state mountpoint', str(source))
                return original_rename(source, target)
            for fail_body in (False, True):
                with self.subTest(fail_body=fail_body), mock.patch.object(Path, 'rename', rename):
                    self.assert_witness_roundtrip(entry, fail_body)

    def test_missing_installation_parent_is_not_writable(self):
        with tempfile.TemporaryDirectory() as temporary:
            entry = ready_installation(Path(temporary).resolve())
            directory = Path(entry['state_dir'])
            original_temporary = tempfile.TemporaryDirectory
            def temporary_with_readonly_parent(*args, **kwargs):
                # Model EACCES at the allocation boundary; works even when
                # CI runs with privileges that bypass real permission bits.
                parent = kwargs.get('dir')
                if parent is not None and Path(parent) == directory.parent:
                    raise PermissionError(errno.EACCES, 'synthetic readonly parent')
                return original_temporary(*args, **kwargs)
            for fail_body in (False, True):
                with self.subTest(fail_body=fail_body), \
                        mock.patch.object(tempfile, 'TemporaryDirectory', temporary_with_readonly_parent):
                    self.assert_witness_roundtrip(entry, fail_body)

    def test_missing_installation_cross_device(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            state_volume, backup_volume = root / 'state-volume', root / 'temporary-volume'
            state_volume.mkdir(); backup_volume.mkdir()
            entry = ready_installation(state_volume)
            directory = Path(entry['state_dir'])
            before = {name: (directory / name).read_bytes() for name in (readiness.MARKER, readiness.SENTINEL)}
            original_temporary, original_rename = tempfile.TemporaryDirectory, Path.rename
            def temporary_on_other_volume(*args, **kwargs):
                kwargs.setdefault('dir', str(backup_volume))
                return original_temporary(*args, **kwargs)
            def volume(path):
                try:
                    Path(path).relative_to(state_volume)
                    return 1
                except ValueError:
                    return 2
            def rename(source, target):
                if volume(source) != volume(target):
                    raise OSError(errno.EXDEV, 'synthetic POSIX cross-device rename', str(source))
                return original_rename(source, target)
            try:
                with mock.patch.object(tempfile, 'TemporaryDirectory', temporary_on_other_volume), mock.patch.object(Path, 'rename', rename):
                    with missing_installation(entry):
                        self.assertFalse(any((directory / name).exists() for name in before))
            finally:
                self.assertEqual({name: (directory / name).read_bytes() for name in before}, before)
            self.assertEqual(readiness.state(directory), readiness.READY)


if __name__ == '__main__':
    unittest.main()
