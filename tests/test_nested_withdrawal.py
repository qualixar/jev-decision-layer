"""A folder you revoke or refuse stays off, and so does everything inside it.

A grant that covers child folders used to walk past a nearer revoked or
refused folder and keep covering the projects inside it, at the grant's own
data scope. The nearest recorded choice now governs. A plain expiry is not a
withdrawal: an expired folder still falls through to the covering grant, as
documented. A file is governed exactly like the folder that holds it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "plugins" / "qualixar-jev-decision-layer" / "runtime"
sys.path.insert(0, str(RUNTIME))

from jev_auto.claude_hook import handle as claude_handle  # noqa: E402
from jev_auto.common import AutoError, state_dir, workspace, write_private  # noqa: E402
from jev_auto.settings import (  # noqa: E402
    DESCENDANT_COVERAGE_APPROVED,
    enrollment_binding,
    enrollment_state,
    make_policy,
    revoke,
    save_policy_new,
)


def _repo(path: Path) -> None:
    path.mkdir(parents=True)
    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True, capture_output=True)


def _cover(path: Path) -> None:
    save_policy_new(path, make_policy(path, "typesafe", covers_descendants=True,
                                      descendant_approval=DESCENDANT_COVERAGE_APPROVED))


class _Tree(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        env = patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "state-home")})
        env.start()
        self.addCleanup(env.stop)
        self.granted = self.root / "granted"
        self.granted.mkdir()
        _cover(self.granted)
        self.client = self.granted / "client"
        self.project = self.client / "app"
        _repo(self.project)
        self.plain = self.client / "notes"
        self.plain.mkdir()

    def assertOff(self, path: Path):
        with self.assertRaises(AutoError) as caught:
            enrollment_binding(path)
        self.assertEqual(str(caught.exception), "AUTO_DISABLED_OR_EXPIRED")
        self.assertEqual(enrollment_state(path)["state"], "withdrawn")


class WithdrawnFolderTests(_Tree):
    def test_everything_inside_a_refused_folder_is_off(self):
        self.assertEqual(enrollment_binding(self.project)["scope"], "descendant")
        revoke(self.client)
        self.assertOff(self.client)
        self.assertOff(self.project)
        self.assertOff(self.plain)
        deeper = self.project / "src" / "deeper"
        deeper.mkdir(parents=True)
        self.assertOff(deeper)

    def test_everything_inside_a_revoked_exact_grant_is_off(self):
        save_policy_new(self.client, make_policy(self.client, "typesafe"))
        revoke(self.client)
        self.assertOff(self.client)
        self.assertOff(self.project)
        self.assertOff(self.plain)

    def test_every_hook_stays_silent_inside_a_refused_folder(self):
        revoke(self.client)
        for event in ("SessionStart", "UserPromptSubmit", "SubagentStart"):
            with self.subTest(event=event):
                payload = {"hook_event_name": event, "cwd": str(self.project), "session_id": "s-1"}
                self.assertEqual(claude_handle(payload), "")

    def test_an_unreadable_nearer_choice_fails_closed(self):
        destination = state_dir(self.client) / "policy.json"
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        destination.write_text("{not json")
        destination.chmod(0o600)
        with self.assertRaises(AutoError) as caught:
            enrollment_binding(self.project)
        self.assertEqual(str(caught.exception), "AUTO_DISABLED_OR_EXPIRED")

    def test_revoking_a_project_inside_a_refused_folder_records_its_own_refusal(self):
        revoke(self.client)
        revoke(self.project)
        policy = json.loads((state_dir(self.project) / "policy.json").read_text())
        self.assertEqual((policy["enabled"], policy["consent"]), (False, "refused"))

    def test_refusing_the_same_folder_twice_is_not_an_error(self):
        revoke(self.client)
        revoke(self.client)
        self.assertOff(self.client)


class ExpiryIsNotWithdrawalTests(_Tree):
    def test_an_expired_nearer_grant_still_falls_through_to_the_covering_grant(self):
        save_policy_new(self.client, make_policy(self.client, "typesafe"))
        policy_file = state_dir(self.client) / "policy.json"
        policy = json.loads(policy_file.read_text())
        write_private(policy_file, {**policy, "expires_at": time.time() - 60})
        binding = enrollment_binding(self.project)
        self.assertEqual((binding["scope"], binding["workspace"]), ("descendant", workspace(self.granted)))

    def test_a_covered_folder_with_no_choice_of_its_own_stays_covered(self):
        binding = enrollment_binding(self.project)
        self.assertEqual((binding["scope"], binding["workspace"]), ("descendant", workspace(self.granted)))


class FileInsideAGrantTests(_Tree):
    def test_a_file_directly_in_the_covering_folder_is_covered(self):
        notes = self.granted / "notes.txt"
        notes.write_text("x")
        binding = enrollment_binding(notes)
        self.assertEqual((binding["scope"], binding["workspace"]), ("exact", workspace(self.granted)))
        self.assertEqual(enrollment_state(notes)["state"], "enrolled")

    def test_a_file_in_an_exact_grant_is_covered(self):
        exact = self.root / "exact"
        exact.mkdir()
        save_policy_new(exact, make_policy(exact, "typesafe"))
        target = exact / "main.py"
        target.write_text("x")
        self.assertEqual(enrollment_binding(target)["scope"], "exact")

    def test_a_file_inside_a_refused_folder_is_off(self):
        revoke(self.client)
        target = self.plain / "memo.txt"
        target.write_text("x")
        self.assertOff(target)

    def test_a_file_in_an_expired_folder_reads_as_expired_not_withdrawn(self):
        lone = self.root / "lone"
        lone.mkdir()
        save_policy_new(lone, make_policy(lone, "typesafe"))
        policy_file = state_dir(lone) / "policy.json"
        write_private(policy_file, {**json.loads(policy_file.read_text()), "expires_at": time.time() - 60})
        target = lone / "a.txt"
        target.write_text("x")
        self.assertEqual(enrollment_state(target)["state"], "expired")


if __name__ == "__main__":
    unittest.main()
