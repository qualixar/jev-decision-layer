"""Hierarchical workspace consent shared by every host adapter.

A root grant covers child paths only when the user approved that coverage.
An exact child policy, including a refusal, always wins. Home and filesystem
tops cannot be descendant roots. The five host adapters consume that same
resolution; this file does not claim a native host session was run.
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

from jev_auto.agy_hook import handle as agy_handle  # noqa: E402
from jev_auto.claude_hook import handle as claude_handle  # noqa: E402
from jev_auto.common import AutoError, workspace  # noqa: E402
from jev_auto.engine import Engine  # noqa: E402
from jev_auto.hermes_hook import handle as hermes_handle  # noqa: E402
from jev_auto.hooks import handle as codex_handle  # noqa: E402
from jev_auto import ipc  # noqa: E402
from jev_auto.settings import (  # noqa: E402
    DESCENDANT_COVERAGE_APPROVED,
    enrollment_binding,
    governing_workspace,
    load_policy,
    make_policy,
    revoke,
    save_policy_new,
)
from src.adl.api.host_inventory import inventory_hosts  # noqa: E402
from src.adl.api.setup_controller import SetupChoice, SetupController, SetupError  # noqa: E402


def _git(path: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, text=True)


def _repo(path: Path, name: str) -> None:
    path.mkdir(parents=True)
    _git(path, "init", "-q")
    (path / name).write_text(name + "\n")
    _git(path, "add", name)
    _git(path, "-c", "user.email=jev@example.com", "-c", "user.name=Jev", "commit", "-q", "-m", name)


def _cover(path: Path, provider: str = "typesafe"):
    policy = make_policy(
        path, provider, covers_descendants=True, descendant_approval=DESCENDANT_COVERAGE_APPROVED,
    )
    save_policy_new(path, policy)
    return policy


class HierarchicalEnrollmentTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        env = patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "state-home")})
        env.start()
        self.addCleanup(env.stop)
        self.parent = self.root / "parent"
        self.child = self.parent / "nested"
        _repo(self.parent, "parent.txt")
        _repo(self.child, "child.txt")

    def test_a_root_grant_does_not_cover_a_nested_repository_until_approved(self):
        save_policy_new(self.parent, make_policy(self.parent, "typesafe"))
        with self.assertRaises(AutoError) as ctx:
            load_policy(self.child)
        self.assertEqual(str(ctx.exception), "WORKSPACE_NOT_ENROLLED")

    def test_approved_root_grant_covers_a_nested_repository_and_shares_its_broker(self):
        policy = _cover(self.parent, "openrouter")
        loaded = load_policy(self.child)
        self.assertEqual(loaded["policy_id"], policy["policy_id"])
        self.assertEqual(loaded["provider"], "openrouter")
        binding = enrollment_binding(self.child)
        self.assertEqual(binding["scope"], "descendant")
        self.assertEqual(binding["workspace"], workspace(self.parent))
        self.assertEqual(governing_workspace(self.child), workspace(self.parent))
        engine = Engine(self.child, provider=object())
        self.assertEqual(engine.workspace, workspace(self.parent))
        self.assertEqual(engine.root.name, policy["workspace_id"])
        seen = []

        def fake_request(path, _obj, base=None, timeout=16):
            seen.append(Path(path))
            return {"version": "1.0.0"}

        with patch.object(ipc, "request", fake_request):
            ipc.ensure(self.child)
        self.assertEqual(seen, [workspace(self.parent)])

    def test_an_exact_child_policy_wins_over_the_ancestor(self):
        _cover(self.parent, "typesafe")
        child = make_policy(self.child, "openrouter")
        save_policy_new(self.child, child)
        self.assertEqual(load_policy(self.child)["provider"], "openrouter")
        self.assertEqual(enrollment_binding(self.child)["scope"], "exact")
        self.assertEqual(load_policy(self.parent)["provider"], "typesafe")

    def test_revoking_a_covered_child_refuses_only_that_child(self):
        parent = _cover(self.parent)
        revoke(self.child)
        with self.assertRaises(AutoError) as ctx:
            load_policy(self.child)
        self.assertEqual(str(ctx.exception), "AUTO_DISABLED_OR_EXPIRED")
        self.assertEqual(load_policy(self.parent)["policy_id"], parent["policy_id"])
        self.assertTrue(load_policy(self.parent)["enabled"])

    def test_a_sibling_outside_the_root_is_not_covered(self):
        _cover(self.parent)
        sibling = self.root / "sibling"
        _repo(sibling, "sibling.txt")
        with self.assertRaises(AutoError) as ctx:
            load_policy(sibling)
        self.assertEqual(str(ctx.exception), "WORKSPACE_NOT_ENROLLED")

    def test_a_directory_inside_one_repository_keeps_the_exact_grant(self):
        save_policy_new(self.parent, make_policy(self.parent, "typesafe"))
        inside = self.parent / "pkg"
        inside.mkdir()
        self.assertEqual(enrollment_binding(inside)["scope"], "exact")
        self.assertEqual(governing_workspace(inside), workspace(self.parent))

    def test_descendant_coverage_requires_the_approval_phrase_and_rejects_broad_roots(self):
        with self.assertRaises(AutoError) as ctx:
            make_policy(self.parent, "typesafe", covers_descendants=True)
        self.assertEqual(str(ctx.exception), "DESCENDANT_APPROVAL_REQUIRED")
        with self.assertRaises(AutoError) as ctx:
            make_policy(Path.home(), "typesafe", covers_descendants=True,
                        descendant_approval=DESCENDANT_COVERAGE_APPROVED)
        self.assertEqual(str(ctx.exception), "DESCENDANT_ROOT_NOT_ALLOWED")
        with self.assertRaises(AutoError) as ctx:
            make_policy(Path("/"), "typesafe", covers_descendants=True,
                        descendant_approval=DESCENDANT_COVERAGE_APPROVED)
        self.assertEqual(str(ctx.exception), "DESCENDANT_ROOT_NOT_ALLOWED")

    def test_a_symlink_cannot_borrow_a_descendant_grant(self):
        _cover(self.parent)
        outside = self.root / "outside"
        outside.mkdir()
        link = self.parent / "alias"
        link.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(AutoError) as ctx:
            load_policy(link)
        self.assertEqual(str(ctx.exception), "SYMLINK_NOT_ALLOWED")

    def test_setup_refuses_descendant_coverage_without_the_separate_confirmation(self):
        choice = SetupChoice("typesafe", "public", 7, 10, 2000, cover_descendants=True)
        controller = SetupController(self.parent, credential_store=object(),
                                     bridge=lambda *_: None, start=lambda *_: None)
        with self.assertRaises(SetupError) as ctx:
            controller.apply(choice, credential=None, confirmed=True, descendants_confirmed=False)
        self.assertEqual(str(ctx.exception), "DESCENDANT_APPROVAL_REQUIRED")
        self.assertFalse((self.root / "state-home").exists())


class DescendantRootBreadthTests(unittest.TestCase):
    def test_another_users_home_is_not_a_descendant_root(self):
        from jev_auto.settings import descendant_root_allowed
        self.assertFalse(descendant_root_allowed(Path("/Users/someone-else")))
        self.assertFalse(descendant_root_allowed(Path("/home/someone-else")))
        self.assertFalse(descendant_root_allowed(Path.home()))
        self.assertFalse(descendant_root_allowed(Path("/")))

    def test_a_file_inside_a_covered_root_resolves_to_the_grant(self):
        from jev_auto.settings import descendant_root_allowed
        self.assertTrue(descendant_root_allowed(self.parent))
        _cover(self.parent)
        target = self.child / "child.txt"
        self.assertTrue(target.is_file())
        loaded = load_policy(target)
        self.assertTrue(loaded["covers_descendants"])
        self.assertEqual(governing_workspace(target), workspace(self.parent))
        self.assertEqual(enrollment_binding(target)["scope"], "descendant")

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        env = patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "state-home")})
        env.start()
        self.addCleanup(env.stop)
        self.parent = self.root / "parent"
        self.child = self.parent / "nested"
        _repo(self.parent, "parent.txt")
        _repo(self.child, "child.txt")


class HostEnrollmentParityTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        env = patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "state-home")})
        env.start()
        self.addCleanup(env.stop)
        self.parent = self.root / "parent"
        self.child = self.parent / "nested"
        _repo(self.parent, "parent.txt")
        _repo(self.child, "child.txt")
        _cover(self.parent)

    def test_every_host_adapter_sees_the_covered_child_and_native_status_stays_unrun(self):
        child = str(self.child)
        self.assertIn("enrolled for this workspace", claude_handle(
            {"hook_event_name": "SessionStart", "cwd": child}))
        agy = agy_handle({"invocationNum": 0, "workspacePaths": [child]})
        self.assertIn("ephemeralMessage", agy["injectSteps"][0])
        seen = []
        hermes = hermes_handle(
            {"cwd": child, "user_message": "Please fix the enrollment boundary in this nested repository now."},
            starter=lambda path: seen.append(path),
            caller=lambda _path, _payload, _timeout: {
                "selected": ["file:child.txt"],
                "receipt_id": "ab" * 32,
                "reason": "jev_candidate_selection",
            },
        )
        self.assertIn("advisory", hermes["context"])
        self.assertEqual(seen, [workspace(self.parent)])
        codex = codex_handle(
            {"hook_event_name": "SessionStart", "cwd": child, "session_id": "session-1"},
            starter=lambda _path, _base=None: None,
            caller=lambda _path, _obj: {},
        )
        self.assertIn("additionalContext", codex["hookSpecificOutput"])
        engine = Engine(self.child, provider=object())
        self.assertEqual(engine.workspace, workspace(self.parent))
        rows = inventory_hosts(which=lambda _command: None, app_exists=lambda _path: False)
        self.assertEqual({row["id"] for row in rows}, {"codex", "claude_code", "antigravity", "hermes", "vscode"})
        self.assertTrue(all(row["native_status"] == "NOT_RUN" for row in rows))

    def test_a_refused_child_stays_silent_on_every_hook(self):
        revoke(self.child)
        child = str(self.child)
        self.assertEqual(claude_handle({"hook_event_name": "SessionStart", "cwd": child}), "")
        self.assertEqual(agy_handle({"invocationNum": 0, "workspacePaths": [child]}), {})
        self.assertEqual(hermes_handle(
            {"cwd": child, "user_message": "Please fix the enrollment boundary in this nested repository now."},
            starter=lambda _path: self.fail("refused child must not start a broker"),
        ), {})
        self.assertIsNone(codex_handle(
            {"hook_event_name": "SessionStart", "cwd": child, "session_id": "session-1"},
            starter=lambda _path, _base=None: self.fail("refused child must not start a broker"),
        ))


if __name__ == "__main__":
    unittest.main()
