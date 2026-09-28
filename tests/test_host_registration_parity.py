"""Registration parity for Antigravity and VS Code MCP host configs.

These tests prove the package can produce safe host-specific registration
plans and that both entries launch the same MCP server contract. They do not
claim either native host UI has loaded or trusted the server.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "plugins" / "qualixar-jev-decision-layer" / "runtime"
sys.path.insert(0, str(RUNTIME))

from jev_auto import host_mcp  # noqa: E402
from jev_auto.common import AutoError  # noqa: E402


BASELINE_TOOLS = {
    "jev_recipe_catalog",
    "jev_recipe_selftest",
    "jev_setup",
    "jev_route",
    "jev_recipe_try",
    "jev_recall",
}


class NativeConfigShapes(unittest.TestCase):
    def test_platform_registration_selects_checked_in_windows_launcher(self):
        self.assertEqual(host_mcp._launcher_filename("nt"), "launch-jev.cmd")
        self.assertEqual(host_mcp._launcher_filename("posix"), "launch-jev")

    def test_claude_desktop_windows_config_uses_roaming_profile(self):
        home = Path(r"C:\Users\Example User")
        self.assertEqual(
            host_mcp._claude_desktop_config_path("nt", home),
            home / "AppData" / "Roaming" / "Claude" / "claude_desktop_config.json",
        )

    def test_windows_host_registration_uses_running_python_without_shell_or_path_lookup(self):
        with tempfile.TemporaryDirectory() as directory:
            interpreter = Path(directory) / "python.exe"
            interpreter.write_bytes(b"synthetic executable marker")
            python_entry = {
                "command": str(interpreter.resolve()),
                "args": ["-I", "-S", "-B", str(RUNTIME / "auto_entry.py"), "mcp"],
                "env": {},
            }
            with patch.object(host_mcp, "_is_windows", return_value=True), patch.object(host_mcp, "_python_mcp_entry", return_value=python_entry):
                for host in ("vscode", "antigravity", "claude-desktop", "codex-cli", "claude-code-cli"):
                    with self.subTest(host=host):
                        entry = host_mcp.server_entry(host, Path("unused.cmd"))
                        self.assertEqual(entry["command"], str(interpreter.resolve()))
                        self.assertEqual(entry["args"][:3], ["-I", "-S", "-B"])
                        self.assertEqual(entry["args"][-1], "mcp")
                        self.assertTrue(Path(entry["args"][-2]).is_file())
                        self.assertEqual(entry["env"], {})

    def test_windows_json_hosts_show_manual_plan_and_never_write_profile_files(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "mcp_config.json"
            target = replace(host_mcp.TARGETS["antigravity"], user_path=config)
            python_entry = {"command": r"C:\Python\python.exe", "args": ["-I", "-S", "-B", r"C:\Jev\auto_entry.py", "mcp"], "env": {}}
            with patch.object(host_mcp, "_is_windows", return_value=True), \
                    patch.object(host_mcp, "_python_mcp_entry", return_value=python_entry), \
                    patch.dict(host_mcp.TARGETS, {"antigravity": target}):
                plan = host_mcp.plan("antigravity")
                self.assertEqual(plan["action"], "manual-registration-required")
                self.assertIsNone(plan["config_exists"])
                self.assertFalse(config.exists())
                with self.assertRaisesRegex(AutoError, "HOST_MCP_WINDOWS_NATIVE_CONFIG_REQUIRED"):
                    host_mcp.install("antigravity")
                self.assertFalse(config.exists())

    def test_profile_path_resolves_userprofile_after_module_import(self):
        first = Path("/tmp/profile-first")
        second = Path("/tmp/profile-second")
        with patch.dict(os.environ, {"USERPROFILE": str(first), "CODEX_HOME": "", "CLAUDE_CONFIG_DIR": ""}), \
                patch.object(Path, "home", side_effect=lambda: Path(os.environ["USERPROFILE"])):
            self.assertEqual(host_mcp.TARGETS["codex-cli"].config_path(None), first / ".codex" / "config.toml")
            self.assertEqual(host_mcp.TARGETS["claude-code-cli"].config_path(None), first / ".claude.json")
            os.environ["USERPROFILE"] = str(second)
            self.assertEqual(host_mcp.TARGETS["codex-cli"].config_path(None), second / ".codex" / "config.toml")
            self.assertEqual(host_mcp.TARGETS["claude-code-cli"].config_path(None), second / ".claude.json")

    def test_json_host_merge_is_idempotent_and_refuses_a_different_existing_entry(self):
        intended = host_mcp.server_entry("antigravity", Path("/opt/jev/launch-jev"))
        identical = {"mcpServers": {host_mcp.SERVER_NAME: intended, "other": {"command": "other"}}}
        self.assertEqual(
            host_mcp.merge("antigravity", identical, Path("/opt/jev/launch-jev"))["mcpServers"],
            identical["mcpServers"],
        )
        conflicting = {"mcpServers": {host_mcp.SERVER_NAME: {"command": "/old/jev", "args": [], "env": {}}}}
        with self.assertRaisesRegex(AutoError, "HOST_MCP_ENTRY_CONFLICT"):
            host_mcp.merge("antigravity", conflicting, Path("/opt/jev/launch-jev"))

    def test_codex_manual_plan_contains_exact_python_and_runtime_toml(self):
        entry = {
            "command": r"C:\Users\Example User\Python\python.exe",
            "args": ["-I", "-S", "-B", r"C:\Users\Example User\Jev\runtime\auto_entry.py", "mcp"],
            "env": {},
        }
        with patch.object(host_mcp, "_is_windows", return_value=True), \
                patch.object(host_mcp, "_python_mcp_entry", return_value=entry):
            result = host_mcp.plan("codex-cli")
        self.assertEqual(result["action"], "manual-registration-required")
        self.assertEqual(result["registration"], "manual")
        parsed = tomllib.loads(result["manual_config_snippet"])
        self.assertEqual(parsed["mcp_servers"]["qualixar-jev"]["command"], entry["command"])
        self.assertEqual(parsed["mcp_servers"]["qualixar-jev"]["args"], entry["args"])
        self.assertIn("auto_entry.py", result["manual_config_snippet"])

    def test_windows_manual_plans_never_resolve_or_execute_host_binaries(self):
        python_entry = {"command": r"C:\Python\python.exe", "args": ["-I", "-S", "-B", r"C:\Jev\auto_entry.py", "mcp"], "env": {}}
        with patch.object(host_mcp, "_is_windows", return_value=True), \
                patch.object(host_mcp, "_python_mcp_entry", return_value=python_entry), \
                patch.object(shutil, "which") as which, patch.object(subprocess, "run") as run:
            for host in ("codex-cli", "claude-code-cli"):
                plan = host_mcp.plan(host)
                self.assertEqual(plan["action"], "manual-registration-required")
                self.assertEqual(plan["registration"], "manual")
                with self.assertRaisesRegex(AutoError, "HOST_MCP_WINDOWS_NATIVE_CONFIG_REQUIRED"):
                    host_mcp.install(host)
            which.assert_not_called()
            run.assert_not_called()

    def test_claude_manual_plan_contains_exact_json_registration(self):
        entry = {"command": r"C:\Python\python.exe", "args": ["-I", "-S", "-B", r"C:\Jev\auto_entry.py", "mcp"], "env": {}}
        with patch.object(host_mcp, "_is_windows", return_value=True), \
                patch.object(host_mcp, "_python_mcp_entry", return_value=entry):
            result = host_mcp.plan("claude-code-cli")
        self.assertEqual(result["action"], "manual-registration-required")
        parsed = json.loads(result["manual_config_snippet"])
        self.assertEqual(parsed["mcpServers"]["qualixar-jev"], entry)

    def test_antigravity_and_vscode_use_host_native_config_shapes(self):
        vscode = host_mcp.render("vscode", Path("/opt/jev/launch-jev"))
        antigravity = host_mcp.render("antigravity", Path("/opt/jev/launch-jev"))

        self.assertEqual(set(vscode), {"servers"})
        self.assertEqual(set(antigravity), {"mcpServers"})
        vscode_entry = vscode["servers"][host_mcp.SERVER_NAME]
        agy_entry = antigravity["mcpServers"][host_mcp.SERVER_NAME]
        self.assertEqual(vscode_entry["type"], "stdio")
        self.assertNotIn("type", agy_entry)
        for entry in (vscode_entry, agy_entry):
            self.assertEqual(entry["command"], "/opt/jev/launch-jev")
            self.assertEqual(entry["args"], [])
            self.assertEqual(entry["env"], {})

    def test_antigravity_plan_is_read_only_and_names_preserved_servers_only(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "mcp_config.json"
            config_path.write_text(json.dumps({
                "mcpServers": {
                    "existing": {"command": "/usr/bin/existing", "env": {"TOKEN": "secret"}},
                },
                "unrelated": {"setting": True},
            }))
            before = config_path.read_bytes()
            target = replace(host_mcp.TARGETS["antigravity"], user_path=config_path)
            with patch.dict(host_mcp.TARGETS, {"antigravity": target}):
                outcome = host_mcp.plan("antigravity", launcher=Path("/opt/jev/launch-jev"))

            self.assertEqual(config_path.read_bytes(), before)
        self.assertEqual(outcome["config_key"], "mcpServers")
        self.assertEqual(outcome["action"], "add")
        self.assertEqual(outcome["preserved_servers"], ["existing"])
        self.assertFalse(outcome["written"])
        self.assertNotIn("secret", json.dumps(outcome))
        self.assertNotIn("document", outcome)

    def test_antigravity_install_merges_without_touching_other_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "mcp_config.json"
            config_path.write_text(json.dumps({
                "mcpServers": {"existing": {"command": "/usr/bin/existing"}},
                "otherSetting": ["preserve-me"],
            }))
            target = replace(host_mcp.TARGETS["antigravity"], user_path=config_path)
            with patch.dict(host_mcp.TARGETS, {"antigravity": target}):
                result = host_mcp.install("antigravity", launcher=Path("/opt/jev/launch-jev"))

            document = json.loads(config_path.read_text())

        self.assertTrue(result["written"])
        self.assertEqual(document["otherSetting"], ["preserve-me"])
        self.assertEqual(document["mcpServers"]["existing"], {"command": "/usr/bin/existing"})
        self.assertEqual(document["mcpServers"][host_mcp.SERVER_NAME], {
            "command": "/opt/jev/launch-jev", "args": [], "env": {},
        })

    def test_antigravity_refuses_malformed_and_symlinked_configs(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "mcp_config.json"
            target = replace(host_mcp.TARGETS["antigravity"], user_path=config_path)
            with patch.dict(host_mcp.TARGETS, {"antigravity": target}):
                config_path.write_text('{"mcpServers":')
                with self.assertRaises(AutoError):
                    host_mcp.plan("antigravity", launcher=Path("/opt/jev/launch-jev"))

                config_path.unlink()
                elsewhere = Path(directory) / "elsewhere.json"
                elsewhere.write_text("{}")
                config_path.symlink_to(elsewhere)
                with self.assertRaises(AutoError):
                    host_mcp.install("antigravity", launcher=Path("/opt/jev/launch-jev"))


class SharedMcpBaseline(unittest.TestCase):
    def test_packaged_launcher_exposes_the_common_explicit_operations(self):
        launcher = host_mcp.launcher_path()
        requests = (
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        result = subprocess.run(
            [str(launcher)],
            input="".join(json.dumps(request) + "\n" for request in requests),
            capture_output=True,
            text=True,
            cwd=ROOT,
            timeout=30,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        responses = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(responses[0]["result"]["serverInfo"]["name"], host_mcp.SERVER_NAME)
        names = {tool["name"] for tool in responses[1]["result"]["tools"]}
        self.assertTrue(BASELINE_TOOLS <= names, sorted(BASELINE_TOOLS - names))


if __name__ == "__main__":
    unittest.main()
