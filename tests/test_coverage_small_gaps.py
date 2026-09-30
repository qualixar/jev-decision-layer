"""Refusal paths that line coverage had marked as untested.

Each test pins a specific guard: it asserts the exact refusal, and would fail
if the guard were removed. None of them makes a provider or network call, and
state lives under a temporary XDG_STATE_HOME.
"""

from __future__ import annotations

import http.client
import io
import os
import re
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "plugins" / "qualixar-jev-decision-layer" / "runtime"
sys.path.insert(0, str(RUNTIME))

from jev_auto import claude_hook, host_mcp, mcp  # noqa: E402
from jev_auto.common import AutoError  # noqa: E402
from jevkit import providers  # noqa: E402
from src.adl.api import keychain, setup_server  # noqa: E402
from src.adl.api.setup_controller import SetupController  # noqa: E402


class _TempState(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        env = patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "state")})
        env.start()
        self.addCleanup(env.stop)


class ClaudeHookGuidanceGuardTests(_TempState):
    def test_a_path_too_long_to_quote_suppresses_guidance_entirely(self):
        state = {"state": "enrolled", "scope": "exact", "workspace": self.root,
                 "policy": {"generic_query_enabled": True, "provider": "typesafe",
                            "data_classification": "public"}}
        event = {"hook_event_name": "SessionStart", "cwd": str(self.root)}
        self.assertIn("enrolled for this workspace", claude_hook.handle(event, state_loader=lambda _p: state))
        with patch.object(claude_hook, "MAX_PATH_CHARS", 4):
            self.assertEqual(claude_hook.handle(event, state_loader=lambda _p: state), "")


class HostRegistrationGuardTests(unittest.TestCase):
    def test_a_target_with_no_config_location_is_refused(self):
        target = host_mcp.Target("nowhere", "mcpServers", False)
        with self.assertRaisesRegex(AutoError, "^HOST_MCP_CONFIG_UNREADABLE$"):
            target.config_path(None)

    def test_the_windows_interpreter_entry_refuses_a_non_exe_interpreter(self):
        with tempfile.TemporaryDirectory() as directory:
            fake = Path(directory) / "python3"
            fake.write_text("")
            with patch.object(host_mcp.sys, "executable", str(fake)), \
                 self.assertRaisesRegex(AutoError, "^HOST_MCP_PYTHON_INTERPRETER_INVALID$"):
                host_mcp._python_mcp_entry()

    def test_windows_only_hosts_are_refused_elsewhere(self):
        with patch.object(host_mcp, "_is_windows", return_value=False):
            for host in ("codex-cli", "claude-code-cli"):
                with self.subTest(host=host), self.assertRaisesRegex(AutoError, "^HOST_MCP_WINDOWS_ONLY$"):
                    host_mcp._target(host)

    def _old_entry(self, command):
        return {"mcpServers": {host_mcp.SERVER_NAME: {"command": command, "args": [], "env": {}}}}

    def test_a_relative_older_command_is_never_treated_as_our_own_release(self):
        new = Path("/opt/cache/qualixar-jev-decision-layer/1.0.11/scripts/launch-jev")
        with self.assertRaisesRegex(AutoError, "^HOST_MCP_ENTRY_CONFLICT$"):
            host_mcp.merge("antigravity",
                           self._old_entry("cache/qualixar-jev-decision-layer/1.0.7/scripts/launch-jev"), new)

    def test_a_path_that_cannot_be_resolved_is_a_conflict_not_a_crash(self):
        cache = Path("/opt/cache/qualixar-jev-decision-layer")
        document = self._old_entry(str(cache / "1.0.7" / "scripts" / "launch-jev"))
        for error in (RuntimeError("symlink loop"), OSError("unreadable"), ValueError("bad")):
            with self.subTest(error=type(error).__name__), \
                 patch.object(host_mcp.Path, "resolve", side_effect=error), \
                 self.assertRaisesRegex(AutoError, "^HOST_MCP_ENTRY_CONFLICT$"):
                host_mcp.merge("antigravity", document, cache / "1.0.11" / "scripts" / "launch-jev")


class _FakeSetupProcess:
    """A launcher that printed a malformed line and then refused to die cleanly."""

    def __init__(self, stdout):
        self.pid = 424242
        self.stdout = stdout

    def poll(self):
        return None

    def wait(self, timeout=None):
        raise subprocess.TimeoutExpired("open-setup", timeout)


class SetupLauncherCleanupTests(_TempState):
    def test_a_bad_launcher_line_fails_closed_even_when_cleanup_itself_fails(self):
        read_fd, write_fd = os.pipe()
        os.write(write_fd, b"not the setup banner\n")
        os.close(write_fd)
        stdout = os.fdopen(read_fd, "rb")
        process = _FakeSetupProcess(stdout)
        kills = []

        def killpg(pid, sig):
            kills.append((pid, sig))
            raise ProcessLookupError("already gone")

        real_popen = subprocess.Popen

        def popen(command, *args, **kwargs):
            if isinstance(command, list) and command and str(command[0]).endswith("open-setup"):
                return process
            return real_popen(command, *args, **kwargs)

        with patch.object(mcp.subprocess, "Popen", popen), \
             patch.object(mcp.os, "killpg", killpg), \
             self.assertRaisesRegex(AutoError, "^SETUP_START_FAILED$"):
            mcp._open_setup(self.root)
        self.assertEqual(kills, [(process.pid, mcp.signal.SIGKILL)])
        self.assertTrue(stdout.closed)


class LegacyProviderStorageGuardTests(unittest.TestCase):
    def test_legacy_provider_files_are_refused_on_windows_storage(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(providers, "_windows_legacy_storage", return_value=True):
            with self.assertRaisesRegex(providers.SafeError, "^LEGACY_PROVIDER_FILES_UNAVAILABLE_WINDOWS$"):
                providers._safe_regular_file(Path(directory) / "key", "UNSAFE")
            with self.assertRaisesRegex(providers.SafeError, "^LEGACY_PROVIDER_FILES_UNAVAILABLE_WINDOWS$"):
                providers._private_env(Path(directory))

    def test_a_symlinked_credential_root_is_refused_even_if_the_directory_check_passed(self):
        """Defence in depth: `private_dir` already refuses a symlink, so this
        second check is only reachable if that one ever stops doing so."""
        with tempfile.TemporaryDirectory() as directory:
            real = Path(directory) / "real"
            real.mkdir(mode=0o700)
            link = Path(directory) / "link"
            link.symlink_to(real)
            with patch.object(providers, "_windows_legacy_storage", return_value=False), \
                 patch.object(providers, "private_dir", lambda _path: None), \
                 self.assertRaisesRegex(providers.SafeError, "^UNSAFE_CREDENTIAL_PATH$"):
                providers.store_provider_credential("typesafe", "synthetic-key-123456", config_root=link)
            self.assertEqual(list(real.iterdir()), [])


class KeychainAvailabilityTests(unittest.TestCase):
    def test_keychain_is_unavailable_off_macos_without_loading_anything(self):
        with patch.object(keychain.platform, "system", return_value="Linux"), \
             patch.object(keychain, "_SecurityFrameworkBackend",
                          side_effect=AssertionError("must not load the framework")):
            self.assertFalse(keychain.MacKeychain().available())

    def test_a_framework_that_loads_reports_available_and_is_loaded_once(self):
        backend = object()
        with patch.object(keychain.platform, "system", return_value="Darwin"), \
             patch.object(keychain, "_SecurityFrameworkBackend", return_value=backend) as load:
            store = keychain.MacKeychain()
            self.assertTrue(store.available())
            self.assertTrue(store.available())
        load.assert_called_once()
        self.assertIs(store._native(), backend)

    def test_a_framework_that_fails_to_load_reports_unavailable(self):
        with patch.object(keychain.platform, "system", return_value="Darwin"), \
             patch.object(keychain, "_SecurityFrameworkBackend",
                          side_effect=keychain.KeychainError("KEYCHAIN_UNAVAILABLE")):
            self.assertFalse(keychain.MacKeychain().available())


class SetupWizardGuardTests(_TempState):
    def test_an_expiry_the_platform_cannot_render_is_unknown(self):
        with patch.object(setup_server.time, "localtime", side_effect=OverflowError("too large")):
            self.assertEqual(setup_server._expiry(1_790_000_000), "unknown")

    def test_a_failing_root_check_never_pre_ticks_coverage(self):
        folder = self.root / "Documents"
        folder.mkdir()
        with patch.object(setup_server, "descendant_root_allowed", side_effect=RuntimeError("boom")):
            self.assertFalse(setup_server._suggest_coverage(folder, {}, False))

    def test_the_laya_only_review_asks_for_no_provider_key(self):
        project = self.root / "project"
        project.mkdir()
        model = {"repository": "aac6fef/laya-mlx", "revision": "a" * 40, "weight_sha256": "b" * 64,
                 "model_dir": "/synthetic/model", "artifact_manifest": "/synthetic/manifest"}
        controller = SetupController(project, bridge=lambda *_: None, start=lambda *_: None,
                                     local_config=lambda: model, local_attestor=lambda item: dict(item))
        server = setup_server.SetupServer(("127.0.0.1", 0), controller, host_inventory=lambda: [])
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            def request(method, path, values=None, cookie=None):
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                body = urlencode(values).encode() if values is not None else None
                headers = {"Origin": f"http://127.0.0.1:{server.server_port}",
                           "Content-Type": "application/x-www-form-urlencoded"} if body else {}
                if cookie:
                    headers["Cookie"] = cookie
                connection.request(method, path, body=body, headers=headers)
                response = connection.getresponse()
                value = response.status, response.getheader("Set-Cookie"), response.read().decode()
                connection.close()
                return value

            _status, cookie, page = request("GET", "/setup")
            csrf = re.search(r"name='csrf' value=\"([^\"]+)\"", page).group(1)
            status, _, review = request("POST", "/preview", {
                "csrf": csrf, "provider": "typesafe", "mode": "laya-only", "days": "2",
                "daily_calls": "20", "daily_bytes": "20000", "generic": "on", "auto_prepare": "off",
            }, cookie.split(";", 1)[0])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.assertEqual(status, 200, review)
        self.assertIn("No provider key needed for local Laya", review)
        self.assertNotIn("name='credential'", review)
        self.assertFalse(controller.policy_exists())


class _FakeServer:
    instances: list = []

    def __init__(self, address, controller):
        self.server_port = 43210
        self.controller = controller
        _FakeServer.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def serve_forever(self, poll_interval=0.5):
        return None


class SetupMainTests(_TempState):
    def _main(self, environment):
        opened = []
        _FakeServer.instances = []
        out = io.StringIO()
        with patch.object(setup_server, "SetupServer", _FakeServer), \
             patch.object(setup_server.webbrowser, "open", lambda url: opened.append(url)), \
             patch.object(sys, "argv", ["setup_server", "--workspace", str(self.root)]), \
             patch.dict(os.environ, environment), redirect_stdout(out):
            setup_server.main()
        return opened, out.getvalue()

    def test_main_opens_the_loopback_wizard_and_prints_the_parseable_banner(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ADL_SETUP_NO_BROWSER", None)
            opened, printed = self._main({})
        self.assertEqual(opened, ["http://127.0.0.1:43210/setup"])
        self.assertEqual(mcp._extract_setup_url(printed.encode()), "http://127.0.0.1:43210/setup")

    def test_main_never_opens_a_browser_when_told_not_to(self):
        opened, printed = self._main({"ADL_SETUP_NO_BROWSER": "1"})
        self.assertEqual(opened, [])
        self.assertIn("http://127.0.0.1:43210/setup", printed)

    def test_main_refuses_windows_before_doing_anything(self):
        fake_os = types.SimpleNamespace(name="nt", environ={})
        with patch.object(setup_server, "os", fake_os), \
             patch.object(setup_server, "SetupServer", side_effect=AssertionError("must not start")), \
             self.assertRaisesRegex(SystemExit, "^WINDOWS_UNSUPPORTED_IN_1_0_8$"):
            setup_server.main()


if __name__ == "__main__":
    unittest.main()
