"""Opt-in integration check against an ephemeral Linux Secret Service session."""
from __future__ import annotations

import os
import platform
import sys
import unittest
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "plugins" / "qualixar-jev-decision-layer" / "runtime"
sys.path.insert(0, str(RUNTIME))


@unittest.skipUnless(
    platform.system() == "Linux" and os.environ.get("JEV_RUN_NATIVE_SECRET_SERVICE") == "1",
    "requires the isolated Linux Secret Service CI session",
)
class NativeSecretServiceTests(unittest.TestCase):
    def test_fixture_credential_round_trip_and_delete(self):
        from src.adl.api.credential_store import CredentialStoreError, LinuxSecretService

        store = LinuxSecretService(interactive=True)
        self.assertTrue(store.available(), store.availability_error)
        fixture = "jev-ci-fixture-" + uuid.uuid4().hex
        try:
            store.put("typesafe", fixture)
            self.assertEqual(store.get("typesafe"), fixture)
            store.delete("typesafe")
            with self.assertRaisesRegex(CredentialStoreError, "CREDENTIAL_ITEM_MISSING"):
                store.get("typesafe")
        finally:
            try:
                store.delete("typesafe")
            except CredentialStoreError as error:
                if str(error) != "CREDENTIAL_ITEM_MISSING":
                    raise


if __name__ == "__main__":
    unittest.main()
