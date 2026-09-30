"""`_canonical_workspace_path` checks each property once, on the final path.

1.0.11 removed the duplicated checks that validated a property on the raw
value and then again after normalisation, where the second copy could never
fire. This differential test keeps the previous implementation as a frozen
reference and requires the current one to accept and reject exactly the same
inputs, so the restructuring cannot have widened what Hermes treats as a
workspace path.
"""

from __future__ import annotations

import ntpath
import posixpath
import random
import sys
import unittest
from pathlib import Path, PureWindowsPath

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "plugins" / "qualixar-jev-decision-layer" / "runtime"
sys.path.insert(0, str(RUNTIME))

from jev_auto import hermes_tool  # noqa: E402

MAX = hermes_tool.MAX_WORKSPACE_PATH_CHARS


def _reference(value, *, windows):
    """The 1.0.10 implementation, frozen verbatim."""
    if not isinstance(value, str) or not 1 <= len(value) <= MAX or "\x00" in value:
        return None
    if windows:
        path = PureWindowsPath(value)
        if (not path.is_absolute() or not path.drive or path.drive.startswith("\\\\")
                or value.startswith(("\\\\?\\", "\\\\.\\"))):
            return None
        normalized = ntpath.normpath(value)
        canonical = PureWindowsPath(normalized)
        if (not canonical.is_absolute() or canonical.drive.startswith("\\\\")
                or len(normalized) > MAX):
            return None
        return normalized
    if not value.startswith("/"):
        return None
    normalized = posixpath.normpath(value)
    if not normalized.startswith("/") or len(normalized) > MAX:
        return None
    return normalized


_CURATED = [
    None, 7, b"/bytes", "", "\x00", "/a\x00b", "a" * (MAX + 1), "/" + "a" * MAX, "/" + "a" * (MAX - 1),
    "relative", "./relative", "../up", "a/b", "/", "//", "///x", "/a/./b/../c", "/../..", "/a//b/",
    "C:", "C:foo", "C:\\", "C:/", "C:\\a\\..\\..", "C:/a/./b", "c:\\x", "Z:\\a b\\c.",
    "\\\\server\\share", "\\\\server\\share\\x", "//server/share/x", "\\\\?\\C:\\x", "\\\\.\\pipe\\x",
    "\\\\?\\UNC\\s\\x", "\\x", "/x", "\\\\", "C:\\\\\\a", "C:..", "C:\\..\\..\\a",
]


def _fuzz(seed: int, count: int) -> list[str]:
    random.seed(seed)
    heads = ["", "/", "//", "\\", "\\\\", "C:", "C:\\", "C:/", "c:", "\\\\?\\", "\\\\.\\", "\\\\srv\\sh", "..", "."]
    parts = ["..", ".", "a", "", "b..", "..a", "a.", "...", "a b", "C:", "\\\\x"]
    separators = ["/", "\\", "//", "\\\\"]
    values = []
    for _ in range(count):
        separator = random.choice(separators)
        values.append(random.choice(heads) + separator.join(random.choice(parts)
                                                           for _ in range(random.randint(0, 6))))
    return values


class CanonicalWorkspacePathTests(unittest.TestCase):
    def test_behaviour_matches_the_previous_implementation_exactly(self):
        inputs = _CURATED + _fuzz(20260930, 40_000)
        for windows in (True, False):
            mismatches = [(value, _reference(value, windows=windows),
                           hermes_tool._canonical_workspace_path(value, windows=windows))
                          for value in inputs
                          if _reference(value, windows=windows)
                          != hermes_tool._canonical_workspace_path(value, windows=windows)]
            with self.subTest(windows=windows):
                self.assertEqual(mismatches[:5], [])

    def test_a_relative_path_that_normalises_to_an_absolute_one_is_refused(self):
        """ntpath.normpath("./C:/a") is "C:\\a". Validating only the normalised
        value would accept it; the raw value is relative and must be refused."""
        self.assertEqual(ntpath.normpath("./C:/a"), "C:\\a")
        for value in ("./C:/a", ".//C://a b", "./C:///x"):
            with self.subTest(value=value):
                self.assertIsNone(hermes_tool._canonical_workspace_path(value, windows=True))

    def test_relative_and_drive_relative_paths_are_refused(self):
        for value in ("relative", "./x", "../x", "C:foo", "C:..", "\\x"):
            with self.subTest(value=value):
                self.assertIsNone(hermes_tool._canonical_workspace_path(value, windows=True))
        for value in ("relative", "./x", "../x"):
            with self.subTest(value=value):
                self.assertIsNone(hermes_tool._canonical_workspace_path(value, windows=False))

    def test_remote_and_device_paths_are_refused_before_normalisation(self):
        for value in ("\\\\server\\share\\x", "//server/share/x", "\\\\?\\C:\\x", "\\\\.\\pipe\\x"):
            with self.subTest(value=value):
                self.assertIsNone(hermes_tool._canonical_workspace_path(value, windows=True))

    def test_accepted_paths_come_back_normalised(self):
        self.assertEqual(hermes_tool._canonical_workspace_path("C:/a/./b/../c", windows=True), "C:\\a\\c")
        self.assertEqual(hermes_tool._canonical_workspace_path("/a//b/./c/..", windows=False), "/a/b")


if __name__ == "__main__":
    unittest.main()
