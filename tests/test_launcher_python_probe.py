"""Every POSIX launcher finds a Python 3.11 or later the same way.

The launchers are run for real, from a copy of the scripts folder, with shim
interpreters that only report a version. The copy swaps the fixed system
locations for a folder the test controls, so this machine's own Python does
not decide the result. Each run uses the desktop app's conditions: working
folder `/` and only PATH, HOME, USER, LOGNAME and SHELL in the environment.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "qualixar-jev-decision-layer"
SCRIPTS = PLUGIN / "scripts"
PROBE = SCRIPTS / "find-python.sh"
FIXED_LINE = "  for jev_fixed in /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do\n"
SOURCE_LINE = '. "$JEV_PLUGIN_ROOT/scripts/find-python.sh"'

# launcher name -> arguments it needs to reach the interpreter
LAUNCHERS = {
    "launch-jev": [],
    "launch-claude-hook": [],
    "launch-agy-hook": [],
    "jev": ["selftest"],
    "open-setup": ["/tmp"],
    "launch-hermes-hook": [],
    "launch-hermes-tool": [],
    "launch-codex-hook": [],
}
HOOK_LAUNCHERS = ("launch-claude-hook", "launch-agy-hook", "launch-hermes-hook", "launch-hermes-tool",
                  "launch-codex-hook")


def _shim(path: Path, version: tuple[int, int], label: str = "RAN") -> Path:
    """An executable that answers the launchers' version check and records a real run."""
    path.parent.mkdir(parents=True, exist_ok=True)
    ok = 0 if version >= (3, 11) else 3
    path.write_text(
        "#!/bin/sh\n"
        'if [ "$#" -eq 4 ] && [ "$1" = "-I" ] && [ "$2" = "-S" ] && [ "$3" = "-c" ]; then\n'
        f"  exit {ok}\n"
        "fi\n"
        f"printf '{label} %s\\n' \"$0\"\n"
        'printf \'ARG %s\\n\' "$@"\n'
        "exit 0\n")
    path.chmod(0o755)
    return path


class _LauncherCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(os.path.realpath(self.tmp.name))
        # A space in the path, as under "Application Support".
        self.plugin = base / "Application Support" / "plugin"
        shutil.copytree(SCRIPTS, self.plugin / "scripts")
        (self.plugin / "runtime").mkdir()
        (self.plugin / "hooks").mkdir()
        (self.plugin / "hooks" / "jev_hook.py").write_text("")
        self.system = base / "system"
        probe = self.plugin / "scripts" / "find-python.sh"
        text = probe.read_text()
        self.assertEqual(text.count(FIXED_LINE), 1, "the fixed system locations moved; update this test")
        probe.write_text(text.replace(FIXED_LINE, f'  for jev_fixed in "{self.system}/python3"; do\n'))
        self.home = base / "home"
        self.home.mkdir()
        self.bin = base / "bin"

    def run_launcher(self, name, *, path, extra_env=None, cwd="/", bare_name=False):
        env = {"PATH": path, "HOME": str(self.home), "USER": "tester", "LOGNAME": "tester", "SHELL": "/bin/zsh",
               **(extra_env or {})}
        script = self.plugin / "scripts" / name
        # `sh name` from the scripts folder is the one way to start it with no
        # slash in $0; a direct exec always passes the full path.
        command = ["/bin/sh", name] if bare_name else [str(script)]
        return subprocess.run([*command, *LAUNCHERS[name]], cwd=cwd, env=env, capture_output=True, text=True,
                              timeout=30, stdin=subprocess.DEVNULL)

    def chosen(self, result):
        lines = [line for line in result.stdout.splitlines() if line.startswith(("RAN ", "HOSTILE "))]
        return lines[0].split(" ", 1)[1] if lines else None


class SharedProbeTests(unittest.TestCase):
    def test_every_launcher_sources_the_one_shared_probe(self):
        for name in LAUNCHERS:
            with self.subTest(launcher=name):
                text = (SCRIPTS / name).read_text()
                self.assertIn(SOURCE_LINE, text)
                self.assertIn("jev_find_python", text)
                self.assertNotIn("/opt/homebrew/bin/python3", text, "candidates belong in find-python.sh only")

    def test_the_probe_is_a_sourced_file_not_a_command(self):
        self.assertTrue(PROBE.is_file())
        self.assertFalse(os.access(PROBE, os.X_OK))
        self.assertEqual(PROBE.read_text().count(FIXED_LINE), 1)

    def test_codex_hooks_run_through_the_launcher_not_a_bare_python3(self):
        config = json.loads((PLUGIN / "hooks" / "codex-hooks.json").read_text())
        commands = [hook["command"] for groups in config["hooks"].values() for group in groups
                    for hook in group["hooks"]]
        self.assertEqual(len(commands), 4)
        for command in commands:
            self.assertEqual(command, '"${PLUGIN_ROOT}/scripts/launch-codex-hook"')


class InterpreterSelectionTests(_LauncherCase):
    def test_a_stock_mac_uses_a_python_on_path_instead_of_failing(self):
        _shim(self.system / "python3", (3, 9))
        wanted = _shim(self.bin / "python3.12", (3, 12))
        _shim(self.bin / "python3", (3, 9))
        for name in LAUNCHERS:
            with self.subTest(launcher=name):
                # Only the test's own folder on PATH, so this machine's Pythons cannot answer.
                result = self.run_launcher(name, path=str(self.bin))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.chosen(result), str(wanted))

    def test_the_newest_versioned_name_on_path_is_tried_first(self):
        _shim(self.bin / "python3.12", (3, 12))
        wanted = _shim(self.bin / "python3.13", (3, 13))
        result = self.run_launcher("launch-jev", path=str(self.bin))
        self.assertEqual(self.chosen(result), str(wanted))

    def test_a_too_old_python3_earlier_on_path_does_not_hide_a_later_one(self):
        _shim(self.bin / "old" / "python3", (3, 9))
        wanted = _shim(self.bin / "new" / "python3", (3, 12))
        result = self.run_launcher("launch-jev", path=f"{self.bin}/old:{self.bin}/new")
        self.assertEqual(self.chosen(result), str(wanted))

    def test_the_fixed_locations_come_first(self):
        wanted = _shim(self.system / "python3", (3, 12))
        _shim(self.bin / "python3.13", (3, 13))
        custom = _shim(self.home / "custom" / "python", (3, 13))
        result = self.run_launcher("launch-jev", path=str(self.bin), extra_env={"JEV_PYTHON": str(custom)})
        self.assertEqual(self.chosen(result), str(wanted))

    def test_an_absolute_jev_python_is_used_before_path(self):
        custom = _shim(self.home / "custom" / "python", (3, 12))
        _shim(self.bin / "python3.13", (3, 13))
        for name in LAUNCHERS:
            with self.subTest(launcher=name):
                result = self.run_launcher(name, path=str(self.bin), extra_env={"JEV_PYTHON": str(custom)})
                self.assertEqual(self.chosen(result), str(custom))

    def test_a_relative_or_too_old_jev_python_is_ignored(self):
        wanted = _shim(self.bin / "python3.12", (3, 12))
        old = _shim(self.home / "old" / "python", (3, 9))
        for value in ("python3.13", "custom/python", str(old)):
            with self.subTest(value=value):
                result = self.run_launcher("launch-jev", path=str(self.bin), extra_env={"JEV_PYTHON": value})
                self.assertEqual(self.chosen(result), str(wanted))

    def test_per_user_shims_are_the_last_resort(self):
        _shim(self.bin / "python3", (3, 9))
        pyenv = _shim(self.home / ".pyenv" / "shims" / "python3", (3, 12))
        _shim(self.home / ".local" / "bin" / "python3", (3, 13))
        result = self.run_launcher("launch-jev", path=str(self.bin))
        self.assertEqual(self.chosen(result), str(pyenv))
        pyenv.unlink()
        result = self.run_launcher("launch-jev", path=str(self.bin))
        self.assertEqual(self.chosen(result), str(self.home / ".local" / "bin" / "python3"))

    def test_a_hostile_working_folder_is_never_searched(self):
        hostile = Path(self.tmp.name).resolve() / "cloned-repo"
        for name in ("python3.12", "python3"):
            _shim(hostile / name, (3, 12), label="HOSTILE")
        _shim(hostile / "bin" / "python3", (3, 12), label="HOSTILE")
        # ".", an empty entry and "bin" all name the opened folder; none may be searched.
        result = self.run_launcher("launch-jev", path=".::bin", cwd=str(hostile))
        self.assertNotIn("HOSTILE", result.stdout)
        self.assertEqual(result.returncode, 3)
        self.assertIn("python3.12 (not on PATH)", result.stderr)

    def test_an_absolute_path_entry_inside_the_opened_folder_is_not_used(self):
        hostile = Path(self.tmp.name).resolve() / "cloned-repo"
        _shim(hostile / ".venv" / "bin" / "python3.12", (3, 12), label="HOSTILE")
        wanted = _shim(self.bin / "python3.11", (3, 11))
        for cwd, entry in ((hostile, hostile / ".venv" / "bin"), (hostile / ".venv", hostile / ".venv" / "bin")):
            with self.subTest(cwd=cwd.name):
                result = self.run_launcher("launch-jev", path=f"{entry}:{self.bin}", cwd=str(cwd))
                self.assertNotIn("HOSTILE", result.stdout)
                self.assertEqual(self.chosen(result), str(wanted))
        result = self.run_launcher("launch-jev", path=str(hostile / ".venv" / "bin"), cwd=str(hostile))
        self.assertEqual(result.returncode, 3)
        self.assertIn("python3.12 (inside the opened folder, not used)", result.stderr)
        # Started from "/", as the desktop app does, nothing counts as the opened folder.
        result = self.run_launcher("launch-jev", path=str(hostile / ".venv" / "bin"))
        self.assertIn("HOSTILE", result.stdout)

    def test_a_host_started_in_the_home_folder_still_uses_a_python_under_home(self):
        # Home is not a cloned repository: tools such as uv and pyenv keep
        # their Python under it, and a CLI started in ~ must still find it.
        wanted = _shim(self.home / ".local" / "share" / "uv" / "bin" / "python3.12", (3, 12))
        result = self.run_launcher("launch-jev", path=str(wanted.parent), cwd=str(self.home))
        self.assertEqual(self.chosen(result), str(wanted))

    def test_the_error_lists_every_place_it_looked(self):
        _shim(self.system / "python3", (3, 9))
        _shim(self.bin / "python3", (3, 9))
        for name in LAUNCHERS:
            with self.subTest(launcher=name):
                result = self.run_launcher(name, path=str(self.bin))
                self.assertEqual(result.returncode, 3)
                self.assertEqual(result.stdout, "")
                lines = result.stderr.splitlines()
                self.assertEqual(lines[0], "PYTHON_3_11_REQUIRED")
                for probed in (str(self.plugin / "runtime/python/bin/python3") + " (not found)",
                               str(self.system / "python3") + " (older than 3.11, or does not run)",
                               "JEV_PYTHON (not set)", "python3.14 (not on PATH)", "python3.11 (not on PATH)",
                               str(self.bin / "python3") + " (older than 3.11, or does not run)",
                               str(self.home / ".pyenv/shims/python3") + " (not found)",
                               str(self.home / ".local/bin/python3") + " (not found)"):
                    self.assertIn(probed, result.stderr)
                self.assertIn("set JEV_PYTHON", lines[-1])


class HookExitCodeTests(_LauncherCase):
    """Exit code 2 blocks the prompt in Claude Code, so a hook launcher never uses it."""

    def test_a_hook_launcher_started_without_a_path_does_not_block(self):
        _shim(self.bin / "python3.12", (3, 12))
        for name in ("launch-claude-hook", "launch-codex-hook"):
            with self.subTest(launcher=name):
                result = self.run_launcher(name, path=str(self.bin), cwd=str(self.plugin / "scripts"), bare_name=True)
                self.assertNotEqual(result.returncode, 2)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")

    def test_a_missing_probe_file_does_not_block(self):
        (self.plugin / "scripts" / "find-python.sh").unlink()
        for name in HOOK_LAUNCHERS:
            with self.subTest(launcher=name):
                result = self.run_launcher(name, path="/usr/bin:/bin")
                self.assertEqual(result.returncode, 3)
                self.assertEqual(result.stdout, "")

    def test_the_codex_hook_runs_the_packaged_hook_script(self):
        wanted = _shim(self.bin / "python3.12", (3, 12))
        result = self.run_launcher("launch-codex-hook", path=str(self.bin))
        self.assertEqual(self.chosen(result), str(wanted))
        self.assertIn(f"ARG {self.plugin}/hooks/jev_hook.py", result.stdout)
        self.assertIn("ARG -I", result.stdout)


class RealLauncherTests(unittest.TestCase):
    """The shipped launcher, unmodified, from `/` with the desktop app's environment."""

    def test_the_codex_hook_is_silent_in_an_unenrolled_folder(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(os.path.realpath(directory))
            workspace = home / "project"
            workspace.mkdir()
            env = {"PATH": "/usr/bin:/bin", "HOME": str(home), "USER": "tester", "LOGNAME": "tester",
                   "SHELL": "/bin/zsh", "XDG_STATE_HOME": str(home / "state"), "XDG_CONFIG_HOME": str(home / "config")}
            event = json.dumps({"hook_event_name": "SessionStart", "cwd": str(workspace), "session_id": "s1"})
            result = subprocess.run([str(SCRIPTS / "launch-codex-hook")], input=event, cwd="/", env=env,
                                    capture_output=True, text=True, timeout=60)
        if result.returncode == 3 and result.stderr.startswith("PYTHON_3_11_REQUIRED"):
            self.skipTest("no Python 3.11 or later on this machine's fixed locations or /usr/bin")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
