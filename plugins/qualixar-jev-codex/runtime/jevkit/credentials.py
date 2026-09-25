"""Backward-compatible direct-TypeSafe credential helpers."""
from __future__ import annotations

from pathlib import Path

from .providers import (
    credential_path as _credential_path,
    get_provider_credential,
    provider_profile,
    validate_key,
)


def credential_path() -> Path:
    """Return the new TypeSafe path; reads still accept the legacy path."""
    return _credential_path(provider_profile("typesafe"))


def get_credential() -> str:
    return get_provider_credential(provider_profile("typesafe"))
