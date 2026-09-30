"""The policy notice must work in the processes users actually see it through.

Hosts start the Jev server and the setup wizard with a reduced environment.
The Claude desktop app passes stdio servers only a small set of variables, so
a CLAUDE_CONFIG_DIR set for GUI apps with `launchctl setenv` never reaches
them, and the wizard launcher passes only an allow-list. These tests hold the
notice to the environment each surface really gets.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "qualixar-jev-decision-layer"
RUNTIME = PLUGIN / "runtime"
sys.path.insert(0, str(RUNTIME))


_HOOKS_ONLY = {"allowManagedHooksOnly": True}


class WizardEnvironmentTests(unittest.TestCase):
    """The wizard launcher's allow-list must carry the Claude configuration folder."""

    def _launch_environment(self, extra):
        from jev_auto import mcp

        seen = {}

        def popen(command, **kwargs):
            seen.update(kwargs["env"])
            raise OSError("stop after capturing the environment")

        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(os.environ, extra, clear=False), \
                patch.object(mcp, "workspace", side_effect=lambda value: Path(value)), \
                patch.object(mcp.subprocess, "Popen", side_effect=popen):
            with self.assertRaises(Exception):
                mcp._open_setup(directory)
        return seen

    def test_the_configuration_folder_reaches_the_wizard(self):
        seen = self._launch_environment({"CLAUDE_CONFIG_DIR": "/custom/claude-config"})
        self.assertEqual(seen.get("CLAUDE_CONFIG_DIR"), "/custom/claude-config")

    def test_the_allow_list_still_drops_everything_else(self):
        seen = self._launch_environment({"CLAUDE_CONFIG_DIR": "/c", "TYPESAFE_API_KEY": "synthetic-key-0000",
                                         "OPENROUTER_API_KEY": "synthetic-key-1111", "SOME_TOKEN": "x"})
        self.assertNotIn("TYPESAFE_API_KEY", seen)
        self.assertNotIn("OPENROUTER_API_KEY", seen)
        self.assertNotIn("SOME_TOKEN", seen)


class RealLauncherTests(unittest.TestCase):
    """Run the shipped `scripts/jev doctor` with the reduced environment a host gives."""

    LAUNCHER = PLUGIN / "scripts" / "jev"

    def _doctor(self, config: Path, home: Path, workspace: Path):
        environment = {"HOME": str(home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin",
                       "CLAUDE_CONFIG_DIR": str(config), "XDG_STATE_HOME": str(home / "state")}
        result = subprocess.run([str(self.LAUNCHER), "doctor", "--workspace", str(workspace)], cwd="/",
                                env=environment, capture_output=True, text=True, timeout=120, check=False)
        return json.loads(result.stdout)

    def test_doctor_names_a_policy_in_a_custom_configuration_folder_and_is_silent_without_one(self):
        manifest = json.loads((RUNTIME / "RUNTIME_MANIFEST.json").read_text())
        if "jev_auto/host_policy.py" not in manifest.get("files", {}):
            self.skipTest("runtime manifest resealing is owned by the package lane")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home, workspace, config, empty = root / "home", root / "project", root / "cfg", root / "empty"
            for folder in (home, workspace, config, empty):
                folder.mkdir()
            (config / "remote-settings.json").write_text(json.dumps(
                {**_HOOKS_ONLY, "companyAnnouncements": ["PRIVATE-MARKER"]}))
            flagged = self._doctor(config, home, workspace)
            plain = self._doctor(empty, home, workspace)
        notice = [check for check in flagged["checks"] if check["id"] == "claude_code_policy"]
        self.assertEqual(len(notice), 1, flagged)
        self.assertEqual(notice[0]["status"], "NOTICE")
        self.assertEqual(notice[0]["codes"], ["PLUGIN_HOOKS_BLOCKED"])
        self.assertEqual(notice[0]["sources"][0]["location"], str(config / "remote-settings.json"))
        self.assertNotIn("PRIVATE-MARKER", json.dumps(flagged))
        self.assertEqual([check["id"] for check in plain["checks"]],
                         ["runtime_manifest", "python", "workspace_policy", "offline_gate",
                          "host_surface", "receipt_index"])
        self.assertEqual(flagged["overall"], plain["overall"])


if __name__ == "__main__":
    unittest.main()
