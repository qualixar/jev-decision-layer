"""Public plugin files are self-contained and manifest-bound."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "qualixar-jev-decision-layer"
CODEX_PACKAGE = ROOT / "plugins" / "qualixar-jev-codex"


class PluginPackageTests(unittest.TestCase):
    def test_browser_skill_uses_selective_host_agnostic_jev_routing(self):
        skill = (PLUGIN / "skills/jev-browser-choice/SKILL.md").read_text()
        self.assertIn("jev_route", skill)
        self.assertIn("No Jev call", skill)
        self.assertIn("observed", skill)
        self.assertIn("re-observe", skill)
        self.assertNotIn("loadConfig", skill)
        self.assertNotIn("createSession", skill)
        self.assertNotIn("node:net", skill)

    def test_marketplace_uses_codex_hook_compatible_package(self):
        marketplace = json.loads((ROOT / ".agents/plugins/marketplace.json").read_text())
        self.assertEqual(marketplace["plugins"][0]["source"]["path"], "./plugins/qualixar-jev-codex")
        self.assertFalse((CODEX_PACKAGE / "plugin.json").exists())
        overlay = json.loads((CODEX_PACKAGE / ".codex-plugin/plugin.json").read_text())
        self.assertEqual(overlay["hooks"], "./hooks/hooks.json")
        self.assertTrue((CODEX_PACKAGE / overlay["hooks"]).is_file())

    def test_codex_and_claude_mcp_descriptors_start_the_same_bundled_server(self):
        codex = json.loads((CODEX_PACKAGE / ".mcp.json").read_text())["mcpServers"]["qualixar-jev"]
        claude = json.loads((PLUGIN / ".mcp.json").read_text())["mcpServers"]["qualixar-jev"]
        self.assertEqual(codex["command"], "./scripts/launch-jev")
        self.assertEqual(codex["cwd"], ".")
        self.assertEqual(claude["command"], "${CLAUDE_PLUGIN_ROOT}/scripts/launch-jev")
        self.assertEqual(
            hashlib.sha256((CODEX_PACKAGE / "scripts/launch-jev").read_bytes()).digest(),
            hashlib.sha256((PLUGIN / "scripts/launch-jev").read_bytes()).digest(),
        )

    def test_codex_mcp_descriptor_launches_and_answers_offline_initialize(self):
        config = json.loads((CODEX_PACKAGE / ".mcp.json").read_text())["mcpServers"]["qualixar-jev"]
        request = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                   "params": {"protocolVersion": "2025-06-18"}}
        result = subprocess.run(
            [config["command"], *config["args"]], input=json.dumps(request) + "\n",
            capture_output=True, text=True, cwd=CODEX_PACKAGE / config["cwd"], timeout=15,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout.splitlines()[0])["result"]["serverInfo"]["name"],
                         "qualixar-jev-decision-layer")

    def test_readme_relative_links_and_release_images_exist(self):
        readme = (ROOT / "README.md").read_text()
        self.assertIn('src="docs/assets/jev-mark.svg"', readme)
        self.assertIn('width="56" height="56"', readme)
        self.assertIn('src="docs/assets/hero.svg"', readme)
        self.assertIn('width="820"', readme)
        targets = re.findall(r"\]\(([^)]+)\)", readme)
        for target in targets:
            if target.startswith(("https://", "http://")):
                continue
            with self.subTest(target=target):
                self.assertTrue((ROOT / target).is_file())
        social = (ROOT / "docs/assets/social-preview.png").read_bytes()
        self.assertTrue(social.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual((int.from_bytes(social[16:20]), int.from_bytes(social[20:24])), (1280, 640))
        self.assertLess(len(social), 1_000_000)
        self.assertTrue((ROOT / "docs/assets/decision-flow.gif").read_bytes().startswith(b"GIF89a"))

    def test_agent_capability_manifest_matches_packaged_counts_and_limits(self):
        capabilities = json.loads((ROOT / "docs" / "capabilities.json").read_text())
        schema = json.loads((ROOT / "docs" / "capabilities.schema.json").read_text())
        recipes = json.loads((PLUGIN / "runtime" / "recipe_catalog.json").read_text())
        self.assertEqual(schema["properties"]["schema_version"]["const"], capabilities["schema_version"])
        self.assertEqual(capabilities["recipe_counts"]["legacy_contracts"], len(list((PLUGIN / "runtime" / "fixtures").iterdir())))
        self.assertEqual(capabilities["recipe_counts"]["additional_specifications"], len(recipes["recipes"]))
        self.assertEqual({mode["id"] for mode in capabilities["decision_modes"]},
                         {"jev-public", "jev-internal", "jev-maximum", "hybrid", "laya-only"})
        self.assertFalse(capabilities["model_answer_authorizes_execution"])
        self.assertFalse(capabilities["automatic_all_internal_choices_intercepted"])
        codex = next(host for host in capabilities["host_support"] if host["id"] == "codex-desktop")
        self.assertTrue(codex["explicit_jev_receipt_verified"])
        self.assertFalse(codex["automatic_jev_hook_receipt_verified"])
        self.assertFalse(codex["local_laya_receipt_verified"])
        self.assertIsNone(capabilities["measured_host_token_savings"])
        self.assertIsNone(capabilities["measured_host_cost_savings"])

    def test_hermes_native_adapter_accepts_receipt_bound_jev_guidance(self):
        spec = importlib.util.spec_from_file_location("qualixar_jev_hermes_plugin", PLUGIN / "__init__.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        guidance = "Qualixar Jev decision (advisory):\nReceipt: " + "a" * 64 + "\n- skill:skills/review/SKILL.md"
        result = module.prepare_context(
            "Please review and test the synthetic routing implementation for this project.",
            runner=lambda _payload: {"context": guidance},
        )
        self.assertEqual(result, {"context": guidance})

    def test_hermes_plugin_registers_decision_tools_skills_and_hook(self):
        spec = importlib.util.spec_from_file_location("qualixar_jev_hermes_plugin", PLUGIN / "__init__.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        class FakeContext:
            def __init__(self):
                self.tools = []
                self.skills = []
                self.hooks = []

            def register_tool(self, **kwargs):
                self.tools.append(kwargs)

            def register_skill(self, name, path):
                self.skills.append((name, path))

            def register_hook(self, name, callback):
                self.hooks.append(name)

        context = FakeContext()
        module.register(context)
        self.assertTrue({"jev_auto_status", "jev_route", "jev_recipe_catalog", "jev_recipe_try", "jev_review_diff"}
                        <= {tool["name"] for tool in context.tools})
        self.assertEqual({name for name, _ in context.skills}, {"jev-decision-guide", "jev-browser-choice", "jev-use-cases"})
        self.assertEqual(context.hooks, ["pre_llm_call"])

    def test_identity_and_runtime_hashes(self):
        plugin = json.loads((PLUGIN / "plugin.json").read_text())
        overlay = json.loads((PLUGIN / ".codex-plugin" / "plugin.json").read_text())
        self.assertEqual(plugin["name"], "qualixar-jev-decision-layer")
        self.assertEqual(overlay["name"], plugin["name"])
        self.assertEqual(plugin["version"], "1.0.5")
        self.assertEqual(overlay["version"], plugin["version"])
        self.assertEqual(overlay["hooks"], "./hooks/hooks.json")
        self.assertTrue((PLUGIN / overlay["hooks"]).is_file())
        extension = plugin["extensions"]["com.openai"]
        self.assertEqual(extension["hooks"], overlay["hooks"])
        self.assertEqual(extension["interface"], overlay["interface"])
        runtime = PLUGIN / "runtime"
        manifest = json.loads((runtime / "RUNTIME_MANIFEST.json").read_text())
        self.assertEqual(manifest["adapter_version"], "1.0.0")
        prefix = "plugins/qualixar-jev-decision-layer/runtime/"
        tracked = subprocess.check_output(
            ["git", "ls-files", "--", prefix], cwd=ROOT, text=True
        ).splitlines()
        actual = {name[len(prefix):] for name in tracked if name != prefix + "RUNTIME_MANIFEST.json"}
        self.assertFalse(any(name.endswith(".pyc") or "__pycache__" in Path(name).parts for name in actual))
        self.assertEqual(actual, set(manifest["files"]))
        for name, expected in manifest["files"].items():
            with self.subTest(file=name):
                path = runtime / name
                self.assertFalse(path.is_symlink())
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), expected)

    def test_upstream_notices_and_user_docs_exist(self):
        for relative in ("README.md", "LICENSE", "docs/USE_CASES.md", "docs/GETTING_STARTED.md",
                         "docs/SECURITY.md", "docs/ROLLBACK.md", "plugins/qualixar-jev-decision-layer/THIRD_PARTY_NOTICES.md",
                         "plugins/qualixar-jev-decision-layer/licenses/jev-browser-use-MIT.txt",
                         "plugins/qualixar-jev-decision-layer/licenses/winnow-MIT.txt"):
            with self.subTest(file=relative):
                self.assertTrue((ROOT / relative).is_file())


if __name__ == "__main__":
    unittest.main()
