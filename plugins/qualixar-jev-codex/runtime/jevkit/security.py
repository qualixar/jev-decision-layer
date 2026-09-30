"""Best-effort secret/PII screening. This is not a comprehensive DLP product."""
from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any


class SafeError(Exception):
    """Messages must contain safe codes and descriptions, never raw input."""


# Contact, workspace-path, phone and private-network patterns. Credential
# formats come from jev_auto.secret_rules, shared by every outgoing screen.
RULES = [
    ("EMAIL", re.compile(r"\b[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,24}\b")),
    (
        "PRIVATE_URL",
        re.compile(r'https?://[^\s"<>]{0,2048}?(?:\.internal|\.local|localhost|127(?:\.\d{1,3}){3}|0\.0\.0\.0|\[::1\])[^\s"<>]{0,2048}', re.I),
    ),
    (
        "PRIVATE_IP",
        re.compile(
            r"\b(?:10(?:\.\d{1,3}){3}|127(?:\.\d{1,3}){3}|0\.0\.0\.0|192\.168(?:\.\d{1,3}){2}|"
            r"172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2})\b"
        ),
    ),
    (
        "HOME_PATH",
        re.compile(r'(?i)(?:(?:/Users/|/home/)|(?:[A-Z]:[\\/]|\\\\[^\\\s]+\\[^\\\s]+[\\/])Users[\\/])[^\s"<>]+'),
    ),
    ("PHONE", re.compile(r"(?<!\w)\+\d[\d ()-]{8,}\d(?!\w)")),
]


_CONTEXT_FIELDS = frozenset({"EMAIL", "HOME_PATH"})


def screen(value: Any, secrets: tuple[str, ...] = (), *, allow_context: bool = False) -> tuple[Any, list[str]]:
    """Screen secrets, with a scoped allowance for reviewed contact/path context.

    Callers may set ``allow_context`` only after checking an enrolled
    non-public data scope. This does not relax credentials or private network
    endpoint checks and is not comprehensive DLP. A string holding a
    credential is replaced whole, so no part of it survives redaction.
    """
    from jev_auto.common import _child_place
    from jev_auto.secret_rules import credential_field, find, variants

    found: set[str] = set()

    def visit(item, where="root"):
        if isinstance(item, dict):
            result = {}
            for key, nested in item.items():
                clean_key = visit(str(key), "other")
                # The option labels of a choice question (questions.<name>.criteria)
                # are categories, not credentials.
                if where != "labels" and credential_field(str(key), nested):
                    found.add("CREDENTIAL")
                    result[clean_key] = "[REDACTED:CREDENTIAL]"
                else:
                    result[clean_key] = visit(nested, _child_place(where, key))
            return result
        if isinstance(item, list):
            return [visit(nested) for nested in item]
        if not isinstance(item, str):
            return item
        forms = variants(item)
        if any(secret and len(secret) >= 4 and secret in form for secret in secrets for form in forms):
            found.add("API_KEY")
            return "[REDACTED:API_KEY]"
        labels = find(item)
        if labels:
            found.update(labels)
            return "[REDACTED:" + ",".join(sorted(labels)) + "]"
        for label, regex in RULES:
            if allow_context and label in _CONTEXT_FIELDS:
                continue
            if any(regex.search(form) for form in forms):
                found.add(label)
                item = regex.sub("[REDACTED:" + label + "]", item)
        return item

    return visit(value), sorted(found)


def canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode()
    except (TypeError, ValueError, RecursionError):
        raise SafeError("INVALID_JSON: finite JSON values required") from None


def load_json(path: Path, max_bytes: int = 1_000_000) -> Any:
    if path.is_symlink() or not path.is_file():
        raise SafeError("UNSAFE_FILE: require an ordinary file, not a symlink")
    if path.stat().st_size > max_bytes:
        raise SafeError("FILE_TOO_LARGE")
    try:
        def no_constant(_value):
            raise ValueError()

        return json.loads(path.read_text(), parse_constant=no_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise SafeError("INVALID_JSON_FILE") from None


def validate_private_path(path: Path) -> None:
    """Reject symlinks before resolving or creating a private path."""
    original = path.absolute()
    for component in (original, *original.parents):
        if component.is_symlink():
            if component == Path("/var") and component.resolve() == Path("/private/var"):
                continue
            raise SafeError("UNSAFE_PATH: symlink in private directory")


def _windows_private_storage() -> bool:
    return os.name == "nt"


def private_dir(path: Path) -> None:
    if _windows_private_storage():
        # POSIX chmod is not a Windows access control check. Use the same
        # handle-verified, protected-DACL directory backend as jev_auto.
        from jev_auto.common import AutoError
        from jev_auto.platform_fs import ensure_private_dir

        try:
            ensure_private_dir(path)
        except AutoError as exc:
            raise SafeError(str(exc)) from None
        return
    validate_private_path(path)
    original = path.absolute()
    original.mkdir(parents=True, exist_ok=True, mode=0o700)
    original.chmod(0o700)


def private_json(path: Path, value: Any, *, no_clobber: bool = False) -> None:
    if _windows_private_storage():
        from jev_auto.common import AutoError
        from jev_auto.platform_fs import atomic_write_private

        try:
            atomic_write_private(path, canonical(value) + b"\n", replace=not no_clobber)
        except AutoError as exc:
            if no_clobber and str(exc) == "WORKSPACE_ALREADY_ENROLLED":
                raise FileExistsError(path) from None
            raise SafeError(str(exc)) from None
        return
    private_dir(path.parent)
    if path.is_symlink():
        raise SafeError("UNSAFE_PATH: refusing symlink")
    if no_clobber and path.exists():
        raise FileExistsError(path)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=path.name + ".tmp-", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(canonical(value) + b"\n")
        if no_clobber:
            # link(2) atomically refuses an existing destination.
            os.link(temporary, path)
            temporary.unlink()
        else:
            os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary.exists():
            temporary.unlink()
