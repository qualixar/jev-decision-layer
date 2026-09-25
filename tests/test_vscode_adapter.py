"""VS Code host adapter, and the local route that must stay off until asked.

Both are about not surprising the user: a config file they own is merged rather
than replaced, and a local model route is never enabled by shipping a default.
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
sys.path.insert(0, str(RUNTIME / "src"))

from adl.api.host_inventory import inventory_hosts  # noqa: E402
from jev_auto.common import AutoError  # noqa: E402
from jev_auto.settings import DEFAULTS, make_policy, validate_policy  # noqa: E402
from jev_auto.vscode_adapter import CONFIG_RELPATH, SERVER_NAME, install, merge, plan, render  # noqa: E402

LAUNCHER = Path("/opt/qualixar/launch-jev")


class ConfigFormat(unittest.TestCase):
    def test_servers_is_the_top_level_key_not_mcpservers(self):
        """VS Code ignores `mcpServers` silently: no error, no server."""
        document = render(LAUNCHER)
        self.assertEqual(set(document), {"servers"})
        self.assertNotIn("mcpServers", document)

    def test_the_entry_is_a_stdio_server_with_an_absolute_command(self):
        entry = render(LAUNCHER)["servers"][SERVER_NAME]
        self.assertEqual(entry["type"], "stdio")
        self.assertTrue(Path(entry["command"]).is_absolute())
        self.assertNotIn("${", entry["command"], "VS Code expands no plugin-root variable")

    def test_the_shipped_launcher_exists_and_is_executable(self):
        launcher = RUNTIME.parent / "scripts" / "launch-jev"
        self.assertTrue(launcher.is_file())
        self.assertTrue(launcher.stat().st_mode & 0o111, "VS Code spawns this directly")


class MergingSomeoneElsesFile(unittest.TestCase):
    def test_existing_servers_and_unrelated_keys_survive(self):
        existing = {"servers": {"other": {"type": "stdio", "command": "/bin/echo"}}, "inputs": [{"id": "key"}]}
        merged = merge(existing, LAUNCHER)
        self.assertEqual(merged["inputs"], [{"id": "key"}])
        self.assertEqual(merged["servers"]["other"], {"type": "stdio", "command": "/bin/echo"})
        self.assertIn(SERVER_NAME, merged["servers"])

    def test_plan_writes_nothing_and_names_what_it_would_keep(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            config = workspace / CONFIG_RELPATH
            config.parent.mkdir(parents=True)
            config.write_text(json.dumps({"servers": {"other": {"command": "/bin/echo"}}}))
            before = config.read_text()
            outcome = plan(workspace, LAUNCHER)
            self.assertEqual(outcome["action"], "add")
            self.assertEqual(outcome["preserved_servers"], ["other"])
            self.assertFalse(outcome["written"])
            self.assertEqual(config.read_text(), before)

    def test_installing_twice_is_a_no_op_the_second_time(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            first = install(workspace, LAUNCHER)
            self.assertEqual(first["action"], "create")
            self.assertTrue(first["written"])
            second = install(workspace, LAUNCHER)
            self.assertEqual(second["action"], "unchanged")
            self.assertFalse(second["written"])

    def test_a_file_that_does_not_parse_is_refused_not_rewritten(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            config = workspace / CONFIG_RELPATH
            config.parent.mkdir(parents=True)
            config.write_text('{"servers": {"other": ')  # truncated by a bad hand-edit
            before = config.read_text()
            with self.assertRaises(AutoError):
                install(workspace, LAUNCHER)
            self.assertEqual(config.read_text(), before, "a file we cannot read is not ours to replace")

    def test_a_symlinked_config_is_never_followed(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            outside = workspace / "elsewhere.json"
            outside.write_text("{}")
            config = workspace / CONFIG_RELPATH
            config.parent.mkdir(parents=True)
            config.symlink_to(outside)
            with self.assertRaises(AutoError):
                install(workspace, LAUNCHER)


class HostInventory(unittest.TestCase):
    def test_vscode_now_reports_a_shipped_adapter(self):
        row = next(host for host in inventory_hosts() if host["id"] == "vscode")
        self.assertTrue(row["native_adapter"])

    def test_shipping_an_adapter_is_not_a_conformance_claim(self):
        for row in inventory_hosts():
            self.assertEqual(row["native_status"], "NOT_RUN", row["id"])
            self.assertFalse(row["auto_mode_allowed"], row["id"])


class LocalRouteStaysOff(unittest.TestCase):
    """Laya is wired, and shipping it must not switch it on for anyone."""

    def test_the_shipped_default_is_off(self):
        self.assertFalse(DEFAULTS["local_laya_enabled"])

    def test_a_new_workspace_gets_the_hosted_route(self):
        with tempfile.TemporaryDirectory() as directory:
            policy = make_policy(Path(directory), "typesafe")
            self.assertFalse(policy["local_laya_enabled"])
            self.assertEqual(policy["provider"], "typesafe")

    def test_turning_it_on_without_an_attested_install_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            policy = make_policy(workspace, "typesafe")
            policy["local_laya_enabled"] = True
            with self.assertRaises(AutoError) as caught:
                validate_policy(policy, workspace)
            self.assertIn("LOCAL_ROUTE_NOT_ATTESTED", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
