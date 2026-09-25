"""Bounded native process worker for already-provisioned Laya artifacts."""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import selectors
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jev_auto.common import AutoError, home_root, read_private, safe_path


class WorkerError(RuntimeError):
    pass


class WorkerBusy(WorkerError):
    pass


def _approved_interpreter(path: Path) -> bool:
    supplied = Path(os.path.abspath(path))
    installed = home_root() / "mlx-env" / "bin" / "python"
    legacy_installed = Path.home() / ".local" / "state" / "qualixar-jev-auto" / "mlx-env" / "bin" / "python"
    if supplied in (installed, legacy_installed):
        try:
            metadata = supplied.lstat()
            safe_path(supplied)
        except (AutoError, OSError):
            return False
        return stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1
    current = Path(os.path.abspath(sys.executable))
    base = Path(getattr(sys, "_base_executable", sys.executable)).resolve()
    return supplied == current and supplied.resolve() == base


@dataclass(frozen=True)
class PreparedArtifact:
    profile_id: str
    checkpoint_revision: str
    checkpoint_sha256: str
    model_path: Path


def _sha256(path: Path) -> str:
    try:
        path = safe_path(path)
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except (AutoError, OSError) as error:
        raise WorkerError("ARTIFACT_NOT_READY") from error
    digest = hashlib.sha256()
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise WorkerError("ARTIFACT_NOT_READY")
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        after = os.fstat(descriptor)
        name_after = path.lstat()
        def identity(item):
            return (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns, item.st_ctime_ns)
        if identity(before) != identity(after) or identity(after) != identity(name_after):
            raise WorkerError("ARTIFACT_CHANGED_DURING_HASH")
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def _verify_artifact(artifact: PreparedArtifact) -> None:
    if (
        not isinstance(artifact, PreparedArtifact)
        or not artifact.profile_id
        or re.fullmatch(r"[a-f0-9]{40}", artifact.checkpoint_revision) is None
        or re.fullmatch(r"[a-f0-9]{64}", artifact.checkpoint_sha256) is None
    ):
        raise WorkerError("ARTIFACT_NOT_READY")
    path = Path(artifact.model_path)
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_mode & 0o022:
            raise WorkerError("ARTIFACT_NOT_READY")
        digest = _sha256(path)
        observed = path.lstat()
    except (OSError, AutoError) as error:
        raise WorkerError("ARTIFACT_NOT_READY") from error
    if (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns) != (
        observed.st_dev, observed.st_ino, observed.st_size, observed.st_mtime_ns, observed.st_ctime_ns
    ) or digest != artifact.checkpoint_sha256:
        raise WorkerError("ARTIFACT_NOT_READY")


class PersistentLayaWorker:
    """Retired in-process interface; never run model code in the Codex host."""

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        raise WorkerError("IN_PROCESS_WORKER_DISABLED")


class SandboxedLayaProcess:
    """Production process boundary for the existing local Laya protocol.

    This does not enroll a workspace or authorize inference. The broker must
    perform those checks before constructing this process. On unsupported OSes,
    inference fails closed rather than treating a Python guard as isolation.
    """

    SANDBOX_PROFILE = "(version 1) (deny default) (deny network*)"
    SANDBOX_BINARY = Path("/usr/bin/sandbox-exec")

    def __init__(self, artifact: PreparedArtifact, config: dict[str, Any], python: Path, *, max_pending: int = 16):
        if not isinstance(max_pending, int) or isinstance(max_pending, bool) or not 1 <= max_pending <= 256:
            raise ValueError("INVALID_QUEUE_BOUND")
        _verify_artifact(artifact)
        if not isinstance(config, dict):
            raise WorkerError("ARTIFACT_CONFIG_MISMATCH")
        try:
            model_dir = Path(config["model_dir"]).resolve(strict=True)
            configured_hash = config["weight_sha256"]
        except (KeyError, OSError, TypeError, ValueError) as error:
            raise WorkerError("ARTIFACT_CONFIG_MISMATCH") from error
        if model_dir != Path(artifact.model_path).resolve().parent or configured_hash != artifact.checkpoint_sha256:
            raise WorkerError("ARTIFACT_CONFIG_MISMATCH")
        try:
            manifest_path = Path(config["artifact_manifest"])
            metadata = manifest_path.lstat()
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077 or metadata.st_size > 100_000:
                raise WorkerError("ARTIFACT_CONFIG_MISMATCH")
            manifest_digest = _sha256(manifest_path)
            manifest = read_private(manifest_path, 100_000)
            if _sha256(manifest_path) != manifest_digest or manifest_path.lstat().st_ino != metadata.st_ino:
                raise WorkerError("ARTIFACT_CONFIG_MISMATCH")
        except (KeyError, OSError, TypeError, ValueError, AutoError) as error:
            raise WorkerError("ARTIFACT_CONFIG_MISMATCH") from error
        if (
            not isinstance(manifest, dict)
            or manifest.get("schema_version") != 1
            or manifest.get("revision") != artifact.checkpoint_revision
            or config.get("revision") != artifact.checkpoint_revision
            or manifest.get("repository") != config.get("repository")
            or not isinstance(manifest.get("files"), dict)
            or manifest["files"].get("model.safetensors") != artifact.checkpoint_sha256
        ):
            raise WorkerError("ARTIFACT_CONFIG_MISMATCH")
        self.require_native_sandbox()
        self.artifact = artifact
        self._manifest_digest = manifest_digest
        self.config = {**config, "manifest_sha256": manifest_digest}
        self.python = Path(python)
        if not _approved_interpreter(self.python):
            raise WorkerError("MLX_PYTHON_UNAPPROVED")
        if not self.python.is_file():
            raise WorkerError("MLX_PYTHON_MISSING")
        self._process: subprocess.Popen[bytes] | None = None
        self._closing = threading.Event()
        self._buffer = b""
        self._lock = threading.Lock()
        self._admission = threading.BoundedSemaphore(max_pending)
        self._cold_load_ms: float | None = None
        self._calls = 0
        self._runtime_default_device = "unknown"
        self._device_source = "not_reported"
        self._temp = tempfile.TemporaryDirectory(prefix="qualixar-laya-")
        self._temp_root = Path(self._temp.name)

    @classmethod
    def require_native_sandbox(cls) -> None:
        if platform.system() != "Darwin" or not cls.SANDBOX_BINARY.is_file():
            raise WorkerError("OFFLINE_SANDBOX_UNAVAILABLE")

    @classmethod
    def command(
        cls, python: Path, *, model_dir: Path | None = None,
        artifact_manifest: Path | None = None, repository_root: Path | None = None,
        temp_root: Path | None = None,
    ) -> list[str]:
        profile = cls.sandbox_profile(python=python, model_dir=model_dir,
                                      artifact_manifest=artifact_manifest,
                                      repository_root=repository_root, temp_root=temp_root)
        return [str(cls.SANDBOX_BINARY), "-p", profile, str(python), "-u", "-m", "jev_auto.mlx_worker"]

    @classmethod
    def sandbox_profile(
        cls, *, python: Path | None = None, model_dir: Path | None = None,
        artifact_manifest: Path | None = None, repository_root: Path | None = None,
        temp_root: Path | None = None, additional_denied_roots: tuple[Path, ...] = (),
    ) -> str:
        # Only system/runtime files, verified model artifacts and one private
        # scratch directory are visible. No workspace/home blanket allowance.
        read_roots = [Path("/System"), Path("/usr"), Path("/Library/Frameworks"),
                      Path("/opt/homebrew"), Path("/private/etc"), Path("/dev/fd")]
        read_literals = [Path("/"), Path("/dev/null"), Path("/dev/zero"),
                         Path("/dev/random"), Path("/dev/urandom")]
        execution_roots: list[Path] = []
        if python is not None:
            interpreter = Path(python)
            if not _approved_interpreter(interpreter):
                raise WorkerError("PYTHON_RUNTIME_UNTRUSTED")
            read_roots.extend((interpreter.parent.parent, interpreter.resolve().parent.parent))
            read_literals.append(interpreter)
            execution_roots.append(interpreter.resolve().parent.parent)
            venv_config = interpreter.parent.parent / "pyvenv.cfg"
            if venv_config.is_file() and venv_config.stat().st_size <= 8192:
                for line in venv_config.read_text(encoding="utf-8").splitlines():
                    key, separator, value = line.partition("=")
                    if separator and key.strip() == "executable":
                        base = Path(value.strip()).resolve(strict=True)
                        system_roots = (Path("/opt/homebrew"), Path("/usr/local"),
                                        Path("/System"), Path("/Library/Frameworks"))
                        if not base.is_file() or not any(base.is_relative_to(root) for root in system_roots):
                            raise WorkerError("PYTHON_RUNTIME_UNTRUSTED")
                        read_roots.append(base.parent.parent)
                        execution_roots.append(base.parent.parent)
                        break
        if model_dir is not None:
            read_roots.append(Path(model_dir).resolve())
        if artifact_manifest is not None:
            manifest = Path(artifact_manifest).resolve()
            read_literals.extend((manifest.parent, manifest))
        if repository_root is not None:
            root = Path(repository_root).resolve()
            read_literals.append(root)
            read_roots.append(root / "jev_auto")
        if temp_root is not None:
            read_roots.append(Path(temp_root).resolve())
        if any(path == Path("/") for path in (*read_roots, *execution_roots)):
            raise WorkerError("PYTHON_RUNTIME_UNTRUSTED")
        for allowed in (*read_roots, *read_literals):
            read_literals.extend(parent for parent in allowed.parents if parent not in read_literals)
        read_rules = " ".join(f'(subpath {json.dumps(str(path))})' for path in dict.fromkeys(read_roots))
        literal_rules = " ".join(f'(literal {json.dumps(str(path))})' for path in dict.fromkeys(read_literals))
        rules = [cls.SANDBOX_PROFILE, "(allow process-info*)", "(allow sysctl-read)",
                 "(allow mach-lookup)", "(allow iokit-open)",
                 f"(allow file-read* {read_rules} {literal_rules})",
                 '(allow file-write-data (literal "/dev/null") (literal "/dev/zero"))']
        if python is not None:
            interpreter = Path(python)
            rules.append('(allow process-exec '
                         f'(literal {json.dumps(str(interpreter))}) '
                         + " ".join(f'(subpath {json.dumps(str(root))})' for root in dict.fromkeys(execution_roots)) + ')')
        if temp_root is not None:
            rules.append(f'(allow file-write* (subpath {json.dumps(str(Path(temp_root).resolve()))}))')
        rules.extend(
            f'(deny file-read* file-write* (subpath {json.dumps(str(root.resolve()))}))'
            for root in additional_denied_roots
        )
        return " ".join(rules)

    @staticmethod
    def child_environment(repository_root: Path, temp_root: Path | None = None) -> dict[str, str]:
        environment = {
            "PATH": os.defpath,
            "PYTHONPATH": str(repository_root.resolve()),
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "HF_HUB_DISABLE_TELEMETRY": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        if temp_root is not None:
            environment.update({"HOME": str(temp_root), "TMPDIR": str(temp_root), "XDG_CACHE_HOME": str(temp_root)})
        if os.environ.get("LANG"):
            environment["LANG"] = os.environ["LANG"]
        return environment

    @staticmethod
    def kill_process_group(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=3)

    def _spawn(self) -> subprocess.Popen[bytes]:
        repository_root = Path(__file__).resolve().parents[3]
        try:
            return subprocess.Popen(
                self.command(self.python, model_dir=Path(self.config["model_dir"]),
                             artifact_manifest=Path(self.config["artifact_manifest"]),
                             repository_root=repository_root, temp_root=self._temp_root),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=self.child_environment(repository_root, self._temp_root),
                start_new_session=True,
                bufsize=0,
            )
        except OSError as error:
            raise WorkerError("OFFLINE_SANDBOX_LAUNCH_FAILED") from error

    def _stop_locked(self) -> None:
        process = self._process
        self._process = None
        self._buffer = b""
        if process is None:
            return
        self.kill_process_group(process)
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
        if process.stdout is not None:
            process.stdout.close()

    def close(self) -> None:
        # An active inference can hold _lock while waiting on the child. Kill
        # the child first so close() is also an active cancellation operation.
        self._closing.set()
        process = self._process
        if process is not None:
            self.kill_process_group(process)
        with self._lock:
            self._stop_locked()
            self._temp.cleanup()

    def __enter__(self) -> SandboxedLayaProcess:
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()

    def _exchange(self, payload: dict[str, Any], deadline: float) -> dict[str, Any]:
        process = self._process
        if process is None or process.poll() is not None or process.stdin is None or process.stdout is None:
            raise WorkerError("MLX_WORKER_STOPPED")
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        if len(encoded) > 150_000:
            raise WorkerError("MLX_REQUEST_SIZE")
        pending = memoryview(encoded + b"\n")
        os.set_blocking(process.stdin.fileno(), False)
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdin, selectors.EVENT_WRITE)
            while pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise WorkerError("WORKER_DEADLINE_EXCEEDED")
                try:
                    count = os.write(process.stdin.fileno(), pending)
                except BlockingIOError:
                    continue
                except OSError as error:
                    raise WorkerError("MLX_WORKER_STOPPED") from error
                pending = pending[count:]
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while b"\n" not in self._buffer:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise WorkerError("WORKER_DEADLINE_EXCEEDED")
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    raise WorkerError("MLX_WORKER_STOPPED")
                self._buffer += chunk
                if len(self._buffer) > 1_000_000:
                    raise WorkerError("MLX_RESPONSE_SIZE")
        line, self._buffer = self._buffer.split(b"\n", 1)
        try:
            message = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise WorkerError("MLX_PROTOCOL_INVALID") from error
        if not isinstance(message, dict) or message.get("ok") is not True or not isinstance(message.get("result"), dict):
            code = message.get("error") if isinstance(message, dict) else None
            raise WorkerError(code if isinstance(code, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{1,80}", code) else "MLX_PROTOCOL_INVALID")
        return message["result"]

    def _load_locked(self, deadline: float) -> bool:
        if self._process is not None:
            return False
        _verify_artifact(self.artifact)
        if _sha256(Path(self.config["artifact_manifest"])) != self._manifest_digest:
            raise WorkerError("ARTIFACT_CONFIG_CHANGED")
        self._process = self._spawn()
        if self._closing.is_set():
            raise WorkerError("WORKER_CLOSED")
        load_start = time.monotonic()
        loaded = self._exchange({"kind": "load", "config": self.config}, deadline)
        if loaded.get("ready") is not True:
            raise WorkerError("MLX_LOAD_NOT_READY")
        device = loaded.get("default_device")
        source = loaded.get("device_source")
        if device not in {"cpu", "gpu"} or not isinstance(source, str) or not source:
            raise WorkerError("DEVICE_TELEMETRY_REQUIRED")
        self._runtime_default_device = device
        self._device_source = source
        self._cold_load_ms = (time.monotonic() - load_start) * 1000
        return True

    def warmup(self, *, timeout: float = 120.0) -> dict[str, Any]:
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 0 < timeout <= 120:
            raise ValueError("INVALID_DEADLINE")
        if self._closing.is_set():
            raise WorkerError("WORKER_CLOSED")
        deadline = time.monotonic() + timeout
        if not self._lock.acquire(timeout=timeout):
            raise WorkerError("WORKER_DEADLINE_EXCEEDED")
        try:
            if self._closing.is_set():
                raise WorkerError("WORKER_CLOSED")
            self._load_locked(deadline)
            return {"ready": True, "default_device": self._runtime_default_device,
                    "actual_device": "UNVERIFIED", "device_source": self._device_source}
        except Exception as error:
            self._stop_locked()
            if self._closing.is_set():
                raise WorkerError("WORKER_CLOSED") from error
            raise
        finally:
            self._lock.release()

    def predict(self, state: Any, questions: dict[str, Any], *, timeout: float = 30.0) -> dict[str, Any]:
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 0 < timeout <= 120:
            raise ValueError("INVALID_DEADLINE")
        if self._closing.is_set():
            raise WorkerError("WORKER_CLOSED")
        if not self._admission.acquire(blocking=False):
            raise WorkerBusy("WORKER_QUEUE_FULL")
        deadline = time.monotonic() + timeout
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not self._lock.acquire(timeout=remaining):
                raise WorkerError("WORKER_DEADLINE_EXCEEDED")
            try:
                if self._closing.is_set():
                    raise WorkerError("WORKER_CLOSED")
                cold = self._load_locked(deadline)
                start = time.monotonic()
                result = self._exchange({"kind": "predict", "state": state, "questions": questions}, deadline)
                self._calls += 1
                return {
                    "output": result,
                    "telemetry": {
                        "cold": cold,
                        "cold_load_ms": round(self._cold_load_ms, 3) if cold and self._cold_load_ms is not None else None,
                        "inference_ms": round((time.monotonic() - start) * 1000, 3),
                        "runtime_default_device": self._runtime_default_device,
                        "actual_device": "UNVERIFIED",
                        "device_source": self._device_source,
                        "device_status": "CPU_DEFAULT_DEVICE" if self._runtime_default_device == "cpu" else "DEVICE_UNVERIFIED",
                        "offline_guard": "MACOS_SANDBOX_DENY_NETWORK",
                        "checkpoint_revision": self.artifact.checkpoint_revision,
                        "checkpoint_sha256": self.artifact.checkpoint_sha256,
                        "resident_calls": self._calls,
                    },
                }
            except Exception as error:
                recoverable = isinstance(error, WorkerError) and str(error).startswith(
                    ("MLX_OPTION_WOULD_", "MLX_RUBRIC_WOULD_", "MLX_INSTRUCTIONS_WOULD_", "MLX_STATE_WOULD_")
                ) and self._process is not None and self._process.poll() is None
                if not recoverable:
                    self._stop_locked()
                if self._closing.is_set():
                    raise WorkerError("WORKER_CLOSED") from error
                raise
            finally:
                self._lock.release()
        finally:
            self._admission.release()
