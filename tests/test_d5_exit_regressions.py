"""Unrepaired U8 exit-review counterexamples, preserved for owner adjudication."""
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
    def test_non_utf8_filesystem_keeps_legacy_default_key(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            context = root / 'caf\u00e9'
            context.mkdir()
            legacy = hashlib.sha256(str(context).encode('utf-8')).hexdigest()[:12]
            native = hashlib.sha256(str(context).encode('latin-1')).hexdigest()[:12]
            self.assertNotEqual(legacy, native)
            xdg = root / 'xdg'
            old_project = xdg / 'hanig-swarm' / 'projects' / ('caf-' + legacy)
            old_state, old_runs = old_project / 'state', old_project / 'runs'
            old_state.mkdir(parents=True)
            old_runs.mkdir()
            history = old_state / 'swarm-state.json'
            history.write_bytes(b'{"existing":"run"}\n')
            # The Mac fixes filesystem encoding to UTF-8. Model only Linux's
            # fsencode boundary; path discovery and default resolution stay real.
            native_os = types.SimpleNamespace(**vars(os))
            native_os.fsencode = lambda value: os.fspath(value).encode('latin-1', 'surrogateescape')
            with mock.patch.object(paths, 'os', native_os), mock.patch.dict(os.environ, {'XDG_STATE_HOME': str(xdg)}):
                state, runs, worktrees = paths.resolve_paths(cwd=context, need_root=True)
            self.assertEqual(history.read_bytes(), b'{"existing":"run"}\n')
            self.assertEqual(worktrees, [])
            self.assertEqual((state, runs), (old_state, old_runs))

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
