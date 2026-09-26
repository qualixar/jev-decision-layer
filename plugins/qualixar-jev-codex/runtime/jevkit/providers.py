"""Fixed Jev provider profiles and private local credential selection."""
from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from .security import SafeError, canonical, private_dir


@dataclass(frozen=True)
class ProviderProfile:
    provider_id: str
    display_name: str
    endpoint: str
    model: str
    env_var: str
    key_filename: str

    @property
    def profile_sha256(self) -> str:
        return hashlib.sha256(canonical(asdict(self))).hexdigest()


_PROFILES = {
    "typesafe": ProviderProfile(
        provider_id="typesafe",
        display_name="TypeSafe direct",
        endpoint="https://api.typesafe.ai/v1/systemone",
        model="jev-1.13.0",
        env_var="TYPESAFE_API_KEY",
        key_filename="typesafe-api-key",
    ),
    "openrouter": ProviderProfile(
        provider_id="openrouter",
        display_name="OpenRouter Decisions",
        endpoint="https://openrouter.ai/api/alpha/decisions",
        model="typesafe/jev-1.13",
        env_var="OPENROUTER_API_KEY",
        key_filename="openrouter-api-key",
    ),
}
_PRIVATE_ENV_KEYS = frozenset(
    {"JEV_PROVIDER", "OPENROUTER_API_KEY", "TYPESAFE_API_KEY"}
)


def default_config_root() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    if base:
        return Path(base).expanduser() / "qualixar-jev-decision-layer"
    return Path.home() / ".config" / "qualixar-jev-decision-layer"


def provider_profile(provider_id: str) -> ProviderProfile:
    try:
        return _PROFILES[provider_id]
    except KeyError:
        raise SafeError("INVALID_PROVIDER: choose typesafe or openrouter") from None


def _safe_regular_file(path: Path, error: str) -> None:
    for component in (path, *path.parents):
        if component.is_symlink():
            # macOS ships /var as a symlink to /private/var, so every path
            # under the system temporary directory -- and any config root a
            # user points there -- has a symlinked ancestor by construction.
            # Without this carve-out the credential store WROTE a key file and
            # then refused to read back its own file, reporting
            # UNSAFE_CREDENTIAL_PERMISSIONS for what was never a permissions
            # problem. jevkit/security.py::validate_private_path and
            # jev_auto/common.py::safe_path already carve out the same case;
            # this guard was the one that did not.
            if component == Path("/var") and component.resolve() == Path("/private/var"):
                continue
            raise SafeError(error)
    metadata = path.stat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_mode & 0o077
        or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
    ):
        raise SafeError(error)


def _private_env(config_root: Path) -> dict[str, str]:
    path = config_root / ".env"
    if not path.exists():
        return {}
    root_metadata = config_root.stat()
    if (
        not stat.S_ISDIR(root_metadata.st_mode)
        or root_metadata.st_mode & 0o077
        or (hasattr(os, "getuid") and root_metadata.st_uid != os.getuid())
    ):
        raise SafeError("UNSAFE_PROVIDER_ENV_DIRECTORY")
    _safe_regular_file(path, "UNSAFE_PROVIDER_ENV")
    if path.stat().st_size > 12_000:
        raise SafeError("INVALID_PROVIDER_ENV")
    values: dict[str, str] = {}
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            raise SafeError("INVALID_PROVIDER_ENV")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key not in _PRIVATE_ENV_KEYS:
            raise SafeError("INVALID_PROVIDER_ENV_KEY")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def resolve_provider(
    provider_id: str | None = None, *, config_root: Path | None = None
) -> ProviderProfile:
    root = (config_root or default_config_root()).expanduser()
    private_values = _private_env(root)
    selected = provider_id or os.environ.get("JEV_PROVIDER", "").strip().lower()
    selection_path = root / "provider"
    if not selected and selection_path.exists():
        _safe_regular_file(selection_path, "UNSAFE_PROVIDER_CONFIG")
        if selection_path.stat().st_size > 64:
            raise SafeError("INVALID_PROVIDER_CONFIG")
        selected = selection_path.read_text().strip().lower()
    if not selected:
        selected = private_values.get("JEV_PROVIDER", "").strip().lower()
    if not selected:
        available = [
            candidate.provider_id
            for candidate in _PROFILES.values()
            if (
                os.environ.get(candidate.env_var, "").strip()
                or private_values.get(candidate.env_var, "").strip()
            )
        ]
        if len(available) == 1:
            selected = available[0]
        elif not available:
            selected = "typesafe"
        else:
            raise SafeError("PROVIDER_SELECTION_REQUIRED: configure typesafe or openrouter")
    return provider_profile(selected)


def validate_key(key: str) -> str:
    if not 8 <= len(key) <= 4096 or not key.isascii() or any(c.isspace() for c in key):
        raise SafeError("INVALID_CREDENTIAL: malformed local key")
    return key


def credential_path(
    provider: ProviderProfile | str, *, config_root: Path | None = None
) -> Path:
    profile = provider_profile(provider) if isinstance(provider, str) else provider
    return (config_root or default_config_root()).expanduser() / profile.key_filename


def get_provider_credential(
    provider: ProviderProfile | str, *, config_root: Path | None = None, credential_store: str = "legacy"
) -> str:
    profile = provider_profile(provider) if isinstance(provider, str) else provider
    if credential_store == "keychain":
        from src.adl.api.keychain import KeychainError, MacKeychain

        try:
            return validate_key(MacKeychain().get(profile.provider_id))
        except KeychainError:
            raise SafeError("KEYCHAIN_CREDENTIAL_UNAVAILABLE") from None
    if credential_store != "legacy":
        raise SafeError("INVALID_CREDENTIAL_STORE")
    value = os.environ.get(profile.env_var, "").strip()
    if not value:
        root = (config_root or default_config_root()).expanduser()
        value = _private_env(root).get(profile.env_var, "").strip()
    if value:
        return validate_key(value)
    path = credential_path(profile, config_root=config_root)
    if not path.exists():
        raise SafeError(
            f"NO_CREDENTIAL: configure {profile.env_var} using the private terminal helper"
        )
    _safe_regular_file(path, "UNSAFE_CREDENTIAL_PERMISSIONS")
    if path.stat().st_size > 4096:
        raise SafeError("INVALID_CREDENTIAL")
    return validate_key(path.read_text().strip())


def store_provider_credential(
    provider_id: str, key: str, *, config_root: Path | None = None
) -> ProviderProfile:
    profile = provider_profile(provider_id)
    value = validate_key(key)
    root = (config_root or default_config_root()).expanduser()
    private_dir(root)
    if root.is_symlink():
        raise SafeError("UNSAFE_CREDENTIAL_PATH")
    key_path = credential_path(profile, config_root=root)
    selection_path = root / "provider"
    for path, content in ((key_path, value + "\n"), (selection_path, profile.provider_id + "\n")):
        if path.is_symlink():
            raise SafeError("UNSAFE_CREDENTIAL_PATH")
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=path.name + ".new-", dir=root
        )
        temporary = Path(temporary_name)
        try:
            os.chmod(temporary, 0o600)
            with os.fdopen(descriptor, "w") as stream:
                descriptor = -1
                stream.write(content)
            os.replace(temporary, path)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary.exists():
                temporary.unlink()
        path.chmod(0o600)
    return profile


def credential_available(
    provider: ProviderProfile | str, *, config_root: Path | None = None
) -> bool:
    try:
        get_provider_credential(provider, config_root=config_root)
        return True
    except SafeError:
        return False
