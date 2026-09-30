"""Hermes pre_llm_call must look at the directory Hermes itself works in.

Hermes passes no `cwd` to `pre_llm_call` (agent/turn_context.py passes exactly
session_id, task_id, turn_id, user_message, conversation_history,
is_first_turn, model, platform, parent_session_id and sender_id). Its own cwd
consumers go through agent.runtime_cwd.resolve_agent_cwd(): a session override,
then TERMINAL_CWD, then the launch directory. Worktree mode and the messaging
gateway set TERMINAL_CWD while the process cwd stays somewhere else, so the
process cwd alone points the hook at the wrong workspace.

These are in-process adapter tests. No Hermes installation is used.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "qualixar-jev-decision-layer"

MESSAGE = "Please fix the failing enrollment boundary test in this repository and explain why."


def _plugin():
    spec = importlib.util.spec_from_file_location("qualixar_jev_hermes_cwd", PLUGIN / "__init__.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _hermes_kwargs() -> dict:
    """The exact keyword set Hermes passes to pre_llm_call."""
    return {"session_id": "s-1", "task_id": "t-1", "turn_id": "u-1", "user_message": MESSAGE,
            "conversation_history": [], "is_first_turn": True, "model": "m", "platform": "cli",
            "parent_session_id": "", "sender_id": ""}


class HermesWorkingDirectoryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name).resolve()
        self.launch = root / "launch"
        self.worktree = root / "worktree"
        self.session = root / "session"
        for folder in (self.launch, self.worktree, self.session):
            folder.mkdir()
        self.plugin = _plugin()
        self.seen = []
        runner = patch.object(self.plugin, "_run_child", self._record)
        runner.start()
        self.addCleanup(runner.stop)
        previous = os.getcwd()
        os.chdir(self.launch)
        self.addCleanup(os.chdir, previous)
        for name in ("agent", "agent.runtime_cwd"):
            self.addCleanup(sys.modules.pop, name, None)
            sys.modules.pop(name, None)

    def _record(self, payload, **_kwargs):
        self.seen.append(payload["cwd"])
        return {}

    def _call(self, **extra):
        self.plugin.pre_llm_call(**{**_hermes_kwargs(), **extra})
        return self.seen[-1]

    def _fake_hermes_resolver(self, result):
        package = types.ModuleType("agent")
        package.__path__ = []
        module = types.ModuleType("agent.runtime_cwd")
        if isinstance(result, Exception):
            def resolve_agent_cwd():
                raise result
        else:
            def resolve_agent_cwd():
                return result
        module.resolve_agent_cwd = resolve_agent_cwd
        sys.modules["agent"] = package
        sys.modules["agent.runtime_cwd"] = module

    def test_terminal_cwd_wins_over_the_launch_directory(self):
        with patch.dict(os.environ, {"TERMINAL_CWD": str(self.worktree)}):
            self.assertEqual(self._call(), str(self.worktree))

    def test_hermes_own_resolver_wins_when_it_is_importable(self):
        self._fake_hermes_resolver(self.session)
        with patch.dict(os.environ, {"TERMINAL_CWD": str(self.worktree)}):
            self.assertEqual(self._call(), str(self.session))

    def test_an_agent_package_hermes_did_not_load_is_never_imported(self):
        """The hook must not import `agent.runtime_cwd` itself: whatever
        `agent` package sits first on sys.path would run inside Hermes's hook
        thread and could name any directory. Only a module Hermes already
        loaded is consulted."""
        planted = Path(self._tmp.name) / "planted"
        (planted / "agent").mkdir(parents=True)
        marker = planted / "side-effect-ran"
        (planted / "agent" / "__init__.py").write_text(f"open({str(marker)!r}, 'w').write('x')\n")
        (planted / "agent" / "runtime_cwd.py").write_text(
            f"from pathlib import Path\ndef resolve_agent_cwd():\n    return Path({str(self.session)!r})\n")
        sys.path.insert(0, str(planted))
        self.addCleanup(sys.path.remove, str(planted))
        with patch.dict(os.environ, {"TERMINAL_CWD": str(self.worktree)}):
            self.assertEqual(self._call(), str(self.worktree))
        self.assertFalse(marker.exists(), "the hook imported a package Hermes never loaded")
        self.assertNotIn("agent.runtime_cwd", sys.modules)

    def test_a_broken_or_odd_resolver_falls_back_to_terminal_cwd(self):
        for result in (RuntimeError("boom"), self.launch / "missing", "relative/dir", 42):
            with self.subTest(result=result):
                self._fake_hermes_resolver(result)
                with patch.dict(os.environ, {"TERMINAL_CWD": str(self.worktree)}):
                    self.assertEqual(self._call(), str(self.worktree))

    def test_an_unusable_terminal_cwd_falls_back_to_the_launch_directory(self):
        for value in ("", "relative/dir", str(self.launch / "missing")):
            with self.subTest(value=value), patch.dict(os.environ, {"TERMINAL_CWD": value}):
                self.assertEqual(self._call(), str(self.launch))

    def test_without_any_hermes_signal_the_launch_directory_is_used(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TERMINAL_CWD", None)
            self.assertEqual(self._call(), str(self.launch))

    def test_an_explicit_cwd_argument_still_wins(self):
        with patch.dict(os.environ, {"TERMINAL_CWD": str(self.worktree)}):
            self.assertEqual(self._call(cwd=str(self.session)), str(self.session))

    def test_a_deleted_launch_directory_never_raises_into_hermes(self):
        with patch.dict(os.environ, {}, clear=False), \
             patch.object(self.plugin.os, "getcwd", side_effect=FileNotFoundError("gone")):
            os.environ.pop("TERMINAL_CWD", None)
            self.assertIsNone(self.plugin.pre_llm_call(**_hermes_kwargs()))
        self.assertEqual(self.seen, [])


if __name__ == "__main__":
    unittest.main()
