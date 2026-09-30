"""A folder with its own narrower grant is never overridden inside by a broader parent.

With `~/Documents` covered at Jev maximum and a client folder given its own
narrower grant (for example Laya only), the client folder's subfolders and
nested repositories used to fall through to the parent's hosted grant. The
nearest recorded choice now governs: if it does not cover its subfolders,
they are not covered at all until the user extends it.
"""

from __future__ import annotations

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

from jev_auto import common  # noqa: E402
from jev_auto.common import AutoError, workspace  # noqa: E402
from jev_auto.settings import (  # noqa: E402
    DESCENDANT_COVERAGE_APPROVED,
    enrollment_binding,
    make_policy,
    save_policy_new,
)


def _git(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)


class NarrowChildTests(unittest.TestCase):
    def setUp(self):
        common._ROOTS.clear()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name).resolve()
        env = patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state")})
        env.start()
        self.addCleanup(env.stop)
        self.parent = root / "Documents"
        self.parent.mkdir()
        save_policy_new(self.parent, make_policy(self.parent, "typesafe", covers_descendants=True,
                                                 descendant_approval=DESCENDANT_COVERAGE_APPROVED))
        self.client_a = self.parent / "client-a"
        (self.client_a / "contracts").mkdir(parents=True)
        _git(self.client_a / "app")
        save_policy_new(self.client_a, make_policy(self.client_a, "openrouter"))
        self.client_b = self.parent / "client-b"
        _git(self.client_b)
        (self.client_b / "src").mkdir()
        _git(self.client_b / "vendor" / "lib")
        save_policy_new(self.client_b, make_policy(self.client_b, "openrouter"))

    def assertNotCovered(self, path):
        with self.assertRaises(AutoError) as caught:
            enrollment_binding(path)
        self.assertEqual(str(caught.exception), "WORKSPACE_NOT_ENROLLED")

    def test_the_narrower_folder_keeps_its_own_grant(self):
        self.assertEqual(enrollment_binding(self.client_a)["policy"]["provider"], "openrouter")
        self.assertEqual(enrollment_binding(self.client_b / "src")["policy"]["provider"], "openrouter")

    def test_its_subfolders_and_nested_repositories_do_not_fall_through_to_the_parent(self):
        for path in (self.client_a / "contracts", self.client_a / "app", self.client_b / "vendor" / "lib"):
            with self.subTest(path=path.name):
                self.assertNotCovered(path)

    def test_other_children_of_the_parent_stay_covered(self):
        other = self.parent / "notes"
        other.mkdir()
        binding = enrollment_binding(other)
        self.assertEqual((binding["scope"], binding["workspace"]), ("descendant", workspace(self.parent)))


if __name__ == "__main__":
    unittest.main()
