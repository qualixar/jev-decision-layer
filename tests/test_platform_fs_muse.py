"""Focused regression tests for the independent private-filesystem review."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
import ctypes
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

RUNTIME = Path(__file__).resolve().parents[1] / "plugins" / "qualixar-jev-decision-layer" / "runtime"
sys.path.insert(0, str(RUNTIME))

from jev_auto import platform_fs  # noqa: E402
from jev_auto.common import AutoError  # noqa: E402


class PosixPrivateStateTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "POSIX filesystem semantics")
    def test_replace_rejects_existing_world_readable_destination_without_changing_it(self):
        with tempfile.TemporaryDirectory() as directory:
            dest = Path(directory) / "policy.json"
            dest.write_bytes(b"original")
            dest.chmod(0o644)
            with self.assertRaisesRegex(AutoError, "UNSAFE_PRIVATE_FILE"):
                platform_fs.atomic_write_private(dest, b"replacement", replace=True)
            self.assertEqual(dest.read_bytes(), b"original")

    @unittest.skipIf(os.name == "nt", "POSIX filesystem semantics")
    def test_replace_rejects_existing_symlink_without_touching_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            target.write_bytes(b"original")
            (root / "policy.json").symlink_to(target)
            with self.assertRaisesRegex(AutoError, "SYMLINK_NOT_ALLOWED"):
                platform_fs.atomic_write_private(root / "policy.json", b"replacement", replace=True)
            self.assertEqual(target.read_bytes(), b"original")

    @unittest.skipIf(os.name == "nt", "POSIX filesystem semantics")
    def test_replace_accepts_existing_private_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            dest = Path(directory) / "policy.json"
            dest.write_bytes(b"original")
            dest.chmod(0o600)
            platform_fs.atomic_write_private(dest, b"replacement", replace=True)
            self.assertEqual(dest.read_bytes(), b"replacement")
            self.assertEqual(dest.stat().st_mode & 0o777, 0o600)

    @unittest.skipIf(os.name == "nt", "POSIX filesystem semantics")
    def test_replace_rejects_hardlinked_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dest = root / "policy.json"
            dest.write_bytes(b"original")
            dest.chmod(0o600)
            sibling = root / "other-link"
            os.link(dest, sibling)
            with self.assertRaisesRegex(AutoError, "UNSAFE_PRIVATE_FILE"):
                platform_fs.atomic_write_private(dest, b"replacement", replace=True)
            self.assertEqual(sibling.read_bytes(), b"original")
            self.assertEqual(dest.read_bytes(), b"original")

    @unittest.skipIf(os.name == "nt", "POSIX filesystem semantics")
    def test_world_writable_nonsticky_ancestor_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory) / "shared"
            parent.mkdir()
            parent.chmod(0o777)
            try:
                with self.assertRaisesRegex(AutoError, "PRIVATE_DIRECTORY_OWNER"):
                    platform_fs.ensure_private_dir(parent / "private")
                self.assertFalse((parent / "private").exists())
            finally:
                parent.chmod(0o700)

    @unittest.skipIf(os.name == "nt", "POSIX filesystem semantics")
    def test_sticky_ancestor_can_contain_owner_private_state(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory) / "shared"
            parent.mkdir()
            parent.chmod(0o1777)
            try:
                private = platform_fs.ensure_private_dir(parent / "private")
                platform_fs.verify_private_dir(private)
                self.assertEqual(private.stat().st_mode & 0o777, 0o700)
            finally:
                parent.chmod(0o700)

    @unittest.skipIf(os.name == "nt", "POSIX filesystem semantics")
    def test_relative_xdg_state_home_ignored_independent_of_cwd(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake_home = root / "home"
            fake_home.mkdir()
            previous = Path.cwd()
            try:
                os.chdir(root)
                with patch.dict(os.environ, {"XDG_STATE_HOME": "relative-state"}), patch.object(
                        Path, "home", return_value=fake_home):
                    self.assertEqual(
                        platform_fs.user_state_root(),
                        fake_home / ".local" / "state" / "qualixar-jev-decision-layer",
                    )
            finally:
                os.chdir(previous)
            self.assertFalse((root / "relative-state").exists())


class WindowsFlagContractTests(unittest.TestCase):
    def test_failed_open_always_reports_win32_error_without_retry_or_name_error(self):
        ops = platform_fs._WindowsOps.__new__(platform_fs._WindowsOps)
        calls = []

        def create_file(*args):
            calls.append(args)
            return ops.INVALID_HANDLE_VALUE

        ops.kernel32 = SimpleNamespace(CreateFileW=create_file, LocalFree=lambda _descriptor: None)
        with patch.object(ops, "_descriptor", return_value=ctypes.c_void_p(1)), patch.object(
                platform_fs.ctypes, "get_last_error", return_value=183, create=True):
            with self.assertRaises(OSError) as caught:
                ops._create_file_handle(Path("C:/private/policy.json"), os.O_CREAT | os.O_RDWR)
        self.assertIn("183", str(caught.exception))
        self.assertEqual(len(calls), 1)

    def test_exclusive_without_create_opens_existing(self):
        ops = platform_fs._WindowsOps
        self.assertEqual(ops._file_disposition(os.O_EXCL | os.O_RDWR), ops.OPEN_EXISTING)
        self.assertEqual(ops._file_disposition(os.O_CREAT | os.O_EXCL | os.O_RDWR), ops.CREATE_NEW)

    def test_truncate_without_create_opens_existing(self):
        ops = platform_fs._WindowsOps
        self.assertEqual(ops._file_disposition(os.O_TRUNC | os.O_WRONLY), ops.OPEN_EXISTING)
        self.assertEqual(ops._file_disposition(os.O_TRUNC | os.O_CREAT | os.O_WRONLY), ops.OPEN_ALWAYS)

    def test_truncate_occurs_after_handle_verification(self):
        ops = platform_fs._WindowsOps.__new__(platform_fs._WindowsOps)
        events = []
        with patch.object(ops, "_verify_path_ancestors"), patch.object(
                ops, "_create_file_handle", return_value=object()), patch.object(
                    ops, "_fd_from_handle", return_value=73), patch.object(
                        ops, "verify_fd", side_effect=lambda _fd: events.append("verified")), patch.object(
                            platform_fs.os, "ftruncate", side_effect=lambda _fd, _size: events.append("truncated")):
            self.assertEqual(ops.open_file(Path("C:/private/state"), os.O_TRUNC | os.O_WRONLY), 73)
        self.assertEqual(events, ["verified", "truncated"])

    def test_failed_verification_never_truncates(self):
        ops = platform_fs._WindowsOps.__new__(platform_fs._WindowsOps)
        with patch.object(ops, "_verify_path_ancestors"), patch.object(
                ops, "_create_file_handle", return_value=object()), patch.object(
                    ops, "_fd_from_handle", return_value=73), patch.object(
                        ops, "verify_fd", side_effect=AutoError("UNSAFE_PRIVATE_FILE")), patch.object(
                            platform_fs.os, "ftruncate") as truncate, patch.object(
                                platform_fs.os, "close") as close:
            with self.assertRaisesRegex(AutoError, "UNSAFE_PRIVATE_FILE"):
                ops.open_file(Path("C:/private/state"), os.O_TRUNC | os.O_WRONLY)
        truncate.assert_not_called()
        close.assert_called_once_with(73)


if __name__ == "__main__":
    unittest.main()
