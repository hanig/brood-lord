"""Same-filesystem staging and restoration for D5 corruption fixtures."""
import errno
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tests.fixtures.d5.ready import ready_installation, missing_installation, readiness


class TestD5FixtureStorage(unittest.TestCase):
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
