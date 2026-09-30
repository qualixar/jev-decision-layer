"""Keep native Claude and Codex hook discovery isolated by manifest."""

from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PORTABLE = ROOT / "plugins" / "qualixar-jev-decision-layer"


class HookPackagingTests(unittest.TestCase):
    def test_claude_manifest_uses_only_its_narrow_native_hook_file(self):
        manifest = json.loads((PORTABLE / ".claude-plugin/plugin.json").read_text())
        self.assertEqual(manifest["hooks"], "./hooks/claude-hooks.json")
        self.assertFalse((PORTABLE / "hooks/hooks.json").exists())
        hooks = json.loads((PORTABLE / manifest["hooks"]).read_text())["hooks"]
        self.assertEqual(set(hooks), {"SessionStart", "UserPromptSubmit", "SubagentStart"})
        self.assertNotIn("PreToolUse", hooks)
        self.assertNotIn("calibrated", manifest["description"].lower())

    def test_codex_overlay_points_to_codex_hooks_in_the_portable_source(self):
        overlay = json.loads((PORTABLE / ".codex-plugin/plugin.json").read_text())
        self.assertEqual(overlay["hooks"], "./hooks/codex-hooks.json")
        config_path = PORTABLE / overlay["hooks"]
        self.assertTrue(config_path.is_file())
        hooks = json.loads(config_path.read_text())["hooks"]
        self.assertEqual(set(hooks), {"SessionStart", "UserPromptSubmit", "SubagentStart", "PostToolUse"})
        self.assertNotIn("PreToolUse", hooks)

    def test_codex_build_rejects_stale_legacy_hook_path(self):
        source = (ROOT / "tools/build_codex_package.py").read_text()
        self.assertIn('LEGACY_CODEX_HOOK_NAME = Path("hooks/hooks.json")', source)
        self.assertIn('(TARGET / LEGACY_CODEX_HOOK_NAME).unlink(missing_ok=True)', source)


if __name__ == "__main__":
    unittest.main()
