"""Keep Claude Code's packaged hook surface narrow and independently safe."""

from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "qualixar-jev-decision-layer"


class ClaudePluginRegistrationTests(unittest.TestCase):
    def test_manifest_points_to_real_claude_mcp_and_hook_files(self):
        manifest = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())
        self.assertEqual(manifest["mcpServers"], "./.mcp.json")
        self.assertEqual(manifest["hooks"], "./hooks/claude-hooks.json")
        self.assertTrue((PLUGIN / manifest["mcpServers"]).is_file())
        self.assertTrue((PLUGIN / manifest["hooks"]).is_file())

    def test_registered_hooks_are_only_fixed_advisory_lifecycle_events(self):
        config = json.loads((PLUGIN / "hooks" / "claude-hooks.json").read_text())
        hooks = config["hooks"]
        self.assertEqual(set(hooks), {"SessionStart", "UserPromptSubmit"})
        for event, groups in hooks.items():
            with self.subTest(event=event):
                self.assertEqual(len(groups), 1)
                commands = groups[0]["hooks"]
                self.assertEqual(len(commands), 1)
                command = commands[0]
                self.assertEqual(command["type"], "command")
                self.assertEqual(command["command"], "${CLAUDE_PLUGIN_ROOT}/scripts/launch-claude-hook")
                self.assertEqual(command["timeout"], 20)
                self.assertNotIn("matcher", groups[0])
                self.assertNotIn("PreToolUse", json.dumps(command))

    def test_mcp_server_uses_plugin_relative_launcher_without_credentials(self):
        config = json.loads((PLUGIN / ".mcp.json").read_text())
        server = config["mcpServers"]["qualixar-jev"]
        self.assertEqual(server["type"], "stdio")
        self.assertEqual(server["command"], "${CLAUDE_PLUGIN_ROOT}/scripts/launch-jev")
        self.assertEqual(server["args"], [])
        self.assertEqual(server["env"], {})


if __name__ == "__main__":
    unittest.main()
