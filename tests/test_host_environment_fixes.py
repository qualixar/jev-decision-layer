"""Behaviour that depends on how a host starts Jev, held to the real host conditions.

The Claude desktop app starts its servers in `/` with a handful of variables;
a Linux desktop needs its session variables for the key store and browser;
USER and LOGNAME are ordinary environment variables anyone can set.
"""

from __future__ import annotations

import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "plugins" / "qualixar-jev-decision-layer" / "runtime"
sys.path.insert(0, str(RUNTIME))

from jev_auto import mcp, policy_sources  # noqa: E402
from jev_auto.common import AutoError  # noqa: E402

_LEGACY = types.SimpleNamespace(tools=lambda scope: [])


class RelativeWorkspaceTests(unittest.TestCase):
    def test_a_relative_path_is_refused_when_the_host_runs_from_root_or_home(self):
        for cwd in (os.sep, str(Path.home())):
            for value in (".", "project", "./sub"):
                with self.subTest(cwd=cwd, value=value), patch.object(mcp.os, "getcwd", return_value=cwd):
                    with self.assertRaisesRegex(AutoError, "WORKSPACE_PATH_NOT_ABSOLUTE"):
                        mcp.dispatch("jev_auto_status", {"workspace_path": value}, _LEGACY,
                                     caller=lambda request: {}, claude_policy=lambda: None)

    def test_the_setup_wizard_is_never_opened_for_the_filesystem_root(self):
        opened = []
        with patch.object(mcp.os, "getcwd", return_value=os.sep):
            with self.assertRaisesRegex(AutoError, "WORKSPACE_PATH_NOT_ABSOLUTE"):
                mcp.dispatch("jev_setup", {"workspace_path": "."}, _LEGACY, setup_launcher=opened.append)
        self.assertEqual(opened, [])

    def test_a_relative_path_from_a_project_folder_still_works(self):
        seen = []
        with tempfile.TemporaryDirectory() as directory, patch.object(mcp.os, "getcwd", return_value=directory):
            result = mcp.dispatch("jev_prepare", {"workspace_path": ".", "goal": "focus"}, _LEGACY,
                                  caller=lambda request: seen.append(request) or {"ok": True})
        self.assertEqual(result, {"ok": True})
        self.assertEqual(seen[0]["op"], "prepare")

    def test_an_absolute_path_is_unchanged_from_any_folder(self):
        with patch.object(mcp.os, "getcwd", return_value=os.sep):
            result = mcp.dispatch("jev_prepare", {"workspace_path": "/work/project", "goal": "focus"}, _LEGACY,
                                  caller=lambda request: {"ok": True})
        self.assertEqual(result, {"ok": True})

    def test_an_unreadable_working_folder_is_treated_as_unsafe(self):
        with patch.object(mcp.os, "getcwd", side_effect=FileNotFoundError("deleted")):
            with self.assertRaisesRegex(AutoError, "WORKSPACE_PATH_NOT_ABSOLUTE"):
                mcp.dispatch("jev_auto_status", {"workspace_path": "."}, _LEGACY, caller=lambda request: {})


class WizardSessionVariableTests(unittest.TestCase):
    _SESSION = {"DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus", "DISPLAY": ":0",
                "WAYLAND_DISPLAY": "wayland-0", "XDG_RUNTIME_DIR": "/run/user/1000", "BROWSER": "firefox"}

    def _launch_environment(self, extra):
        seen = {}

        def popen(command, **kwargs):
            seen.update(kwargs["env"])
            raise OSError("stop after capturing the environment")

        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, extra, clear=False), \
                patch.object(mcp, "workspace", side_effect=lambda value: Path(value)), \
                patch.object(mcp.subprocess, "Popen", side_effect=popen):
            with self.assertRaises(AutoError):
                mcp._open_setup(directory)
        return seen

    def test_the_key_store_and_browser_variables_reach_the_wizard(self):
        seen = self._launch_environment(self._SESSION)
        for name, value in self._SESSION.items():
            with self.subTest(name=name):
                self.assertEqual(seen.get(name), value)

    def test_credentials_still_never_reach_the_wizard(self):
        seen = self._launch_environment({**self._SESSION, "TYPESAFE_API_KEY": "synthetic-key-0000",
                                         "AWS_SECRET_ACCESS_KEY": "synthetic-0000", "GITHUB_TOKEN": "x"})
        for name in ("TYPESAFE_API_KEY", "AWS_SECRET_ACCESS_KEY", "GITHUB_TOKEN"):
            self.assertNotIn(name, seen)


@unittest.skipIf(os.name == "nt", "POSIX account database")
class ManagedProfileUserTests(unittest.TestCase):
    def test_the_account_comes_from_the_system_not_from_user_or_logname(self):
        import pwd

        real = pwd.getpwuid(os.getuid()).pw_name
        with patch.dict(os.environ, {"USER": "spoofed", "LOGNAME": "spoofed"}):
            self.assertEqual(policy_sources._current_user(), real)

    def test_a_missing_account_entry_falls_back_without_raising(self):
        import pwd

        with patch.object(pwd, "getpwuid", side_effect=KeyError("no entry")), \
                patch.dict(os.environ, {"LOGNAME": "fallback-user", "USER": "fallback-user"}):
            self.assertEqual(policy_sources._current_user(), "fallback-user")


if __name__ == "__main__":
    unittest.main()
