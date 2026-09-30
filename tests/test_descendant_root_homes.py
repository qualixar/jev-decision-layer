"""Only real home folders are refused as a covering root, not any folder named "home".

Child coverage may not attach to a home folder: yours, or another user's.
The check used to refuse any folder named `home` or `Users` with one child
anywhere in a path, so a project at `~/work/home/website` could not cover its
own subfolders. It now matches the places home folders actually live.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "plugins" / "qualixar-jev-decision-layer" / "runtime"
sys.path.insert(0, str(RUNTIME))

from jev_auto.settings import descendant_root_allowed  # noqa: E402


class HomeFolderTests(unittest.TestCase):
    def test_real_home_locations_are_refused(self):
        for path in ("/Users/someone", "/home/someone", "/System/Volumes/Data/home/someone",
                     "/var/home/someone", "/export/home/someone", "/Volumes/Data/Users/someone"):
            with self.subTest(path=path), patch.object(Path, "resolve", lambda self, strict=False: self):
                self.assertFalse(descendant_root_allowed(Path(path)))

    def test_folders_that_are_only_named_like_a_home_are_allowed(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            for relative in ("home/Documents", "work/home/website", "clients/Users/portal"):
                with self.subTest(path=relative):
                    folder = base / relative
                    folder.mkdir(parents=True)
                    self.assertTrue(descendant_root_allowed(folder))

    def test_your_own_home_and_the_filesystem_top_stay_refused(self):
        self.assertFalse(descendant_root_allowed(Path.home()))
        self.assertFalse(descendant_root_allowed(Path("/")))


if __name__ == "__main__":
    unittest.main()
