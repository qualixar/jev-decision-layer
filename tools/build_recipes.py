#!/usr/bin/env python3
"""Validate public recipe sources and rebuild the plugin's data-only catalog."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from recipe_registry import load_registry


ROOT = Path(__file__).resolve().parents[1]
RECIPES = ROOT / "recipes"
RUNTIME = ROOT / "plugins" / "qualixar-jev-decision-layer" / "runtime"
PUBLIC_FIELDS = ("id", "title", "audience", "input_schema", "questions", "status", "limitations")


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
    for path in sources:
        source = json.loads(path.read_text())
        recipes.append({key: source[key] for key in PUBLIC_FIELDS})
    if {item.id for item in registered} != {item["id"] for item in recipes}:
        raise ValueError("RECIPE_REGISTRY_MISMATCH")
    encoded = json.dumps({"schema_version": 1, "recipes": recipes}, indent=2, sort_keys=True) + "\n"
    catalog_path = RUNTIME / "recipe_catalog.json"
    manifest_path = RUNTIME / "RUNTIME_MANIFEST.json"
    if catalog_path.is_symlink() or manifest_path.is_symlink():
        raise ValueError("RUNTIME_FILE_INVALID")
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("adapter_version") != "1.0.0" or "recipe_catalog.json" not in manifest.get("files", {}):
        raise ValueError("RUNTIME_MANIFEST_INVALID")
    for name, expected in manifest["files"].items():
        if name != "recipe_catalog.json" and _digest(RUNTIME / name) != expected:
            raise ValueError("RUNTIME_MANIFEST_MISMATCH")
    if check:
        if catalog_path.read_text() != encoded or _digest(catalog_path) != manifest["files"]["recipe_catalog.json"]:
            raise ValueError("RECIPE_CATALOG_STALE")
    else:
        catalog_path.write_text(encoded)
        manifest["files"]["recipe_catalog.json"] = _digest(catalog_path)
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(f"Validated {len(recipes)} public recipes; runtime catalog {'current' if check else 'updated'}.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build the sanitized Qualixar Jev recipe catalog")
    parser.add_argument("--check", action="store_true", help="Verify without changing files")
    args = parser.parse_args()
    raise SystemExit(build(check=args.check))
