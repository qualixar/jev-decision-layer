"""Coverage-floor tests for jev_auto/settings.py consent guards.

Targets the baseline-missing lines listed for this module: the OSError/
symlink/malformed-record defenses inside descendant_root_allowed,
make_policy, validate_policy, _existing_file, _ancestor_may_cover,
_strictly_inside, _inherited_root, _expired_not_withdrawn, the Windows
branch of save_policy_new, and the WORKSPACE_ALREADY_ENROLLED guard in
_write_descendant_refusal.

Every guard is asserted on its exact AutoError code and, where it refuses
a write, on the state of disk afterward (nothing written / unchanged).
Platform-only code (os.name == 'nt') is reached only by patching this
module's own `os` reference (jev_auto.settings.os), never the process
global, per the hard rule against touching global os.name.
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

from jev_auto import settings  # noqa: E402
from jev_auto.common import AutoError, state_dir, workspace  # noqa: E402
from jev_auto.settings import (  # noqa: E402
    DESCENDANT_COVERAGE_APPROVED,
    descendant_root_allowed,
    enrollment_binding,
    load_policy,
    make_policy,
    revoke,
    save_policy_new,
    validate_policy,
)


def _git(path: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, text=True)


def _repo(path: Path, name: str = "f.txt") -> None:
    path.mkdir(parents=True)
    _git(path, "init", "-q")
    (path / name).write_text(name + "\n")
    _git(path, "add", name)
    _git(path, "-c", "user.email=jev@example.com", "-c", "user.name=Jev", "commit", "-q", "-m", name)


class _TempWorkspace(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        env = patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "state-home")})
        env.start()
        self.addCleanup(env.stop)
        self.ws = self.root / "project"
        self.ws.mkdir()

    def _valid_policy(self, **overrides):
        return make_policy(self.ws, "typesafe", **overrides)


class DescendantRootAllowedOSErrorTests(unittest.TestCase):
    """Line 44-45: Path(root).resolve() failing must not raise past this guard."""

    def test_a_resolve_failure_is_treated_as_not_allowed(self):
        def _boom_ctor(*_a, **_kw):
            class _Boom:
                def resolve(self_inner):
                    raise OSError(5, "synthetic resolve failure")
            return _Boom()

        with patch.object(settings, "Path", _boom_ctor):
            self.assertFalse(descendant_root_allowed("/anything"))

    def test_top_level_temp_directories_are_still_blocked_roots(self):
        # Exercises line 61: len(parts) < 3 or member of the blocked set,
        # independent of the OSError branch above.
        self.assertFalse(descendant_root_allowed(Path("/tmp")))
        self.assertFalse(descendant_root_allowed(Path("/opt")))


class AncestorMayCoverTests(unittest.TestCase):
    """_ancestor_may_cover: OSError from resolve(), and the home/anchor guard."""

    def test_a_resolve_failure_on_the_candidate_itself_is_not_coverable(self):
        class _BoomResolve:
            def resolve(self):
                raise OSError(5, "synthetic resolve failure")

        self.assertFalse(settings._ancestor_may_cover(_BoomResolve()))

    def test_home_itself_can_never_be_an_ancestor_grant(self):
        self.assertFalse(settings._ancestor_may_cover(Path.home()))


class StrictlyInsideTests(unittest.TestCase):
    """_strictly_inside must fail closed on both ValueError and identity."""

    def test_a_path_outside_the_parent_is_not_strictly_inside(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent = root / "grant"
            sibling = root / "sibling"
            parent.mkdir()
            sibling.mkdir()
            self.assertFalse(settings._strictly_inside(sibling, parent))

    def test_a_path_equal_to_the_parent_is_not_strictly_inside(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            self.assertFalse(settings._strictly_inside(parent, parent))


class ExistingFileTests(unittest.TestCase):
    def test_a_symlink_target_is_never_reported_as_an_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            real = root / "real.txt"
            real.write_text("data")
            link = root / "link.txt"
            link.symlink_to(real)
            self.assertFalse(settings._existing_file(link))

    def test_a_plain_missing_path_is_not_an_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertFalse(settings._existing_file(Path(directory) / "missing.txt"))


class MakePolicyConfigGuardTests(_TempWorkspace):
    def test_a_non_boolean_truthy_covers_descendants_is_rejected(self):
        with self.assertRaises(AutoError) as ctx:
            make_policy(self.ws, "typesafe", covers_descendants="yes")
        self.assertEqual(str(ctx.exception), "POLICY_CONFIG")
        self.assertFalse((state_dir(self.ws) / "policy.json").exists())

    def test_descendant_approval_without_requesting_coverage_is_rejected(self):
        with self.assertRaises(AutoError) as ctx:
            make_policy(self.ws, "typesafe", descendant_approval=DESCENDANT_COVERAGE_APPROVED)
        self.assertEqual(str(ctx.exception), "POLICY_CONFIG")

    def test_workspace_path_without_requesting_coverage_is_rejected(self):
        with self.assertRaises(AutoError) as ctx:
            make_policy(self.ws, "typesafe", workspace_path=str(self.ws))
        self.assertEqual(str(ctx.exception), "POLICY_CONFIG")


class ValidatePolicyDescendantGuardTests(_TempWorkspace):
    """Directly probes validate_policy with hand-crafted (not make_policy-built)
    dicts, the same technique test_expired_grant_fallthrough.py uses via
    write_private, to reach validate_policy's own independent re-checks."""

    def _covering_policy(self):
        return make_policy(self.ws, "typesafe", covers_descendants=True,
                            descendant_approval=DESCENDANT_COVERAGE_APPROVED)

    def test_covers_descendants_true_with_wrong_approval_string_is_rejected(self):
        p = {**self._covering_policy(), "descendant_approval": "NOPE"}
        with self.assertRaises(AutoError) as ctx:
            validate_policy(p, self.ws)
        self.assertEqual(str(ctx.exception), "DESCENDANT_APPROVAL_REQUIRED")

    def test_covers_descendants_false_with_a_leftover_approval_key_is_policy_config(self):
        p = {**self._covering_policy(), "covers_descendants": False}
        with self.assertRaises(AutoError) as ctx:
            validate_policy(p, self.ws)
        self.assertEqual(str(ctx.exception), "POLICY_CONFIG")

    def test_a_non_string_workspace_path_is_policy_config(self):
        p = {**self._covering_policy(), "workspace_path": 12345}
        with self.assertRaises(AutoError) as ctx:
            validate_policy(p, self.ws)
        self.assertEqual(str(ctx.exception), "POLICY_CONFIG")

    def test_a_workspace_path_pointing_elsewhere_is_a_mismatch(self):
        other = self.root / "elsewhere"
        other.mkdir()
        p = {**self._covering_policy(), "workspace_path": str(other)}
        with self.assertRaises(AutoError) as ctx:
            validate_policy(p, self.ws)
        self.assertEqual(str(ctx.exception), "POLICY_WORKSPACE_MISMATCH")

    def test_a_symlinked_workspace_path_reraises_the_original_autoerror(self):
        real = self.root / "real-target"
        real.mkdir()
        link = self.root / "alias"
        link.symlink_to(real, target_is_directory=True)
        p = {**self._covering_policy(), "workspace_path": str(link)}
        with self.assertRaises(AutoError) as ctx:
            validate_policy(p, self.ws)
        self.assertEqual(str(ctx.exception), "SYMLINK_NOT_ALLOWED")

    def test_an_embedded_nul_in_workspace_path_is_policy_config_not_a_crash(self):
        p = {**self._covering_policy(), "workspace_path": "bad\x00path"}
        with self.assertRaises(AutoError) as ctx:
            validate_policy(p, self.ws)
        self.assertEqual(str(ctx.exception), "POLICY_CONFIG")

    def test_an_approved_workspace_that_becomes_a_blocked_root_is_still_rejected(self):
        # descendant_root_allowed already guarantees this in practice (make_policy
        # and validate_policy both call it on the same `approved` value before a
        # grant can ever be saved); this proves validate_policy does not just
        # trust a previously-approved workspace_path if that guarantee were ever
        # weakened elsewhere.
        p = self._covering_policy()
        with patch.object(settings, "descendant_root_allowed", return_value=False):
            with self.assertRaises(AutoError) as ctx:
                validate_policy(p, self.ws)
        self.assertEqual(str(ctx.exception), "DESCENDANT_ROOT_NOT_ALLOWED")


class InheritedRootAnchorSearchTests(unittest.TestCase):
    """The anchor-search loop (candidate.is_dir() for path in requested.parents)
    is not reachable through a well-formed real filesystem path: pathlib's
    is_dir() swallows every ignorable OSError and returns False, "/" is always
    an existing directory so the loop always finds an anchor eventually, and a
    malformed path already fails earlier at safe_path/resolve. Both branches
    are reached here with a minimal duck-typed stand-in for the object
    `safe_path(...).resolve()` returns, substituted only for this one call via
    this module's own `safe_path` reference -- no real Path, symlink, or
    permission trick can produce either shape portably across macOS/Linux."""

    def test_a_candidate_is_dir_failure_during_the_anchor_search_returns_none(self):
        class _Boom:
            def is_dir(self):
                raise OSError(13, "synthetic EACCES")

        class _FakeResolved:
            def resolve(self):
                return self

            def is_dir(self):
                return False

            @property
            def parents(self):
                return [_Boom()]

        with patch.object(settings, "safe_path", return_value=_FakeResolved()):
            self.assertIsNone(settings._inherited_root("/whatever-not-real"))

    def test_no_ancestor_is_ever_a_directory_returns_none(self):
        class _FakeResolved:
            def resolve(self):
                return self

            def is_dir(self):
                return False

            @property
            def parents(self):
                return [self]

        with patch.object(settings, "safe_path", return_value=_FakeResolved()):
            self.assertIsNone(settings._inherited_root("/whatever-not-real"))


class InheritedRootDefenseTests(_TempWorkspace):
    def test_a_symlinked_query_path_returns_no_inherited_root(self):
        outside = self.root / "outside"
        outside.mkdir()
        link = self.root / "alias"
        link.symlink_to(outside, target_is_directory=True)
        self.assertIsNone(settings._inherited_root(link))

    def test_a_workspace_lookup_failure_for_the_anchor_itself_returns_none(self):
        # Lines 199-200: `start = workspace(anchor)` raising must stop the
        # climb immediately rather than fall into the ancestor loop. Real
        # filesystem behaviour can't produce this for an anchor that is
        # already a verified directory, so this patches this module's own
        # `workspace` reference to fail for exactly this one resolved path,
        # delegating to the real implementation otherwise.
        child = self.ws / "nested"
        child.mkdir()
        child_resolved = child.resolve()
        real_workspace = settings.workspace

        def flaky_workspace(path, *a, **kw):
            if Path(path).resolve() == child_resolved:
                raise AutoError("WORKSPACE_REQUIRED")
            return real_workspace(path, *a, **kw)

        with patch.object(settings, "workspace", side_effect=flaky_workspace):
            result = settings._inherited_root(child)
        self.assertIsNone(result)

    def test_an_ancestor_whose_own_workspace_lookup_fails_is_skipped(self):
        # Line 207-208: `identity = workspace(parent)` raising AutoError for one
        # specific ancestor must not abort the whole climb -- the loop should
        # keep looking further up. Real filesystem behaviour can't produce this
        # for an ancestor that is already a directory (workspace() only raises
        # WORKSPACE_REQUIRED for a non-directory or an unsafe path), so this
        # patches this module's own `workspace` reference to fail for exactly
        # one path and delegates to the real implementation for every other
        # call, keeping the rest of the resolution real.
        child = self.ws / "nested"
        child.mkdir()
        real_workspace = settings.workspace
        flaky_target = self.ws.resolve()

        def flaky_workspace(path, *a, **kw):
            if Path(path).resolve() == flaky_target:
                raise AutoError("WORKSPACE_REQUIRED")
            return real_workspace(path, *a, **kw)

        with patch.object(settings, "workspace", side_effect=flaky_workspace):
            result = settings._inherited_root(child)
        self.assertIsNone(result)

    def test_a_nested_repository_climbing_through_a_shared_outer_root_deduplicates(self):
        # Lines 210-211 ("key in seen or identity == start"): build
        # outer(git) / middle(no git) / inner(git) / a/b so that walking
        # inner's parents visits `middle` and `outer` in turn, and both
        # resolve (via `git rev-parse --show-toplevel`) to the SAME
        # workspace identity (outer). The second visit must be skipped as
        # a duplicate rather than re-processed.
        outer = self.root / "outer"
        _repo(outer, "outer.txt")
        middle = outer / "middle"
        middle.mkdir()
        inner = middle / "inner"
        _repo(inner, "inner.txt")
        leaf = inner / "a" / "b"
        leaf.mkdir(parents=True)

        # No policy anywhere: after deduplicating `outer`, the climb finds
        # nothing and must return None rather than loop or double count.
        result = settings._inherited_root(leaf)
        self.assertIsNone(result)

        # Now put a real covering grant on `outer` itself and confirm the
        # dedup path still finds it exactly once.
        policy = make_policy(outer, "typesafe", covers_descendants=True,
                              descendant_approval=DESCENDANT_COVERAGE_APPROVED)
        save_policy_new(outer, policy)
        found = settings._inherited_root(leaf)
        self.assertEqual(found, workspace(outer))

    def test_a_malformed_ancestor_grant_record_is_defended_against_independently(self):
        # Lines 221, 224-225, 227: _inherited_root re-checks a loaded grant's
        # shape (workspace_path is a string, resolves cleanly, and matches an
        # allowed root) even though validate_policy (inside _load_exact)
        # already guarantees all three for any grant that can be *saved*
        # through the normal API. This proves _inherited_root does not blindly
        # trust whatever _load_exact hands back, by substituting a loader that
        # bypasses validate_policy entirely for one ancestor.
        child = self.ws / "nested"
        child.mkdir()
        ws_resolved = self.ws.resolve()

        real_load_exact = settings._load_exact
        base_policy = {**self._valid_policy(), "covers_descendants": True,
                       "descendant_approval": DESCENDANT_COVERAGE_APPROVED}

        for bad_workspace_path, label in (
            (12345, "non-string"),
            ("bad\x00path", "unresolvable"),
            (str(self.root / "elsewhere-entirely"), "mismatched-root"),
        ):
            with self.subTest(label=label):
                malformed = {**base_policy, "workspace_path": bad_workspace_path}

                def fake_load_exact(path, base=None, _malformed=malformed):
                    if Path(path).resolve() == ws_resolved:
                        return _malformed
                    return real_load_exact(path, base)

                with patch.object(settings, "_load_exact", side_effect=fake_load_exact):
                    result = settings._inherited_root(child)
                self.assertIsNone(result, label)


class ExpiredNotWithdrawnDirectTests(_TempWorkspace):
    def test_a_workspace_with_no_grant_at_all_is_not_a_plain_expiry(self):
        # Lines 241-242: _read_exact_file raising (here, FileNotFoundError,
        # itself an OSError subtype) must resolve to False, not propagate.
        self.assertFalse(settings._expired_not_withdrawn(self.ws))


class WriteDescendantRefusalTests(_TempWorkspace):
    def test_refusing_an_already_enrolled_exact_workspace_is_rejected_and_does_not_overwrite(self):
        policy = self._valid_policy()
        save_policy_new(self.ws, policy)
        with self.assertRaises(AutoError) as ctx:
            settings._write_descendant_refusal(self.ws)
        self.assertEqual(str(ctx.exception), "WORKSPACE_ALREADY_ENROLLED")
        self.assertEqual(load_policy(self.ws)["policy_id"], policy["policy_id"])


class SavePolicyNewWindowsBranchTests(_TempWorkspace):
    """Lines 353-355: os.name == 'nt' branch, reached by patching this
    module's own `os` reference rather than the process-global os.name."""

    def test_windows_branch_writes_via_atomic_write_private_and_returns_early(self):
        class _FakeNTOs:
            name = "nt"

            def __getattr__(self, item):
                return getattr(os, item)

        policy = self._valid_policy()
        with patch.object(settings, "os", _FakeNTOs()):
            save_policy_new(self.ws, policy)
        saved = json.loads((state_dir(self.ws) / "policy.json").read_bytes().split(b"\n")[0])
        self.assertEqual(saved["policy_id"], policy["policy_id"])

    def test_windows_branch_never_replaces_an_existing_policy(self):
        first = self._valid_policy()
        save_policy_new(self.ws, first)
        second = make_policy(self.ws, "openrouter")

        class _FakeNTOs:
            name = "nt"

            def __getattr__(self, item):
                return getattr(os, item)

        with patch.object(settings, "os", _FakeNTOs()):
            with self.assertRaises(AutoError) as ctx:
                save_policy_new(self.ws, second)
        self.assertEqual(str(ctx.exception), "WORKSPACE_ALREADY_ENROLLED")
        self.assertEqual(load_policy(self.ws)["policy_id"], first["policy_id"])


if __name__ == "__main__":
    unittest.main()
