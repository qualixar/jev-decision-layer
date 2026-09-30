"""A damaged receipts file is set aside, a failed start fails fast, old evidence goes.

A crash or a full disk mid-write used to leave a receipts database the broker
could never open again: every call and every hook paid about five seconds and
then failed, with no repair path. The damaged file is now kept aside (still
private) and a fresh one created. A failed broker start is remembered for a
minute so hooks do not pay the start timeout on every prompt. Evidence past
its retention is pruned hourly, not only when a broker starts.
"""

from __future__ import annotations

import os
import sqlite3
import stat
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
from jev_auto.common import AutoError  # noqa: E402
from jev_auto.store import Store  # noqa: E402


class _Root(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve() / "state"


class RepairTests(_Root):
    def corrupt(self):
        Store(self.root)
        database = self.root / "auto.sqlite3"
        with open(database, "r+b") as handle:
            handle.write(b"\0" * 100)
        return database

    def test_a_damaged_database_is_set_aside_privately_and_replaced(self):
        self.corrupt()
        store = Store(self.root)
        kept = sorted(self.root.glob("auto.sqlite3.corrupt-*"))
        self.assertEqual(len(kept), 1)
        self.assertEqual(stat.S_IMODE(kept[0].stat().st_mode), 0o600)
        store.event("probe", {"ok": True})
        with store.connection() as connection:
            kinds = [row[0] for row in connection.execute("SELECT kind FROM events ORDER BY id")]
        self.assertEqual(kinds, ["store_reset", "probe"])

    def test_a_busy_database_is_never_set_aside(self):
        Store(self.root)
        with patch("jev_auto.store.sqlite3.connect", side_effect=sqlite3.OperationalError("database is locked")):
            with self.assertRaises(sqlite3.OperationalError):
                Store(self.root)
        self.assertEqual(list(self.root.glob("auto.sqlite3.corrupt-*")), [])


class FailedStartTests(_Root):
    def setUp(self):
        super().setUp()
        self.workspace = Path(self._tmp.name).resolve() / "workspace"
        self.workspace.mkdir()

    def ensure(self):
        with patch.object(ipc, "load_policy", return_value=None), \
             patch.object(ipc, "request", side_effect=AutoError("BROKER_UNAVAILABLE")), \
             patch.object(ipc.time, "sleep"), \
             patch.object(ipc.subprocess, "Popen") as popen:
            with self.assertRaises(AutoError) as caught:
                ipc.ensure(self.workspace, self.root)
        self.assertEqual(str(caught.exception), "BROKER_START_FAILED")
        # Popen also backs subprocess.run (git); count broker spawns only.
        return sum("jev_auto.server" in str(call) for call in popen.call_args_list)

    def test_a_failed_start_is_not_retried_for_a_minute_while_a_service_holds_the_lock(self):
        from jev_auto.common import private_dir, state_dir
        from jev_auto.platform_fs import file_lock
        self.assertEqual(self.ensure(), 1)
        with file_lock(private_dir(state_dir(self.workspace, self.root)) / "broker.lock"):
            self.assertEqual(self.ensure(), 0)
        self.assertEqual(self.ensure(), 0, "seconds after a failure, fail at once")
        marker = private_dir(state_dir(self.workspace, self.root)) / "start-failed"
        old = time.time() - 15
        os.utime(marker, (old, old))
        self.assertEqual(self.ensure(), 1, "later, with the lock free, the start is tried again")

    def test_after_a_minute_the_start_is_tried_again(self):
        self.assertEqual(self.ensure(), 1)
        marker = next(self.root.rglob("start-failed"))
        old = time.time() - 61
        os.utime(marker, (old, old))
        self.assertEqual(self.ensure(), 1)


class PruneTimerTests(unittest.TestCase):
    def test_a_busy_broker_prunes_once_an_hour(self):
        pruned = []
        engine = SimpleNamespace(prune=lambda: pruned.append(1))
        last = server.prune_if_due(engine, 0.0, 100.0)
        self.assertEqual((pruned, last), ([], 0.0))
        last = server.prune_if_due(engine, 0.0, 3601.0)
        self.assertEqual((pruned, last), ([1], 3601.0))

    def test_a_prune_that_fails_does_not_stop_the_broker(self):
        def boom():
            raise AutoError("AUTO_DISABLED_OR_EXPIRED")
        self.assertEqual(server.prune_if_due(SimpleNamespace(prune=boom), 0.0, 4000.0), 4000.0)


if __name__ == "__main__":
    unittest.main()
