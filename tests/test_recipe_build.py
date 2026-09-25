"""Public recipe contributions must produce a sanitized bundled catalog."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tools.recipe_registry import RegistryError, load_registry


ROOT = Path(__file__).resolve().parents[1]


class RecipeBuildTests(unittest.TestCase):
    def test_registry_rejects_symlink_and_malformed_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root / "outside.json"
            outside.write_text('{"secret":"not for recipe loading"}')
            recipes = root / "recipes"
            recipes.mkdir()
            (recipes / "linked.json").symlink_to(outside)
            with self.assertRaisesRegex(RegistryError, "RECIPE_SOURCE_INVALID"):
                load_registry(recipes)
            (recipes / "linked.json").unlink()
            (recipes / "bad.json").write_text("{")
            with self.assertRaisesRegex(RegistryError, "RECIPE_SOURCE_INVALID"):
                load_registry(recipes)

    def test_public_sources_match_bundled_catalog(self):
        command = [sys.executable, str(ROOT / "tools" / "build_recipes.py"), "--check"]
        result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        catalog = json.loads((ROOT / "plugins" / "qualixar-jev-decision-layer" / "runtime" / "recipe_catalog.json").read_text())
        source_ids = {json.loads(path.read_text())["id"] for path in (ROOT / "recipes").rglob("*.json")}
        self.assertEqual({recipe["id"] for recipe in catalog["recipes"]}, source_ids)

    def test_public_recipe_specs_have_no_internal_provenance_or_mock_answers(self):
        for path in (ROOT / "recipes").rglob("*.json"):
            with self.subTest(path=path.name):
                recipe = json.loads(path.read_text())
                self.assertFalse({"sources", "fixtures", "benefit_hypothesis", "evidence_requirements"} & set(recipe))
                self.assertEqual(recipe["status"], "SPECIFICATION_NOT_MODEL_EVALUATED")


if __name__ == "__main__":
    unittest.main()
