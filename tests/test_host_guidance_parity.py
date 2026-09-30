"""Codex and Antigravity tell the model the same exact arguments Claude Code does.

A model told to "use Jev" without the workspace path and data scope guesses
them, and a wrong guess is a failed call it rarely repeats. The Claude hook
already named them; the Codex session and subagent hooks and the Antigravity
first-invocation hint now carry the same guidance, including the Jev + Laya
rule, and none of them carries undefined internal jargon.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "plugins" / "qualixar-jev-decision-layer" / "runtime"
sys.path.insert(0, str(RUNTIME))

from jev_auto import agy_hook, hooks  # noqa: E402
from jev_auto.common import workspace  # noqa: E402
from jev_auto.settings import make_policy, save_policy_new  # noqa: E402

JARGON = ("Preserve ", "Computer Use skill", "standing budget")


class _Enrolled(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        env = patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "state")})
        env.start()
        self.addCleanup(env.stop)
        self.repo = self.root / "project"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)

    def enroll(self, **fields):
        save_policy_new(self.repo, make_policy(self.repo, "typesafe", generic_query_enabled=True, **fields))

    def codex(self, event_name):
        event = {"hook_event_name": event_name, "cwd": str(self.repo), "session_id": "s-1"}
        out = hooks.handle(event, caller=lambda *_: {}, starter=lambda *a, **k: None)
        return out["hookSpecificOutput"]["additionalContext"]

    def agy(self):
        out = agy_hook.handle({"invocationNum": 0, "workspacePaths": [str(self.repo)]})
        return out["injectSteps"][0]["ephemeralMessage"]


class ParityTests(_Enrolled):
    def test_codex_session_and_subagents_name_the_exact_arguments(self):
        self.enroll()
        for event_name in ("SessionStart", "SubagentStart"):
            with self.subTest(event=event_name):
                text = self.codex(event_name)
                self.assertIn("workspace_path=" + json.dumps(str(workspace(self.repo))), text)
                self.assertIn('data_classification="public"', text)
                self.assertIn("jev_route", text)
                self.assertIn("Never create or widen a Jev grant yourself", text)

    def test_antigravity_names_the_exact_arguments_and_stays_advisory(self):
        self.enroll()
        text = self.agy()
        self.assertIn("workspace_path=" + json.dumps(str(workspace(self.repo))), text)
        self.assertIn('data_classification="public"', text)
        self.assertIn("advisory", text)
        self.assertIn("native permissions", text)
        self.assertLess(len(text), 1000)

    def test_a_jev_plus_laya_grant_says_how_to_keep_private_content_local_everywhere(self):
        self.enroll(local_laya_enabled=True, mlx={"repository": "aac6fef/laya-mlx"},
                    data_classification="internal-minimized")
        for label, text in (("codex", self.codex("SessionStart")), ("agy", self.agy())):
            with self.subTest(host=label):
                self.assertIn('data_classification="restricted"', text)
                self.assertIn("Laya on this Mac", text)

    def test_no_host_text_carries_undefined_jargon(self):
        self.enroll()
        for label, text in (("codex-session", self.codex("SessionStart")),
                            ("codex-subagent", self.codex("SubagentStart")), ("agy", self.agy())):
            for phrase in JARGON:
                with self.subTest(host=label, phrase=phrase):
                    self.assertNotIn(phrase, text)


if __name__ == "__main__":
    unittest.main()
