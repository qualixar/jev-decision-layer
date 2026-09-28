"""Mocked Windows filesystem contract tests; native ACL behavior needs Windows CI."""
from __future__ import annotations

import os
import ctypes
import ctypes
import sys
import tempfile
import unittest
from pathlib import Path, PureWindowsPath
from unittest.mock import patch

RUNTIME = Path(__file__).resolve().parents[1] / "plugins" / "qualixar-jev-decision-layer" / "runtime"
sys.path.insert(0, str(RUNTIME))

from jev_auto import platform_fs
from jev_auto.common import AutoError


USER = "S-1-5-21-1000-2000-3000-1001"


class SddlValidationTests(unittest.TestCase):
    def test_accepts_only_protected_user_and_system_acl(self):
        cases = (
            (f"O:{USER}D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;{USER})", True),
            (f"O:{USER}D:P(A;;FA;;;SY)(A;;FA;;;{USER})", False),
            (f"O:{USER}D:P(A;;FA;;;SY)(A;;FA;;;{USER})", False),
        )
        for sddl, is_directory in cases:
            with self.subTest(sddl=sddl, is_directory=is_directory):
                self.assertTrue(platform_fs._verify_sddl(sddl, USER, directory=is_directory))

    def test_rejects_inherited_or_unprotected_dacl(self):
        unsafe = [
            f"O:{USER}D:(A;OICI;FA;;;SY)(A;OICI;FA;;;{USER})",
            f"O:{USER}D:AI(A;OICI;FA;;;SY)(A;OICI;FA;;;{USER})",
            f"O:{USER}D:P(A;OICI;FA;;;WD)(A;OICI;FA;;;{USER})",
            f"O:{USER}D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;{USER})(A;OICI;FA;;;WD)",
            f"O:{USER}D:P(D;OICI;FA;;;WD)(A;OICI;FA;;;{USER})",
            f"O:S-1-5-18D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;{USER})",
            f"O:{USER}D:P(A;OICI;GR;;;SY)(A;OICI;FA;;;{USER})",
        ]
        for sddl in unsafe:
            with self.subTest(sddl=sddl):
                self.assertFalse(platform_fs._verify_sddl(sddl, USER, directory=True))

    def test_by_handle_file_information_matches_win32_layout(self):
        info = platform_fs._WindowsOps._BY_HANDLE_FILE_INFORMATION
        self.assertEqual(ctypes.sizeof(platform_fs._FILETIME), 8)
        self.assertEqual(info.nNumberOfLinks.offset, 40)
        self.assertEqual(info.nFileIndexHigh.offset, 44)
        self.assertEqual(info.nFileIndexLow.offset, 48)
        self.assertEqual(ctypes.sizeof(info), 52)

    def test_handle_policy_rejects_reparse_directories_and_hardlinks(self):
        validate = platform_fs._WindowsOps._validate_handle_attributes
        directory = platform_fs._WindowsOps.FILE_ATTRIBUTE_DIRECTORY
        reparse = platform_fs._WindowsOps.FILE_ATTRIBUTE_REPARSE_POINT
        validate(directory, 1, directory=True)
        validate(0, 1, directory=False, single_link=True)
        with self.assertRaisesRegex(AutoError, "SYMLINK_NOT_ALLOWED"):
            validate(directory | reparse, 1, directory=True)
        with self.assertRaisesRegex(AutoError, "UNSAFE_PRIVATE_FILE"):
            validate(directory, 1, directory=False)
        with self.assertRaisesRegex(AutoError, "UNSAFE_PRIVATE_FILE"):
            validate(0, 2, directory=False, single_link=True)

    def test_rejects_extra_sacl(self):
        sddl = f"O:{USER}D:P(A;;FA;;;SY)(A;;FA;;;{USER})S:(AU;SA;FA;;;WD)"
        self.assertTrue(platform_fs._verify_sddl(sddl, USER, directory=False))

    def test_private_directory_repair_sets_the_explicit_user_owner(self):
        class FakeAdvapi:
            def __init__(self):
                self.arguments = None

            def GetSecurityDescriptorDacl(self, _descriptor, present, dacl, _defaulted):
                ctypes.cast(present, ctypes.POINTER(ctypes.c_int))[0] = 1
                ctypes.cast(dacl, ctypes.POINTER(ctypes.c_void_p))[0] = 0x1234
                return 1

            def GetSecurityDescriptorOwner(self, _descriptor, owner, defaulted):
                ctypes.cast(owner, ctypes.POINTER(ctypes.c_void_p))[0] = 0x5678
                ctypes.cast(defaulted, ctypes.POINTER(ctypes.c_int))[0] = 0
                return 1

            def SetSecurityInfo(self, *arguments):
                self.arguments = arguments
                return 0

        class FakeKernel:
            def LocalFree(self, _descriptor):
                return None

        ops = platform_fs._WindowsOps.__new__(platform_fs._WindowsOps)
        ops.advapi32 = FakeAdvapi()
        ops.kernel32 = FakeKernel()
        ops._descriptor = lambda _directory: ctypes.c_void_p(0x9999)
        ops._set_private_dacl(ctypes.c_void_p(0x1111), directory=True)

        _, object_type, flags, owner, group, dacl, sacl = ops.advapi32.arguments
        self.assertEqual(object_type, ops.SE_FILE_OBJECT)
        self.assertEqual(flags, 0x1 | ops.DACL_SECURITY_INFORMATION | ops.PROTECTED_DACL_SECURITY_INFORMATION)
        self.assertEqual(owner.value, 0x5678)
        self.assertIsNone(group)
        self.assertEqual(dacl.value, 0x1234)
        self.assertIsNone(sacl)


class FakeWindowsOps:
    def __init__(self):
        self.calls = []
        self.lock_acquired = True

    def ensure_dir(self, path):
        self.calls.append(("ensure_dir", path))
        return path

    def verify_dir(self, path):
        self.calls.append(("verify_dir", path))

    def open_file(self, path, flags, mode):
        self.calls.append(("open_file", path, flags, mode))
        return 73

    def verify_fd(self, fd):
        self.calls.append(("verify_fd", fd))

    def atomic_write(self, path, data, *, replace):
        self.calls.append(("atomic_write", path, data, replace))

    def lock(self, fd, *, blocking):
        self.calls.append(("lock", fd, blocking))
        return self.lock_acquired

    def unlock(self, fd):
        self.calls.append(("unlock", fd))


class WindowsDispatchTests(unittest.TestCase):
    def setUp(self):
        self.ops = FakeWindowsOps()
        self.previous = platform_fs._WINDOWS_OPS
        platform_fs._WINDOWS_OPS = self.ops

    def tearDown(self):
        platform_fs._WINDOWS_OPS = self.previous

    def test_state_root_uses_local_app_data(self):
        posix_path = type(Path("."))
        with patch.object(platform_fs.os, "name", "nt"), patch.dict(
                os.environ, {"LOCALAPPDATA": r"C:\Users\test\AppData\Local"}), patch.object(
                    platform_fs, "Path", posix_path):
            self.assertEqual(str(platform_fs.user_state_root()),
                             str(posix_path(r"C:\Users\test\AppData\Local") / "Qualixar" / "JevDecisionLayer"))

    def test_public_private_storage_apis_delegate_to_native_backend(self):
        path = PureWindowsPath("C:/private/state")
        with patch.object(platform_fs.os, "name", "nt"):
            self.assertEqual(platform_fs.ensure_private_dir(path), path)
            platform_fs.verify_private_dir(path)
            self.assertEqual(platform_fs.open_private_file(path / "x", os.O_RDONLY), 73)
            platform_fs.verify_private_file_fd(73)
            platform_fs.atomic_write_private(path / "x", b"data", replace=False)
        self.assertEqual(self.ops.calls[0], ("ensure_dir", path))
        self.assertEqual(self.ops.calls[1], ("verify_dir", path))
        self.assertEqual(self.ops.calls[2][0], "open_file")
        self.assertIn(("verify_fd", 73), self.ops.calls)
        self.assertEqual(self.ops.calls[-1], ("atomic_write", path / "x", b"data", False))

    def test_windows_lock_unlocks_only_when_lock_was_acquired(self):
        with patch.object(platform_fs.os, "name", "nt"), patch.object(platform_fs.os, "close") as close:
            with platform_fs.file_lock(PureWindowsPath("C:/private/state/lock"), blocking=False) as acquired:
                self.assertTrue(acquired)
            self.assertIn(("lock", 73, False), self.ops.calls)
            self.assertIn(("unlock", 73), self.ops.calls)
            close.assert_called_once_with(73)

    def test_nonblocking_busy_lock_is_reported_without_unlock(self):
        self.ops.lock_acquired = False
        with patch.object(platform_fs.os, "name", "nt"), patch.object(platform_fs.os, "close") as close:
            with platform_fs.file_lock(PureWindowsPath("C:/private/state/lock"), blocking=False) as acquired:
                self.assertFalse(acquired)
            self.assertFalse(any(call[0] == "unlock" for call in self.ops.calls))
            close.assert_called_once_with(73)

    def test_native_backend_unavailability_fails_closed(self):
        with patch.object(platform_fs.os, "name", "nt"), patch.object(
                platform_fs, "_WINDOWS_OPS", None), patch.object(platform_fs, "_WindowsOps",
                                                                  side_effect=OSError("no Win32")):
            with self.assertRaisesRegex(AutoError, "WINDOWS_PRIVATE_STATE_UNVERIFIED"):
                platform_fs.ensure_private_dir(PureWindowsPath("C:/private"))


if __name__ == "__main__":
    unittest.main()
