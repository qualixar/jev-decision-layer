"""On a Mac, a folder typed in different letter case is the same folder.

The default macOS volume ignores letter case, so `client-x` and `Client-X`
name one folder. Workspace identity used the spelling as typed, so revoking
`client-x` recorded the refusal under a second identity and reported
success while the real folder stayed covered. The on-disk spelling is now
used everywhere a workspace is named.
"""

from __future__ import annotations

import os
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
    revoke,
    save_policy_new,
)


def _case_insensitive(folder: Path) -> bool:
    probe = folder / "CaseProbe"
    probe.mkdir()
    return (folder / "caseprobe").exists()


class CaseTests(unittest.TestCase):
    def setUp(self):
        common._ROOTS.clear()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        if not _case_insensitive(self.root):
            self.skipTest("this volume distinguishes letter case")
        env = patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "state")})
        env.start()
        self.addCleanup(env.stop)
        self.granted = self.root / "Documents"
        self.client = self.granted / "Client-X"
        self.client.mkdir(parents=True)
        save_policy_new(self.granted, make_policy(self.granted, "typesafe", covers_descendants=True,
                                                  descendant_approval=DESCENDANT_COVERAGE_APPROVED))

    def test_any_spelling_names_the_folder_as_it_is_on_disk(self):
        self.assertEqual(workspace(self.root / "documents" / "client-x"), self.client)

    def test_a_revoke_typed_in_other_letter_case_applies_to_the_real_folder(self):
        revoke(self.root / "documents" / "CLIENT-x")
        for spelling in (self.client, self.root / "documents" / "client-x"):
            with self.subTest(spelling=str(spelling)), self.assertRaises(AutoError) as caught:
                enrollment_binding(spelling)
            self.assertEqual(str(caught.exception), "AUTO_DISABLED_OR_EXPIRED")

    def test_a_grant_found_through_another_spelling_is_the_same_grant(self):
        binding = enrollment_binding(self.root / "DOCUMENTS")
        self.assertEqual((binding["scope"], binding["workspace"]), ("exact", self.granted))


if __name__ == "__main__":
    unittest.main()
