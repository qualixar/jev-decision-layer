"""`jev host-register` may replace only an older release of its own launcher.

Every version bump moves the versioned plugin-cache path. Before 1.0.11 the
registration refused its own previous entry as a conflict, so a host kept
launching the old release. Anything that is not provably an older copy of the
same launcher is still refused.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "plugins" / "qualixar-jev-decision-layer" / "runtime"
sys.path.insert(0, str(RUNTIME))

from jev_auto import host_mcp  # noqa: E402
from jev_auto.common import AutoError  # noqa: E402

CACHE = Path("/opt/cache/qualixar/qualixar-jev-decision-layer")
NEW = CACHE / "1.0.11" / "scripts" / "launch-jev"


def _document(command: str, **extra) -> dict:
    return {"mcpServers": {host_mcp.SERVER_NAME: {"command": command, "args": [], "env": {}, **extra},
                           "other": {"command": "other"}}}


class SelfUpgradeTests(unittest.TestCase):
    def test_an_older_release_of_the_same_launcher_is_replaced(self):
        merged = host_mcp.merge("antigravity", _document(str(CACHE / "1.0.7" / "scripts" / "launch-jev")), NEW)
        self.assertEqual(merged["mcpServers"][host_mcp.SERVER_NAME]["command"], str(NEW))
        self.assertEqual(merged["mcpServers"]["other"], {"command": "other"})

    def test_semantic_ordering_not_string_ordering(self):
        merged = host_mcp.merge("antigravity", _document(str(CACHE / "1.0.9" / "scripts" / "launch-jev")),
                                CACHE / "1.0.10" / "scripts" / "launch-jev")
        self.assertTrue(merged["mcpServers"][host_mcp.SERVER_NAME]["command"].endswith("1.0.10/scripts/launch-jev"))

    def test_a_downgrade_is_refused(self):
        with self.assertRaisesRegex(AutoError, "HOST_MCP_ENTRY_CONFLICT"):
            host_mcp.merge("antigravity", _document(str(CACHE / "1.0.12" / "scripts" / "launch-jev")), NEW)

    def test_a_different_install_location_is_refused(self):
        other = Path("/elsewhere/qualixar-jev-decision-layer/1.0.7/scripts/launch-jev")
        with self.assertRaisesRegex(AutoError, "HOST_MCP_ENTRY_CONFLICT"):
            host_mcp.merge("antigravity", _document(str(other)), NEW)

    def test_a_different_launcher_name_is_refused(self):
        with self.assertRaisesRegex(AutoError, "HOST_MCP_ENTRY_CONFLICT"):
            host_mcp.merge("antigravity", _document(str(CACHE / "1.0.7" / "scripts" / "evil")), NEW)

    def test_a_non_release_directory_is_refused(self):
        for label in ("latest", "1.0", "1.0.7-rc1", "v1.0.7", "01.0.7"):
            with self.subTest(label=label), self.assertRaisesRegex(AutoError, "HOST_MCP_ENTRY_CONFLICT"):
                host_mcp.merge("antigravity", _document(str(CACHE / label / "scripts" / "launch-jev")), NEW)

    def test_a_user_customised_entry_is_refused_even_at_an_older_release(self):
        old = str(CACHE / "1.0.7" / "scripts" / "launch-jev")
        for extra in ({"env": {"TOKEN": "x"}}, {"args": ["--debug"]}, {"cwd": "/tmp"}):
            with self.subTest(extra=extra), self.assertRaisesRegex(AutoError, "HOST_MCP_ENTRY_CONFLICT"):
                host_mcp.merge("antigravity", _document(old, **extra), NEW)

    def test_a_non_string_command_is_refused(self):
        document = {"mcpServers": {host_mcp.SERVER_NAME: {"command": ["x"], "args": [], "env": {}}}}
        with self.assertRaisesRegex(AutoError, "HOST_MCP_ENTRY_CONFLICT"):
            host_mcp.merge("antigravity", document, NEW)

    def test_a_symlinked_spelling_of_the_same_cache_is_recognised(self):
        with tempfile.TemporaryDirectory() as directory:
            real = Path(directory).resolve() / "real" / "qualixar-jev-decision-layer"
            (real / "1.0.7" / "scripts").mkdir(parents=True)
            (real / "1.0.11" / "scripts").mkdir(parents=True)
            alias = Path(directory).resolve() / "alias"
            alias.symlink_to(real.parent)
            old = alias / "qualixar-jev-decision-layer" / "1.0.7" / "scripts" / "launch-jev"
            new = real / "1.0.11" / "scripts" / "launch-jev"
            merged = host_mcp.merge("antigravity", _document(str(old)), new)
            self.assertEqual(merged["mcpServers"][host_mcp.SERVER_NAME]["command"], str(new))

    def test_dot_dot_segments_cannot_smuggle_a_different_location(self):
        sneaky = Path("/opt/cache/qualixar/qualixar-jev-decision-layer/1.0.7/../../../../elsewhere/"
                      "qualixar-jev-decision-layer/1.0.7/scripts/launch-jev")
        with self.assertRaisesRegex(AutoError, "HOST_MCP_ENTRY_CONFLICT"):
            host_mcp.merge("antigravity", _document(str(sneaky)), NEW)

    def test_the_plan_reports_an_update_and_shows_the_replaced_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            config = workspace / host_mcp.TARGETS["vscode"].workspace_relative
            config.parent.mkdir(parents=True)
            old = {"type": "stdio", "command": str(CACHE / "1.0.7" / "scripts" / "launch-jev"), "args": [], "env": {}}
            config.write_text(json.dumps({"servers": {host_mcp.SERVER_NAME: old}}))
            outcome = host_mcp.plan("vscode", workspace, NEW)
            self.assertEqual(outcome["action"], "update")
            self.assertEqual(outcome["replaced_entry"], old)
            self.assertEqual(outcome["entry"]["command"], str(NEW))


if __name__ == "__main__":
    unittest.main()
