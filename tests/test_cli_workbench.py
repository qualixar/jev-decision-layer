"""CLI launch and registration coverage for the local recipe workbench."""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "plugins" / "qualixar-jev-decision-layer" / "runtime"
sys.path.insert(0, str(RUNTIME))

import jev_auto.cli as cli  # noqa: E402


class CliWorkbenchTests(unittest.TestCase):
    def run_cli(self, argv: list[str]) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = cli.main(argv)
        return result, stdout.getvalue(), stderr.getvalue()

    def test_workbench_opens_ephemeral_loopback_url_and_clears_session_on_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            service = object()
            server = Mock()
            server.server_port = 43127
            server.session = SimpleNamespace(token="session-token", csrf="csrf-token", expires_at=123.0)
            server.reviews = {"review": ("value", 123.0, False)}
            server.serve_forever.side_effect = KeyboardInterrupt

            with patch("jev_auto.recipe_workbench.RecipeWorkbench", return_value=service) as service_factory, \
                    patch("src.adl.api.recipe_workbench_server.WorkbenchServer", return_value=server) as server_factory, \
                    patch("webbrowser.open", return_value=True) as browser_open:
                result, stdout, stderr = self.run_cli(["workbench", "--workspace", str(workspace)])

        expected_url = "http://127.0.0.1:43127/"
        self.assertEqual(result, 0)
        self.assertIn(expected_url, stdout)
        self.assertEqual(stderr, "")
        service_factory.assert_called_once_with(workspace.resolve())
        server_factory.assert_called_once_with(("127.0.0.1", 0), service)
        server.serve_forever.assert_called_once()
        browser_open.assert_called_once_with(expected_url)
        server.server_close.assert_called_once()
        self.assertEqual(server.session.token, "")
        self.assertEqual(server.session.csrf, "")
        self.assertEqual(server.session.expires_at, 0)
        self.assertEqual(server.reviews, {})

    def test_browser_open_failure_does_not_prevent_manual_workbench_url(self):
        with tempfile.TemporaryDirectory() as directory:
            server = Mock()
            server.server_port = 43210
            server.session = SimpleNamespace(token="t", csrf="c", expires_at=1.0)
            server.reviews = {}
            server.serve_forever.side_effect = KeyboardInterrupt
            with patch("jev_auto.recipe_workbench.RecipeWorkbench"), \
                    patch("src.adl.api.recipe_workbench_server.WorkbenchServer", return_value=server), \
                    patch("webbrowser.open", side_effect=RuntimeError("no browser")):
                result, stdout, _stderr = self.run_cli(["workbench", "--workspace", directory])

        self.assertEqual(result, 0)
        self.assertIn("http://127.0.0.1:43210/", stdout)
        server.server_close.assert_called_once()


class CliWindowsHostRegisterTests(unittest.TestCase):
    def run_cli(self, argv: list[str]) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = cli.main(argv)
        return result, stdout.getvalue(), stderr.getvalue()

    def test_windows_host_registration_is_plan_only_without_write(self):
        from jev_auto import host_mcp

        for host in ("codex-cli", "claude-code-cli"):
            with self.subTest(host=host), patch.object(
                host_mcp, "plan", return_value={"host": host, "action": "create", "written": False}
            ) as planned, patch.object(host_mcp, "install") as installed:
                result, stdout, stderr = self.run_cli(["host-register", "--host", host])

            self.assertEqual(result, 0)
            self.assertEqual(planned.call_args.args, (host, None))
            installed.assert_not_called()
            self.assertFalse(json.loads(stdout)["written"])
            self.assertIn("Nothing written", stderr)

    def test_windows_host_registration_requires_explicit_write(self):
        from jev_auto import host_mcp

        for host in ("codex-cli", "claude-code-cli"):
            with self.subTest(host=host), patch.object(
                host_mcp, "install", return_value={"host": host, "action": "create", "written": True}
            ) as installed, patch.object(host_mcp, "plan") as planned:
                result, stdout, stderr = self.run_cli(["host-register", "--host", host, "--write"])

            self.assertEqual(result, 0)
            self.assertTrue(json.loads(stdout)["written"])
            self.assertEqual(stderr, "")
            installed.assert_called_once_with(host, None)
            planned.assert_not_called()


if __name__ == "__main__":
    unittest.main()
