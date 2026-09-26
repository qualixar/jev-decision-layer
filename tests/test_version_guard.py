"""The released version number must mean one tree, on every host.

Nothing enforced that until 1.0.8. Users do not install a tag -- the
marketplace resolves each plugin by relative path out of the default branch --
so a shipped change landing on main under an already-released number leaves two
trees answering to one version. A host holding that version sees no upgrade to
perform and the user keeps the old code while every version check agrees they
are current.

It happened here: 1.0.7's runtime changed twice under one number and the
installed cache went stale exactly that way.

These tests drive tools/version_guard.py against synthetic repositories rather
than the real one, so they assert on its judgement instead of on whatever
today's working tree happens to contain.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "tools" / "version_guard.py"
PLUGIN_JSON = Path("plugins/qualixar-jev-decision-layer/plugin.json")


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(("git", "-C", str(cwd), *args),
                          capture_output=True, text=True, check=False)


class _Repo:
    """A throwaway git repository shaped like this one, for the guard to judge."""

    def __init__(self, stack: unittest.TestCase):
        self.path = Path(tempfile.mkdtemp())
        stack.addCleanup(shutil.rmtree, self.path, ignore_errors=True)
        _git(self.path, "init", "--quiet", "-b", "main")
        _git(self.path, "config", "user.email", "guard@test.invalid")
        _git(self.path, "config", "user.name", "guard test")
        (self.path / "tools").mkdir()
        shutil.copy(GUARD, self.path / "tools" / "version_guard.py")
        for relative in (PLUGIN_JSON,
                         Path("plugins/qualixar-jev-codex/runtime/x.py"),
                         Path("plugins/qualixar-jev-decision-layer/hooks.json"),
                         Path(".claude-plugin/marketplace.json"),
                         Path("docs/GETTING_STARTED.md")):
            target = self.path / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("{}" if target.suffix == ".json" else "original\n")
        self.set_version("1.0.7")

    def set_version(self, version: str) -> None:
        (self.path / PLUGIN_JSON).write_text(json.dumps({"version": version}))

    def commit(self, message: str = "change") -> None:
        _git(self.path, "add", "-A")
        _git(self.path, "commit", "--quiet", "-m", message)

    def tag(self, name: str) -> None:
        _git(self.path, "tag", "-a", name, "-m", name)

    def write(self, relative: str, text: str) -> None:
        (self.path / relative).write_text(text)

    def run(self) -> subprocess.CompletedProcess:
        return subprocess.run((sys.executable, "tools/version_guard.py"),
                              cwd=self.path, capture_output=True, text=True, check=False)


class VersionGuard(unittest.TestCase):
    def setUp(self):
        self.repo = _Repo(self)
        self.repo.commit("release")
        self.repo.tag("v1.0.7")

    def test_an_untouched_release_passes(self):
        result = self.repo.run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("clean", result.stdout)

    def test_a_shipped_change_under_a_released_version_is_refused(self):
        """The defect this exists for: two trees, one version number."""
        for shipped in ("plugins/qualixar-jev-decision-layer/hooks.json",
                        "plugins/qualixar-jev-codex/runtime/x.py",
                        ".claude-plugin/marketplace.json"):
            with self.subTest(shipped=shipped):
                self.repo.write(shipped, "changed\n")
                self.repo.commit()
                result = self.repo.run()
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn(shipped, result.stderr)
                self.repo.write(shipped, "original\n")
                self.repo.commit()

    def test_every_host_package_is_covered_not_just_claudes(self):
        """Five hosts ship from these two packages; a guard on one is no guard."""
        for package in ("qualixar-jev-decision-layer", "qualixar-jev-codex"):
            with self.subTest(package=package):
                path = f"plugins/{package}/runtime/only.py"
                (self.repo.path / path).parent.mkdir(parents=True, exist_ok=True)
                self.repo.write(path, "new\n")
                self.repo.commit()
                self.assertEqual(self.repo.run().returncode, 1)
                (self.repo.path / path).unlink()
                self.repo.commit()

    def test_a_documentation_change_needs_no_new_version(self):
        """A README fix is not an upgrade. Bumping for one teaches people to
        ignore the number, which is the failure this guard is trying to avoid."""
        self.repo.write("docs/GETTING_STARTED.md", "rewritten\n")
        self.repo.commit()
        result = self.repo.run()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_bumping_the_version_is_how_you_clear_it(self):
        """The guard must not block the very fix it asks for."""
        self.repo.write("plugins/qualixar-jev-codex/runtime/x.py", "changed\n")
        self.repo.set_version("1.0.8")
        self.repo.commit()
        result = self.repo.run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("unreleased", result.stdout)

    def test_reusing_a_number_that_was_already_tagged_is_still_refused(self):
        """Bumping to a version someone already released is the same defect."""
        self.repo.set_version("1.0.8")
        self.repo.commit()
        self.repo.tag("v1.0.8")
        self.repo.write("plugins/qualixar-jev-codex/runtime/x.py", "changed again\n")
        self.repo.commit()
        result = self.repo.run()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("v1.0.8", result.stderr)

    def test_an_unpacked_archive_is_skipped_rather_than_failed(self):
        """Someone running the suite from a tarball has no tags to compare."""
        shutil.rmtree(self.repo.path / ".git")
        result = self.repo.run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("skipped", result.stdout)


class EveryHostIsCovered(unittest.TestCase):
    """A guard that watches one host is not a guard.

    Five harnesses install from this repository and each finds the layer
    through a different file. If any one of their entry points sits outside
    what the guard compares, that host can take a silent same-version change
    while the other four are protected -- which is worse than no guard,
    because the four passing checks imply the fifth was checked too.

    Each path below was located in the tree, not assumed.
    """

    HOST_ENTRY_POINTS: dict[str, tuple[str, ...]] = {
        "claude_code": (
            "plugins/qualixar-jev-decision-layer/.claude-plugin/plugin.json",
            "plugins/qualixar-jev-decision-layer/runtime/jev_auto/claude_hook.py",
        ),
        "codex": (
            "plugins/qualixar-jev-codex/.codex-plugin/plugin.json",
            "plugins/qualixar-jev-decision-layer/.codex-plugin/plugin.json",
            "plugins/qualixar-jev-codex/mcp.json",
        ),
        "antigravity": (
            # No manifest key declares this; Antigravity finds it by convention.
            "plugins/qualixar-jev-decision-layer/hooks.json",
            "plugins/qualixar-jev-decision-layer/runtime/jev_auto/agy_hook.py",
        ),
        "hermes": (
            "plugins/qualixar-jev-decision-layer/scripts/launch-hermes-tool",
            "plugins/qualixar-jev-decision-layer/scripts/launch-hermes-hook",
            "plugins/qualixar-jev-codex/runtime/jev_auto/hermes_tool.py",
        ),
        "vscode": (
            "plugins/qualixar-jev-decision-layer/commands/jev-vscode.md",
            "plugins/qualixar-jev-decision-layer/runtime/jev_auto/vscode_adapter.py",
            "plugins/qualixar-jev-codex/runtime/jev_auto/vscode_adapter.py",
        ),
    }

    def _shipped(self) -> tuple[str, ...]:
        sys.path.insert(0, str(ROOT / "tools"))
        try:
            import version_guard
            return version_guard.SHIPPED
        finally:
            sys.path.pop(0)

    def test_every_host_entry_point_is_watched_by_the_guard(self):
        shipped = self._shipped()
        for host, entry_points in self.HOST_ENTRY_POINTS.items():
            for entry in entry_points:
                with self.subTest(host=host, entry=entry):
                    self.assertTrue(
                        (ROOT / entry).exists(),
                        f"{entry} is gone -- the guard's coverage claim is stale")
                    self.assertTrue(
                        any(entry == rule or entry.startswith(rule.rstrip("/") + "/")
                            for rule in shipped),
                        f"{host}: {entry} is outside {shipped}, so that host can "
                        f"take a silent same-version change")

    def test_all_five_supported_hosts_are_listed_here(self):
        """Adding a sixth host without listing it leaves it unguarded."""
        self.assertEqual(
            set(self.HOST_ENTRY_POINTS),
            {"claude_code", "codex", "antigravity", "hermes", "vscode"})

    def test_ci_fetches_tags_or_the_guard_is_inert_there(self):
        """actions/checkout defaults to a shallow clone with no tags.

        With no tags the guard finds no `v<version>`, calls the version
        unreleased, and passes every pull request while enforcing nothing.
        """
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
        self.assertIn("fetch-depth: 0", workflow,
                      "CI checkout must fetch full history and tags")


class TheRealRepositoryObeysIt(unittest.TestCase):
    def test_this_checkout_does_not_reuse_a_released_version(self):
        result = subprocess.run((sys.executable, str(GUARD)), cwd=ROOT,
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0,
                         f"{result.stdout}\n{result.stderr}")


if __name__ == "__main__":
    unittest.main()
