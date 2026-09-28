"""Native Windows Credential Manager CRUD using a unique disposable target."""
from __future__ import annotations

import os
import secrets
import unittest
import uuid
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "plugins" / "qualixar-jev-decision-layer" / "runtime"
sys.path.insert(0, str(RUNTIME))


@unittest.skipUnless(os.name == "nt", "requires native Windows Credential Manager")
class WindowsCredentialManagerNativeTests(unittest.TestCase):
    def test_fixture_credential_write_read_delete_is_isolated_and_silent(self):
        from src.adl.api.credential_store import (
            CredentialStoreError,
            WindowsCredentialManager,
        )

        # A random namespace avoids reading, replacing, or deleting the
        # provider targets used by an actual Jev installation.
        isolated = type(
            "IsolatedWindowsCredentialManager",
            (WindowsCredentialManager,),
            {"_PREFIX": "Qualixar/JevDecisionLayer/ci-fixture/" + uuid.uuid4().hex + "/"},
        )
        store = isolated()
        provider = "typesafe"
        fixture = secrets.token_urlsafe(32)
        self.assertTrue(store.available())
        try:
            with self.assertRaisesRegex(CredentialStoreError, "CREDENTIAL_ITEM_MISSING"):
                store.get(provider)
            store.put(provider, fixture)
            actual = store.get(provider)
            self.assertTrue(secrets.compare_digest(actual, fixture))
            store.delete(provider)
            with self.assertRaisesRegex(CredentialStoreError, "CREDENTIAL_ITEM_MISSING"):
                store.get(provider)
        finally:
            try:
                store.delete(provider)
            except CredentialStoreError as error:
                if str(error) != "CREDENTIAL_ITEM_MISSING":
                    raise


if __name__ == "__main__":
    unittest.main()
