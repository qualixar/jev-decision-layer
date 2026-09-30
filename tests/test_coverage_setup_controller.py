"""Coverage-floor tests for src/adl/api/setup_controller.py.

Targets the baseline-missing lines for this module: the constructor's
injection-conflict guard, the local-model-record fallback read's outer
except, the laya-only mode resolution, the provider-change guard on both
preview() and the upgrade path (including a TOCTOU race between the two),
the descendant-coverage and local-laya branches of an upgrade, every
selector branch of _read_policy_credential, and the unexpected-KeychainError
re-raise inside apply()'s fresh-enrollment credential check.

Every guard that refuses is also asserted to leave the on-disk policy
untouched -- consent tests must prove nothing was written on refusal.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "plugins" / "qualixar-jev-decision-layer" / "runtime"
sys.path.insert(0, str(RUNTIME))

from jev_auto.common import state_dir  # noqa: E402
from jev_auto.settings import (  # noqa: E402
    DESCENDANT_COVERAGE_APPROVED,
    load_policy,
    make_policy,
    save_policy,
)
from src.adl.api.keychain import KeychainError  # noqa: E402
from src.adl.api.setup_controller import (  # noqa: E402
    SetupChoice,
    SetupController,
    SetupError,
)


class _MemoryKeychain:
    def __init__(self, values=None):
        self.keys = dict(values or {})

    def get(self, provider):
        if provider not in self.keys:
            raise KeychainError("KEYCHAIN_ITEM_MISSING")
        return self.keys[provider]

    def put(self, provider, value):
        self.keys[provider] = value


class _TempProject(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        env = patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "state")})
        env.start()
        self.addCleanup(env.stop)
        self.project = self.root / "project"
        self.project.mkdir()


class ConstructorInjectionConflictTests(_TempProject):
    def test_supplying_both_credential_store_and_keychain_is_rejected(self):
        # Line 62.
        with self.assertRaises(ValueError) as ctx:
            SetupController(self.project, credential_store=object(), keychain=object())
        self.assertEqual(str(ctx.exception), "CREDENTIAL_STORE_INJECTION_CONFLICT")


class ReadLocalConfigOuterExceptTests(_TempProject):
    def test_a_symlinked_primary_record_falls_back_to_none_not_a_crash(self):
        # Lines 101-102: read_private raising something other than
        # FileNotFoundError (here, an O_NOFOLLOW OSError from a symlink) at
        # the PRIMARY location must be swallowed, not left to propagate.
        from jev_auto.common import home_root

        state_root = home_root()
        state_root.mkdir(parents=True, exist_ok=True)
        real_target = self.root / "elsewhere.json"
        real_target.write_text("{}")
        link = state_root / "mlx-installation.json"
        link.symlink_to(real_target)
        self.assertIsNone(SetupController._read_local_config())


class ResolvedModeTests(unittest.TestCase):
    def test_laya_mlx_provider_resolves_to_laya_only_with_no_explicit_mode(self):
        # Line 144.
        choice = SetupChoice("laya-mlx", "restricted", 1, 1, 1000)
        self.assertEqual(SetupController._resolved_mode(choice), "laya-only")


class PreviewProviderChangeTests(_TempProject):
    def test_preview_rejects_a_provider_switch_against_a_ready_policy(self):
        # Line 185.
        keychain = _MemoryKeychain({"typesafe": "synthetic-key-123456"})
        original = make_policy(self.project, "typesafe", days=1, data_classification="public",
                                credential_store="keychain", setup_origin="local_wizard",
                                setup_state="ready")
        save_policy(self.project, original)
        controller = SetupController(self.project, keychain=keychain,
                                      bridge=lambda *_: None, start=lambda *_: None)
        choice = SetupChoice("openrouter", "public", 1, 1, 1000)
        with self.assertRaises(SetupError) as ctx:
            controller.preview(choice)
        self.assertEqual(str(ctx.exception), "PROVIDER_CHANGE_REQUIRES_SEPARATE_SETUP")
        self.assertEqual(load_policy(self.project)["policy_id"], original["policy_id"])


class ApplyUpgradeProviderRaceTests(_TempProject):
    def test_a_provider_changed_between_preview_and_apply_is_rejected_in_apply_upgrade(self):
        # Line 216: preview() passed with a matching provider, but the stored
        # policy's provider changed before apply() re-reads it (a TOCTOU the
        # provider-match check in preview() alone cannot catch).
        keychain = _MemoryKeychain({"typesafe": "synthetic-key-123456", "openrouter": "synthetic-key-2"})
        original = make_policy(self.project, "typesafe", days=1, data_classification="public",
                                credential_store="keychain", setup_origin="local_wizard",
                                setup_state="ready")
        save_policy(self.project, original)
        controller = SetupController(self.project, keychain=keychain,
                                      bridge=lambda *_: None, start=lambda *_: None)
        choice = SetupChoice("typesafe", "internal-minimized", 2, 20, 20000)
        controller.preview(choice)
        rewritten = {**original, "provider": "openrouter"}
        save_policy(self.project, rewritten)
        with self.assertRaises(SetupError) as ctx:
            controller.apply(choice, credential=None, confirmed=True)
        self.assertEqual(str(ctx.exception), "PROVIDER_CHANGE_REQUIRES_SEPARATE_SETUP")
        self.assertEqual(load_policy(self.project)["provider"], "openrouter")


class ApplyUpgradeDescendantCoverageTests(_TempProject):
    def test_upgrading_to_cover_descendants_persists_the_grant_fields(self):
        # Lines 237-239.
        keychain = _MemoryKeychain({"typesafe": "synthetic-key-123456"})
        original = make_policy(self.project, "typesafe", days=1, data_classification="public",
                                credential_store="keychain", setup_origin="local_wizard",
                                setup_state="ready")
        save_policy(self.project, original)
        controller = SetupController(self.project, keychain=keychain,
                                      bridge=lambda *_: None, start=lambda *_: None)
        choice = SetupChoice("typesafe", "public", 2, 20, 20000, cover_descendants=True)
        controller.preview(choice)
        controller.apply(choice, credential=None, confirmed=True, descendants_confirmed=True)
        saved = load_policy(self.project)
        self.assertIs(saved["covers_descendants"], True)
        self.assertEqual(saved["descendant_approval"], DESCENDANT_COVERAGE_APPROVED)
        self.assertEqual(saved["workspace_path"], str(controller.workspace))


class ApplyUpgradeLocalLayaTests(_TempProject):
    def test_upgrading_to_add_local_laya_as_a_secondary_route_persists_mlx_and_routes(self):
        # Lines 250-252.
        keychain = _MemoryKeychain({"typesafe": "synthetic-key-123456"})
        original = make_policy(self.project, "typesafe", days=1, data_classification="public",
                                credential_store="keychain", setup_origin="local_wizard",
                                setup_state="ready")
        save_policy(self.project, original)
        model = {"repository": "aac6fef/laya-mlx", "revision": "a" * 40,
                 "weight_sha256": "b" * 64, "model_dir": "/synthetic/model",
                 "artifact_manifest": "/synthetic/manifest"}
        controller = SetupController(self.project, keychain=keychain,
                                      bridge=lambda *_: None, start=lambda *_: None,
                                      local_config=lambda: model,
                                      local_attestor=lambda cfg: dict(cfg))
        choice = SetupChoice("typesafe", "public", 2, 20, 20000, local_laya_enabled=True)
        review = controller.preview(choice)
        self.assertTrue(review["upgrade"])
        controller.apply(choice, credential=None, confirmed=True)
        saved = load_policy(self.project)
        self.assertEqual(saved["mlx"]["weight_sha256"], model["weight_sha256"])
        self.assertEqual(saved["routes"]["sieve"], "laya-mlx")
        self.assertEqual(saved["routes"]["probe"], "laya-mlx")


class ReadPolicyCredentialSelectorTests(_TempProject):
    def test_keychain_selector_is_unavailable_without_a_native_service_marker(self):
        # Line 293: not compat-injected (constructed via credential_store=)
        # and the store exposes no _SERVICES marker.
        controller = SetupController(self.project, credential_store=object(),
                                      bridge=lambda *_: None, start=lambda *_: None)
        with self.assertRaises(SetupError) as ctx:
            controller._read_policy_credential("typesafe", "keychain")
        self.assertEqual(str(ctx.exception), "KEYCHAIN_CREDENTIAL_UNAVAILABLE")

    def test_os_selector_reads_straight_from_the_injected_store(self):
        # Lines 295-296.
        store = _MemoryKeychain({"typesafe": "synthetic-os-store-key"})
        controller = SetupController(self.project, credential_store=store,
                                      bridge=lambda *_: None, start=lambda *_: None)
        self.assertEqual(controller._read_policy_credential("typesafe", "os"),
                          "synthetic-os-store-key")

    def test_legacy_selector_reads_a_real_environment_credential(self):
        # Lines 297-299, 301-302 (the success return).
        controller = SetupController(self.project, credential_store=object(),
                                      bridge=lambda *_: None, start=lambda *_: None)
        with patch.dict(os.environ, {"TYPESAFE_API_KEY": "synthetic-legacy-credential-1"}):
            self.assertEqual(controller._read_policy_credential("typesafe", "legacy"),
                              "synthetic-legacy-credential-1")

    def test_legacy_selector_fails_closed_with_no_credential_anywhere(self):
        # Lines 303-304 (SafeError caught and remapped).
        controller = SetupController(self.project, credential_store=object(),
                                      bridge=lambda *_: None, start=lambda *_: None)
        with patch.dict(os.environ, {"TYPESAFE_API_KEY": "", "XDG_CONFIG_HOME": str(self.root / "empty-config")}):
            with self.assertRaises(SetupError) as ctx:
                controller._read_policy_credential("typesafe", "legacy")
        self.assertEqual(str(ctx.exception), "LEGACY_CREDENTIAL_UNAVAILABLE")

    def test_an_unrecognised_selector_is_unsupported(self):
        # Line 305.
        controller = SetupController(self.project, credential_store=object(),
                                      bridge=lambda *_: None, start=lambda *_: None)
        with self.assertRaises(SetupError) as ctx:
            controller._read_policy_credential("typesafe", "bogus-selector")
        self.assertEqual(str(ctx.exception), "CREDENTIAL_STORE_UNSUPPORTED")


class FreshEnrollUnexpectedKeychainErrorTests(_TempProject):
    def test_an_unexpected_keychain_error_while_checking_for_a_prior_key_propagates_mapped(self):
        # Line 408: the bare `raise` for a KeychainError whose code is NOT one
        # of the ignorable "item missing" shapes, met while looking up an
        # existing credential during a fresh enrollment with a new credential
        # supplied. Confirms the failure is mapped by _keychain_error and that
        # nothing is persisted.
        class _FlakyStore:
            def get(self, provider):
                raise KeychainError("KEYCHAIN_UNAVAILABLE")

            def put(self, provider, value):
                raise AssertionError("must not store a credential after a failed lookup")

        controller = SetupController(self.project, credential_store=_FlakyStore(),
                                      bridge=lambda *_: None, start=lambda *_: None)
        choice = SetupChoice("typesafe", "public", 1, 20, 20000)
        with self.assertRaises(SetupError) as ctx:
            controller.apply(choice, credential="brand-new-key-123456", confirmed=True)
        self.assertEqual(str(ctx.exception), "KEYCHAIN_UNAVAILABLE")
        self.assertFalse((state_dir(self.project) / "policy.json").exists())


if __name__ == "__main__":
    unittest.main()
