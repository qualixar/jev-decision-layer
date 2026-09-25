"""Compile a bounded typed query after a broker-owned workspace policy check.

This module does not read credentials, call providers, activate recipes, or grant
tool authority. The installed broker must load the policy; a caller-supplied
policy dict is suitable only for offline validation tests.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from jevkit.security import screen


_QUESTION_ID = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,127}\Z")
_REVISION = re.compile(r"[a-f0-9]{40}\Z")
_SHA256 = re.compile(r"[a-f0-9]{64}\Z")
_MODELS = {
    "typesafe": "jev-1.13.0",
    "openrouter": "typesafe/jev-1.13",
}
_MAX_REQUEST_BYTES = 48_000
_MAX_QUESTION_BYTES = 20_000
_MAX_DEPTH = 32
_MAX_STRING_CHARS = 8_000


class QueryError(ValueError):
    """Stable rejection code without caller data in the message."""


@dataclass(frozen=True)
class CompiledQuery:
    provider: str
    expected_model: str
    request_sha256: str
    payload_json: str
    data_classification: str
    local_preflight_required: bool
    execution_authorized: bool = False
    calibration_status: str = "NOT_EVALUATED"
    workspace_id: str | None = None
    policy_sha256: str | None = None
    policy_expires_at: float | None = None
    transport_authorized: bool = False


def _canonical(value: Any, error: str) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, RecursionError) as cause:
        raise QueryError(error) from cause


def _bounded_shape(value: Any) -> None:
    pending = [(value, 0)]
    nodes = 0
    while pending:
        current, depth = pending.pop()
        nodes += 1
        if nodes > 20_000 or depth > _MAX_DEPTH:
            raise QueryError("REQUEST_DEPTH_OR_SIZE")
        if isinstance(current, dict):
            if any(not isinstance(key, str) or len(key) > 128 for key in current):
                raise QueryError("REQUEST_SHAPE_INVALID")
            pending.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            pending.extend((item, depth + 1) for item in current)
        elif isinstance(current, str):
            if len(current) > _MAX_STRING_CHARS:
                raise QueryError("REQUEST_TOO_LARGE")
        elif current is not None and not isinstance(current, (int, float, bool)):
            raise QueryError("REQUEST_SHAPE_INVALID")


def _validate_questions(questions: Any) -> None:
    if not isinstance(questions, dict) or not 1 <= len(questions) <= 60:
        raise QueryError("QUESTION_INVALID")
    for question_id, question in questions.items():
        if not isinstance(question_id, str) or _QUESTION_ID.fullmatch(question_id) is None:
            raise QueryError("QUESTION_INVALID")
        if not isinstance(question, dict) or set(question) - {"type", "instructions", "criteria"}:
            raise QueryError("QUESTION_INVALID")
        kind = question.get("type")
        instruction = question.get("instructions")
        if kind not in ("choice", "score", "noul") or not isinstance(instruction, str) or not instruction.strip():
            raise QueryError("QUESTION_INVALID")
        criteria = question.get("criteria")
        if kind == "choice" and (
            not isinstance(criteria, dict) or not 2 <= len(criteria) <= 64
            or any(not isinstance(label, str) or not label or len(label) > 128 for label in criteria)
            or any(not isinstance(description, str) or not description or len(description) > 2_000 for description in criteria.values())
        ):
            raise QueryError("QUESTION_INVALID")
        if kind == "score" and (
            not isinstance(criteria, list) or not 2 <= len(criteria) <= 10
            or any(not isinstance(level, str) or not level or len(level) > 2_000 for level in criteria)
        ):
            raise QueryError("QUESTION_INVALID")
        if kind == "noul" and criteria is not None and (
            not isinstance(criteria, dict) or set(criteria) - {"true", "false"}
            or any(not isinstance(description, str) or not description or len(description) > 2_000 for description in criteria.values())
        ):
            raise QueryError("QUESTION_INVALID")
    if len(_canonical(questions, "QUESTION_INVALID").encode("utf-8")) > _MAX_QUESTION_BYTES:
        raise QueryError("QUESTION_INVALID")


def _prepare_query_with_policy(
    state: str | dict[str, Any] | list[Any],
    questions: dict[str, Any],
    *,
    provider: str,
    policy: dict[str, Any],
    data_classification: str,
    automatic: bool = False,
) -> CompiledQuery:
    if automatic:
        raise QueryError("GENERIC_AUTO_FORBIDDEN")
    if not isinstance(policy, dict) or policy.get("generic_query_enabled") is not True:
        raise QueryError("GENERIC_QUERY_NOT_ENROLLED")
    if not isinstance(policy.get("routes", {}), dict):
        raise QueryError("POLICY_INVALID")
    selected = policy.get("routes", {}).get("generic", policy.get("provider"))
    local_opt_in = (provider == "laya-mlx" and policy.get("local_laya_enabled") is True
                    and isinstance(policy.get("mlx"), dict))
    if (provider != selected and not local_opt_in) or provider not in (*_MODELS, "laya-mlx"):
        raise QueryError("PROCESSOR_SWITCH_NEEDS_CONSENT")
    if data_classification not in ("public", "internal-minimized", "restricted"):
        raise QueryError("DATA_CLASSIFICATION_INVALID")
    if provider != "laya-mlx":
        if data_classification == "restricted" and not (
            policy.get("decision_mode") == "jev-maximum"
            and policy.get("data_classification") == "restricted"
        ):
            raise QueryError("REMOTE_RESTRICTED_DATA")
        enrolled = policy.get("data_classification")
        if enrolled not in ("public", "internal-minimized", "restricted") or (
            data_classification == "internal-minimized" and enrolled not in ("internal-minimized", "restricted")
        ):
            raise QueryError("DATA_CLASSIFICATION_NOT_ENROLLED")
    if not isinstance(state, (str, dict, list)):
        raise QueryError("STATE_INVALID")
    _bounded_shape({"state": state, "questions": questions})
    _validate_questions(questions)
    try:
        _screened, findings = screen({"state": state, "questions": questions})
    except RecursionError as error:
        raise QueryError("REQUEST_DEPTH_OR_SIZE") from error
    if findings:
        raise QueryError("INPUT_DATA_BLOCKED")
    if provider == "laya-mlx":
        from jev_auto.common import AutoError, read_private, safe_path

        mlx = policy.get("mlx")
        if not isinstance(mlx, dict):
            raise QueryError("LOCAL_MODEL_NOT_ATTESTED")
        repository = mlx.get("repository")
        revision = mlx.get("revision")
        weight_hash = mlx.get("weight_sha256")
        if (
            repository not in ("aac6fef/laya-mlx", "aac6fef/laya-multilingual-mlx")
            or not isinstance(revision, str) or _REVISION.fullmatch(revision) is None
            or not isinstance(weight_hash, str) or _SHA256.fullmatch(weight_hash) is None
            or not isinstance(mlx.get("model_dir"), str)
            or not isinstance(mlx.get("artifact_manifest"), str)
        ):
            raise QueryError("LOCAL_MODEL_NOT_ATTESTED")
        try:
            model_file = safe_path(Path(mlx["model_dir"]) / "model.safetensors")
            manifest = read_private(Path(mlx["artifact_manifest"]), 100_000)
            descriptor = os.open(model_file, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(descriptor, "rb") as stream:
                metadata = os.fstat(stream.fileno())
                actual_hash = hashlib.file_digest(stream, "sha256").hexdigest()
        except (OSError, AutoError, ValueError) as error:
            raise QueryError("LOCAL_MODEL_NOT_ATTESTED") from error
        if (
            not stat.S_ISREG(metadata.st_mode) or model_file.is_symlink() or metadata.st_size == 0
            or actual_hash != weight_hash
            or not isinstance(manifest, dict)
            or manifest.get("repository") != repository
            or manifest.get("revision") != revision
            or not isinstance(manifest.get("files"), dict)
            or manifest["files"].get("model.safetensors") != weight_hash
        ):
            raise QueryError("LOCAL_MODEL_NOT_ATTESTED")
        expected_model = f"laya-mlx@{revision}"
    else:
        expected_model = _MODELS[provider]
    payload = _canonical({"model": expected_model, "state": state, "questions": questions}, "REQUEST_INVALID")
    if len(payload.encode("utf-8")) > _MAX_REQUEST_BYTES:
        raise QueryError("REQUEST_TOO_LARGE")
    return CompiledQuery(
        provider=provider,
        expected_model=expected_model,
        request_sha256=hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        payload_json=payload,
        data_classification=data_classification,
        local_preflight_required=provider == "laya-mlx",
    )


def prepare_query(
    workspace_path: str | Path,
    state: str | dict[str, Any] | list[Any],
    questions: dict[str, Any],
    *,
    provider: str,
    data_classification: str,
    automatic: bool = False,
) -> CompiledQuery:
    """Prepare only from the installed broker's persisted workspace policy.

    The result remains advisory and cannot itself reserve a budget or call a
    provider. The broker must reload and compare policy immediately before
    any transport. A raw caller policy is never accepted by this entrypoint.
    """
    from jev_auto.common import AutoError, digest
    from jev_auto.settings import load_policy

    try:
        policy = load_policy(workspace_path)
    except AutoError as error:
        raise QueryError("GENERIC_QUERY_NOT_ENROLLED") from error
    compiled = _prepare_query_with_policy(
        state, questions, provider=provider, policy=policy,
        data_classification=data_classification, automatic=automatic,
    )
    return replace(
        compiled,
        workspace_id=policy["workspace_id"],
        policy_sha256=digest(policy),
        policy_expires_at=policy["expires_at"],
    )
