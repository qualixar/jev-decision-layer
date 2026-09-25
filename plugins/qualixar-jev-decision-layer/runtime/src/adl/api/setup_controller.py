"""First-run consent controller for the local browser wizard.

This is invoked only after a user-click confirmation in the local UI. It never
prints or returns a provider key, and it refuses to replace an existing policy
until a separately reviewed upgrade flow exists.
"""

from __future__ import annotations

import hashlib
import hmac
import platform
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from jev_auto.common import AutoError, canonical, home_root, read_private, state_dir, workspace
from jev_auto.settings import load_policy, make_policy, replace_reviewed_policy, save_policy_new, transition_setup_policy_ready, validate_policy
from jevkit.engine import catalog

from .keychain import KeychainError, MacKeychain


class SetupError(RuntimeError):
    """A stable user-facing code; never include a credential or stack trace."""


@dataclass(frozen=True)
class SetupChoice:
    provider: str
    data_classification: str
    days: int
    daily_calls: int
    daily_bytes: int
    generic_query_enabled: bool = False
    auto_prepare_jev: bool = False
    local_laya_enabled: bool = False
    decision_mode: str | None = None


class SetupController:
    def __init__(
        self,
        workspace_path: Path,
        *,
        keychain: Any | None = None,
        persist: Callable[[Path, dict[str, Any]], None] = save_policy_new,
        complete: Callable[[Path, dict[str, Any]], None] = transition_setup_policy_ready,
        upgrade: Callable[[Path, dict[str, Any], dict[str, Any]], None] = replace_reviewed_policy,
        bridge: Callable[[Path, dict[str, Any]], None] | None = None,
        start: Callable[[Path], Any] | None = None,
        policy_exists: Callable[[], bool] | None = None,
        load_existing: Callable[[], dict[str, Any]] | None = None,
        local_config: Callable[[], dict[str, Any] | None] | None = None,
        local_attestor: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ):
        self.workspace = workspace(workspace_path)
        self.keychain = keychain if keychain is not None else MacKeychain()
        self.persist = persist
        self.complete = complete
        self.upgrade = upgrade
        if bridge is None:
            from jev_auto.cli import bridge_record

            bridge = bridge_record
        if start is None:
            from jev_auto.ipc import ensure

            start = ensure
        self.bridge = bridge
        self.start = start
        self.policy_exists = policy_exists if policy_exists is not None else (lambda: (state_dir(self.workspace) / "policy.json").exists())
        self.load_existing = load_existing if load_existing is not None else (lambda: load_policy(self.workspace))
        self.local_config = local_config or self._read_local_config
        self.local_attestor = local_attestor
        self._previewed_local_identity: tuple[object, ...] | None = None
        self._previewed_existing: dict[str, Any] | None = None

    @staticmethod
    def _read_local_config() -> dict[str, Any] | None:
        try:
            return read_private(home_root() / "mlx-installation.json", 100_000)
        except FileNotFoundError:
            # Reuse only a separately installed local model record. Provider
            # credentials and old workspace consent are never imported.
            try:
                return read_private(Path.home() / ".local" / "state" / "qualixar-jev-auto" / "mlx-installation.json", 100_000)
            except (AutoError, OSError):
                return None
        except (AutoError, OSError):
            return None

    @staticmethod
    def _validate_choice(choice: SetupChoice) -> None:
        mode_shape = {
            "jev-public": ("public", False),
            "jev-internal": ("internal-minimized", False),
            "jev-maximum": ("restricted", False),
            "hybrid": ("internal-minimized", True),
            "laya-only": ("restricted", False),
        }
        if (
            not isinstance(choice, SetupChoice)
            or choice.provider not in ("typesafe", "openrouter", "laya-mlx")
            or choice.data_classification not in ("public", "internal-minimized", "restricted")
            or not isinstance(choice.days, int) or isinstance(choice.days, bool) or not 1 <= choice.days <= 365
            or not isinstance(choice.daily_calls, int) or isinstance(choice.daily_calls, bool) or not 1 <= choice.daily_calls <= 100_000
            or not isinstance(choice.daily_bytes, int) or isinstance(choice.daily_bytes, bool) or not 1_000 <= choice.daily_bytes <= 10**10
            or not isinstance(choice.generic_query_enabled, bool)
            or not isinstance(choice.auto_prepare_jev, bool)
            or not isinstance(choice.local_laya_enabled, bool)
            or (choice.auto_prepare_jev and not choice.generic_query_enabled)
            or (choice.auto_prepare_jev and choice.provider == "laya-mlx")
            or (choice.local_laya_enabled and choice.provider == "laya-mlx")
            or (choice.decision_mode is not None and (
                choice.decision_mode not in mode_shape
                or (choice.data_classification, choice.local_laya_enabled) != mode_shape[choice.decision_mode]
                or (choice.provider == "laya-mlx") != (choice.decision_mode == "laya-only")
            ))
        ):
            raise SetupError("SETUP_SCOPE_INVALID")

    @staticmethod
    def _local_identity(config: dict[str, Any]) -> tuple[object, ...]:
        return tuple(config.get(name) for name in ("repository", "revision", "weight_sha256", "model_dir", "artifact_manifest"))

    @staticmethod
    def _resolved_mode(choice: SetupChoice) -> str | None:
        if choice.decision_mode is not None:
            return choice.decision_mode
        if choice.provider == "laya-mlx":
            return "laya-only"
        if choice.local_laya_enabled:
            return "hybrid" if choice.data_classification == "internal-minimized" else None
        return {"public": "jev-public", "internal-minimized": "jev-internal",
                "restricted": "jev-maximum"}[choice.data_classification]

    def _attested_local_model(self) -> dict[str, Any] | None:
        if self.local_attestor is None:
            return None
        try:
            config = self.local_config()
            if not isinstance(config, dict) or not config.get("repository") or not config.get("revision"):
                return None
            attested = self.local_attestor(config)
            return attested if isinstance(attested, dict) and self._local_identity(attested) == self._local_identity(config) else None
        except Exception:
            return None

    def local_ready(self) -> bool:
        return self._attested_local_model() is not None

    def preview(self, choice: SetupChoice) -> dict[str, Any]:
        self._validate_choice(choice)
        if isinstance(self.keychain, MacKeychain) and platform.system() != "Darwin":
            raise SetupError("GUIDED_SETUP_MACOS_ONLY")
        current: dict[str, Any] | None = None
        if self.policy_exists():
            try:
                existing = self.load_existing()
            except (AutoError, OSError):
                raise SetupError("EXISTING_POLICY_REVIEW_REQUIRED") from None
            if existing.get("setup_origin") == "local_wizard" and existing.get("setup_state") == "ready":
                if choice.provider != existing.get("provider"):
                    raise SetupError("PROVIDER_CHANGE_REQUIRES_SEPARATE_SETUP")
                current = existing
        self._previewed_existing = current
        if choice.provider == "laya-mlx" or choice.local_laya_enabled:
            model = self._attested_local_model()
            if model is None:
                raise SetupError("LOCAL_MODEL_NOT_ATTESTED")
            self._previewed_local_identity = self._local_identity(model)
        return {
            "status": "REVIEW_REQUIRED",
            "workspace": str(self.workspace),
            "provider": choice.provider,
            "data_classification": choice.data_classification,
            "days": choice.days,
            "daily_calls": choice.daily_calls,
            "daily_bytes": choice.daily_bytes,
            "generic_query_enabled": choice.generic_query_enabled,
            "local_laya_enabled": choice.local_laya_enabled,
            "cloud_transfer": choice.provider != "laya-mlx",
            "provider_key_required": choice.provider != "laya-mlx",
            "native_hook_trust": "USER_REVIEW_REQUIRED",
            "changes_applied": False,
            "upgrade": current is not None,
            "current_data_classification": current.get("data_classification") if current else None,
        }

    def _apply_upgrade(self, choice: SetupChoice, existing: dict[str, Any], credential: str | None) -> dict[str, Any]:
        if self._previewed_existing is None or credential:
            raise SetupError("SETUP_REVIEW_REQUIRED")
        if choice.provider != existing.get("provider") or choice.provider != self._previewed_existing.get("provider"):
            raise SetupError("PROVIDER_CHANGE_REQUIRES_SEPARATE_SETUP")
        if choice.provider != "laya-mlx":
            try:
                self.keychain.get(choice.provider)
            except KeychainError as error:
                raise self._keychain_error(error) from None
        upgraded = {**existing,
                    "expires_at": time.time() + choice.days * 86400,
                    "max_calls_per_day": choice.daily_calls,
                    "max_bytes_per_day": choice.daily_bytes,
                    "data_classification": choice.data_classification,
                    "generic_query_enabled": choice.generic_query_enabled,
                    "auto_prepare_jev": choice.auto_prepare_jev,
                    "local_laya_enabled": choice.local_laya_enabled,
                    "decision_mode": self._resolved_mode(choice),
                    "setup_choice_digest": self._choice_digest(choice)}
        if choice.local_laya_enabled or choice.provider == "laya-mlx":
            model = self._attested_local_model()
            if model is None:
                raise SetupError("LOCAL_MODEL_NOT_ATTESTED")
            if self._previewed_local_identity is not None and self._local_identity(model) != self._previewed_local_identity:
                raise SetupError("LOCAL_MODEL_CHANGED_SINCE_REVIEW")
            upgraded["mlx"] = model
            if choice.local_laya_enabled:
                upgraded["routes"] = {**existing.get("routes", {}), "sieve": "laya-mlx", "probe": "laya-mlx"}
        else:
            upgraded["routes"] = {key: value for key, value in existing.get("routes", {}).items()
                                  if value != "laya-mlx"}
            upgraded.pop("mlx", None)
        try:
            validate_policy(upgraded, self.workspace)
            self.upgrade(self.workspace, self._previewed_existing, upgraded)
        except (AutoError, OSError):
            raise SetupError("SETUP_RECOVERY_CONFLICT") from None
        self._previewed_existing = None
        return {"status": "UPDATED_PENDING_HOST_TRUST", "workspace": str(self.workspace),
                "provider": choice.provider, "native_hook_trust": "USER_REVIEW_REQUIRED",
                "credentials": "KEYCHAIN", "live_provider_test": "NOT_RUN"}

    def _choice_digest(self, choice: SetupChoice) -> str:
        return hashlib.sha256(canonical({"workspace": str(self.workspace), "choice": asdict(choice)})).hexdigest()

    @staticmethod
    def _keychain_error(error: KeychainError) -> SetupError:
        safe_codes = {"KEYCHAIN_WRITE_FAILED", "KEYCHAIN_READ_FAILED", "KEYCHAIN_ITEM_MISSING", "KEYCHAIN_CREDENTIAL_INVALID", "KEYCHAIN_UNAVAILABLE"}
        code = str(error) if str(error) in safe_codes else "KEYCHAIN_OPERATION_FAILED"
        return SetupError(code)

    def _finish(self, policy: dict[str, Any], *, keychain_verified: bool = False) -> dict[str, Any]:
        if policy["provider"] != "laya-mlx" and not keychain_verified:
            try:
                self.keychain.get(policy["provider"])
            except KeychainError as error:
                raise self._keychain_error(error) from None
        try:
            self.bridge(self.workspace, policy)
            self.start(self.workspace)
            self.complete(self.workspace, policy)
        except Exception as error:
            raise SetupError("SETUP_PARTIAL_POLICY_SAVED") from error
        return {
            "status": "ENROLLED_PENDING_HOST_TRUST",
            "workspace": str(self.workspace),
            "provider": policy["provider"],
            "native_hook_trust": "USER_REVIEW_REQUIRED",
            "credentials": "KEYCHAIN" if policy["provider"] != "laya-mlx" else "NOT_REQUIRED",
            "live_provider_test": "NOT_RUN",
        }

    def apply(self, choice: SetupChoice, *, credential: str | None, confirmed: bool,
              replace_existing_credential: bool = False) -> dict[str, Any]:
        if confirmed is not True:
            raise SetupError("USER_CONFIRMATION_REQUIRED")
        self._validate_choice(choice)
        choice_digest = self._choice_digest(choice)
        if self.policy_exists():
            try:
                existing = self.load_existing()
            except (AutoError, OSError):
                raise SetupError("EXISTING_POLICY_REVIEW_REQUIRED") from None
            if existing.get("setup_origin") == "local_wizard" and existing.get("setup_state") == "ready":
                return self._apply_upgrade(choice, existing, credential)
            if existing.get("setup_origin") != "local_wizard" or existing.get("setup_state") != "pending":
                raise SetupError("EXISTING_POLICY_REVIEW_REQUIRED")
            if existing.get("setup_choice_digest") != choice_digest:
                raise SetupError("SETUP_RECOVERY_CONFLICT")
            if credential or replace_existing_credential:
                raise SetupError("SETUP_RECOVERY_KEY_REVIEW_REQUIRED")
            return self._finish(existing)

        local_model: dict[str, Any] | None = None
        if choice.provider == "laya-mlx" or choice.local_laya_enabled:
            if credential and choice.provider == "laya-mlx":
                raise SetupError("LOCAL_ROUTE_DOES_NOT_USE_CLOUD_KEY")
            local_model = self.local_config()
            if not isinstance(local_model, dict) or not local_model.get("repository") or not local_model.get("revision"):
                raise SetupError("LOCAL_MODEL_NOT_PREPARED")
            local_model = self._attested_local_model()
            if local_model is None:
                raise SetupError("LOCAL_MODEL_NOT_ATTESTED")
            if self._previewed_local_identity is not None and self._local_identity(local_model) != self._previewed_local_identity:
                raise SetupError("LOCAL_MODEL_CHANGED_SINCE_REVIEW")

        try:
            policy = make_policy(
                self.workspace,
                choice.provider,
                choice.days,
                max_calls_per_day=choice.daily_calls,
                max_bytes_per_day=choice.daily_bytes,
                data_classification=choice.data_classification,
                generic_query_enabled=choice.generic_query_enabled,
                auto_prepare_jev=choice.auto_prepare_jev,
                local_laya_enabled=choice.local_laya_enabled,
                decision_mode=self._resolved_mode(choice),
                routes={"sieve": "laya-mlx", "probe": "laya-mlx"} if choice.local_laya_enabled else {},
                mlx=local_model if local_model is not None else None,
                credential_store="keychain" if choice.provider != "laya-mlx" else "legacy",
                case_ids=[case["id"] for case in catalog()],
                setup_origin="local_wizard",
                setup_state="pending",
                setup_choice_digest=choice_digest,
            )
        except (AutoError, KeyError, TypeError, ValueError) as error:
            raise SetupError("SETUP_SCOPE_INVALID") from error
        if local_model is None:
            policy.pop("mlx", None)

        keychain_changed = False
        if choice.provider != "laya-mlx":
            try:
                if credential:
                    try:
                        existing_credential = self.keychain.get(choice.provider)
                    except KeychainError as error:
                        if str(error) != "KEYCHAIN_ITEM_MISSING":
                            raise
                        existing_credential = None
                    if existing_credential is None:
                        self.keychain.put(choice.provider, credential)
                        keychain_changed = True
                    elif not hmac.compare_digest(existing_credential, credential):
                        if not replace_existing_credential:
                            raise SetupError("KEYCHAIN_REPLACEMENT_REQUIRES_CONFIRMATION")
                        self.keychain.put(choice.provider, credential)
                        keychain_changed = True
                else:
                    self.keychain.get(choice.provider)
            except KeychainError as error:
                raise self._keychain_error(error) from None

        try:
            self.persist(self.workspace, policy)
        except Exception as error:
            raise SetupError("SETUP_PARTIAL_KEYCHAIN_STORED" if keychain_changed else "SETUP_POLICY_WRITE_FAILED") from error
        return self._finish(policy, keychain_verified=True)
