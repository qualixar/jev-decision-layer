"""Edge cases of the local service that must not turn into outages.

A busy service is running, not missing. One failed start must not lock
every call out for a minute. A receipts file damaged while the service runs
makes it stop so the next start repairs it. The Mac data-volume spelling of a
home folder is a home folder.
"""

from __future__ import annotations

import os
import socket
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "plugins" / "qualixar-jev-decision-layer" / "runtime"
sys.path.insert(0, str(RUNTIME))

from jev_auto import ipc, server  # noqa: E402
from jev_auto.common import AutoError, private_dir, state_dir, write_private  # noqa: E402
from jev_auto.platform_fs import file_lock  # noqa: E402
from jev_auto.settings import descendant_root_allowed  # noqa: E402


class _Folder(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name).resolve()
        self.workspace = base / "workspace"
        self.workspace.mkdir()
        self.base = base / "state"
        self.root = private_dir(state_dir(self.workspace, self.base))

    def ensure(self, health):
        with patch.object(ipc, "load_policy", return_value=None), \
             patch.object(ipc, "request", side_effect=health), \
             patch.object(ipc.time, "sleep"), \
             patch.object(ipc.subprocess, "Popen") as popen:
            try:
                ipc.ensure(self.workspace, self.base)
                outcome = "ok"
            except AutoError as error:
                outcome = str(error)
        return outcome, sum("jev_auto.server" in str(call) for call in popen.call_args_list)


class BusyIsRunningTests(_Folder):
    def test_a_busy_service_is_used_not_restarted(self):
        self.assertEqual(self.ensure(AutoError("BROKER_BUSY")), ("ok", 0))


class StartMarkerTests(_Folder):
    def mark(self, mtime):
        write_private(self.root / "start-failed", {"failed_at": mtime})
        os.utime(self.root / "start-failed", (mtime, mtime))

    def test_a_recent_failure_is_retried_when_no_service_holds_the_lock(self):
        self.mark(time.time() - 15)
        self.assertEqual(self.ensure(AutoError("BROKER_UNAVAILABLE")), ("BROKER_START_FAILED", 1))

    def test_a_recent_failure_fails_fast_while_a_service_holds_the_lock(self):
        self.mark(time.time() - 15)
        with file_lock(self.root / "broker.lock"):
            self.assertEqual(self.ensure(AutoError("BROKER_UNAVAILABLE")), ("BROKER_START_FAILED", 0))

    def test_a_failure_seconds_ago_fails_fast_even_with_the_lock_free(self):
        self.mark(time.time() - 2)
        self.assertEqual(self.ensure(AutoError("BROKER_UNAVAILABLE")), ("BROKER_START_FAILED", 0))

    def test_a_marker_dated_in_the_future_is_ignored(self):
        self.mark(time.time() + 3600)
        self.assertFalse(ipc._failed_recently(self.root / "start-failed"))


class RequestedPathTests(unittest.TestCase):
    def test_a_relative_path_is_sent_as_the_absolute_folder_it_names(self):
        sent = []
        with tempfile.TemporaryDirectory() as folder, patch.object(ipc, "_ipc_workspace", side_effect=lambda p, b=None: Path(p)), \
             patch.object(ipc, "address", return_value=Path(folder) / "missing.sock"), \
             patch.object(ipc, "_exchange", side_effect=lambda addr, data, timeout: sent.append(data) or {"ok": True, "result": {}}):
            cwd = os.getcwd()
            os.chdir(folder)
            try:
                ipc.request(".", {"op": "health"}, timeout=1)
            finally:
                os.chdir(cwd)
        import json
        self.assertEqual(Path(json.loads(sent[0])["requested_path"]).resolve(), Path(folder).resolve())
        self.assertTrue(os.path.isabs(json.loads(sent[0])["requested_path"]))


class DamagedWhileRunningTests(unittest.TestCase):
    def test_the_service_stops_so_the_next_start_repairs_the_file(self):
        with tempfile.TemporaryDirectory() as folder:
            def damaged(_request):
                raise sqlite3.DatabaseError("file is not a database")
            srv = server.Server(Path(folder) / "s.sock", SimpleNamespace(dispatch=damaged))
            self.addCleanup(srv.server_close)
            a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
            with b:
                b.sendall(b'{"op":"x"}\n')
                handler = server.Handler.__new__(server.Handler)
                handler.request, handler.server = a, srv
                handler.handle()
                reply = b.recv(4096)
            a.close()
        self.assertIn(b"BROKER_STORE_DAMAGED", reply)
        self.assertTrue(srv.stopping.is_set())


class HomeSpellingTests(unittest.TestCase):
    def test_the_data_volume_spelling_of_a_home_is_a_home(self):
        with patch.object(Path, "resolve", lambda self, strict=False: self):
            self.assertFalse(descendant_root_allowed(Path("/System/Volumes/Data/Users/someone")))


if __name__ == "__main__":
    unittest.main()
