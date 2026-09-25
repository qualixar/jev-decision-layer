"""Compatibility facade for the native, bounded, no-network Laya worker."""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from .common import AutoError


class MLXProcess:
    """Keep the broker interface while removing its former direct child launch."""

    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        self.proc: Any = None
        self.last_telemetry: dict[str, Any] | None = None
        self._worker: Any = None
        self._lock = threading.Lock()

    def _create_worker(self) -> Any:
        from src.adl.providers.laya_worker import PreparedArtifact, SandboxedLayaProcess, WorkerError

        try:
            artifact = PreparedArtifact(
                profile_id="installed-local",
                checkpoint_revision=self.cfg["revision"],
                checkpoint_sha256=self.cfg["weight_sha256"],
                model_path=Path(self.cfg["model_dir"]) / "model.safetensors",
            )
            return SandboxedLayaProcess(artifact, self.cfg, Path(self.cfg["python"]))
        except (KeyError, TypeError, ValueError, WorkerError) as error:
            raise AutoError("MLX_ARTIFACT_NOT_READY") from error

    def _discard_locked(self) -> None:
        worker, self._worker = self._worker, None
        self.proc = None
        self.last_telemetry = None
        if worker is not None:
            worker.close()

    def warmup(self) -> dict[str, Any]:
        from src.adl.providers.laya_worker import WorkerError

        with self._lock:
            if self._worker is None:
                self._worker = self._create_worker()
            try:
                result = self._worker.warmup(timeout=120)
                self.proc = self._worker._process
                self.last_telemetry = {key: value for key, value in result.items() if key != "ready"}
                return {"ready": result["ready"], "provider": "laya-mlx"}
            except (WorkerError, OSError) as error:
                self._discard_locked()
                raise AutoError("MLX_WARMUP_FAILED") from error

    def predict(self, state: Any, questions: dict[str, Any], timeout: float) -> dict[str, Any]:
        from src.adl.providers.laya_worker import WorkerError

        with self._lock:
            if self._worker is None or self.proc is None:
                raise AutoError("MLX_WARMUP_REQUIRED")
            try:
                result = self._worker.predict(state, questions, timeout=timeout)
                self.proc = self._worker._process
                self.last_telemetry = result["telemetry"]
                return result["output"]
            except (WorkerError, OSError) as error:
                code = str(error)
                if code.startswith(("MLX_OPTION_WOULD_", "MLX_RUBRIC_WOULD_", "MLX_INSTRUCTIONS_WOULD_", "MLX_STATE_WOULD_")) and self._worker._process is not None:
                    self.proc = self._worker._process
                    raise AutoError(code) from error
                self._discard_locked()
                raise AutoError("MLX_WORKER_FAILURE") from error

    def close(self) -> None:
        # Native close cancels an active inference before taking its own lock.
        worker = self._worker
        if worker is not None:
            worker.close()
        with self._lock:
            self._worker = None
            self.proc = None
            self.last_telemetry = None
