"""Another user pre-creating the broker folder name in /tmp cannot block Jev.

The broker socket lives in a per-user folder with a fixed name in the shared
temp folder. On a shared machine another account could create that name
first, and every broker start then failed. A folder with a random name,
recorded in this user's private state, is used instead.
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

from jev_auto import ipc  # noqa: E402
from jev_auto.common import AutoError  # noqa: E402

REAL_PRIVATE_DIR = ipc.private_dir


class SquatTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name).resolve()
        env = patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state")})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("XDG_RUNTIME_DIR", None)  # restored by env.stop
        self.workspace = root / "project"
        self.workspace.mkdir()
        self.made = []

    def squatted(self, path):
        if path.name == f"qualixar-jev-decision-layer-{os.getuid()}":
            raise AutoError("PRIVATE_DIRECTORY_OWNER")
        self.made.append(path)
        return REAL_PRIVATE_DIR(path)

    def test_a_squatted_folder_falls_back_to_a_private_random_one_used_consistently(self):
        with patch.object(ipc, "private_dir", side_effect=self.squatted):
            first = ipc.address(self.workspace)
            second = ipc.address(self.workspace)
        self.assertEqual(first, second)
        self.assertNotIn(f"qualixar-jev-decision-layer-{os.getuid()}", str(first))
        if sys.platform == "darwin":
            # This user's own temporary folder, which other accounts cannot list.
            self.assertEqual(first.parent, ipc._darwin_user_temp() / "qj")
        else:
            self.assertRegex(first.parent.name, r"^qualixar-jev-[0-9]+-[0-9a-f]{16}$")
        self.assertLessEqual(len(str(first).encode()), 100)

    def test_a_taken_random_name_is_replaced_by_a_new_one(self):
        taken = []

        def squatted_twice(path):
            if path.name == f"qualixar-jev-decision-layer-{os.getuid()}" or (path.name.startswith("qualixar-jev-")
                                                                             and not taken):
                taken.append(path.name)
                raise AutoError("PRIVATE_DIRECTORY_OWNER")
            return REAL_PRIVATE_DIR(path)

        with patch.object(ipc.sys, "platform", "linux"), patch.object(ipc, "private_dir", side_effect=squatted_twice):
            folder = ipc._private_socket_folder(Path(tempfile.gettempdir()).resolve(), os.getuid())
        self.assertNotIn(folder.name, taken)
        self.assertRegex(folder.name, r"^qualixar-jev-[0-9]+-[0-9a-f]{16}$")

    def test_any_other_folder_problem_is_still_reported(self):
        def broken(path):
            raise AutoError("SYMLINK_NOT_ALLOWED")
        with patch.object(ipc, "private_dir", side_effect=broken), self.assertRaises(AutoError) as caught:
            ipc.address(self.workspace)
        self.assertEqual(str(caught.exception), "SYMLINK_NOT_ALLOWED")


if __name__ == "__main__":
    unittest.main()
