"""Native Windows checks for host-generated config and stdio server entries."""
from __future__ import annotations

import json
import os
import tempfile
import subprocess
import sys
import unittest
import tomllib
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "plugins" / "qualixar-jev-decision-layer" / "runtime"
sys.path.insert(0, str(RUNTIME))


class GeneratedMcpEntryHandshakeTests(unittest.TestCase):
    def test_isolated_entry_bootstrap_handshakes_without_importing_from_cwd(self):
        from jev_auto import host_mcp

        actual_interpreter = sys.executable
        with tempfile.TemporaryDirectory(prefix="jev-python-entry-") as temporary:
            # Exercise the Windows-generated argument vector on this platform.
            # The marker only satisfies the Windows executable-path validation;
            # the subprocess uses this platform's real interpreter.
            fake_windows_interpreter = Path(temporary) / "python.exe"
            fake_windows_interpreter.write_bytes(b"test marker")
            with patch.object(host_mcp.sys, "executable", str(fake_windows_interpreter)):
                entry = host_mcp._python_mcp_entry()

            self.assertEqual(entry["command"], str(fake_windows_interpreter.resolve()))
            self.assertEqual(entry["args"][:4], ["-I", "-S", "-B", "-c"])
            self.assertEqual(entry["args"][-3], str(RUNTIME))
            self.assertEqual(entry["args"][-2], str(RUNTIME / "auto_entry.py"))
            self.assertEqual(entry["args"][-1], "mcp")

            initialize = {
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18"},
            }
            list_tools = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
            process = subprocess.run(
                [actual_interpreter, *entry["args"]],
                cwd=temporary,
                input=json.dumps(initialize) + "\n" + json.dumps(list_tools) + "\n",
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=20,
                check=False,
            )
        self.assertEqual(process.returncode, 0, process.stderr)
        replies = [json.loads(line) for line in process.stdout.splitlines() if line.strip()]
        self.assertEqual([reply["id"] for reply in replies], [1, 2])
        names = {tool["name"] for tool in replies[1]["result"]["tools"]}
        self.assertIn("jev_setup", names)
        self.assertIn("jev_recipe_selftest", names)


@unittest.skipUnless(os.name == "nt", "requires Windows host registration paths")
class WindowsHostRegistrationNativeTests(unittest.TestCase):
    def test_generated_absolute_python_entry_completes_mcp_handshake(self):
        from jev_auto import host_mcp

        entry = host_mcp.server_entry("claude-code-cli")
        expected_interpreter = str(Path(sys.executable).resolve())
        self.assertEqual(entry["command"], expected_interpreter)
        self.assertEqual(entry["args"][:4], ["-I", "-S", "-B", "-c"])
        self.assertEqual(entry["args"][-3], str(RUNTIME))
        self.assertEqual(entry["args"][-2], str(RUNTIME / "auto_entry.py"))
        self.assertEqual(entry["args"][-1], "mcp")

        process = subprocess.Popen(
            [entry["command"], *entry["args"]],
            cwd=ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            env=os.environ.copy(),
        )
        initialize = {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18"},
        }
        list_tools = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        try:
            stdout, stderr = process.communicate(
                json.dumps(initialize) + "\n" + json.dumps(list_tools) + "\n",
                timeout=20,
            )
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
        self.assertEqual(process.returncode, 0, stderr)
        replies = [json.loads(line) for line in stdout.splitlines() if line.strip()]
        self.assertEqual([reply["id"] for reply in replies], [1, 2])
        names = {tool["name"] for tool in replies[1]["result"]["tools"]}
        self.assertIn("jev_setup", names)
        self.assertIn("jev_recipe_selftest", names)

    def test_fake_profile_plans_are_manual_and_registration_never_writes(self):
        from jev_auto import host_mcp
        from jev_auto.common import AutoError

        with tempfile.TemporaryDirectory(prefix="jev profile Ω ") as temporary:
            profile = Path(temporary) / "Isolated User Profile"
            profile.mkdir()
            claude_config = profile / ".claude.json"
            codex_config = profile / ".codex" / "config.toml"

            # The module is imported before this environment patch. Target
            # paths must still follow the isolated profile supplied at call time.
            with patch.dict(os.environ, {"USERPROFILE": str(profile), "HOME": str(profile)}):
                claude_plan = host_mcp.plan("claude-code-cli")
                codex_plan = host_mcp.plan("codex-cli")
                self.assertEqual(Path(claude_plan["config_path"]), claude_config)
                self.assertEqual(Path(codex_plan["config_path"]), codex_config)
                self.assertEqual(claude_plan["action"], "manual-registration-required")
                self.assertEqual(codex_plan["action"], "manual-registration-required")
                self.assertEqual(claude_plan["registration"], "manual")
                self.assertEqual(codex_plan["registration"], "manual")

                entry = codex_plan["entry"]
                self.assertEqual(entry["command"], str(Path(sys.executable).resolve()))
                self.assertEqual(entry["args"][:4], ["-I", "-S", "-B", "-c"])
                self.assertEqual(entry["args"][-3], str(RUNTIME))
                self.assertEqual(entry["args"][-2], str(RUNTIME / "auto_entry.py"))
                self.assertEqual(entry["args"][-1], "mcp")
                codex_snippet = tomllib.loads(codex_plan["manual_config_snippet"])
                self.assertEqual(codex_snippet["mcp_servers"]["qualixar-jev"], {
                    "command": entry["command"], "args": entry["args"],
                })
                claude_snippet = json.loads(claude_plan["manual_config_snippet"])
                self.assertEqual(claude_snippet["mcpServers"]["qualixar-jev"], claude_plan["entry"])

                with patch("subprocess.run", side_effect=AssertionError("Windows registration must not execute")):
                    for host in ("codex-cli", "claude-code-cli"):
                        with self.subTest(host=host):
                            with self.assertRaisesRegex(AutoError, "HOST_MCP_WINDOWS_NATIVE_CONFIG_REQUIRED"):
                                host_mcp.install(host)
                self.assertFalse(claude_config.exists())
                self.assertFalse(codex_config.exists())

    def test_codex_and_claude_profile_overrides_resolve_at_call_time(self):
        from jev_auto import host_mcp

        with tempfile.TemporaryDirectory(prefix="jev-profile-overrides-") as temporary:
            root = Path(temporary)
            codex_home = root / "codex home"
            claude_home = root / "claude home"
            with patch.dict(os.environ, {
                "CODEX_HOME": str(codex_home),
                "CLAUDE_CONFIG_DIR": str(claude_home),
            }):
                self.assertEqual(
                    Path(host_mcp.plan("codex-cli")["config_path"]),
                    codex_home / "config.toml",
                )
                self.assertEqual(
                    Path(host_mcp.plan("claude-code-cli")["config_path"]),
                    claude_home / ".claude.json",
                )


if __name__ == "__main__":
    unittest.main()
