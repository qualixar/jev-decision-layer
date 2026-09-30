"""Resolving a workspace no longer starts a git process for every folder, every time.

Hooks resolve the same folders many times per prompt, and each lookup used to
start `git`. A Codex prompt measured 6.5 to 13 seconds before any decision was
made. Git is now asked only when a `.git` entry exists at or above the folder,
and each answer is kept for 30 seconds. The symlink refusal is not cached.
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

REAL_RUN = subprocess.run


class _Counted(unittest.TestCase):
    def setUp(self):
        common._ROOTS.clear()
        self.addCleanup(common._ROOTS.clear)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.spawns = []
        env = {key: value for key, value in os.environ.items()
               if key not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_CEILING_DIRECTORIES")}
        environment = patch.dict(os.environ, env, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

        def counting(args, *rest, **kwargs):
            if args[:1] == ["git"] and "rev-parse" in args:
                self.spawns.append(args)
            return REAL_RUN(args, *rest, **kwargs)

        runner = patch.object(common.subprocess, "run", side_effect=counting)
        runner.start()
        self.addCleanup(runner.stop)


class LookupCostTests(_Counted):
    def test_a_folder_with_no_repository_above_never_starts_git(self):
        folder = self.root / "a" / "b" / "c"
        folder.mkdir(parents=True)
        for _ in range(5):
            self.assertEqual(workspace(folder), folder)
        self.assertEqual(self.spawns, [])

    def test_a_repository_folder_asks_git_once_and_is_remembered(self):
        repo = self.root / "repo"
        inside = repo / "src" / "pkg"
        inside.mkdir(parents=True)
        REAL_RUN(["git", "init", "-q", str(repo)], check=True)
        for _ in range(5):
            self.assertEqual(workspace(inside), repo)
        self.assertEqual(len(self.spawns), 1)

    def test_a_repository_created_later_is_noticed_once_the_answer_expires(self):
        folder = self.root / "later" / "child"
        folder.mkdir(parents=True)
        self.assertEqual(workspace(folder), folder)
        REAL_RUN(["git", "init", "-q", str(self.root / "later")], check=True)
        with patch.object(common.time, "monotonic", return_value=common.time.monotonic() + 31):
            self.assertEqual(workspace(folder), self.root / "later")

    def test_a_folder_swapped_for_an_untrusted_symlink_is_refused_even_when_remembered(self):
        shared = self.root / "shared"
        shared.mkdir()
        folder = shared / "real"
        folder.mkdir()
        self.assertEqual(workspace(folder), folder)
        folder.rename(self.root / "moved")
        folder.symlink_to(self.root / "moved", target_is_directory=True)
        shared.chmod(0o777)  # anyone could now have swapped the link
        self.addCleanup(shared.chmod, 0o700)
        with self.assertRaises(AutoError) as caught:
            workspace(folder)
        self.assertEqual(str(caught.exception), "SYMLINK_NOT_ALLOWED")

    def test_git_pointed_elsewhere_by_the_environment_is_always_asked(self):
        folder = self.root / "plain"
        folder.mkdir()
        with patch.dict(os.environ, {"GIT_CEILING_DIRECTORIES": str(self.root)}):
            workspace(folder)
        self.assertEqual(len(self.spawns), 1)


if __name__ == "__main__":
    unittest.main()
