"""A folder reached through your own symlink works; one someone else could swap does not.

Any symlink anywhere in a path used to switch the layer off: a home folder on
a symlinked mount could not enroll at all, and a project opened through a
link got silent hooks. A symlinked folder on the way is now accepted when
only this user or the system could have made or replaced it. Private files
themselves are never symlinks, and reading files from a repository stays
strict.
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
from jev_auto.common import AutoError, state_dir, trusted_path, workspace  # noqa: E402
from jev_auto.prepare import candidates  # noqa: E402
from jev_auto.settings import (  # noqa: E402
    DESCENDANT_COVERAGE_APPROVED,
    enrollment_binding,
    load_policy,
    make_policy,
    save_policy_new,
)


def _repo(path: Path) -> None:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)


class _Tmp(unittest.TestCase):
    def setUp(self):
        common._ROOTS.clear()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()

    def state_under(self, folder: Path):
        env = patch.dict(os.environ, {"XDG_STATE_HOME": str(folder / "state")})
        env.start()
        self.addCleanup(env.stop)


class TrustedLinkTests(_Tmp):
    def test_state_under_your_own_symlinked_folder_works(self):
        real = self.root / "real-home"
        real.mkdir()
        (self.root / "home").symlink_to(real, target_is_directory=True)
        self.state_under(self.root / "home")
        project = self.root / "project"
        _repo(project)
        save_policy_new(project, make_policy(project, "typesafe"))
        self.assertEqual(load_policy(project)["provider"], "typesafe")
        self.assertTrue((real / "state").is_dir())

    def test_a_project_opened_through_your_own_link_uses_its_real_grant(self):
        self.state_under(self.root)
        real = self.root / "real"
        project = real / "proj"
        _repo(project)
        save_policy_new(project, make_policy(project, "typesafe"))
        (self.root / "code").symlink_to(real, target_is_directory=True)
        binding = enrollment_binding(self.root / "code" / "proj")
        self.assertEqual((binding["scope"], binding["workspace"]), ("exact", project))
        link_to_project = self.root / "shortcut"
        link_to_project.symlink_to(project, target_is_directory=True)
        self.assertEqual(workspace(link_to_project), project)

    def test_a_link_inside_a_covered_folder_cannot_borrow_its_grant(self):
        self.state_under(self.root)
        granted = self.root / "granted"
        granted.mkdir()
        save_policy_new(granted, make_policy(granted, "typesafe", covers_descendants=True,
                                             descendant_approval=DESCENDANT_COVERAGE_APPROVED))
        outside = self.root / "outside"
        outside.mkdir()
        (granted / "alias").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(AutoError) as caught:
            enrollment_binding(granted / "alias")
        self.assertEqual(str(caught.exception), "WORKSPACE_NOT_ENROLLED")

    def test_a_link_in_a_folder_others_can_write_is_refused(self):
        shared = self.root / "shared"
        shared.mkdir()
        shared.chmod(0o777)
        self.addCleanup(shared.chmod, 0o700)
        target = self.root / "target"
        target.mkdir()
        (shared / "link").symlink_to(target, target_is_directory=True)
        for path in (shared / "link", shared / "link" / "inner"):
            with self.subTest(path=path.name), self.assertRaises(AutoError) as caught:
                trusted_path(path, allow_link_leaf=True)
            self.assertEqual(str(caught.exception), "SYMLINK_NOT_ALLOWED")

    def test_a_private_file_itself_is_never_a_symlink(self):
        self.state_under(self.root)
        project = self.root / "project"
        _repo(project)
        save_policy_new(project, make_policy(project, "typesafe"))
        policy = state_dir(project) / "policy.json"
        real = policy.with_name("elsewhere.json")
        policy.rename(real)
        policy.symlink_to(real)
        with self.assertRaises(AutoError) as caught:
            load_policy(project)
        self.assertEqual(str(caught.exception), "SYMLINK_NOT_ALLOWED")


class RepositoryReadsStayStrictTests(_Tmp):
    def test_skill_files_behind_a_link_in_a_repository_are_not_read(self):
        self.state_under(self.root)
        repo = self.root / "cloned"
        _repo(repo)
        elsewhere = self.root / "private-notes" / "auth-login"
        elsewhere.mkdir(parents=True)
        (elsewhere / "SKILL.md").write_text("---\nname: auth login test\n---\nauth login test fix notes\n")
        skills = repo / ".agents" / "skills"
        skills.mkdir(parents=True)
        (skills / "auth-login").symlink_to(elsewhere, target_is_directory=True)
        ids = [item["id"] for item in candidates(repo, "Please fix the auth login test in this project now.")]
        self.assertNotIn("skill:auth-login", ids)


if __name__ == "__main__":
    unittest.main()
