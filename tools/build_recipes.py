#!/usr/bin/env python3
"""Validate public recipe sources and rebuild the plugin's data-only catalog."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from recipe_registry import load_registry


ROOT = Path(__file__).resolve().parents[1]
RECIPES = ROOT / "recipes"
RUNTIME = ROOT / "plugins" / "qualixar-jev-decision-layer" / "runtime"
PUBLIC_FIELDS = ("id", "title", "audience", "input_schema", "questions", "status", "limitations")
# Fields used ONLY for local gating. They are emitted in a separate `gates`
# array and are never part of a recipe the model sees: a threshold is not
# evidence, and sending it would invite the model to reason about its own
# gate instead of answering the question.
GATE_FIELDS = ("id", "policy")
# Offline self-test cases. They live OUTSIDE `recipes/` on purpose: a public
# recipe is a specification, and a hand-written answer sitting inside one reads
# as evidence of what the provider does. They are emitted beside the recipes in
# the catalog, never inside one — a worked example in the prompt would steer the
# answer it is meant to check. Their purpose is to let a host prove this layer
# behaves before it spends a single host token on it.
FIXTURES = ROOT / "fixtures"
VARIANTS = ("nominal", "uncertain", "adversarial")


def _fixtures(recipe_ids: set[str]) -> list[dict]:
    """Validate against the SAME contract the runtime enforces.

    The builder used to accept a case carrying only `variant` and
    `data_classification`, which the runtime's `validate_fixtures` then
    rejected. A build that passes must not produce a catalog the runtime
    refuses to load, so the runtime's own validator is the authority here.
    """
    sys.path.insert(0, str(RUNTIME))
    from jev_auto.common import AutoError
    from jev_auto.recipe_fixtures import validate_fixtures

    paths = sorted(FIXTURES.glob("*.json"))
    if any(path.is_symlink() for path in paths):
        raise ValueError("RECIPE_FIXTURES_INVALID")
    entries = []
    for path in paths:
        try:
            document = json.loads(path.read_text())
        except (OSError, ValueError):
            raise ValueError("RECIPE_FIXTURES_INVALID") from None
        if not isinstance(document, dict) or set(document) != {"id", "cases"}:
            raise ValueError("RECIPE_FIXTURES_INVALID")
        if not isinstance(document["id"], str) or path.stem != document["id"]:
            raise ValueError("RECIPE_FIXTURES_INVALID")
        entries.append(document)
    try:
        validate_fixtures(entries)
    except AutoError as error:
        raise ValueError(f"RECIPE_FIXTURES_INVALID: {error}") from None
    if {entry["id"] for entry in entries} != recipe_ids:
        raise ValueError("RECIPE_FIXTURES_INCOMPLETE")
    return entries


def _digest(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise ValueError("RUNTIME_FILE_INVALID")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(*, check: bool = False) -> int:
    registered = load_registry(RECIPES)
    sources = sorted(RECIPES.rglob("*.json"))
    if not 1 <= len(sources) <= 128 or any(path.is_symlink() for path in sources):
        raise ValueError("RECIPE_SOURCE_INVALID")
    recipes = []
    gates = []
    for path in sources:
        try:
            source = json.loads(path.read_text())
        except (OSError, ValueError):
            raise ValueError("RECIPE_SOURCE_INVALID") from None
        if not isinstance(source, dict) or not set(PUBLIC_FIELDS + GATE_FIELDS) <= set(source):
            # A missing field used to surface as a bare KeyError naming one
            # key, which reads like a crash rather than an invalid recipe.
            raise ValueError("RECIPE_SOURCE_INVALID")
        recipes.append({key: source[key] for key in PUBLIC_FIELDS})
        gates.append({key: source[key] for key in GATE_FIELDS})
    if {item.id for item in registered} != {item["id"] for item in recipes}:
        raise ValueError("RECIPE_REGISTRY_MISMATCH")
    fixtures = _fixtures({recipe["id"] for recipe in recipes})
    encoded = json.dumps(
        {"schema_version": 1, "recipes": recipes, "gates": gates, "fixtures": fixtures},
        indent=2, sort_keys=True) + "\n"
    catalog_path = RUNTIME / "recipe_catalog.json"
    manifest_path = RUNTIME / "RUNTIME_MANIFEST.json"
    if catalog_path.is_symlink() or manifest_path.is_symlink():
        raise ValueError("RUNTIME_FILE_INVALID")
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, ValueError):
        raise ValueError("RUNTIME_MANIFEST_INVALID") from None
    if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), dict):
        raise ValueError("RUNTIME_MANIFEST_INVALID")
    if manifest.get("adapter_version") != "1.0.0" or "recipe_catalog.json" not in manifest["files"]:
        raise ValueError("RUNTIME_MANIFEST_INVALID")
    for name, expected in manifest["files"].items():
        if name != "recipe_catalog.json" and _digest(RUNTIME / name) != expected:
            raise ValueError("RUNTIME_MANIFEST_MISMATCH")
    if check:
        if not catalog_path.is_file():
            raise ValueError("RECIPE_CATALOG_STALE")
        if catalog_path.read_text() != encoded or _digest(catalog_path) != manifest["files"]["recipe_catalog.json"]:
            raise ValueError("RECIPE_CATALOG_STALE")
    else:
        catalog_path.write_text(encoded)
        manifest["files"]["recipe_catalog.json"] = _digest(catalog_path)
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    cases = sum(len(entry["cases"]) for entry in fixtures)
    print(f"Validated {len(recipes)} public recipes, {len(gates)} gates and {cases} offline fixtures; "
          f"runtime catalog {'current' if check else 'updated'}.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build the sanitized Qualixar Jev recipe catalog")
    parser.add_argument("--check", action="store_true", help="Verify without changing files")
    args = parser.parse_args()
    raise SystemExit(build(check=args.check))
