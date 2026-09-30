"""`host-register --write` edits someone else's config as little as possible.

It keeps the user's key order and file mode, refuses a file with comments by
name instead of calling it unparseable, and never writes the user's home
folder into a repository file.
"""

from __future__ import annotations

import io
import json
import os
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "plugins" / "qualixar-jev-decision-layer" / "runtime"
sys.path.insert(0, str(RUNTIME))

from jev_auto import host_mcp  # noqa: E402
from jev_auto.common import AutoError  # noqa: E402

OUTSIDE = Path("/opt/qualixar/launch-jev")


class _Case(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(os.path.realpath(tmp.name))
        self.home = self.root / "home" / "alex"
        self.home.mkdir(parents=True)
        environment = patch.dict(os.environ, {"HOME": str(self.home)})
        environment.start()
        self.addCleanup(environment.stop)
        self.workspace = self.root / "repo"
        self.workspace.mkdir()
        self.config = self.workspace / ".vscode" / "mcp.json"
        self.config.parent.mkdir()

    def launcher_in_home(self, release="1.0.13"):
        path = self.home / ".claude" / "plugins" / "cache" / "qualixar" / "qualixar-jev-decision-layer" / release / "scripts" / "launch-jev"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\n")
        path.chmod(0o755)
        return path


class KeyOrderAndModeTests(_Case):
    def test_the_users_key_order_is_kept(self):
        self.config.write_text(json.dumps({"inputs": [], "servers": {"zeta": {"command": "/bin/z"},
                                                                     "alpha": {"command": "/bin/a"}}, "after": 1},
                                          indent=4))
        host_mcp.install("vscode", self.workspace, OUTSIDE)
        document = json.loads(self.config.read_text())
        self.assertEqual(list(document), ["inputs", "servers", "after"])
        self.assertEqual(list(document["servers"]), ["zeta", "alpha", host_mcp.SERVER_NAME])

    def test_an_existing_entry_keeps_its_place(self):
        old = self.launcher_in_home("1.0.12")
        new = self.launcher_in_home("1.0.13")
        self.config.write_text(json.dumps({"servers": {"a": {"command": "/bin/a"},
                                                       host_mcp.SERVER_NAME: host_mcp.server_entry("vscode", old),
                                                       "b": {"command": "/bin/b"}}}))
        host_mcp.install("vscode", self.workspace, new)
        self.assertEqual(list(json.loads(self.config.read_text())["servers"]), ["a", host_mcp.SERVER_NAME, "b"])

    def test_the_file_mode_is_kept(self):
        for mode in (0o644, 0o600, 0o640):
            with self.subTest(mode=oct(mode)):
                self.config.write_text(json.dumps({"servers": {"other": {"command": f"/bin/{mode}"}}}))
                self.config.chmod(mode)
                host_mcp.install("vscode", self.workspace, OUTSIDE)
                self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), mode)
                self.config.unlink()

    def test_a_new_file_is_private(self):
        host_mcp.install("vscode", self.workspace, OUTSIDE)
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o600)

    def test_a_config_whose_mode_cannot_be_read_is_refused(self):
        self.config.write_text("{}")
        real_lstat = os.lstat

        def lstat(path, *args, **kwargs):
            if Path(path) == self.config:
                raise PermissionError("denied")
            return real_lstat(path, *args, **kwargs)

        with patch.object(host_mcp.os, "lstat", side_effect=lstat):
            with self.assertRaisesRegex(AutoError, "^HOST_MCP_CONFIG_UNREADABLE$"):
                host_mcp.install("vscode", self.workspace, OUTSIDE)
        self.assertEqual(self.config.read_text(), "{}")


class CommentTests(_Case):
    def test_a_file_with_comments_is_refused_by_name_and_left_alone(self):
        for text in ('{\n  // my servers\n  "servers": {}\n}\n', '{"servers": {} /* keep */}',
                     '{\n  "servers": {\n    "a": {"command": "/bin/a"}, // trailing\n  }\n}\n'):
            with self.subTest(text=text[:20]):
                self.config.write_text(text)
                with self.assertRaisesRegex(AutoError, "^HOST_MCP_CONFIG_HAS_COMMENTS$"):
                    host_mcp.install("vscode", self.workspace, OUTSIDE)
                self.assertEqual(self.config.read_text(), text)
                with self.assertRaisesRegex(AutoError, "^HOST_MCP_CONFIG_HAS_COMMENTS$"):
                    host_mcp.plan("vscode", self.workspace, OUTSIDE)

    def test_slashes_inside_strings_are_not_comments(self):
        self.config.write_text(json.dumps({"servers": {"web": {"url": "http://example.test/*x*/"}}}))
        host_mcp.install("vscode", self.workspace, OUTSIDE)
        self.assertIn("web", json.loads(self.config.read_text())["servers"])

    def test_a_commented_file_that_is_broken_anyway_is_unparseable(self):
        for text in ('{\n  // note\n  "servers": {"a": \n}', '{"servers": {} /* never closed', '// only a comment'):
            with self.subTest(text=text[:20]):
                self.config.write_text(text)
                with self.assertRaisesRegex(AutoError, "^HOST_MCP_CONFIG_UNPARSEABLE$"):
                    host_mcp.install("vscode", self.workspace, OUTSIDE)

    def test_a_file_that_is_not_text_is_unparseable(self):
        self.config.write_bytes(b'{"servers": "\xff\xfe"}')
        with self.assertRaisesRegex(AutoError, "^HOST_MCP_CONFIG_UNPARSEABLE$"):
            host_mcp.install("vscode", self.workspace, OUTSIDE)

    def test_a_broken_file_without_comments_is_still_unparseable(self):
        for text in ('{"servers": {"other": ', '{"servers": "// not a comment" ', "[1, 2"):
            with self.subTest(text=text):
                self.config.write_text(text)
                with self.assertRaisesRegex(AutoError, "^HOST_MCP_CONFIG_UNPARSEABLE$"):
                    host_mcp.install("vscode", self.workspace, OUTSIDE)


class HomePathTests(_Case):
    def test_the_repository_file_names_the_home_folder_by_variable(self):
        launcher = self.launcher_in_home()
        outcome = host_mcp.install("vscode", self.workspace, launcher)
        text = self.config.read_text()
        self.assertNotIn(str(self.home), text)
        self.assertNotIn("alex", text)
        command = json.loads(text)["servers"][host_mcp.SERVER_NAME]["command"]
        self.assertEqual(command, "${userHome}/" + str(launcher.relative_to(self.home)))
        self.assertEqual(outcome["entry"]["command"], command)
        self.assertNotIn("home_path_warning", outcome)

    def test_a_launcher_outside_the_home_folder_keeps_its_absolute_path(self):
        host_mcp.install("vscode", self.workspace, OUTSIDE)
        self.assertEqual(json.loads(self.config.read_text())["servers"][host_mcp.SERVER_NAME]["command"], str(OUTSIDE))

    def test_user_level_configs_keep_absolute_paths(self):
        launcher = self.launcher_in_home()
        for host in ("antigravity", "claude-desktop"):
            with self.subTest(host=host):
                self.assertEqual(host_mcp.server_entry(host, launcher)["command"], str(launcher))

    def test_an_older_absolute_entry_is_upgraded_to_the_portable_spelling(self):
        old = self.launcher_in_home("1.0.12")
        new = self.launcher_in_home("1.0.13")
        self.config.write_text(json.dumps({"servers": {host_mcp.SERVER_NAME: {"type": "stdio", "command": str(old),
                                                                              "args": [], "env": {}}}}))
        outcome = host_mcp.install("vscode", self.workspace, new)
        self.assertEqual(outcome["action"], "update")
        self.assertEqual(json.loads(self.config.read_text())["servers"][host_mcp.SERVER_NAME]["command"],
                         "${userHome}/" + str(new.relative_to(self.home)))

    def test_the_same_launcher_written_absolutely_is_rewritten_not_refused(self):
        launcher = self.launcher_in_home()
        self.config.write_text(json.dumps({"servers": {host_mcp.SERVER_NAME: {"type": "stdio", "command": str(launcher),
                                                                              "args": [], "env": {}}}}))
        outcome = host_mcp.install("vscode", self.workspace, launcher)
        self.assertEqual(outcome["action"], "update")
        self.assertNotIn(str(self.home), self.config.read_text())

    def test_a_portable_entry_from_an_older_release_is_upgraded_too(self):
        old = self.launcher_in_home("1.0.12")
        new = self.launcher_in_home("1.0.13")
        host_mcp.install("vscode", self.workspace, old)
        outcome = host_mcp.install("vscode", self.workspace, new)
        self.assertEqual(outcome["action"], "update")
        self.assertIn("1.0.13", self.config.read_text())

    def test_no_usable_home_folder_keeps_the_absolute_path(self):
        launcher = self.launcher_in_home()
        with patch.dict(os.environ, {"HOME": "/"}):
            self.assertEqual(host_mcp.server_entry("vscode", launcher)["command"], str(launcher))
            self.assertEqual(host_mcp._expand_home("${userHome}/x"), "${userHome}/x")
        with patch.object(host_mcp.Path, "home", side_effect=RuntimeError("no home")):
            self.assertEqual(host_mcp.server_entry("vscode", launcher)["command"], str(launcher))

    def test_a_different_users_path_is_still_a_conflict(self):
        launcher = self.launcher_in_home()
        self.config.write_text(json.dumps({"servers": {host_mcp.SERVER_NAME: {
            "type": "stdio", "command": "/home/someone-else/launch-jev", "args": [], "env": {}}}}))
        with self.assertRaisesRegex(AutoError, "^HOST_MCP_ENTRY_CONFLICT$"):
            host_mcp.install("vscode", self.workspace, launcher)


class CommandLineHelpTests(_Case):
    def _run(self, argv):
        from jev_auto import cli

        out, err = io.StringIO(), io.StringIO()
        with patch.object(host_mcp, "launcher_path", return_value=OUTSIDE), redirect_stdout(out), redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_a_commented_file_gets_the_entry_to_add_by_hand(self):
        text = '{\n  // team servers\n  "servers": {}\n}\n'
        self.config.write_text(text)
        for argv in (["vscode", "--workspace", str(self.workspace), "--write"],
                     ["host-register", "--host", "vscode", "--workspace", str(self.workspace)]):
            with self.subTest(command=argv[0]):
                code, out, err = self._run(argv)
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertIn("HOST_MCP_CONFIG_HAS_COMMENTS", err)
                self.assertIn('Add this entry under "servers" yourself', err)
                self.assertIn(f'"command": "{OUTSIDE}"', err)
                self.assertEqual(self.config.read_text(), text)

    def test_other_refusals_print_only_their_code(self):
        self.config.write_text('{"servers": ')
        code, _out, err = self._run(["vscode", "--workspace", str(self.workspace), "--write"])
        self.assertEqual(code, 2)
        self.assertEqual(err.strip(), "HOST_MCP_CONFIG_UNPARSEABLE")


if __name__ == "__main__":
    unittest.main()
