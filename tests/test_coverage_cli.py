"""Coverage-floor tests for jev_auto/cli.py.

Targets the baseline-missing lines: the Windows guard on the browser-bridge
lock (reached only by patching this module's own `os` reference, never the
process-global os.name), the "COVER CHILDREN" descendant-approval prompt
inside `enroll`, and the defensive trailing `return 0` that every real
subcommand already returns past.

`enroll` is exercised as a real CLI invocation through `main()`: stdin is
faked as a TTY, `input()` is scripted, and `bridge_record`/`ensure` are
stubbed so no browser bridge file or broker process is touched. Every
refusal path additionally asserts that no policy.json was written.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "plugins" / "qualixar-jev-decision-layer" / "runtime"
sys.path.insert(0, str(RUNTIME))

from jev_auto import cli  # noqa: E402
from jev_auto.common import AutoError, state_dir  # noqa: E402
from jev_auto.settings import load_policy  # noqa: E402


class _TempWorkspace(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        env = patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "state-home")})
        env.start()
        self.addCleanup(env.stop)
        self.ws = self.root / "project"
        self.ws.mkdir()


class BridgeLockWindowsGuardTests(unittest.TestCase):
    """Line 15: reached by patching jev_auto.cli's own `os` reference."""

    def test_bridge_lock_refuses_on_a_simulated_windows_os_name(self):
        class _FakeNTOs:
            name = "nt"

            def __getattr__(self, item):
                return getattr(os, item)

        with tempfile.TemporaryDirectory() as directory:
            with patch.object(cli, "os", _FakeNTOs()):
                with self.assertRaises(AutoError) as ctx:
                    with cli._bridge_lock(Path(directory)):
                        pass
            self.assertEqual(str(ctx.exception), "WINDOWS_BROWSER_BRIDGE_UNVERIFIED")


class EnrollCoverChildrenPromptTests(_TempWorkspace):
    def _run_enroll(self, *, cover_flag: bool, answers: list[str]):
        argv = ["enroll", "--workspace", str(self.ws), "--provider", "typesafe"]
        if cover_flag:
            argv.append("--cover-descendants")
        with patch("sys.stdin.isatty", return_value=True), \
             patch("builtins.input", side_effect=answers), \
             patch.object(cli, "bridge_record") as bridge_mock, \
             patch.object(cli, "ensure") as ensure_mock:
            code = cli.main(argv)
        return code, bridge_mock, ensure_mock

    def test_the_approval_phrase_activates_descendant_coverage(self):
        # Lines 146-151 (success arm): correct "ENABLE" then "COVER CHILDREN".
        code, bridge_mock, ensure_mock = self._run_enroll(
            cover_flag=True, answers=["ENABLE", "COVER CHILDREN"])
        self.assertEqual(code, 0)
        saved = load_policy(self.ws)
        self.assertTrue(saved["covers_descendants"])
        self.assertEqual(saved["workspace_path"], str(self.ws.resolve()))
        bridge_mock.assert_called_once()
        ensure_mock.assert_called_once()

    def test_a_wrong_phrase_refuses_coverage_and_saves_nothing(self):
        # Lines 147-149 (refusal arm): a wrong confirmation phrase must raise
        # DESCENDANT_APPROVAL_REQUIRED and persist no policy at all -- not
        # even a non-covering one.
        code, bridge_mock, ensure_mock = self._run_enroll(
            cover_flag=True, answers=["ENABLE", "definitely not the phrase"])
        self.assertEqual(code, 2)
        self.assertFalse((state_dir(self.ws) / "policy.json").exists())
        bridge_mock.assert_not_called()
        ensure_mock.assert_not_called()

    def test_declining_the_initial_enable_prompt_never_reaches_the_cover_prompt(self):
        # Sanity check on the same guarded flow: SETUP_CANCELLED at the first
        # prompt must short-circuit before line 146 is even evaluated, and
        # input() must not be asked a second question.
        code, bridge_mock, ensure_mock = self._run_enroll(
            cover_flag=True, answers=["nope"])
        self.assertEqual(code, 2)
        self.assertFalse((state_dir(self.ws) / "policy.json").exists())
        bridge_mock.assert_not_called()
        ensure_mock.assert_not_called()


class MainDefensiveFallthroughTests(_TempWorkspace):
    """Line 205: every currently-defined subcommand returns before this
    trailing `return 0`, so it is unreachable through any argument list
    argparse will accept -- its choices are fixed by `choices=[...]` on the
    `command` subparsers. This proves the fallback itself still behaves
    (returns 0, not None) if a future subcommand were ever wired up without
    its own explicit return, by substituting argparse's own parse_args just
    enough to hand main() a `command` value that matches none of the
    existing branches, while every other argument main() reads keeps its
    real, valid value."""

    def test_an_unrecognised_but_parsed_command_falls_through_to_return_zero(self):
        namespace = argparse.Namespace(command="future-command", workspace=str(self.ws))
        with patch.object(argparse.ArgumentParser, "parse_args", return_value=namespace), \
             patch.object(cli, "ensure") as ensure_mock:
            code = cli.main(["--unused"])
        self.assertEqual(code, 0)
        ensure_mock.assert_called_once()


if __name__ == "__main__":
    unittest.main()
