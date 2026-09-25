"""Read-only verification of an already installed local Laya artifact.

This proves installed file identity, not sandbox isolation, model quality, or
approval of another model release.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import selectors
import signal
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from jev_auto.common import AutoError, home_root, read_private, safe_path


_SHA256 = re.compile(r"[a-f0-9]{64}\Z")
_REVISION = re.compile(r"[a-f0-9]{40}\Z")
_CONFIG_KEYS = frozenset({"artifact_manifest", "model_dir", "python", "repository", "revision", "runtime_commit", "weight_sha256"})
_MAX_ARTIFACT_BYTES = 4_000_000_000
_MAX_TOTAL_BYTES = 2_000_000_000


@dataclass(frozen=True)
class ApprovedPin:
    repository: str
    revision: str
    weight_sha256: str
    runtime_commit: str


# An approved published English artifact/runtime tuple; model weights are installed separately.
# A different upstream revision is not silently substituted for this installed artifact.
PRODUCTION_PINS = (
    ApprovedPin(
        "aac6fef/laya-mlx",
        "047678560251f28113ee8f5df4be82102c7bf336",
        "b9c07bf14be2fa5c78a9193a3e6d840ac80e89e62fc40f425834c3d8a6eaa3de",
        "0a859518634112655cb97c745dbf04f5191aaf13",
    ),
    ApprovedPin(
        "aac6fef/laya-multilingual-mlx",
        "ba40c87fcb357f1643d04d71323af9cdc3b9e591",
        "7fc5834af4d8fdfb268d272a9d1a66e5819a0daac98241651c4c888cc43adff1",
        "0a859518634112655cb97c745dbf04f5191aaf13",
    ),
)


class AttestationError(ValueError):
    """Fixed code only; never includes local paths or model contents."""


def _supports_secure_file_open() -> bool:
    return os.name == "posix" and all(hasattr(os, name) for name in ("O_DIRECTORY", "O_NOFOLLOW"))


def _runtime_commit_from_python(python: Path) -> str:
    code = "import importlib.metadata as m; print(m.distribution('laya-mlx').read_text('direct_url.json'))"
    process = None
    selector = None
    try:
        process = subprocess.Popen(
            [str(python), "-I", "-c", code],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
            env={"PATH": os.defpath, "PYTHONDONTWRITEBYTECODE": "1", "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"},
        )
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        deadline = time.monotonic() + 5
        output = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                raise AttestationError("LOCAL_RUNTIME_NOT_APPROVED")
            chunk = os.read(process.stdout.fileno(), min(1024, 4097 - len(output)))
            if not chunk:
                break
            output.extend(chunk)
            if len(output) > 4096:
                raise AttestationError("LOCAL_RUNTIME_NOT_APPROVED")
        if process.wait(timeout=max(0.1, deadline - time.monotonic())) != 0:
            raise AttestationError("LOCAL_RUNTIME_NOT_APPROVED")
        metadata = json.loads(output.decode("utf-8"))
        if not isinstance(metadata, dict) or not isinstance(metadata.get("vcs_info"), dict):
            raise AttestationError("LOCAL_RUNTIME_NOT_APPROVED")
        commit = metadata["vcs_info"].get("commit_id")
        if not isinstance(commit, str) or _REVISION.fullmatch(commit) is None:
            raise AttestationError("LOCAL_RUNTIME_NOT_APPROVED")
        return commit
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, UnicodeError):
        raise AttestationError("LOCAL_RUNTIME_NOT_APPROVED") from None
    finally:
        if selector is not None:
            selector.close()
        if process is not None:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.wait(timeout=1)
            if process.stdout is not None:
                process.stdout.close()


def _hash_artifact(root: Path, name: str, remaining_bytes: int) -> tuple[str, int]:
    path = PurePosixPath(name)
    if (
        not isinstance(name, str)
        or not name
        or path.is_absolute()
        or "\\" in name
        or "//" in name
        or any(part in ("", ".", "..") for part in name.split("/"))
    ):
        raise AttestationError("LOCAL_ARTIFACT_MISMATCH")
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
    try:
        for component in path.parts[:-1]:
            following = os.open(component, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory)
            os.close(directory)
            directory = following
        descriptor = os.open(path.parts[-1], os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or not 0 < metadata.st_size <= _MAX_ARTIFACT_BYTES:
                raise AttestationError("LOCAL_ARTIFACT_MISMATCH")
            if metadata.st_size > remaining_bytes:
                raise AttestationError("LOCAL_ARTIFACT_TOO_LARGE")
            digest = hashlib.sha256()
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
            return digest.hexdigest(), metadata.st_size
        finally:
            os.close(descriptor)
    finally:
        os.close(directory)


def attest_local_config(
    config: dict[str, Any],
    *,
    approved_pins: tuple[ApprovedPin, ...] = PRODUCTION_PINS,
    expected_python: Path | None = None,
    runtime_probe: Callable[[Path], str] = _runtime_commit_from_python,
) -> dict[str, Any]:
    """Verify a private installation record and every manifest-listed file."""
    if not isinstance(config, dict) or set(config) - _CONFIG_KEYS:
        raise AttestationError("LOCAL_ARTIFACT_MISMATCH")
    if not _supports_secure_file_open():
        raise AttestationError("LOCAL_PLATFORM_UNVERIFIED")
    repository = config.get("repository")
    revision = config.get("revision")
    weight_hash = config.get("weight_sha256")
    if (
        not isinstance(repository, str)
        or not isinstance(revision, str) or _REVISION.fullmatch(revision) is None
        or not isinstance(weight_hash, str) or _SHA256.fullmatch(weight_hash) is None
        or not isinstance(config.get("model_dir"), str)
        or not isinstance(config.get("artifact_manifest"), str)
    ):
        raise AttestationError("LOCAL_ARTIFACT_MISMATCH")
    identity = ApprovedPin(repository, revision, weight_hash, config.get("runtime_commit"))
    if identity not in approved_pins:
        raise AttestationError("LOCAL_PIN_NOT_APPROVED")
    if expected_python is None:
        current_python = home_root() / "mlx-env" / "bin" / "python"
        legacy_python = Path.home() / ".local" / "state" / "qualixar-jev-auto" / "mlx-env" / "bin" / "python"
        expected_python = legacy_python if config.get("python") == str(legacy_python) else current_python
    python_path = config.get("python")
    try:
        trusted_python = safe_path(expected_python)
    except (AutoError, OSError):
        raise AttestationError("LOCAL_RUNTIME_NOT_APPROVED") from None
    if not isinstance(python_path, str) or python_path != str(expected_python) or not trusted_python.is_file() or not os.access(trusted_python, os.X_OK):
        raise AttestationError("LOCAL_RUNTIME_NOT_APPROVED")
    try:
        runtime_commit = runtime_probe(trusted_python)
    except Exception:
        raise AttestationError("LOCAL_RUNTIME_NOT_APPROVED") from None
    if runtime_commit != identity.runtime_commit:
        raise AttestationError("LOCAL_RUNTIME_NOT_APPROVED")
    model_dir = Path(config["model_dir"])
    manifest_path = Path(config["artifact_manifest"])
    if not model_dir.is_absolute() or not manifest_path.is_absolute():
        raise AttestationError("LOCAL_ARTIFACT_MISMATCH")
    try:
        model_dir = safe_path(model_dir)
        manifest = read_private(manifest_path, 100_000)
        if not model_dir.is_dir() or not isinstance(manifest, dict):
            raise AttestationError("LOCAL_ARTIFACT_MISMATCH")
        files = manifest.get("files")
        if (
            manifest.get("schema_version") != 1
            or manifest.get("repository") != repository
            or manifest.get("revision") != revision
            or not isinstance(files, dict)
            or not 1 <= len(files) <= 64
            or files.get("model.safetensors") != weight_hash
        ):
            raise AttestationError("LOCAL_ARTIFACT_MISMATCH")
        remaining_bytes = _MAX_TOTAL_BYTES
        for name, expected in files.items():
            if not isinstance(expected, str) or _SHA256.fullmatch(expected) is None:
                raise AttestationError("LOCAL_ARTIFACT_MISMATCH")
            actual, size = _hash_artifact(model_dir, name, remaining_bytes)
            remaining_bytes -= size
            if actual != expected:
                raise AttestationError("LOCAL_ARTIFACT_MISMATCH")
    except AttestationError:
        raise
    except (AutoError, OSError, TypeError, ValueError, AttributeError):
        raise AttestationError("LOCAL_ARTIFACT_MISMATCH") from None
    return dict(config)
