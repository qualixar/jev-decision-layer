"""Trusted facade for the local, single-user recipe workbench.

This module is deliberately not an HTTP handler. The browser can ask for
public recipe contracts and synthetic fixture replays. A live request is
reviewed against the persisted workspace policy, then executed once through
the existing Engine only after the server returns the stored review object.
"""

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any, Callable

from .common import AutoError, canonical, digest, workspace
from .engine import Engine
from .recipe_fixtures import VARIANTS, run_fixture
from .recipe_runtime import _catalog, prepare_recipe
from .settings import load_policy


_FEATURED = {
    "qualixar.meeting-action-routing": "Managers",
    "qualixar.brief-fit": "Content creators",
    "qualixar.test-selection": "Junior developers",
}
_CLASSIFICATIONS = frozenset({"public", "internal-minimized", "restricted"})
_RECEIPT_ID = re.compile(r"[a-f0-9]{64}\Z")
_LIVE_FIELDS = frozenset({
    "recipe_id", "values", "data_classification", "policy_digest", "request_digest",
    "provider", "model", "questions", "title", "audience", "recipe_status",
})
_RESULT_FIELDS = (
    "status", "recipe_id", "title", "audience", "answer", "provider", "model",
    "receipt_id", "cache_hit", "calibration_status", "recipe_status", "gate_status",
    "host_action", "reason", "recommendation", "policy_receipt_id",
)


def _safe_catalog() -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    for recipe in _catalog():
        # Keep the browser contract intentionally narrower than the packaged
        # catalog. Fixture answers and gate policy never enter this response.
        cards.append({
            "id": recipe["id"],
            "title": recipe["title"],
            "audience": recipe["audience"],
            "input_schema": copy.deepcopy(recipe["input_schema"]),
            "questions": copy.deepcopy(recipe["questions"]),
            "status": recipe["status"],
            "limitations": recipe["limitations"],
            "featured_for": _FEATURED.get(recipe["id"]),
        })
    return cards


class RecipeWorkbench:
    """Policy-aware facade. Workspace is fixed when the process starts."""

    def __init__(
        self,
        workspace_path: str | Path,
        *,
        base: Path | None = None,
        engine_factory: Callable[..., Engine] = Engine,
        policy_loader: Callable[..., dict[str, Any]] = load_policy,
        query_preparer: Callable[..., Any] | None = None,
    ) -> None:
        self.workspace = workspace(workspace_path)
        self.base = base
        self.engine_factory = engine_factory
        self.policy_loader = policy_loader
        if query_preparer is None:
            from src.adl.queries.typed import prepare_query

            query_preparer = prepare_query
        self.query_preparer = query_preparer

    def list_cards(self) -> list[dict[str, Any]]:
        return _safe_catalog()

    def offline_example(self, recipe_id: str, variant: str = "nominal") -> dict[str, Any]:
        if variant not in VARIANTS:
            raise AutoError("RECIPE_FIXTURE_VARIANT_INVALID")
        outcome = run_fixture(recipe_id, variant)
        return {
            "mode": "fixture",
            "data_classification": "synthetic",
            "outcome": outcome,
            "disclaimer": "Synthetic fixture replay through the local gate; no provider call. This is not accuracy evidence.",
        }

    def preview_live(
        self,
        workspace_path: str | Path,
        recipe_id: str,
        values: Any,
        classification: str,
    ) -> dict[str, Any]:
        path = self._fixed_workspace(workspace_path)
        if classification not in _CLASSIFICATIONS:
            raise AutoError("DATA_CLASSIFICATION")
        prepared = prepare_recipe(recipe_id, values)
        policy = self.policy_loader(path, self.base)
        # Recipes compile to the engine's generic typed-query capability. The
        # setup wizard's case_ids are legacy Jev use-case IDs, not recipe IDs;
        # generic query consent is the authority for this path.
        if policy.get("generic_query_enabled") is not True:
            raise AutoError("GENERIC_QUERY_DISABLED")
        provider = self._provider_for(policy, classification)
        query = self.query_preparer(
            path,
            prepared["state"],
            prepared["questions"],
            provider=provider,
            data_classification=classification,
            base=self.base,
        )
        current_digest = digest(policy)
        if query.policy_sha256 != current_digest:
            raise AutoError("POLICY_CHANGED")
        payload_bytes = len(query.payload_json.encode("utf-8"))
        if payload_bytes > policy["max_request_bytes"]:
            raise AutoError("REQUEST_BYTE_BUDGET")
        is_local = query.provider == "laya-mlx"
        external_confirmation = not is_local and classification == "restricted"
        review = {
            "title": prepared["title"],
            "recipe_id": recipe_id,
            "provider": query.provider,
            "model": query.expected_model,
            "route_kind": "local" if is_local else "hosted",
            "data_classification": classification,
            "estimated_request_bytes": payload_bytes,
            "max_request_bytes": policy["max_request_bytes"],
            "max_calls_per_day": policy["max_calls_per_day"],
            "max_bytes_per_day": policy["max_bytes_per_day"],
            "text_leaves_device": not is_local,
            "requires_external_scope_confirmation": external_confirmation,
            "receipt_persistence": True,
            "cache_persistence": True,
            "calibration_status": query.calibration_status,
            "notice": (
                "This request is advisory. Jev will not send, publish, assign, execute a tool, or change project files. "
                "Input screening is best effort, not complete data-loss prevention. The existing engine may retain "
                "its normal local receipt, cache, budget and event records."
            ),
        }
        reviewed_request = {
            "recipe_id": recipe_id,
            "values": copy.deepcopy(values),
            "data_classification": classification,
            "policy_digest": current_digest,
            "request_digest": query.request_sha256,
            "provider": query.provider,
            "model": query.expected_model,
            "questions": copy.deepcopy(prepared["questions"]),
            "title": prepared["title"],
            "audience": prepared["audience"],
            "recipe_status": prepared["status"],
        }
        return {"review": review, "reviewed_request": reviewed_request}

    def run_live(
        self,
        workspace_path: str | Path,
        reviewed_request: Any,
        *,
        confirmation: bool,
        external_scope_confirmation: bool = False,
    ) -> dict[str, Any]:
        path = self._fixed_workspace(workspace_path)
        if confirmation is not True:
            raise AutoError("WORKBENCH_CONFIRMATION_REQUIRED")
        if not isinstance(reviewed_request, dict) or set(reviewed_request) != _LIVE_FIELDS:
            raise AutoError("WORKBENCH_REVIEW_INVALID")
        classification = reviewed_request.get("data_classification")
        if classification not in _CLASSIFICATIONS:
            raise AutoError("DATA_CLASSIFICATION")

        # Re-read policy and recompile the exact request at the authority
        # boundary. A changed route, policy or input invalidates the review.
        current_policy = self.policy_loader(path, self.base)
        if current_policy.get("generic_query_enabled") is not True:
            raise AutoError("GENERIC_QUERY_DISABLED")
        if digest(current_policy) != reviewed_request["policy_digest"]:
            raise AutoError("POLICY_CHANGED")
        prepared = prepare_recipe(reviewed_request["recipe_id"], reviewed_request["values"])
        provider = self._provider_for(current_policy, classification)
        query = self.query_preparer(
            path,
            prepared["state"],
            prepared["questions"],
            provider=provider,
            data_classification=classification,
            base=self.base,
        )
        if (
            query.policy_sha256 != reviewed_request["policy_digest"]
            or query.request_sha256 != reviewed_request["request_digest"]
            or query.provider != reviewed_request["provider"]
            or query.expected_model != reviewed_request["model"]
        ):
            raise AutoError("WORKBENCH_REVIEW_CHANGED")
        if query.provider != "laya-mlx" and classification == "restricted" and not external_scope_confirmation:
            raise AutoError("EXTERNAL_SCOPE_CONFIRMATION_REQUIRED")

        engine = self.engine_factory(path, base=self.base)
        try:
            result = engine.try_recipe({
                "recipe_id": reviewed_request["recipe_id"],
                "input": reviewed_request["values"],
                "data_classification": classification,
                "expected_policy_digest": reviewed_request["policy_digest"],
            })
        finally:
            self._close_engine(engine)
        if not isinstance(result, dict) or result.get("execution_authorized") is not False:
            raise AutoError("WORKBENCH_ENGINE_CONTRACT")
        output = {key: copy.deepcopy(result[key]) for key in _RESULT_FIELDS if key in result}
        output["execution_authorized"] = False
        if not _RECEIPT_ID.fullmatch(output.get("receipt_id", "")) or not _RECEIPT_ID.fullmatch(output.get("policy_receipt_id", "")):
            raise AutoError("WORKBENCH_RECEIPT_INVALID")
        if len(canonical(output)) > 12_000:
            raise AutoError("WORKBENCH_RESULT_TOO_LARGE")
        return output

    def read_receipt(self, workspace_path: str | Path, receipt_id: str) -> dict[str, Any]:
        path = self._fixed_workspace(workspace_path)
        if not isinstance(receipt_id, str) or _RECEIPT_ID.fullmatch(receipt_id) is None:
            raise AutoError("EVIDENCE_ID")
        engine = self.engine_factory(path, base=self.base)
        try:
            record = engine.store.get(receipt_id)
        finally:
            self._close_engine(engine)
        if not isinstance(record, dict):
            raise AutoError("WORKBENCH_RECEIPT_INVALID")
        safe = {"receipt_id": receipt_id, "kind": record.get("kind")}
        for key in ("recipe", "recipe_id", "provider", "model_identity", "model", "recorded_at", "recipe_status"):
            value = record.get(key)
            if isinstance(value, (str, int, float)) and not isinstance(value, bool):
                safe[key] = value
        gate = record.get("gate")
        if isinstance(gate, dict):
            safe["gate"] = {key: copy.deepcopy(gate[key]) for key in ("status", "host_action", "recommendation", "reasons") if key in gate}
        return safe

    def _fixed_workspace(self, path: str | Path) -> Path:
        resolved = workspace(path)
        if resolved != self.workspace:
            raise AutoError("WORKBENCH_WORKSPACE_MISMATCH")
        return self.workspace

    @staticmethod
    def _close_engine(engine: Any) -> None:
        providers = getattr(engine, "providers", None)
        close = getattr(providers, "close", None)
        if callable(close):
            close()

    @staticmethod
    def _provider_for(policy: dict[str, Any], classification: str) -> str:
        if (
            classification == "restricted"
            and policy.get("local_laya_enabled") is True
            and isinstance(policy.get("mlx"), dict)
        ):
            return "laya-mlx"
        routes = policy.get("routes", {})
        provider = routes.get("generic", policy.get("provider")) if isinstance(routes, dict) else None
        if provider not in ("typesafe", "openrouter", "laya-mlx"):
            raise AutoError("PROVIDER_ROUTE")
        return provider
