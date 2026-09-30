"""An exact grant that merely expired must not shadow an approved ancestor.

Withdrawal is different from expiry: a disabled exact grant or a recorded
refusal still blocks the ancestor, so `jev revoke` keeps meaning "not here".
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


def _rewrite(path: Path, **changes) -> None:
    policy_file = state_dir(path) / "policy.json"
    write_private(policy_file, {**json.loads(policy_file.read_text()), **changes})


class ExpiredGrantFallthroughTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        env = patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "state-home")})
        env.start()
        self.addCleanup(env.stop)
        self.parent = self.root / "parent"
        self.child = self.parent / "nested"
        _repo(self.parent)
        _repo(self.child)

    def _cover_parent(self):
        policy = make_policy(self.parent, "typesafe", covers_descendants=True,
                             descendant_approval=DESCENDANT_COVERAGE_APPROVED)
        save_policy_new(self.parent, policy)
        return policy

    def _child_grant(self):
        save_policy_new(self.child, make_policy(self.child, "openrouter"))

    def test_an_expired_child_falls_through_to_the_covering_parent(self):
        parent = self._cover_parent()
        self._child_grant()
        _rewrite(self.child, expires_at=time.time() - 60)
        binding = enrollment_binding(self.child)
        self.assertEqual(binding["scope"], "descendant")
        self.assertEqual(binding["workspace"], workspace(self.parent))
        self.assertEqual(binding["policy"]["policy_id"], parent["policy_id"])

    def test_an_unexpired_child_still_wins(self):
        self._cover_parent()
        self._child_grant()
        self.assertEqual(enrollment_binding(self.child)["scope"], "exact")
        self.assertEqual(enrollment_binding(self.child)["policy"]["provider"], "openrouter")

    def test_a_revoked_child_still_blocks_the_parent(self):
        self._cover_parent()
        self._child_grant()
        revoke(self.child)
        with self.assertRaisesRegex(AutoError, "AUTO_DISABLED_OR_EXPIRED"):
            enrollment_binding(self.child)

    def test_a_revoked_child_that_also_expired_still_blocks_the_parent(self):
        self._cover_parent()
        self._child_grant()
        revoke(self.child)
        _rewrite(self.child, expires_at=time.time() - 60)
        with self.assertRaisesRegex(AutoError, "AUTO_DISABLED_OR_EXPIRED"):
            enrollment_binding(self.child)

    def test_a_refused_child_still_blocks_the_parent(self):
        self._cover_parent()
        revoke(self.child)
        with self.assertRaisesRegex(AutoError, "AUTO_DISABLED_OR_EXPIRED"):
            enrollment_binding(self.child)

    def test_an_expired_child_without_a_covering_parent_stays_expired(self):
        self._child_grant()
        _rewrite(self.child, expires_at=time.time() - 60)
        with self.assertRaisesRegex(AutoError, "AUTO_DISABLED_OR_EXPIRED"):
            enrollment_binding(self.child)

    def test_an_expired_parent_covers_nothing(self):
        self._cover_parent()
        _rewrite(self.parent, expires_at=time.time() - 60)
        with self.assertRaisesRegex(AutoError, "WORKSPACE_NOT_ENROLLED"):
            enrollment_binding(self.child)

    def test_a_malformed_expiry_is_not_treated_as_a_plain_expiry(self):
        self._cover_parent()
        self._child_grant()
        _rewrite(self.child, expires_at="soon")
        with self.assertRaisesRegex(AutoError, "AUTO_DISABLED_OR_EXPIRED"):
            enrollment_binding(self.child)


class EnrollmentStateTests(ExpiredGrantFallthroughTests):
    def test_states_distinguish_expiry_from_withdrawal(self):
        self.assertEqual(enrollment_state(self.child)["state"], "not_enrolled")
        self._child_grant()
        self.assertEqual(enrollment_state(self.child)["state"], "enrolled")
        _rewrite(self.child, expires_at=time.time() - 60)
        self.assertEqual(enrollment_state(self.child)["state"], "expired")
        revoke(self.child)
        self.assertEqual(enrollment_state(self.child)["state"], "withdrawn")

    def test_enrolled_state_carries_the_binding(self):
        parent = self._cover_parent()
        state = enrollment_state(self.child)
        self.assertEqual(state["state"], "enrolled")
        self.assertEqual(state["scope"], "descendant")
        self.assertEqual(state["workspace"], workspace(self.parent))
        self.assertEqual(state["policy"]["policy_id"], parent["policy_id"])

    def test_other_errors_are_not_folded_into_a_state(self):
        with self.assertRaisesRegex(AutoError, "WORKSPACE_REQUIRED"):
            enrollment_state(self.root / "missing")


if __name__ == "__main__":
    unittest.main()
