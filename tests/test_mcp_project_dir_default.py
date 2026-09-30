"""A missing workspace_path falls back to CLAUDE_PROJECT_DIR, and only to that.

Claude Code sets CLAUDE_PROJECT_DIR for the stdio servers it spawns. Other
hosts do not, so without it a missing workspace_path is still refused, and the
advertised tool schemas are identical for every host.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "plugins" / "qualixar-jev-decision-layer" / "runtime"
sys.path.insert(0, str(RUNTIME))

from jev_auto import mcp  # noqa: E402
from jev_auto.common import AutoError  # noqa: E402
from jevkit import mcp_server as legacy  # noqa: E402


class ProjectDirDefaultTests(unittest.TestCase):
    def _dispatch(self, args, environment):
        seen = []
        with patch.dict(os.environ, environment, clear=False), \
             patch.object(mcp, "ensure", lambda path: seen.append(path)), \
             patch.object(mcp, "request", lambda path, obj: {"path": path, "op": obj["op"]}):
            if "CLAUDE_PROJECT_DIR" not in environment:
                os.environ.pop("CLAUDE_PROJECT_DIR", None)
            return mcp.dispatch("jev_auto_status", args, legacy), seen

    def test_missing_workspace_path_uses_claude_project_dir(self):
        result, seen = self._dispatch({}, {"CLAUDE_PROJECT_DIR": "/work/project"})
        self.assertEqual(seen, ["/work/project"])
        self.assertEqual(result["health"]["path"], "/work/project")

    def test_an_explicit_workspace_path_wins(self):
        _result, seen = self._dispatch({"workspace_path": "/explicit"}, {"CLAUDE_PROJECT_DIR": "/work/project"})
        self.assertEqual(seen, ["/explicit"])

    def test_without_claude_project_dir_a_missing_path_is_still_refused(self):
        with self.assertRaisesRegex(AutoError, "MCP_ARGUMENTS"):
            self._dispatch({}, {})

    def test_a_relative_or_empty_project_dir_is_ignored(self):
        for value in ("", "relative/dir"):
            with self.subTest(value=value), self.assertRaisesRegex(AutoError, "MCP_ARGUMENTS"):
                self._dispatch({}, {"CLAUDE_PROJECT_DIR": value})

    def test_tools_without_a_workspace_argument_are_untouched(self):
        with patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": "/work/project"}):
            with self.assertRaisesRegex(AutoError, "MCP_ARGUMENTS"):
                mcp.dispatch("jev_recipe_catalog", {"workspace_path": "/x"}, legacy)

    def test_advertised_schemas_still_require_workspace_path(self):
        with patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": "/work/project"}):
            tools = {tool["name"]: tool for tool in mcp.definitions(legacy)}
        self.assertIn("workspace_path", tools["jev_route"]["inputSchema"]["required"])
        self.assertIn("workspace_path", tools["jev_auto_status"]["inputSchema"]["required"])


if __name__ == "__main__":
    unittest.main()
