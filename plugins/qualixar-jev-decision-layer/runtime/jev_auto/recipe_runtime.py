"""Explicit advisory access to the reviewed, data-only recipe catalog."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .common import AutoError


_ID = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,127}\Z")
_PUBLIC_FIELDS = ("id", "title", "audience", "input_schema", "questions", "status", "limitations")


def _validate_catalog(recipes: Any) -> list[dict[str, Any]]:
    if not isinstance(recipes, list) or not 1 <= len(recipes) <= 128:
        raise AutoError("RECIPE_CATALOG_INVALID")
    ids = []
    for recipe in recipes:
        if not isinstance(recipe, dict) or set(recipe) != set(_PUBLIC_FIELDS):
            raise AutoError("RECIPE_CATALOG_INVALID")
        identifier = recipe.get("id")
        if not isinstance(identifier, str) or not _ID.fullmatch(identifier):
            raise AutoError("RECIPE_CATALOG_INVALID")
        if (
            not isinstance(recipe.get("title"), str) or not 1 <= len(recipe["title"]) <= 150
            or not isinstance(recipe.get("audience"), str) or not 1 <= len(recipe["audience"]) <= 40
            or recipe.get("status") != "SPECIFICATION_NOT_MODEL_EVALUATED"
            or not isinstance(recipe.get("limitations"), str)
            or not isinstance(recipe.get("input_schema"), dict)
            or not isinstance(recipe.get("questions"), dict)
        ):
            raise AutoError("RECIPE_CATALOG_INVALID")
        ids.append(identifier)
    if len(set(ids)) != len(ids):
        raise AutoError("RECIPE_CATALOG_INVALID")
    return recipes


def catalog_document() -> dict[str, Any] | None:
    """The packaged catalog as written, or None when only sources are present.

    `recipes` is what the model sees. `gates` and `fixtures` sit beside it and
    are never merged in — a model must not be shown its own threshold, nor a
    worked example of the answer it is being asked for.
    """
    packaged = Path(__file__).resolve().parents[1] / "recipe_catalog.json"
    if not packaged.exists():
        return None
    if packaged.is_symlink() or packaged.stat().st_size > 512_000:
        raise AutoError("RECIPE_CATALOG_INVALID")
    try:
        data = json.loads(packaged.read_text())
    except (OSError, ValueError):
        raise AutoError("RECIPE_CATALOG_INVALID") from None
    if not isinstance(data, dict) or data.get("schema_version") != 1 or not isinstance(data.get("recipes"), list):
        raise AutoError("RECIPE_CATALOG_INVALID")
    return data


def _catalog() -> list[dict[str, Any]]:
    root = Path(__file__).resolve().parents[1]
    data = catalog_document()
    if data is not None:
        if "gates" in data:
            from .recipe_gate import validate_gates

            validate_gates(data["gates"])
        recipes = data["recipes"]
    else:
        from src.adl.recipes.registry import load_registry

        source = root / "recipes"
        load_registry(source)
        recipes = []
        for path in sorted(source.rglob("*.json")):
            if path.name != "legacy-business-aliases.json":
                raw = json.loads(path.read_text())
                recipes.append({key: raw[key] for key in _PUBLIC_FIELDS})
    return _validate_catalog(recipes)


def catalog_preview() -> dict[str, Any]:
    return {"status": "SPECIFICATION_NOT_MODEL_EVALUATED", "recipes": [
        {"id": recipe["id"], "title": recipe["title"], "audience": recipe["audience"]}
        for recipe in _catalog()
    ]}


def prepare_recipe(recipe_id: str, values: Any) -> dict[str, Any]:
    if not isinstance(recipe_id, str) or not _ID.fullmatch(recipe_id):
        raise AutoError("RECIPE_NOT_FOUND")
    recipe = next((item for item in _catalog() if item["id"] == recipe_id), None)
    if recipe is None:
        raise AutoError("RECIPE_NOT_FOUND")
    schema = recipe["input_schema"]
    if not isinstance(schema, dict) or schema.get("type") != "object" or schema.get("additionalProperties") is not False:
        raise AutoError("RECIPE_CATALOG_INVALID")
    properties = schema.get("properties")
    required = schema.get("required")
    if not isinstance(properties, dict) or not isinstance(required, list) or not isinstance(values, dict):
        raise AutoError("RECIPE_INPUT_INVALID")
    if set(values) != set(required) or set(properties) != set(required):
        raise AutoError("RECIPE_INPUT_INVALID")
    for key, value in values.items():
        rule = properties[key]
        if (
            not isinstance(rule, dict) or rule.get("type") != "string"
            or not isinstance(value, str) or not value.strip()
            or len(value) < rule.get("minLength", 1)
            or len(value) > min(rule.get("maxLength", 12_000), 12_000)
        ):
            raise AutoError("RECIPE_INPUT_INVALID")
    questions = recipe.get("questions")
    if not isinstance(questions, dict) or set(questions) != {"decision"}:
        raise AutoError("RECIPE_CATALOG_INVALID")
    return {"recipe_id": recipe_id, "title": recipe["title"], "audience": recipe["audience"],
            "state": dict(values), "questions": questions, "status": recipe["status"]}
