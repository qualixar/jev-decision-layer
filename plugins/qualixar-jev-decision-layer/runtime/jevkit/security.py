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


RULES = [
    (
        "PRIVATE_KEY",
        re.compile(
            r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----[\s\S]*?"
            r"-----END [^-]*PRIVATE KEY-----"
        ),
    ),
    ("BEARER_TOKEN", re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}")),
    (
        "API_KEY",
        re.compile(
            r"\b(?:sk-(?:proj-|ant-)?[A-Za-z0-9_-]{16,}|"
            r"gh[pousr]_[A-Za-z0-9_]{20,}|AKIA[A-Z0-9]{16})\b"
        ),
    ),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
    ("EMAIL", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    (
        "PRIVATE_URL",
        re.compile(r'https?://[^\s"<>]*(?:\.internal|\.local|localhost|127(?:\.\d{1,3}){3}|0\.0\.0\.0|\[::1\])[^\s"<>]*', re.I),
    ),
    (
        "PRIVATE_IP",
        re.compile(
            r"\b(?:10(?:\.\d{1,3}){3}|127(?:\.\d{1,3}){3}|0\.0\.0\.0|192\.168(?:\.\d{1,3}){2}|"
            r"172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2})\b"
        ),
    ),
    ("HOME_PATH", re.compile(r'(?:/Users/|/home/)[^\s"<>]+')),
    ("PHONE", re.compile(r"(?<!\w)\+\d[\d ()-]{8,}\d(?!\w)")),
    (
        "CREDENTIAL_ASSIGNMENT",
        re.compile(
            r"(?i)\b(?:[a-z0-9]+[_-])*(?:api[_-]?key|password|secret|"
            r"access[_-]?token)\s*[:=]\s*[\"']?[^\s,;\"']{6,}"
        ),
    ),
    ("CREDENTIAL_URL", re.compile(r"\b\w+://[^/\s:@]+:[^/\s@]+@[^\s]+")),
]

SENSITIVE_KEYS = re.compile(
    r"^(?:[a-z0-9]+[_-])*(?:api[_-]?key|password|secret|access[_-]?token|"
    r"authorization|credential|private[_-]?key)$",
    re.I,
)


def screen(value: Any, secrets: tuple[str, ...] = ()) -> tuple[Any, list[str]]:
    found: set[str] = set()

    def visit(item):
        if isinstance(item, dict):
            result = {}
            for key, nested in item.items():
                clean_key = visit(str(key))
                if SENSITIVE_KEYS.match(str(key)) and nested:
                    found.add("CREDENTIAL")
                    result[clean_key] = "[REDACTED:CREDENTIAL]"
                else:
                    result[clean_key] = visit(nested)
            return result
        if isinstance(item, list):
            return [visit(nested) for nested in item]
        if not isinstance(item, str):
            return item
        for secret in secrets:
            if secret and len(secret) >= 4 and secret in item:
                found.add("API_KEY")
                item = item.replace(secret, "[REDACTED:API_KEY]")
        for label, regex in RULES:
            if regex.search(item):
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


def private_dir(path: Path) -> None:
    validate_private_path(path)
    original = path.absolute()
    original.mkdir(parents=True, exist_ok=True, mode=0o700)
    original.chmod(0o700)


def private_json(path: Path, value: Any, *, no_clobber: bool = False) -> None:
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
