"""Antigravity launcher boundary tests.

Antigravity documents a plugin-root working directory for hooks, but its MCP
configuration reference documents only ``command``, ``args`` and ``env``.  It
does not promise plugin-relative command resolution or a ``cwd`` field.  A
relative MCP command can therefore resolve against an opened workspace on some
host versions.  v1 deliberately ships no Antigravity MCP registration until
that host contract is documented and verified natively.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "qualixar-jev-decision-layer"
HOOK = PLUGIN / "scripts" / "launch-agy-hook"
AGY_MCP_CONFIG = PLUGIN / "mcp_config.json"
AGY_HOOKS_DOCS = Path.home() / ".gemini/antigravity/builtin/skills/agy-customizations/docs/hooks.md"
AGY_MCP_DOCS = Path.home() / ".gemini/antigravity/builtin/skills/agy-customizations/docs/mcp_servers.md"


class AgyLauncherBoundaryTests(unittest.TestCase):
    def test_plugin_does_not_ship_an_undocumented_relative_mcp_launcher(self):
        self.assertFalse(
            AGY_MCP_CONFIG.exists(),
            "Do not register ./scripts/launch-jev for Antigravity until its "
            "plugin-relative MCP command resolution is documented and natively verified.",
        )

    def test_preinvocation_launcher_is_root_bound_under_hostile_cwd(self):
        if AGY_HOOKS_DOCS.is_file():
            self.assertIn(
                "working directory is set to the directory containing `hooks.json`",
                AGY_HOOKS_DOCS.read_text(),
            )
        self.assertTrue(HOOK.is_file())

        with tempfile.TemporaryDirectory() as directory:
            hostile = Path(directory)
            # If a hook command were resolved from an opened workspace, this is
            # the file it would execute.  We invoke the packaged launcher by its
            # absolute plugin path, mirroring Antigravity's documented hook CWD.
            trap = hostile / "scripts" / "launch-agy-hook"
            trap.parent.mkdir()
            trap.write_text("#!/bin/sh\nprintf '%s\\n' WORKSPACE_TRAP\n")
            trap.chmod(0o700)
            result = subprocess.run(
                [str(HOOK)],
                cwd=hostile,
                input=json.dumps({"invocationNum": 1}),
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {})
        self.assertNotIn("WORKSPACE_TRAP", result.stdout)

    def test_mcp_docs_do_not_advertise_a_plugin_root_or_cwd_contract(self):
        if not AGY_MCP_DOCS.is_file():
            self.skipTest("Antigravity host documentation is not installed")
        text = AGY_MCP_DOCS.read_text()
        self.assertIn("**`command`** (string, required)", text)
        self.assertIn("**`args`** (array of strings, optional)", text)
        self.assertIn("**`env`** (object, optional)", text)
        self.assertNotIn("PLUGIN_ROOT", text)
        self.assertNotIn("**`cwd`**", text)


if __name__ == "__main__":
    unittest.main()
