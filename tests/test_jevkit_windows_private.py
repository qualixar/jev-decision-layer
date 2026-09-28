"""Legacy jevkit private helpers must use the protected Windows file backend."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

RUNTIME = Path(__file__).resolve().parents[1] / "plugins" / "qualixar-jev-decision-layer" / "runtime"
sys.path.insert(0, str(RUNTIME))

from jev_auto.common import AutoError
from jev_auto import platform_fs
from jevkit import security
from jevkit import authorization, policy_mode, providers


class WindowsPrivateBridgeTests(unittest.TestCase):
    def test_private_dir_uses_verified_backend(self):
        path = Path("private")
        with patch.object(security, "_windows_private_storage", return_value=True), patch.object(
            platform_fs, "ensure_private_dir"
        ) as ensure:
            self.assertIsNone(security.private_dir(path))
        ensure.assert_called_once_with(path)

    def test_private_dir_fails_closed_if_backend_rejects_acl(self):
        with patch.object(security, "_windows_private_storage", return_value=True), patch.object(
            platform_fs, "ensure_private_dir", side_effect=AutoError("WINDOWS_PRIVATE_STATE_UNVERIFIED")
        ):
            with self.assertRaisesRegex(security.SafeError, "WINDOWS_PRIVATE_STATE_UNVERIFIED"):
                security.private_dir(Path("private"))

    def test_private_json_uses_protected_atomic_writer(self):
        path = Path("private") / "receipt.json"
        with patch.object(security, "_windows_private_storage", return_value=True), patch.object(
            platform_fs, "atomic_write_private"
        ) as write:
            security.private_json(path, {"z": 1, "a": 2})
            write.assert_called_once_with(path, b'{"a":2,"z":1}\n', replace=True)
            write.reset_mock()
            security.private_json(path, {"a": 2}, no_clobber=True)
            write.assert_called_once_with(path, b'{"a":2}\n', replace=False)

    def test_private_json_no_clobber_preserves_file_exists_contract(self):
        path = Path("private") / "receipt.json"
        with patch.object(security, "_windows_private_storage", return_value=True), patch.object(
            platform_fs, "atomic_write_private", side_effect=AutoError("WORKSPACE_ALREADY_ENROLLED")
        ):
            with self.assertRaises(FileExistsError):
                security.private_json(path, {"a": 1}, no_clobber=True)

    def test_private_json_rejects_unverified_windows_file_without_fallback(self):
        path = Path("private") / "receipt.json"
        with patch.object(security, "_windows_private_storage", return_value=True), patch.object(
            platform_fs, "atomic_write_private", side_effect=AutoError("UNSAFE_PRIVATE_FILE")
        ):
            with self.assertRaisesRegex(security.SafeError, "UNSAFE_PRIVATE_FILE"):
                security.private_json(path, {"a": 1})
        self.assertFalse(path.exists())

    @unittest.skipUnless(os.name == "nt", "native Windows ACL check")
    def test_native_private_json_has_verified_dacl_and_no_clobber(self):
        appdata = os.environ.get("LOCALAPPDATA")
        if not appdata:
            self.skipTest("LOCALAPPDATA unavailable")
        with tempfile.TemporaryDirectory(dir=appdata) as temp:
            path = Path(temp) / "private" / "receipt.json"
            security.private_json(path, {"a": 1}, no_clobber=True)
            platform_fs.verify_private_dir(path.parent)
            descriptor = platform_fs.open_private_file(path, os.O_RDONLY)
            try:
                self.assertEqual(json.loads(os.read(descriptor, 100)), {"a": 1})
            finally:
                os.close(descriptor)
            with self.assertRaises(FileExistsError):
                security.private_json(path, {"a": 2}, no_clobber=True)


class LegacyWindowsCapabilityTests(unittest.TestCase):
    def test_legacy_sqlite_grants_are_unavailable_before_file_creation(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(
            authorization, "_windows_legacy_storage", return_value=True
        ):
            budget = authorization.CallBudget(Path(temp), storage_root=Path(temp) / "grants")
            with self.assertRaisesRegex(security.SafeError, "LEGACY_GRANTS_UNAVAILABLE_WINDOWS"):
                budget.connect()
            self.assertFalse(budget.path.exists())
            self.assertFalse(budget.path.parent.exists())

    def test_legacy_credential_file_read_and_write_are_unavailable(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(
            providers, "_windows_legacy_storage", return_value=True
        ):
            root = Path(temp) / "config"
            with self.assertRaisesRegex(security.SafeError, "LEGACY_CREDENTIALS_UNAVAILABLE_WINDOWS"):
                providers.get_provider_credential("typesafe", config_root=root)
            with self.assertRaisesRegex(security.SafeError, "LEGACY_CREDENTIALS_UNAVAILABLE_WINDOWS"):
                providers.store_provider_credential("typesafe", "x" * 16, config_root=root)
            self.assertFalse(root.exists())

    def test_os_credential_store_remains_available_for_canonical_hosted_route(self):
        with patch.object(providers, "_windows_legacy_storage", return_value=True), patch(
            "src.adl.api.credential_store.credential_store_for_platform"
        ) as store_factory:
            store_factory.return_value.get.return_value = "x" * 16
            self.assertEqual(
                providers.get_provider_credential("typesafe", credential_store="os"), "x" * 16
            )
            store_factory.return_value.get.assert_called_once_with("typesafe")

    def test_legacy_provider_config_files_are_not_read_on_windows(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(
            providers, "_windows_legacy_storage", return_value=True
        ), patch.dict(os.environ, {"JEV_PROVIDER": ""}):
            root = Path(temp)
            self.assertEqual(providers.resolve_provider(config_root=root).provider_id, "typesafe")
            (root / "provider").write_text("openrouter")
            with self.assertRaisesRegex(security.SafeError, "LEGACY_PROVIDER_FILES_UNAVAILABLE_WINDOWS"):
                providers.resolve_provider(config_root=root)

    def test_legacy_policy_config_write_and_existing_read_are_unavailable(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(
            policy_mode, "_windows_legacy_storage", return_value=True
        ), patch.dict(os.environ, {"QUALIXAR_JEV_POLICY_MODE": ""}):
            os.environ.pop("QUALIXAR_JEV_POLICY_MODE", None)
            root = Path(temp)
            self.assertEqual(policy_mode.policy_mode(root), "assist")
            with self.assertRaisesRegex(security.SafeError, "LEGACY_POLICY_STORAGE_UNAVAILABLE_WINDOWS"):
                policy_mode.write_policy_mode("off", root)
            (root / "policy-mode").write_text("off")
            with self.assertRaisesRegex(security.SafeError, "LEGACY_POLICY_STORAGE_UNAVAILABLE_WINDOWS"):
                policy_mode.policy_mode(root)

    @unittest.skipUnless(os.name == "nt", "native Windows legacy fail-closed check")
    def test_native_legacy_grants_do_not_create_sqlite(self):
        with tempfile.TemporaryDirectory() as temp:
            budget = authorization.CallBudget(Path(temp))
            with self.assertRaisesRegex(security.SafeError, "LEGACY_GRANTS_UNAVAILABLE_WINDOWS"):
                budget.connect()
            self.assertFalse(budget.path.exists())


if __name__ == "__main__":
    unittest.main()
