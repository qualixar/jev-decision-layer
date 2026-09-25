"""Closed, data-only recipe registry.

Recipes are parsed as JSON and validated. Their text is never evaluated,
imported, or treated as permission to run automatically.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any


MAX_BYTES = 64_000
MAX_DEPTH = 12
ALLOWED_KEYS = {
    "schema_version",
    "id",
    "version",
    "title",
    "audience",
    "benefit_hypothesis",
    "legacy_id",
    "status",
    "trigger",
    "input_description",
    "input_schema",
    "questions",
    "acquisition",
    "policy",
    "allowed_actions",
    "effects",
    "risk",
    "provider_support",
    "budget",
    "evidence_requirements",
    "limitations",
    "sources",
    "sample",
    "fixtures",
}
ALLOWED_ACTIONS = {
    "abstain",
    "request_review",
    "route_to_queue",
    "mark_check_passed",
    "select_candidates",
    "quarantine_candidate",
}
FORBIDDEN_TEXT = ("eval(", "exec(", "import ", "subprocess", "http://", "https://", "include(")


class RegistryError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class RegisteredRecipe:
    id: str
    auto_allowed: bool
    provider_ready: bool


def load_registry(source: Path | list[dict[str, Any]]) -> list[RegisteredRecipe]:
    recipes = _read(source)
    seen: set[str] = set()
    loaded: list[RegisteredRecipe] = []
    for recipe in recipes:
        _validate(recipe)
        if recipe["id"] in seen:
            raise RegistryError("DUPLICATE_ID")
        seen.add(recipe["id"])
        support = recipe["provider_support"]
        loaded.append(
            RegisteredRecipe(
                id=recipe["id"],
                auto_allowed=False,
                provider_ready=support.get("jev") == "EVALUATED" and support.get("laya") == "EVALUATED",
            )
        )
    return loaded


def _read(source: Path | list[dict[str, Any]]) -> list[dict[str, Any]]:
    if isinstance(source, list):
        return source
    try:
        if source.is_symlink() or not source.is_dir():
            raise RegistryError("RECIPE_SOURCE_INVALID")
        paths = sorted(source.rglob("*.json"))
    except OSError as error:
        raise RegistryError("RECIPE_SOURCE_INVALID") from error
    recipes = []
    for path in paths:
        if path.name == "legacy-business-aliases.json":
            continue
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(descriptor, "rb") as stream:
                metadata = os.fstat(stream.fileno())
                if not stat.S_ISREG(metadata.st_mode):
                    raise RegistryError("RECIPE_SOURCE_INVALID")
                if metadata.st_size > MAX_BYTES:
                    raise RegistryError("RECIPE_TOO_LARGE")
                raw = stream.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise RegistryError("RECIPE_TOO_LARGE")
            recipes.append(json.loads(raw.decode("utf-8")))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise RegistryError("RECIPE_SOURCE_INVALID") from error
    return recipes


def _validate(recipe: dict[str, Any]) -> None:
    if not isinstance(recipe, dict):
        raise RegistryError("RECIPE_NOT_OBJECT")
    unknown = set(recipe) - ALLOWED_KEYS
    if unknown:
        raise RegistryError("UNKNOWN_FIELD")
    _walk(recipe, 0)
    questions = recipe.get("questions")
    if not isinstance(questions, dict) or "decision" not in questions:
        raise RegistryError("QUESTION_INVALID")
    decision = questions["decision"]
    if decision.get("type") not in {"choice", "score", "noul"}:
        raise RegistryError("QUESTION_INVALID")
    if decision.get("type") == "choice":
        criteria = decision.get("criteria")
        if not isinstance(criteria, dict) or "unknown" not in criteria:
            raise RegistryError("UNKNOWN_OUTCOME_REQUIRED")
    actions = recipe.get("allowed_actions")
    if not isinstance(actions, list) or any(action not in ALLOWED_ACTIONS for action in actions):
        raise RegistryError("ACTION_NOT_ALLOWED")
    if recipe.get("effects") != "advisory_or_registered_reversible_only":
        raise RegistryError("EXECUTABLE_CONTENT")
    if recipe.get("status") != "SPECIFICATION_NOT_MODEL_EVALUATED":
        raise RegistryError("UNVALIDATED_STATUS")


def _walk(value: Any, depth: int) -> None:
    if depth > MAX_DEPTH:
        raise RegistryError("TOO_DEEP")
    if isinstance(value, str):
        lowered = value.lower()
        if any(token in lowered for token in FORBIDDEN_TEXT):
            raise RegistryError("EXECUTABLE_CONTENT")
        return
    if isinstance(value, dict):
        for item in value.values():
            _walk(item, depth + 1)
        return
    if isinstance(value, list):
        for item in value:
            _walk(item, depth + 1)
