"""Coverage-floor tests for jev_auto/store.py's Windows-only code paths.

store.py's `_prepare_windows_database`, `_verify_windows_database`, and the
`os.name=='nt'` branches inside `Store.__init__`/`connection()` can only run
on a real Windows host in the ordinary course of things. To reach them on
macOS/Linux without the forbidden trick of mutating the interpreter-wide
`os.name` (which would make `pathlib` start building `WindowsPath` objects
and explode), every test here patches *store.py's own* `os` name binding to
a small proxy object whose `.name` reads "nt" but which forwards every other
attribute (`os.path`, `os.close`, `os.umask`, the `os.O_*` flag constants,
...) to the real `os` module. The real POSIX filesystem and the real
`sqlite3` module still do the actual work underneath, so these tests create
real private files inside a `tempfile.TemporaryDirectory()` and assert on
their real state - they do not just execute lines.

No test here ever touches the real ~/.local/state directory: Store takes an
explicit `root` argument, never `home_root()`/XDG_STATE_HOME.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

RUNTIME = Path(__file__).resolve().parents[1] / "plugins" / "qualixar-jev-decision-layer" / "runtime"
sys.path.insert(0, str(RUNTIME))

import jev_auto.store as store  # noqa: E402
from jev_auto.common import AutoError  # noqa: E402


class _FakeNtOs:
    """Stand-in for the `os` module that reports `os.name == "nt"` while
    delegating every other attribute (functions, submodules, flag
    constants) to the real `os` module. Patched only onto `store`'s own
    module-level `os` name (`patch.object(store, "os", ...)`), never onto
    the global `os.name`, per the hard rule against mutating interpreter-wide
    platform state."""

    name = "nt"

    def __getattr__(self, attr):
        return getattr(os, attr)


FAKE_NT_OS = _FakeNtOs()


class WindowsStyleConstructionTests(unittest.TestCase):
    """Store.__init__'s `if os.name == 'nt':` branch (store.py:12) and the
    matching branches inside `connection()` (store.py:66,71-78,85)."""

    def test_windows_style_construction_creates_private_db_and_journal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "state"
            with patch.object(store, "os", FAKE_NT_OS):
                instance = store.Store(root)

            # _prepare_windows_database (store.py:12,42-52,56-60) must have
            # created both the sqlite file and its rollback journal as
            # private (mode-0600, owner-owned, single-link) regular files -
            # not just "some file exists".
            db_stat = instance.path.stat()
            journal = Path(str(instance.path) + "-journal")
            self.assertTrue(instance.path.is_file())
            self.assertTrue(journal.is_file())
            self.assertEqual(db_stat.st_mode & 0o777, 0o600)
            self.assertEqual(db_stat.st_nlink, 1)
            self.assertEqual(db_stat.st_uid, os.getuid())

            # The schema executescript() at the end of __init__ ran inside
            # a connection() that took the nt branch the whole way through
            # (store.py:66 verify-before-connect, 71-75 journal-mode check
            # and temp_store pragma, 85 verify-after-commit) without ever
            # raising - proven by real behaviour continuing to work.
            instance.cache("k", {"v": 1}, ttl=60)
            self.assertEqual(instance.cached("k"), {"v": 1})

    def test_connection_rejects_a_journal_mode_that_is_not_persist(self):
        """store.py:72-73: if PRAGMA journal_mode=PERSIST does not actually
        report back 'persist', the connection must be rejected and closed,
        never silently used (store.py:76-78 close-then-reraise)."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "state"
            with patch.object(store, "os", FAKE_NT_OS):
                instance = store.Store(root)  # real bootstrap, real sqlite3

                fake_conn = MagicMock()
                fake_conn.execute.return_value.fetchone.return_value = ["wal"]
                with patch.object(store.sqlite3, "connect", return_value=fake_conn):
                    with self.assertRaisesRegex(AutoError, "UNSAFE_DATABASE"):
                        with instance.connection():
                            self.fail("must never yield a connection in a rejected journal mode")
                fake_conn.close.assert_called_once()


class VerifyWindowsDatabaseTests(unittest.TestCase):
    """`_verify_windows_database` (store.py:25-39) called directly, so each
    of its two independent rejection guards can be pinned precisely."""

    def _bootstrapped_instance(self, stack_root):
        with patch.object(store, "os", FAKE_NT_OS):
            return store.Store(stack_root)

    def test_rejects_a_stray_wal_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "state"
            instance = self._bootstrapped_instance(root)
            Path(str(instance.path) + "-wal").write_bytes(b"stale-wal-from-a-crash")
            with patch.object(store, "os", FAKE_NT_OS):
                with self.assertRaisesRegex(AutoError, "UNSAFE_DATABASE"):
                    instance._verify_windows_database()

    def test_rejects_a_missing_journal_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "state"
            instance = self._bootstrapped_instance(root)
            Path(str(instance.path) + "-journal").unlink()
            with patch.object(store, "os", FAKE_NT_OS):
                with self.assertRaisesRegex(AutoError, "UNSAFE_DATABASE"):
                    instance._verify_windows_database()


class PrepareWindowsDatabaseTests(unittest.TestCase):
    """`_prepare_windows_database` (store.py:41-60) called directly."""

    def _bootstrapped_instance(self, stack_root):
        with patch.object(store, "os", FAKE_NT_OS):
            return store.Store(stack_root)

    def test_also_rejects_a_stray_wal_file_before_touching_any_file(self):
        """_prepare_windows_database has its own wal/shm rejection loop
        (store.py:42-45), independent of _verify_windows_database's."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "state"
            instance = self._bootstrapped_instance(root)
            Path(str(instance.path) + "-shm").write_bytes(b"stale-shm")
            with patch.object(store, "os", FAKE_NT_OS):
                with self.assertRaisesRegex(AutoError, "UNSAFE_DATABASE"):
                    instance._prepare_windows_database()

    def test_recovers_when_the_journal_file_appears_between_check_and_create(self):
        """store.py:52-59: if the O_CREAT|O_EXCL create loses a race to
        another process that just created the same file, the method must
        not fail - it re-opens read-only and moves on."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "state"
            instance = self._bootstrapped_instance(root)
            journal = Path(str(instance.path) + "-journal")
            journal.unlink()  # force _prepare_windows_database to recreate it

            real_open = store.open_private_file
            calls = []

            def racing_open(path, flags, mode=0o600):
                calls.append(flags)
                if len(calls) == 1:
                    # Simulate another host process winning the O_CREAT|O_EXCL
                    # race an instant after our lexists() check passed.
                    journal.write_bytes(b"")
                    os.chmod(journal, 0o600)
                    raise AutoError("UNSAFE_PRIVATE_FILE")
                return real_open(path, flags, mode)

            with patch.object(store, "os", FAKE_NT_OS):
                with patch.object(store, "open_private_file", side_effect=racing_open):
                    instance._prepare_windows_database()

            self.assertTrue(journal.exists())
            self.assertGreaterEqual(len(calls), 2)
            self.assertTrue(calls[0] & os.O_EXCL, "first attempt must be the exclusive create")
            self.assertFalse(calls[-1] & os.O_EXCL, "the recovery re-open must not use O_EXCL")

    def test_reraises_when_the_file_genuinely_never_appears(self):
        """store.py:56-57: if the file *still* does not exist after the
        exception (a real, non-racy failure), the original error must
        propagate - never silently retried against a nonexistent file."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "state"
            instance = self._bootstrapped_instance(root)
            journal = Path(str(instance.path) + "-journal")
            journal.unlink()

            state = {"calls": 0}

            def always_fail_first_then_would_succeed(path, flags, mode=0o600):
                state["calls"] += 1
                if state["calls"] == 1:
                    raise AutoError("SIMULATED_PERMANENT_FAILURE")
                return 999  # only reachable if the guard at store.py:56-57 is broken

            with patch.object(store, "os", FAKE_NT_OS):
                with patch.object(store, "open_private_file",
                                   side_effect=always_fail_first_then_would_succeed):
                    with self.assertRaisesRegex(AutoError, "SIMULATED_PERMANENT_FAILURE"):
                        instance._prepare_windows_database()

            self.assertEqual(state["calls"], 1, "must not retry once the file is confirmed absent")
            self.assertFalse(journal.exists())


if __name__ == "__main__":
    unittest.main()
