"""The shipped wizard can complete synthetic setup without a real credential."""

from __future__ import annotations

import http.client
import os
import re
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "plugins" / "qualixar-jev-decision-layer" / "runtime"
sys.path.insert(0, str(RUNTIME))


class _MemoryKeychain:
    def __init__(self):
        self.keys = {}

    def get(self, provider):
        from src.adl.api.keychain import KeychainError

        if provider not in self.keys:
            raise KeychainError("KEYCHAIN_ITEM_MISSING")
        return self.keys[provider]

    def put(self, provider, value):
        self.keys[provider] = value


class SetupSmokeTests(unittest.TestCase):
    def test_mode_selector_compiles_five_clear_user_choices(self):
        from src.adl.api.setup_server import _SetupHandler

        base = {"provider": "typesafe", "days": "2", "daily_calls": "20", "daily_bytes": "20000"}
        cases = {
            "jev-public": ("typesafe", "public", False),
            "jev-internal": ("typesafe", "internal-minimized", False),
            "jev-maximum": ("typesafe", "restricted", False),
            "hybrid": ("typesafe", "internal-minimized", True),
            "laya-only": ("laya-mlx", "restricted", False),
        }
        for mode, expected in cases.items():
            with self.subTest(mode=mode):
                choice = _SetupHandler._choice({**base, "mode": mode})
                self.assertEqual((choice.provider, choice.data_classification, choice.local_laya_enabled), expected)
                self.assertEqual(choice.decision_mode, mode)
                self.assertTrue(choice.generic_query_enabled)
                self.assertFalse(choice.auto_prepare_jev)

        activated = _SetupHandler._choice({**base, "mode": "jev-public",
                                           "generic": "on", "auto_prepare": "on"})
        self.assertTrue(activated.auto_prepare_jev)

        disabled = _SetupHandler._choice({**base, "mode": "jev-public",
                                          "generic": "off", "auto_prepare": "off"})
        self.assertFalse(disabled.generic_query_enabled)
        self.assertFalse(disabled.auto_prepare_jev)

    def test_private_status_page_shows_scope_without_reflecting_key(self):
        from jev_auto.settings import make_policy, save_policy
        from src.adl.api.setup_controller import SetupController
        from src.adl.api.setup_server import SetupServer

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "project"
            workspace.mkdir()
            keychain = _MemoryKeychain()
            keychain.put("typesafe", "synthetic-key-123456")
            with patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state")}):
                policy = make_policy(workspace, "typesafe", days=1, max_calls_per_day=20,
                                     max_bytes_per_day=20000, data_classification="public",
                                     generic_query_enabled=True, auto_prepare_jev=True,
                                     credential_store="keychain", setup_origin="local_wizard", setup_state="ready")
                save_policy(workspace, policy)
                controller = SetupController(workspace, keychain=keychain,
                                             bridge=lambda *_: None, start=lambda *_: None)
                server = SetupServer(("127.0.0.1", 0), controller, host_inventory=lambda: [])
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                    connection.request("GET", "/setup")
                    response = connection.getresponse()
                    response.read()
                    cookie = response.getheader("Set-Cookie").split(";", 1)[0]
                    connection.close()
                    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                    connection.request("GET", "/status", headers={"Cookie": cookie})
                    response = connection.getresponse()
                    body = response.read()
                    self.assertEqual(response.status, 200)
                    self.assertIn(b"TypeSafe", body)
                    self.assertIn(b"public", body)
                    self.assertIn(b"20 attempts/day", body)
                    self.assertNotIn(b"synthetic-key-123456", body)
                    connection.close()
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=2)

    def test_hosted_workspace_can_review_and_add_attested_laya_as_secondary_route(self):
        from jev_auto.settings import load_policy
        from src.adl.api.setup_controller import SetupChoice, SetupController

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "project"
            workspace.mkdir()
            keychain = _MemoryKeychain()
            keychain.put("typesafe", "synthetic-key-123456")
            model = {"repository": "aac6fef/laya-mlx", "revision": "a" * 40,
                     "weight_sha256": "b" * 64, "model_dir": "/synthetic/model",
                     "artifact_manifest": "/synthetic/manifest", "runtime_commit": "c" * 40,
                     "python": "/synthetic/python"}
            with patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state")}):
                controller = SetupController(workspace, keychain=keychain,
                                             bridge=lambda *_: None, start=lambda *_: None,
                                             local_config=lambda: model,
                                             local_attestor=lambda config: dict(config))
                choice = SetupChoice("typesafe", "public", 2, 20, 20000, True, True,
                                     local_laya_enabled=True)
                self.assertTrue(controller.preview(choice)["local_laya_enabled"])
                controller.apply(choice, credential=None, confirmed=True)
                saved = load_policy(workspace)
            self.assertEqual(saved["provider"], "typesafe")
            self.assertTrue(saved["local_laya_enabled"])
            self.assertEqual(saved["routes"]["probe"], "laya-mlx")
            self.assertEqual(saved["routes"]["sieve"], "laya-mlx")
            self.assertEqual(saved["mlx"]["weight_sha256"], model["weight_sha256"])

    def test_new_hybrid_setup_accepts_hosted_key_and_attested_local_model(self):
        from jev_auto.settings import load_policy
        from src.adl.api.setup_controller import SetupChoice, SetupController

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            project.mkdir()
            keychain = _MemoryKeychain()
            model = {"repository": "aac6fef/laya-mlx", "revision": "a" * 40,
                     "weight_sha256": "b" * 64, "model_dir": "/synthetic/model",
                     "artifact_manifest": "/synthetic/manifest"}
            with patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state")}):
                controller = SetupController(project, keychain=keychain,
                                             bridge=lambda *_: None, start=lambda *_: None,
                                             local_config=lambda: model,
                                             local_attestor=lambda item: dict(item))
                choice = SetupChoice("typesafe", "internal-minimized", 2, 20, 20000,
                                     True, True, True, "hybrid")
                controller.preview(choice)
                controller.apply(choice, credential="synthetic-key-123456", confirmed=True)
                saved = load_policy(project)
            self.assertEqual(saved["decision_mode"], "hybrid")
            self.assertEqual(saved["provider"], "typesafe")
            self.assertEqual(keychain.get("typesafe"), "synthetic-key-123456")

    def test_real_keychain_setup_fails_early_off_macos(self):
        from src.adl.api.setup_controller import SetupChoice, SetupController, SetupError

        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "project"
            project.mkdir()
            controller = SetupController(project, bridge=lambda *_: None, start=lambda *_: None)
            choice = SetupChoice("typesafe", "public", 1, 20, 20000,
                                 generic_query_enabled=True, decision_mode="jev-public")
            with patch("src.adl.api.setup_controller.platform.system", return_value="Linux"):
                with self.assertRaisesRegex(SetupError, "GUIDED_SETUP_MACOS_ONLY"):
                    controller.preview(choice)

    def test_jev_maximum_requires_separate_hosted_scope_confirmation(self):
        from src.adl.api.setup_controller import SetupController
        from src.adl.api.setup_server import SetupServer

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            project.mkdir()
            with patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state")}):
                controller = SetupController(project, keychain=_MemoryKeychain(),
                                             bridge=lambda *_: None, start=lambda *_: None)
                server = SetupServer(("127.0.0.1", 0), controller, host_inventory=lambda: [])
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    def request(method, path, values=None, cookie=None):
                        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                        body = urlencode(values).encode() if values is not None else None
                        headers = {"Origin": f"http://127.0.0.1:{server.server_port}",
                                   "Content-Type": "application/x-www-form-urlencoded"} if body else {}
                        if cookie:
                            headers["Cookie"] = cookie
                        connection.request(method, path, body=body, headers=headers)
                        response = connection.getresponse()
                        value = response.status, dict(response.getheaders()), response.read()
                        connection.close()
                        return value

                    _, headers, page = request("GET", "/setup")
                    cookie = headers["Set-Cookie"].split(";", 1)[0]
                    csrf = re.search(rb'name=[\'\"]csrf[\'\"] value=[\'\"]([^\'\"]+)', page).group(1).decode()
                    fields = {"csrf": csrf, "provider": "typesafe", "mode": "jev-maximum",
                              "days": "1", "daily_calls": "2", "daily_bytes": "2000",
                              "generic": "on", "auto_prepare": "off"}
                    status, _, review = request("POST", "/preview", fields, cookie)
                    self.assertEqual(status, 200)
                    self.assertIn(b"broader hosted-data scope", review)
                    nonce = re.search(rb'name=[\'\"]review_nonce[\'\"] value=[\'\"]([^\'\"]+)', review).group(1).decode()
                    apply_fields = {**fields, "review_nonce": nonce, "confirm": "yes",
                                    "credential": "synthetic-key-123456"}
                    self.assertEqual(request("POST", "/apply", apply_fields, cookie)[0], 400)
                    self.assertFalse(controller.policy_exists())
                    self.assertEqual(request("POST", "/apply", {**apply_fields,
                        "confirm_external_scope": "yes"}, cookie)[0], 200)
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=2)

    def test_legacy_restricted_hosted_setup_requires_external_scope_confirmation(self):
        """Old clients cannot bypass the maximum hosted-data acknowledgement."""
        from src.adl.api.setup_controller import SetupController
        from src.adl.api.setup_server import SetupServer

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            project.mkdir()
            with patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state")}):
                controller = SetupController(project, keychain=_MemoryKeychain(),
                                             bridge=lambda *_: None, start=lambda *_: None)
                server = SetupServer(("127.0.0.1", 0), controller, host_inventory=lambda: [])
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    def request(method, path, values=None, cookie=None):
                        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                        body = urlencode(values).encode() if values is not None else None
                        headers = {"Origin": f"http://127.0.0.1:{server.server_port}",
                                   "Content-Type": "application/x-www-form-urlencoded"} if body else {}
                        if cookie:
                            headers["Cookie"] = cookie
                        connection.request(method, path, body=body, headers=headers)
                        response = connection.getresponse()
                        value = response.status, dict(response.getheaders()), response.read()
                        connection.close()
                        return value

                    _, headers, page = request("GET", "/setup")
                    cookie = headers["Set-Cookie"].split(";", 1)[0]
                    csrf = re.search(rb'name=[\'\"]csrf[\'\"] value=[\'\"]([^\'\"]+)', page).group(1).decode()
                    # This is the pre-five-mode payload shape used by older clients.
                    fields = {"csrf": csrf, "provider": "typesafe", "classification": "restricted",
                              "days": "1", "daily_calls": "2", "daily_bytes": "2000", "generic": "on"}
                    status, _, review = request("POST", "/preview", fields, cookie)
                    self.assertEqual(status, 200)
                    self.assertIn(b"broader hosted-data scope", review)
                    nonce = re.search(rb'name=[\'\"]review_nonce[\'\"] value=[\'\"]([^\'\"]+)', review).group(1).decode()
                    apply_fields = {**fields, "review_nonce": nonce, "confirm": "yes",
                                    "credential": "synthetic-key-123456"}
                    status, _, body = request("POST", "/apply", apply_fields, cookie)
                    self.assertEqual(status, 400)
                    self.assertIn(b"EXTERNAL_SCOPE_CONFIRMATION_REQUIRED", body)
                    self.assertFalse(controller.policy_exists())
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=2)

    def test_setup_page_exposes_five_modes(self):
        from src.adl.api.setup_controller import SetupController
        from src.adl.api.setup_server import SetupServer

        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "project"
            project.mkdir()
            controller = SetupController(project, local_config=lambda: None,
                                         local_attestor=lambda item: item)
            server = SetupServer(("127.0.0.1", 0), controller, host_inventory=lambda: [])
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                connection.request("GET", "/setup")
                response = connection.getresponse()
                body = response.read().decode("utf-8")
                connection.close()
                self.assertEqual(response.status, 200)
                for mode in ("jev-public", "jev-internal", "jev-maximum", "hybrid", "laya-only"):
                    self.assertIn(f"value='{mode}'", body)
                self.assertIn("value='hybrid' disabled", body)
                self.assertIn("value='laya-only' disabled", body)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_existing_mode_and_prompt_setting_are_preselected_on_upgrade(self):
        from src.adl.api.setup_controller import SetupController
        from src.adl.api.setup_server import SetupServer

        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "project"
            project.mkdir()
            existing = {"provider": "typesafe", "data_classification": "restricted",
                        "decision_mode": "jev-maximum", "generic_query_enabled": True,
                        "auto_prepare_jev": True}
            controller = SetupController(project, policy_exists=lambda: True,
                                         load_existing=lambda: existing, local_config=lambda: None)
            server = SetupServer(("127.0.0.1", 0), controller, host_inventory=lambda: [])
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                connection.request("GET", "/setup")
                response = connection.getresponse()
                body = response.read().decode()
                connection.close()
                self.assertEqual(response.status, 200)
                self.assertIn("value='jev-maximum' selected", body)
                self.assertIn("value='on' selected>On for eligible Jev prompts", body)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_upgrade_review_page_shows_old_and_new_scope_before_confirmation(self):
        from jev_auto.settings import make_policy, save_policy
        from src.adl.api.setup_controller import SetupController
        from src.adl.api.setup_server import SetupServer

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "project"
            workspace.mkdir()
            keychain = _MemoryKeychain()
            keychain.put("typesafe", "synthetic-key-123456")
            with patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state")}):
                policy = make_policy(workspace, "typesafe", days=1, data_classification="public",
                                     credential_store="keychain", setup_origin="local_wizard", setup_state="ready")
                save_policy(workspace, policy)
                controller = SetupController(workspace, keychain=keychain,
                                             bridge=lambda *_: None, start=lambda *_: None)
                server = SetupServer(("127.0.0.1", 0), controller, host_inventory=lambda: [])
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                    connection.request("GET", "/setup")
                    response = connection.getresponse()
                    page = response.read()
                    cookie = response.getheader("Set-Cookie").split(";", 1)[0]
                    connection.close()
                    csrf = re.search(rb'name=[\'\"]csrf[\'\"] value=[\'\"]([^\'\"]+)', page).group(1).decode()
                    fields = {"csrf": csrf, "provider": "typesafe", "classification": "internal-minimized",
                              "days": "2", "daily_calls": "20", "daily_bytes": "20000"}
                    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                    connection.request("POST", "/preview", body=urlencode(fields), headers={
                        "Origin": f"http://127.0.0.1:{server.server_port}",
                        "Content-Type": "application/x-www-form-urlencoded", "Cookie": cookie})
                    response = connection.getresponse()
                    review = response.read()
                    self.assertEqual(response.status, 200)
                    self.assertIn(b"Current scope: public", review)
                    self.assertIn(b"New scope: internal-minimized", review)
                    connection.close()
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=2)

    def test_ready_public_policy_upgrades_only_after_review_without_reentering_key(self):
        from jev_auto.settings import load_policy, make_policy, save_policy
        from src.adl.api.setup_controller import SetupChoice, SetupController

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "project"
            workspace.mkdir()
            keychain = _MemoryKeychain()
            keychain.put("typesafe", "synthetic-key-123456")
            with patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state")}):
                original = make_policy(workspace, "typesafe", days=1, data_classification="public",
                                       generic_query_enabled=True, auto_prepare_jev=True,
                                       credential_store="keychain", setup_origin="local_wizard", setup_state="ready")
                save_policy(workspace, original)
                controller = SetupController(workspace, keychain=keychain,
                                             bridge=lambda *_: None, start=lambda *_: None)
                choice = SetupChoice("typesafe", "internal-minimized", 2, 20, 20000, True, True)
                review = controller.preview(choice)
                self.assertEqual(review["current_data_classification"], "public")
                self.assertTrue(review["upgrade"])
                result = controller.apply(choice, credential=None, confirmed=True)
                upgraded = load_policy(workspace)
            self.assertEqual(result["status"], "UPDATED_PENDING_HOST_TRUST")
            self.assertEqual(upgraded["data_classification"], "internal-minimized")
            self.assertEqual(upgraded["policy_id"], original["policy_id"], "scope upgrade reset the daily budget identity")
            self.assertEqual(keychain.get("typesafe"), "synthetic-key-123456")

    def test_scope_upgrade_refuses_unreviewed_or_changed_existing_policy(self):
        from jev_auto.settings import load_policy, make_policy, save_policy
        from src.adl.api.setup_controller import SetupChoice, SetupController, SetupError

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "project"
            workspace.mkdir()
            keychain = _MemoryKeychain()
            keychain.put("typesafe", "synthetic-key-123456")
            with patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state")}):
                original = make_policy(workspace, "typesafe", days=1, data_classification="public",
                                       credential_store="keychain", setup_origin="local_wizard", setup_state="ready")
                save_policy(workspace, original)
                controller = SetupController(workspace, keychain=keychain,
                                             bridge=lambda *_: None, start=lambda *_: None)
                choice = SetupChoice("typesafe", "internal-minimized", 2, 20, 20000, True, True)
                with self.assertRaises(SetupError):
                    controller.apply(choice, credential=None, confirmed=True)
                controller.preview(choice)
                save_policy(workspace, {**original, "max_calls_per_day": 5})
                with self.assertRaises(SetupError):
                    controller.apply(choice, credential=None, confirmed=True)
                self.assertEqual(load_policy(workspace)["data_classification"], "public")

    def test_automatic_hosted_prompt_requires_separate_generic_consent(self):
        from src.adl.api.setup_controller import SetupChoice, SetupController, SetupError

        with self.assertRaises(SetupError):
            SetupController._validate_choice(SetupChoice(
                provider="typesafe", data_classification="internal-minimized", days=1,
                daily_calls=2, daily_bytes=2000, generic_query_enabled=False,
                auto_prepare_jev=True))
        with self.assertRaises(SetupError):
            SetupController._validate_choice(SetupChoice(
                provider="laya-mlx", data_classification="public", days=1,
                daily_calls=2, daily_bytes=2000, generic_query_enabled=True,
                auto_prepare_jev=True))

    def test_existing_laya_record_can_be_discovered_read_only_without_importing_a_key(self):
        from src.adl.api.setup_controller import SetupController

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_record = root / ".local" / "state" / "qualixar-jev-auto" / "mlx-installation.json"
            old_record.parent.mkdir(parents=True, mode=0o700)
            old_record.parent.parent.chmod(0o700)
            old_record.write_text('{"repository":"aac6fef/laya-mlx","revision":"' + 'a' * 40 + '"}')
            old_record.chmod(0o600)
            with patch.object(Path, "home", return_value=root), patch.dict(os.environ, {
                    "XDG_STATE_HOME": str(root / "state")}):
                discovered = SetupController._read_local_config()
            self.assertEqual(discovered["repository"], "aac6fef/laya-mlx")
            self.assertEqual(old_record.read_text().count("repository"), 1, "compatibility read changed the old record")

    def test_review_then_confirm_is_the_only_way_to_save(self):
        from jev_auto.settings import load_policy
        from src.adl.api.setup_controller import SetupController
        from src.adl.api.setup_server import SetupServer

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "synthetic-project"
            workspace.mkdir()
            keychain = _MemoryKeychain()
            with patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state")}):
                controller = SetupController(workspace, keychain=keychain,
                                             bridge=lambda *_: None, start=lambda *_: None)
                server = SetupServer(("127.0.0.1", 0), controller, host_inventory=lambda: [])
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    def request(method, path, values=None, cookie=None):
                        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                        body = urlencode(values).encode() if values is not None else None
                        headers = {"Origin": f"http://127.0.0.1:{server.server_port}",
                                   "Content-Type": "application/x-www-form-urlencoded"} if body else {}
                        if cookie:
                            headers["Cookie"] = cookie
                        connection.request(method, path, body=body, headers=headers)
                        response = connection.getresponse()
                        result = response.status, dict(response.getheaders()), response.read()
                        connection.close()
                        return result

                    status, headers, page = request("GET", "/setup")
                    self.assertEqual(status, 200)
                    self.assertNotIn(b"synthetic-key-123456", page)
                    cookie = headers["Set-Cookie"].split(";", 1)[0]
                    csrf = re.search(rb'name=[\'\"]csrf[\'\"] value=[\'\"]([^\'\"]+)', page).group(1).decode()
                    fields = {"csrf": csrf, "provider": "typesafe", "classification": "public",
                              "days": "1", "daily_calls": "2", "daily_bytes": "2000",
                              "generic": "on", "auto_prepare": "on"}
                    self.assertEqual(request("POST", "/apply", {**fields, "confirm": "yes"}, cookie)[0], 403)
                    status, _, review = request("POST", "/preview", fields, cookie)
                    self.assertEqual(status, 200)
                    self.assertIn(str(workspace).encode(), review)
                    self.assertIn(b"Automatic Jev prompt guidance: Enabled", review)
                    nonce = re.search(rb'name=[\'\"]review_nonce[\'\"] value=[\'\"]([^\'\"]+)', review).group(1).decode()
                    status, _, saved = request("POST", "/apply", {**fields, "review_nonce": nonce,
                        "confirm": "yes", "credential": "synthetic-key-123456"}, cookie)
                    self.assertEqual(status, 200)
                    self.assertNotIn(b"synthetic-key-123456", saved)
                    self.assertEqual(keychain.get("typesafe"), "synthetic-key-123456")
                    self.assertEqual(load_policy(workspace)["setup_state"], "ready")
                    self.assertTrue(load_policy(workspace)["auto_prepare_jev"])
                    self.assertNotIn("synthetic-key-123456", (root / "state" / "qualixar-jev-decision-layer"
                        / load_policy(workspace)["workspace_id"] / "policy.json").read_text())
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
