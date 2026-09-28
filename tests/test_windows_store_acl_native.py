"""Native Windows proof that SQLite state and its rollback journal stay private."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

RUNTIME = Path(__file__).resolve().parents[1] / "plugins" / "qualixar-jev-decision-layer" / "runtime"
sys.path.insert(0, str(RUNTIME))

from jev_auto.common import AutoError
from jev_auto.platform_fs import ensure_private_dir, open_private_file
from jev_auto.store import Store


@unittest.skipUnless(os.name == "nt", "native Windows ACL test")
class WindowsStoreAclTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "jev state"
        ensure_private_dir(self.root)
        self.database = self.root / "auto.sqlite3"
        self.journal = Path(str(self.database) + "-journal")

    def assert_private_file(self, path):
        fd = open_private_file(path, os.O_RDONLY)
        os.close(fd)

    def test_database_and_journal_remain_protected_after_writes_and_reopen(self):
        store = Store(self.root)
        store.put({"test": "first"})
        self.assertTrue(self.database.is_file())
        self.assertTrue(self.journal.is_file())
        self.assert_private_file(self.database)
        self.assert_private_file(self.journal)
        self.assertFalse(Path(str(self.database) + "-wal").exists())
        self.assertFalse(Path(str(self.database) + "-shm").exists())
        reopened = Store(self.root)
        reopened.put({"test": "second"})
        self.assert_private_file(self.database)
        self.assert_private_file(self.journal)

    def test_rejects_database_created_without_a_protected_dacl(self):
        self.database.write_bytes(b"")
        with self.assertRaises(AutoError):
            Store(self.root)
        self.assertEqual(self.database.read_bytes(), b"")

    def test_rejects_unsafe_existing_journal_without_replacing_it(self):
        store = Store(self.root)
        self.journal.unlink()
        self.journal.write_bytes(b"unsafe journal")
        with self.assertRaises(AutoError):
            Store(self.root)
        with self.assertRaises(AutoError):
            with store.connection():
                self.fail("SQLite must not open an unsafe journal")
        self.assertEqual(self.journal.read_bytes(), b"unsafe journal")

    def test_rejects_preexisting_wal_even_when_it_is_private(self):
        Store(self.root)
        wal = Path(str(self.database) + "-wal")
        fd = open_private_file(wal, os.O_CREAT | os.O_EXCL | os.O_RDWR)
        os.close(fd)
        with self.assertRaisesRegex(AutoError, "UNSAFE_DATABASE"):
            Store(self.root)
        self.assertTrue(wal.exists())


if __name__ == "__main__":
    unittest.main()
