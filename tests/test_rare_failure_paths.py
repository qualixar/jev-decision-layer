"""Failure paths that are rare in practice and must still fail safe.

A host without `fcntl`, a folder that cannot be opened, another folder's
retention sweep failing, no readable per-user temp folder on a Mac, a
malformed registry `auth` value, and a busy receipts file. None of these may
block a decision or stop the local service.
"""

from __future__ import annotations

import importlib.util
import socket
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "plugins" / "qualixar-jev-decision-layer" / "runtime"
sys.path.insert(0, str(RUNTIME))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_core_engine_coverage import FakeProviders  # noqa: E402
from test_retention_after_withdrawal import _counts, _Folders  # noqa: E402

from jev_auto import common, ipc, secret_rules, server  # noqa: E402
from jev_auto.common import state_dir  # noqa: E402
from jev_auto.engine import Engine  # noqa: E402


class PathSpellingTests(unittest.TestCase):
    def test_without_fcntl_the_module_loads_and_paths_pass_through(self):
        spec = importlib.util.spec_from_file_location("jev_auto._common_without_fcntl", common.__file__)
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"fcntl": None}):  # `import fcntl` raises, as on Windows
            spec.loader.exec_module(module)
        self.assertIsNone(module.fcntl)
        self.assertEqual(module.on_disk(Path("/some/folder")), Path("/some/folder"))

    def test_a_path_that_cannot_be_opened_keeps_its_own_spelling(self):
        with tempfile.TemporaryDirectory() as folder:
            missing = Path(folder) / "gone"
            self.assertEqual(common.on_disk(missing), missing)

    def test_a_folder_whose_spelling_cannot_be_read_keeps_its_own_spelling(self):
        with tempfile.TemporaryDirectory() as folder:
            typed = Path(folder)
            with patch.object(common.fcntl, "fcntl", side_effect=OSError("not supported here")):
                self.assertEqual(common.on_disk(typed), typed)


class SweepFailureTests(_Folders):
    def test_a_failing_sweep_of_other_folders_never_blocks_this_folders_own_pruning(self):
        folder = self.folder("current", retention_days=7)
        self.fill(folder, 30)
        with patch("jev_auto.engine.sweep_inactive", side_effect=RuntimeError("another folder is unreadable")):
            Engine(folder, provider=FakeProviders())
        self.assertEqual(_counts(state_dir(folder))["evidence"], 0)


class UserTempTests(unittest.TestCase):
    def test_no_readable_user_temp_folder_means_none(self):
        with patch.object(ipc.ctypes, "CDLL", side_effect=OSError("no C library")):
            self.assertIsNone(ipc._darwin_user_temp())


class RegistryAuthTests(unittest.TestCase):
    def test_an_auth_value_that_is_not_base64_is_not_a_credential(self):
        self.assertFalse(secret_rules.credential_field("auth", "not*base64*at*all"))
        self.assertFalse(secret_rules.credential_field("auth", "abcdefghi"))  # bad padding


class BusyStoreTests(unittest.TestCase):
    def test_a_busy_receipts_file_is_an_internal_error_and_the_service_keeps_running(self):
        with tempfile.TemporaryDirectory() as folder:
            def busy(_request):
                raise sqlite3.OperationalError("database is locked")
            srv = server.Server(Path(folder) / "s.sock", SimpleNamespace(dispatch=busy))
            self.addCleanup(srv.server_close)
            a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
            with b:
                b.sendall(b'{"op":"x"}\n')
                handler = server.Handler.__new__(server.Handler)
                handler.request, handler.server = a, srv
                handler.handle()
                reply = b.recv(4096)
            a.close()
        self.assertIn(b"BROKER_INTERNAL_ERROR", reply)
        self.assertFalse(srv.stopping.is_set())


if __name__ == "__main__":
    unittest.main()
