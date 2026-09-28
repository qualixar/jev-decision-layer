"""Focused portability tests for OS credential-store selection.

All credentials below are synthetic. No test accesses a real OS credential
store, provider endpoint, or user key.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import ctypes
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "plugins" / "qualixar-jev-decision-layer" / "runtime"
sys.path.insert(0, str(RUNTIME))


def subprocess_result(returncode: int, stdout: bytes, stderr: bytes = b"") -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class CredentialStoreFactoryTests(unittest.TestCase):
    def test_factory_selects_exact_platform_adapter(self):
        from src.adl.api.credential_store import (
            LinuxSecretService,
            MacKeychain,
            WindowsCredentialManager,
            credential_store_for_platform,
        )

        self.assertIsInstance(credential_store_for_platform(platform_name="Darwin"), MacKeychain)
        self.assertIsInstance(credential_store_for_platform(platform_name="Linux"), LinuxSecretService)
        self.assertIsInstance(credential_store_for_platform(platform_name="Windows"), WindowsCredentialManager)

    def test_factory_fails_closed_for_unknown_platform(self):
        from src.adl.api.credential_store import CredentialStoreError, credential_store_for_platform

        with self.assertRaisesRegex(CredentialStoreError, "CREDENTIAL_STORE_UNSUPPORTED"):
            credential_store_for_platform(platform_name="Plan9")


class LinuxSecretServiceTests(unittest.TestCase):
    def test_missing_secret_tool_is_unavailable_without_plaintext_fallback(self):
        from src.adl.api.credential_store import LinuxSecretService, CredentialStoreError

        store = LinuxSecretService()
        with patch("src.adl.api.credential_store.shutil.which", return_value=None):
            self.assertFalse(store.available())
            self.assertEqual(store.availability_error, "SECRET_SERVICE_DEPENDENCY_MISSING")
            with self.assertRaisesRegex(CredentialStoreError, "SECRET_SERVICE_DEPENDENCY_MISSING"):
                store.get("typesafe")

    def test_native_protocol_round_trip_uses_provider_attributes_and_no_secret_metadata(self):
        from src.adl.api.credential_store import LinuxSecretService

        class Item:
            def __init__(self, attributes, secret):
                self.attributes = attributes
                self.secret = secret

            def get_secret(self):
                return self.secret

            def delete(self):
                collection.items.remove(self)

        class Collection:
            def __init__(self):
                self.items = []
                self.locked = False
                self.created = []

            def is_locked(self):
                return self.locked

            def unlock(self, timeout=None):
                self.locked = False
                return False

            def search_items(self, attributes):
                return [item for item in self.items if all(item.attributes.get(k) == v for k, v in attributes.items())]

            def create_item(self, label, attributes, secret, replace=False):
                if replace:
                    self.items = [item for item in self.items if item.attributes.get("provider") != attributes["provider"]]
                self.created.append((label, dict(attributes), bytes(secret)))
                item = Item(dict(attributes), bytes(secret))
                self.items.append(item)
                return item

        collection = Collection()
        store = LinuxSecretService(backend=collection, interactive=True)
        store.put("typesafe", "synthetic-linux-key-123")
        self.assertEqual(store.get("typesafe"), "synthetic-linux-key-123")
        label, attributes, _secret = collection.created[0]
        self.assertEqual(label, "Qualixar Jev Decision Layer provider credential")
        self.assertEqual(attributes, {
            "application": "qualixar-jev-decision-layer", "provider": "typesafe"
        })
        self.assertNotIn("synthetic-linux-key-123", repr(attributes))
        store.delete("typesafe")
        self.assertEqual(collection.items, [])

    def test_locked_background_secret_service_never_opens_unlock_prompt(self):
        from src.adl.api.credential_store import CredentialStoreError, LinuxSecretService

        class LockedCollection:
            def __init__(self):
                self.unlock_calls = 0

            def is_locked(self):
                return True

            def unlock(self, timeout=None):
                self.unlock_calls += 1
                return False

        collection = LockedCollection()
        store = LinuxSecretService(backend=collection, interactive=False)
        with self.assertRaisesRegex(CredentialStoreError, "SECRET_SERVICE_LOCKED"):
            store.get("typesafe")
        self.assertEqual(collection.unlock_calls, 0)

    def test_secret_tool_store_receives_key_only_on_stdin_and_readback_is_verified(self):
        from src.adl.api.credential_store import LinuxSecretService

        responses = [
            subprocess_result(0, b""),
            subprocess_result(0, b""),
            subprocess_result(0, b"[/org/freedesktop/secrets/item1]\nsecret = synthetic-linux-key-125\n"),
        ]
        invocations = []

        def run(args, **kwargs):
            invocations.append((args, kwargs))
            return responses.pop(0)

        store = LinuxSecretService(interactive=True)
        with patch("src.adl.api.credential_store.shutil.which", return_value="/usr/bin/secret-tool"), \
             patch("src.adl.api.credential_store._trusted_system_executable", return_value="/usr/bin/secret-tool"), \
             patch.dict(os.environ, {"DBUS_SESSION_BUS_ADDRESS": "unix:path=/synthetic"}), \
             patch("src.adl.api.credential_store.subprocess.run", side_effect=run):
            store.put("typesafe", "synthetic-linux-key-125")

        store_call = invocations[1]
        self.assertEqual(store_call[0][:3], ["/usr/bin/secret-tool", "store", "--label"])
        self.assertEqual(store_call[1]["input"], b"synthetic-linux-key-125")
        self.assertNotIn("synthetic-linux-key-125", store_call[0])
        self.assertIs(store_call[1]["stderr"], subprocess.DEVNULL)
        self.assertTrue(all(call[1]["timeout"] == 30.0 for call in invocations))

    def test_background_search_never_requests_unlock_and_locked_service_fails_closed(self):
        from src.adl.api.credential_store import CredentialStoreError, LinuxSecretService

        invocations = []

        def run(args, **kwargs):
            invocations.append((args, kwargs))
            return subprocess_result(1, b"", b"native diagnostic must be discarded")

        store = LinuxSecretService(interactive=False)
        with patch("src.adl.api.credential_store.shutil.which", return_value="/usr/bin/secret-tool"), \
             patch("src.adl.api.credential_store._trusted_system_executable", return_value="/usr/bin/secret-tool"), \
             patch.dict(os.environ, {"DBUS_SESSION_BUS_ADDRESS": "unix:path=/synthetic"}), \
             patch("src.adl.api.credential_store.subprocess.run", side_effect=run):
            with self.assertRaisesRegex(CredentialStoreError, "SECRET_SERVICE_LOCKED"):
                store.get("typesafe")

        self.assertNotIn("--unlock", invocations[0][0])
        self.assertIs(invocations[0][1]["stderr"], subprocess.DEVNULL)
        self.assertIsNone(invocations[0][1]["input"])

    def test_secret_tool_clear_is_verified_without_putting_credential_in_arguments(self):
        from src.adl.api.credential_store import LinuxSecretService

        invocations = []
        responses = [
            subprocess_result(0, b"[/org/freedesktop/secrets/item1]\nsecret = synthetic-linux-key-126\n"),
            subprocess_result(0, b""),
            subprocess_result(0, b""),
        ]

        def run(args, **kwargs):
            invocations.append((args, kwargs))
            return responses.pop(0)

        store = LinuxSecretService(interactive=True)
        with patch("src.adl.api.credential_store.shutil.which", return_value="/usr/bin/secret-tool"), \
             patch("src.adl.api.credential_store._trusted_system_executable", return_value="/usr/bin/secret-tool"), \
             patch.dict(os.environ, {"DBUS_SESSION_BUS_ADDRESS": "unix:path=/synthetic"}), \
             patch("src.adl.api.credential_store.subprocess.run", side_effect=run):
            store.delete("typesafe")

        self.assertEqual(invocations[1][0][1], "clear")
        self.assertNotIn("synthetic-linux-key-126", invocations[1][0])
        self.assertEqual(invocations[2][0][1], "search")

    def test_missing_user_dbus_session_is_unavailable(self):
        from src.adl.api.credential_store import LinuxSecretService

        store = LinuxSecretService()
        with patch("src.adl.api.credential_store.shutil.which", return_value="/usr/bin/secret-tool"), \
             patch("src.adl.api.credential_store._trusted_system_executable", return_value="/usr/bin/secret-tool"), \
             patch.dict(os.environ, {}, clear=True):
            self.assertFalse(store.available())
        self.assertEqual(store.availability_error, "SECRET_SERVICE_UNAVAILABLE")

    def test_path_spoofed_secret_tool_is_rejected_before_provider_key_reaches_process(self):
        from src.adl.api.credential_store import CredentialStoreError, LinuxSecretService

        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "captured-secret"
            fake_tool = Path(directory) / "secret-tool"
            fake_tool.write_text(
                "#!/bin/sh\ncat > " + str(marker) + "\n",
                encoding="utf-8",
            )
            fake_tool.chmod(0o755)
            store = LinuxSecretService(interactive=True)
            with patch("src.adl.api.credential_store.shutil.which", return_value=str(fake_tool)), \
                 patch.dict(os.environ, {"DBUS_SESSION_BUS_ADDRESS": "unix:path=/synthetic"}), \
                 patch("src.adl.api.credential_store.subprocess.run") as run:
                with self.assertRaisesRegex(CredentialStoreError, "SECRET_SERVICE_DEPENDENCY_MISSING"):
                    store.put("typesafe", "synthetic-secret-that-must-not-leak")

            run.assert_not_called()
            self.assertFalse(marker.exists())

    def test_user_writable_path_symlink_is_never_returned_to_subprocess(self):
        from src.adl.api.credential_store import CredentialStoreError, LinuxSecretService

        trusted_target = next((Path(candidate) for candidate in ("/usr/bin/true", "/bin/true")
                               if Path(candidate).exists()), None)
        if trusted_target is None:
            self.skipTest("no standard system executable available for symlink resolution check")

        with tempfile.TemporaryDirectory() as directory:
            path_entry = Path(directory) / "secret-tool"
            path_entry.symlink_to(trusted_target)
            store = LinuxSecretService()
            with patch("src.adl.api.credential_store.shutil.which", return_value=str(path_entry)), \
                 patch.dict(os.environ, {"DBUS_SESSION_BUS_ADDRESS": "unix:path=/synthetic"}):
                try:
                    resolved = store._secret_tool()
                except CredentialStoreError as error:
                    self.assertEqual(str(error), "SECRET_SERVICE_DEPENDENCY_MISSING")
                else:
                    self.assertEqual(resolved, str(trusted_target.resolve()))
                    self.assertNotEqual(resolved, str(path_entry))


class WindowsCredentialManagerTests(unittest.TestCase):
    def test_target_is_provider_scoped_and_no_workspace_or_key_is_in_target(self):
        from src.adl.api.credential_store import WindowsCredentialManager

        store = WindowsCredentialManager(backend=object())
        self.assertEqual(store.target_for("typesafe"), "Qualixar/JevDecisionLayer/provider/typesafe")
        self.assertEqual(store.target_for("openrouter"), "Qualixar/JevDecisionLayer/provider/openrouter")
        from src.adl.api.credential_store import CredentialStoreError
        with self.assertRaises(CredentialStoreError):
            store.target_for("/workspace/typesafe")

    def test_backend_round_trip_verifies_value_and_uses_only_provider_target(self):
        from src.adl.api.credential_store import WindowsCredentialManager

        class Backend:
            def __init__(self):
                self.items = {}
                self.writes = []

            def get(self, target):
                return self.items[target]

            def put(self, target, value):
                self.writes.append((target, value))
                self.items[target] = value

            def delete(self, target):
                del self.items[target]

        backend = Backend()
        store = WindowsCredentialManager(backend=backend)
        store.put("typesafe", "synthetic-provider-key-01")
        self.assertEqual(store.get("typesafe"), "synthetic-provider-key-01")
        self.assertEqual(backend.writes, [
            ("Qualixar/JevDecisionLayer/provider/typesafe", "synthetic-provider-key-01")
        ])

    def test_native_write_uses_generic_local_credential_and_frees_successful_read(self):
        from src.adl.api import credential_store as module

        secret = b"synthetic-windows-key-123"
        buffer = ctypes.create_string_buffer(secret, len(secret))
        record = module._CREDENTIAL()
        record.CredentialBlobSize = len(secret)
        record.CredentialBlob = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
        record_pointer = ctypes.pointer(record)
        reads = []
        writes = []
        freed = []

        def cred_read(target, credential_type, flags, result_pointer):
            reads.append((target, credential_type, flags))
            if len(reads) == 1:
                return 0
            ctypes.cast(result_pointer, ctypes.POINTER(ctypes.POINTER(module._CREDENTIAL)))[0] = record_pointer
            return 1

        def cred_write(record_pointer_arg, flags):
            current = ctypes.cast(record_pointer_arg, ctypes.POINTER(module._CREDENTIAL)).contents
            writes.append((current.TargetName, current.Type, current.Persist,
                           ctypes.string_at(current.CredentialBlob, current.CredentialBlobSize), flags))
            return 1

        api = SimpleNamespace(
            CredReadW=cred_read,
            CredWriteW=cred_write,
            CredDeleteW=lambda *_args: 1,
            CredFree=lambda pointer: freed.append(pointer),
        )
        store = module.WindowsCredentialManager()
        store._advapi32 = api
        with patch.object(module.platform, "system", return_value="Windows"), \
             patch.object(module.ctypes, "get_last_error", return_value=module.WindowsCredentialManager.ERROR_NOT_FOUND,
                          create=True):
            store.put("typesafe", "synthetic-windows-key-123")

        self.assertEqual(writes, [(
            "Qualixar/JevDecisionLayer/provider/typesafe", 1, 2, secret, 0
        )])
        self.assertEqual(reads, [
            ("Qualixar/JevDecisionLayer/provider/typesafe", 1, 0),
            ("Qualixar/JevDecisionLayer/provider/typesafe", 1, 0),
        ])
        self.assertEqual(len(freed), 1)


class SetupCredentialStoreTests(unittest.TestCase):
    class Store:
        def __init__(self, *, available=True):
            self.items = {}
            self._available = available

        @property
        def availability_error(self):
            return "SECRET_SERVICE_DEPENDENCY_MISSING"

        def available(self):
            return self._available

        def get(self, provider):
            from src.adl.api.credential_store import CredentialStoreError
            try:
                return self.items[provider]
            except KeyError:
                raise CredentialStoreError("CREDENTIAL_ITEM_MISSING") from None

        def put(self, provider, value):
            self.items[provider] = value

        def delete(self, provider):
            del self.items[provider]

    def test_preview_reports_specific_missing_linux_secret_service_dependency(self):
        from src.adl.api.setup_controller import SetupChoice, SetupController, SetupError

        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "project"
            workspace.mkdir()
            controller = SetupController(workspace, credential_store=self.Store(available=False))
            choice = SetupChoice("typesafe", "public", 7, 20, 20_000)
            with self.assertRaisesRegex(SetupError, "SECRET_SERVICE_DEPENDENCY_MISSING"):
                controller.preview(choice)

    def test_fresh_hosted_setup_persists_os_selector_after_native_key_verification(self):
        from src.adl.api.setup_controller import SetupChoice, SetupController

        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "project"
            workspace.mkdir()
            store = self.Store()
            persisted = []
            controller = SetupController(
                workspace,
                credential_store=store,
                persist=lambda _workspace, policy: persisted.append(policy),
                complete=lambda *_: None,
                bridge=lambda *_: None,
                start=lambda *_: None,
                policy_exists=lambda: False,
            )
            choice = SetupChoice("typesafe", "public", 7, 20, 20_000)
            controller.preview(choice)
            result = controller.apply(choice, credential="synthetic-provider-key-02", confirmed=True)
            self.assertEqual(result["status"], "ENROLLED_PENDING_HOST_TRUST")
            self.assertEqual(persisted[0]["credential_store"], "os")
            self.assertEqual(store.items["typesafe"], "synthetic-provider-key-02")


class ProviderOsCredentialSelectionTests(unittest.TestCase):
    def test_os_selector_uses_native_store_and_does_not_read_legacy_env_or_file(self):
        from jevkit.providers import get_provider_credential, provider_profile
        from src.adl.api.credential_store import CredentialStoreError
        from src.adl.api import credential_store

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "typesafe-api-key").write_text("legacy-synthetic-key\n")
            with patch.dict(os.environ, {"TYPESAFE_API_KEY": "environment-synthetic-key"}):
                with patch.object(credential_store, "credential_store_for_platform") as factory:
                    factory.return_value.get.side_effect = CredentialStoreError("CREDENTIAL_ITEM_MISSING")
                    with self.assertRaisesRegex(Exception, "CREDENTIAL_ITEM_MISSING"):
                        get_provider_credential(provider_profile("typesafe"), config_root=root, credential_store="os")
            factory.assert_called_once_with()
            factory.return_value.get.assert_called_once_with("typesafe")

    def test_os_selector_returns_native_value(self):
        from jevkit.providers import get_provider_credential, provider_profile
        from src.adl.api import credential_store

        with patch.object(credential_store, "credential_store_for_platform") as factory:
            factory.return_value.get.return_value = "synthetic-native-key-123"
            actual = get_provider_credential(provider_profile("typesafe"), credential_store="os")
        self.assertEqual(actual, "synthetic-native-key-123")

    def test_remote_provider_requests_os_selector_and_refuses_before_network_when_missing(self):
        from jev_auto.providers import Providers
        from jevkit.security import SafeError

        with patch("jevkit.providers.get_provider_credential",
                   side_effect=SafeError("CREDENTIAL_ITEM_MISSING")) as getter, \
             patch("jev_auto.providers.http.client.HTTPSConnection") as connection:
            provider = Providers()
            with self.assertRaisesRegex(SafeError, "CREDENTIAL_ITEM_MISSING"):
                provider.remote(
                    {"provider": "typesafe", "timeout_seconds": 12, "credential_store": "os"},
                    {"input": "synthetic"},
                    {"decision": {"type": "noul", "instructions": "test"}},
                )
        self.assertEqual(getter.call_args.kwargs["credential_store"], "os")
        connection.assert_not_called()


if __name__ == "__main__":
    unittest.main()
