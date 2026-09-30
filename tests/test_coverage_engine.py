"""Coverage-floor tests for jev_auto/engine.py error paths.

WHY THIS FILE EXISTS
---------------------
Line coverage on `Engine.judge()` and `Engine.evaluate_typed()` was 96%, but the
remaining gaps are exactly the defense-in-depth checks that matter most: the
policy-identity re-checks inside the single-flight `work()` closure, the local
`data_classification` gate duplicated in `judge()` itself (as distinct from the
one in `src.adl.queries.typed`), and the post-`prepare_query` policy hash
compare in `evaluate_typed()`. A test suite that never drives these branches
cannot tell "the guard fired" from "the guard was deleted and nothing noticed".

Every test below either asserts the exact `AutoError` code the guard raises, or
(for the two race-condition branches) sets up the exact interleaving the code
comments describe and asserts the resulting dict shape. Nothing here makes a
network or provider call: `FakeProviders` is an in-process double, and the
`evaluate_typed` test patches `src.adl.queries.typed.prepare_query` directly.

ISOLATION
---------
Every test redirects `XDG_STATE_HOME` to a fresh `tempfile.TemporaryDirectory`
so nothing touches the real `~/.local/state/qualixar-jev-decision-layer`.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "plugins" / "qualixar-jev-decision-layer" / "runtime"
sys.path.insert(0, str(RUNTIME))

from jev_auto.common import AutoError, digest  # noqa: E402
from jev_auto.engine import Engine  # noqa: E402
from jev_auto.settings import make_policy, save_policy_new  # noqa: E402


class _FakeProviders:
    """Minimal `Providers` double: no network, no MLX process, no credentials."""

    def __init__(self):
        self.calls = []

    def evaluate(self, p, state, questions):
        self.calls.append((p, state, questions))
        return {"model": "jev-1.13.0", "answers": {"q": {"type": "noul", "noul": 0.1}},
                "usage": {"input_tokens": 1, "output_tokens": 1}}


class _EngineCoverageTestCase(unittest.TestCase):
    """Shared fixture: an isolated project directory and isolated state root."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.project = root / "project"
        self.project.mkdir()
        env_patch = patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state-home")})
        env_patch.start()
        self.addCleanup(env_patch.stop)
        self.questions = {"q": {"type": "noul", "instructions": "x"}}

    def enroll(self, provider="typesafe", **overrides):
        policy = make_policy(self.project, provider, **overrides)
        save_policy_new(self.project, policy)
        return policy

    def build_engine(self, providers=None):
        providers = providers if providers is not None else _FakeProviders()
        return Engine(self.project, provider=providers)


# --------------------------------------------------------------------------
# judge(): the three data-classification / consent guards (lines 25, 30, 32, 42-43)
# --------------------------------------------------------------------------

class JudgeConsentAndClassificationGuardTests(_EngineCoverageTestCase):
    """`judge()` enforces its own data-classification and digest checks even
    though `evaluate_typed`'s callers pass through `src.adl.queries.typed`
    first: this is the second, independent gate a direct `judge()` caller
    (like `evaluate_case()` or `browser()`) still gets. Each test proves the
    exact `AutoError` code and that no provider call happened first.
    """

    def test_explicit_expected_policy_digest_mismatch_raises_before_any_provider_call(self):
        policy = self.enroll(case_ids=["c1"])
        engine = self.build_engine()
        with self.assertRaisesRegex(AutoError, "POLICY_CHANGED"):
            engine.judge("c1", {"x": 1}, self.questions, policy, expected_policy_digest="0" * 64)
        self.assertEqual(engine.providers.calls, [])

    def test_unrecognised_data_classification_value_is_rejected(self):
        self.enroll(case_ids=["c1"])
        engine = self.build_engine()
        with self.assertRaisesRegex(AutoError, "DATA_CLASSIFICATION_INVALID"):
            engine.judge("c1", {"x": 1}, self.questions, data_classification="top-secret")
        self.assertEqual(engine.providers.calls, [])

    def test_internal_minimized_request_against_a_public_only_enrollment_is_rejected(self):
        # DEFAULTS['data_classification'] is 'public'; requesting a stricter
        # scope than the workspace enrolled for must not silently upgrade it.
        self.enroll(case_ids=["c1"])
        engine = self.build_engine()
        with self.assertRaisesRegex(AutoError, "DATA_CLASSIFICATION_NOT_ENROLLED"):
            engine.judge("c1", {"x": 1}, self.questions, data_classification="internal-minimized")
        self.assertEqual(engine.providers.calls, [])

    def test_restricted_request_over_a_non_local_provider_without_jev_maximum_is_rejected(self):
        self.enroll(case_ids=["c1"])  # provider='typesafe', decision_mode=None
        engine = self.build_engine()
        with self.assertRaisesRegex(AutoError, "REMOTE_RESTRICTED_DATA"):
            engine.judge("c1", {"x": 1}, self.questions, data_classification="restricted")
        self.assertEqual(engine.providers.calls, [])

    def test_restricted_request_succeeds_once_the_workspace_is_actually_enrolled_for_it(self):
        # Positive control for the previous test: the *same* call must go
        # through once the policy legitimately clears the gate (data_classification
        # 'restricted' + decision_mode 'jev-maximum'), proving the guard is
        # conditional and not simply unreachable dead code.
        self.enroll(case_ids=["c1"], data_classification="restricted", decision_mode="jev-maximum")
        engine = self.build_engine()
        result = engine.judge("c1", {"x": 1}, self.questions, data_classification="restricted")
        self.assertFalse(result["cache_hit"])
        self.assertIn("receipt_id", result)
        self.assertEqual(len(engine.providers.calls), 1)


# --------------------------------------------------------------------------
# judge(): the inner work() closure's second and third policy re-checks
# (lines 56, 59, 60)
# --------------------------------------------------------------------------

class JudgeInnerRaceRecheckTests(_EngineCoverageTestCase):
    """`work()` re-reads the policy twice more after the outer check at
    engine.py's `if policy_digest!=digest(self.policy())` line: once
    immediately ("Re-read authority immediately before reserving the
    request"), and once more if a cache entry for this exact request appeared
    between the outer check and here. A single, uncontested call can never
    observe its own not-yet-written cache entry, so these two race windows
    are only reachable by patching `Engine.policy` and `Store.cached` to
    return the sequence of values the comment describes.
    """

    def setUp(self):
        super().setUp()
        self.policy = self.enroll(case_ids=["c1"])
        self.engine = self.build_engine()

    def test_policy_changed_between_the_outer_check_and_the_inner_reread_raises(self):
        changed = {**self.policy, "max_calls_per_day": self.policy["max_calls_per_day"] + 1}
        with patch.object(Engine, "policy", side_effect=[self.policy, changed]):
            with self.assertRaisesRegex(AutoError, "POLICY_CHANGED"):
                self.engine.judge("c1", {"a": 1}, self.questions, self.policy)
        self.assertEqual(self.engine.providers.calls, [],
                         "a stale-policy request must never reach the provider")

    def test_cache_entry_appearing_between_outer_and_inner_check_with_stable_policy_is_a_cache_hit(self):
        fake_cached = {"receipt_id": "a" * 64, "answers": {"q": {"type": "noul", "noul": 0.2}},
                       "usage": {"input_tokens": 0, "output_tokens": 0}}
        with patch.object(Engine, "policy", return_value=self.policy), \
             patch.object(self.engine.store, "cached", side_effect=[None, fake_cached]):
            result = self.engine.judge("c1", {"a": 2}, self.questions, self.policy)
        self.assertEqual(result, {**fake_cached, "cache_hit": True, "provider_usage_this_call": None})
        self.assertEqual(self.engine.providers.calls, [],
                         "a request answered from the race-window cache must never reach the provider")

    def test_cache_entry_appearing_between_outer_and_inner_check_with_changed_policy_raises(self):
        changed = {**self.policy, "max_calls_per_day": self.policy["max_calls_per_day"] + 1}
        fake_cached = {"receipt_id": "b" * 64, "answers": {}, "usage": {"input_tokens": 0, "output_tokens": 0}}
        with patch.object(Engine, "policy", side_effect=[self.policy, self.policy, changed]), \
             patch.object(self.engine.store, "cached", side_effect=[None, fake_cached]):
            with self.assertRaisesRegex(AutoError, "POLICY_CHANGED"):
                self.engine.judge("c1", {"a": 3}, self.questions, self.policy)
        self.assertEqual(self.engine.providers.calls, [])


# --------------------------------------------------------------------------
# evaluate_typed(): the post-prepare_query policy hash compare (line 119)
# --------------------------------------------------------------------------

class EvaluateTypedPolicyHashMismatchTests(unittest.TestCase):
    """`evaluate_typed` re-reads `self.policy()` a second time after
    `prepare_query` returns and compares its digest against what the compiled
    query recorded. A compiled query stamped with a stale hash must never be
    allowed to `judge()`; this is checked, and stopped, before `judge()` (and
    therefore before any provider) is ever reached. Follows the
    `object.__new__(Engine)` + `patch.object(Engine, "policy", ...)` pattern
    already used in test_core_contracts.py for isolating `evaluate_typed`
    from `Engine.__init__`'s filesystem/state-dir setup.
    """

    def test_stale_compiled_policy_hash_is_rejected_before_judge_is_called(self):
        policy = {"provider": "typesafe", "routes": {}}
        compiled = SimpleNamespace(provider="typesafe", expected_model="jev-1.13.0",
                                   policy_sha256="0" * 64,  # deliberately does not match digest(policy)
                                   calibration_status="NOT_EVALUATED")
        engine = object.__new__(Engine)
        engine.workspace = Path("/synthetic")
        judge_calls = []
        with patch("src.adl.queries.typed.prepare_query", return_value=compiled), \
             patch.object(Engine, "policy", return_value=policy), \
             patch.object(Engine, "judge", side_effect=lambda *a, **k: judge_calls.append((a, k))):
            with self.assertRaisesRegex(AutoError, "POLICY_CHANGED"):
                engine.evaluate_typed("state", {"decision": {"type": "noul", "instructions": "x"}},
                                      "typesafe", "public")
        self.assertEqual(judge_calls, [], "a stale compiled policy hash must never reach judge()")
        self.assertNotEqual(compiled.policy_sha256, digest(policy))


if __name__ == "__main__":
    unittest.main()
