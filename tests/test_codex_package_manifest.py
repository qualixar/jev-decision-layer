"""The generated Codex package never carries a root plugin.json, and CI holds the coverage floors.

Codex loads a plugin folder with a root plugin.json through its Agent Plugins
loader, which registers no hooks, so every Jev hook would go silent. The
check names that case instead of reporting a generic file-set mismatch.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "plugins" / "qualixar-jev-codex"


def _builder():
    spec = importlib.util.spec_from_file_location("build_codex_package_under_test", ROOT / "tools" / "build_codex_package.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RootManifestTests(unittest.TestCase):
    def setUp(self):
        self.builder = _builder()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.target = Path(tmp.name) / "qualixar-jev-codex"
        shutil.copytree(PACKAGE, self.target, symlinks=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

    def check(self):
        with patch.object(self.builder, "TARGET", self.target), contextlib.redirect_stdout(io.StringIO()):
            self.builder.build(check=True)

    def test_the_generated_package_passes(self):
        self.check()

    def test_a_root_plugin_json_fails_the_check_by_name(self):
        (self.target / "plugin.json").write_text("{}")
        with self.assertRaisesRegex(ValueError, "^CODEX_PACKAGE_ROOT_MANIFEST$"):
            self.check()

    def test_a_dangling_link_named_plugin_json_fails_too(self):
        os.symlink(self.target / "missing.json", self.target / "plugin.json")
        with self.assertRaisesRegex(ValueError, "^CODEX_PACKAGE_ROOT_MANIFEST$"):
            self.check()

    def test_a_build_removes_a_stale_root_plugin_json(self):
        (self.target / "plugin.json").write_text("{}")
        with patch.object(self.builder, "TARGET", self.target), contextlib.redirect_stdout(io.StringIO()):
            self.builder.build(check=False)
        self.assertFalse(os.path.lexists(self.target / "plugin.json"))
        self.check()

    def test_the_builder_refuses_to_ship_a_root_plugin_json(self):
        before = sorted(str(path.relative_to(self.target)) for path in self.target.rglob("*"))
        with patch.object(self.builder, "TARGET", self.target), \
                patch.object(self.builder, "FILES", (*self.builder.FILES, "plugin.json")):
            with self.assertRaisesRegex(ValueError, "^CODEX_PACKAGE_ROOT_MANIFEST$"):
                self.builder.build(check=False)
        self.assertEqual(sorted(str(path.relative_to(self.target)) for path in self.target.rglob("*")), before)


class ContinuousIntegrationTests(unittest.TestCase):
    def test_ci_holds_the_per_module_coverage_floors(self):
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
        steps = workflow.split("\n      - ")
        gate = [step for step in steps if "tools/coverage_gate.py --check" in step]
        self.assertEqual(len(gate), 1, "exactly one step runs the coverage gate")
        # The gate runs the suite under coverage and pytest; CI installs both, pinned.
        self.assertRegex(gate[0], r"coverage==\d+\.\d+\.\d+")
        self.assertRegex(gate[0], r"pytest==\d+\.\d+\.\d+")
        self.assertIn("runner.os == 'macOS'", gate[0])
        gate_source = (ROOT / "tools" / "coverage_gate.py").read_text()
        self.assertIn('"--check"', gate_source)
        self.assertTrue(re.search(r'"-m", "coverage", "run"', gate_source))
        self.assertTrue(re.search(r'"-m",\s*"pytest"', gate_source))

    def test_the_codex_package_check_still_runs(self):
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
        self.assertIn("python3 tools/build_codex_package.py --check", workflow)


if __name__ == "__main__":
    unittest.main()
