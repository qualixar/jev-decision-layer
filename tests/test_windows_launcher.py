"""Native Windows smoke checks for packaged command launchers."""
from __future__ import annotations

import os
import tempfile
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins" / "qualixar-jev-decision-layer" / "scripts"


class WindowsLauncherPackageTests(unittest.TestCase):
    def test_windows_entrypoints_are_shipped(self):
        for name in ("launch-jev.cmd", "jev.cmd", "open-setup.cmd"):
            with self.subTest(name=name):
                self.assertTrue((SCRIPTS / name).is_file())

    def test_batch_launchers_do_not_resolve_interpreters_from_path(self):
        for name in ("launch-jev.cmd", "jev.cmd", "open-setup.cmd"):
            with self.subTest(name=name):
                source = (SCRIPTS / name).read_text(encoding="utf-8").lower()
                self.assertNotIn("where py", source)
                self.assertNotIn("where python", source)
                self.assertIn("get-authenticodeSignature".lower(), source)
                self.assertIn("python software foundation", source)
                self.assertIn('set "jev_python="', source)
                self.assertIn('set "jev_candidate="', source)
                self.assertLess(source.index('set "jev_python="'), source.index("call :find_trusted_python"))
                self.assertLess(source.index('set "jev_candidate="'), source.index("call :find_trusted_python"))

    @unittest.skipUnless(os.name == "nt", "requires Windows cmd.exe and installed Python")
    def test_cli_launcher_runs_offline_policy_gate(self):
        launcher = SCRIPTS / "jev.cmd"
        completed = subprocess.run(
            ["cmd.exe", "/d", "/c", f'"{launcher}" selftest'],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn('"all_passed":true', completed.stdout.replace(" ", ""))

    @unittest.skipUnless(os.name == "nt", "requires Windows cmd.exe and installed Python")
    def test_mcp_launcher_starts_and_exits_on_closed_stdin(self):
        launcher = SCRIPTS / "launch-jev.cmd"
        completed = subprocess.run(
            ["cmd.exe", "/d", "/c", f'"{launcher}"'],
            cwd=ROOT,
            input="",
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    @unittest.skipUnless(os.name == "nt", "requires Windows cmd.exe and installed Python")
    def test_setup_helper_reports_usage_without_starting_wizard(self):
        launcher = SCRIPTS / "open-setup.cmd"
        completed = subprocess.run(
            ["cmd.exe", "/d", "/c", f'"{launcher}" --help'],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("workspace-path", completed.stdout)

    @unittest.skipUnless(os.name == "nt", "requires Windows cmd.exe and profile expansion")
    def test_path_spoofed_python_cannot_register_or_persist_host_config(self):
        with tempfile.TemporaryDirectory(prefix="jev-path-spoof-") as temporary:
            root = Path(temporary)
            fake_bin = root / "spoofed-bin"
            profile = root / "profile"
            local_app_data = root / "local-app-data"
            program_files = root / "program-files"
            marker = root / "spoof-executed.txt"
            fake_bin.mkdir()
            profile.mkdir()
            local_app_data.mkdir()
            program_files.mkdir()
            for name in ("py.cmd", "python.cmd"):
                (fake_bin / name).write_text(
                    '@echo off\r\necho invoked>>"%JEV_SPOOF_MARKER%"\r\nexit /b 0\r\n',
                    encoding="ascii",
                )

            environment = os.environ.copy()
            environment.update({
                "PATH": str(fake_bin),
                "USERPROFILE": str(profile),
                "LOCALAPPDATA": str(local_app_data),
                "ProgramFiles": str(program_files),
                "ProgramFiles(x86)": str(program_files),
                "JEV_SPOOF_MARKER": str(marker),
                "RUNNER_TOOL_CACHE": str(root / "no-runner-cache"),
            })
            cmd = Path(environment["SystemRoot"]) / "System32" / "cmd.exe"
            completed = subprocess.run(
                [str(cmd), "/d", "/c", f'"{SCRIPTS / "jev.cmd"}" host-register --host codex-cli --write'],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            self.assertEqual(completed.returncode, 3, completed.stderr)
            self.assertFalse(marker.exists(), "PATH-provided interpreter must never execute")
            self.assertFalse((profile / ".codex" / "config.toml").exists())
            self.assertFalse((profile / ".claude.json").exists())

    @unittest.skipUnless(os.name == "nt", "requires Windows cmd.exe and native batch semantics")
    def test_inherited_python_override_cannot_execute_or_change_profile_config(self):
        with tempfile.TemporaryDirectory(prefix="jev-python-override-") as temporary:
            root = Path(temporary)
            profile = root / "isolated-profile"
            fake_bin = root / "untrusted-bin"
            profile.mkdir()
            (profile / ".codex").mkdir()
            fake_bin.mkdir()
            marker = root / "untrusted-python-ran.txt"
            fake_python = fake_bin / "python.cmd"
            fake_python.write_text(
                '@echo off\r\necho ran>"%JEV_OVERRIDE_MARKER%"\r\necho forged>"%USERPROFILE%\\.codex\\config.toml"\r\nexit /b 0\r\n',
                encoding="ascii",
            )

            environment = os.environ.copy()
            environment.update({
                "USERPROFILE": str(profile),
                "HOME": str(profile),
                "JEV_PYTHON": str(fake_python),
                "JEV_CANDIDATE": str(fake_python),
                "JEV_OVERRIDE_MARKER": str(marker),
            })
            cmd = Path(environment["SystemRoot"]) / "System32" / "cmd.exe"
            completed = subprocess.run(
                [str(cmd), "/d", "/c", f'"{SCRIPTS / "jev.cmd"}" host-register --host codex-cli --write'],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            self.assertNotEqual(completed.returncode, 0, "manual Windows registration must fail closed")
            self.assertNotIn("TRUSTED_PYTHON_3_11_REQUIRED", completed.stderr)
            self.assertFalse(marker.exists(), "inherited JEV_PYTHON must not execute")
            self.assertFalse((profile / ".codex" / "config.toml").exists())
            self.assertFalse((profile / ".claude.json").exists())


if __name__ == "__main__":
    unittest.main()
