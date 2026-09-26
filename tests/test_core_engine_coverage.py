"""Coverage-focused unit tests for the broker engine and its closest contracts.

WHY THIS FILE EXISTS
---------------------
`jev_auto/engine.py` is the broker's `Engine` class: everything the Unix-socket
server (`jev_auto/server.py`) dispatches to. Before this file, almost nothing in
the suite constructed an `Engine` directly with a fake provider -- coverage of
this module came only incidentally, through whichever ops other tests happened
to exercise over a live broker. This file instead builds an `Engine` in-process
against a fake `Providers` double and drives every dispatch op, including its
error paths, directly. It also closes out the remaining gaps in
`jev_auto/settings.py` (the persisted policy document), `jev_auto/protocol.py`
(the request/response contract and the compact receipt projection),
`jev_auto/review.py` (the diff-triage question compiler), `jev_auto/agy_hook.py`
(the Antigravity PreInvocation advisory), and the vendored
`jev_auto/vendor/winnow_chunk.py` chunker.

HOUSE STYLE
-----------
Followed from `tests/test_core_contracts.py` and `tests/test_hermes_parity.py`:
stdlib only, `ROOT`/`RUNTIME` resolved from this file's own location, and every
`unittest.TestCase` subclass carries a docstring saying why it exists.

ISOLATION
---------
No test touches the real `~/.local/state/qualixar-jev-decision-layer`. Every
`Engine`/`settings` call in this file runs with `XDG_STATE_HOME` redirected to a
`TemporaryDirectory` for the life of the test (see `_EngineTestCase.setUp`).
That redirection is not optional convenience: `Engine.evaluate_typed` calls
`src.adl.queries.typed.prepare_query(self.workspace, ...)` without forwarding
`self.base` (see `EngineBaseParameterNotForwardedTests` below), so the *only*
way to keep every op consistent with a single, temporary policy location is to
move the default (`XDG_STATE_HOME`) rather than rely on the `base=` constructor
argument alone. No test spawns the real Unix-socket broker
(`jev_auto/server.py`/`jev_auto/ipc.py`); `Engine` is exercised in-process.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "plugins" / "qualixar-jev-decision-layer" / "runtime"
sys.path.insert(0, str(RUNTIME))

from jev_auto.common import AutoError  # noqa: E402
from jev_auto.engine import Engine  # noqa: E402
from jev_auto.settings import (  # noqa: E402
    load_policy,
    make_policy,
    replace_reviewed_policy,
    revoke,
    save_policy,
    save_policy_new,
    transition_setup_policy_ready,
    validate_policy,
)
from jev_auto.protocol import compact_receipt, validate_questions, validate_response  # noqa: E402
from jev_auto.review import compile_review  # noqa: E402
from jev_auto.agy_hook import handle as agy_handle, main as agy_main  # noqa: E402
from jev_auto.vendor.winnow_chunk import Block, chunk, group_contiguous  # noqa: E402


# --------------------------------------------------------------------------
# Shared fakes and fixtures
# --------------------------------------------------------------------------

def _one_hot_answers(questions):
    """Build a provider response that satisfies protocol.validate_response.

    Every answer places all probability mass on a single option, which always
    clears the choice-argmax and score-expectation checks regardless of which
    provider identity strings a test uses, and needs no per-test bookkeeping.
    """
    answers = {}
    for key, question in questions.items():
        if question["type"] == "noul":
            answers[key] = {"type": "noul", "noul": 0.1}
        elif question["type"] == "choice":
            labels = list(question["criteria"])
            probabilities = {label: (1.0 if label == labels[0] else 0.0) for label in labels}
            answers[key] = {"type": "choice", "choice": labels[0], "confidence": 0.9,
                             "probabilities": probabilities}
        else:  # score
            count = len(question["criteria"])
            probabilities = {str(i): (1.0 if i == 0 else 0.0) for i in range(count)}
            answers[key] = {"type": "score", "score": 0, "confidence": 0.9, "probabilities": probabilities}
    return answers


_MODEL_BY_PROVIDER = {"typesafe": "jev-1.13.0", "openrouter": "typesafe/jev-1.13"}


class FakeProviders:
    """Minimal stand-in for jev_auto.providers.Providers.

    Engine accepts any object exposing `.evaluate(effective_policy, state,
    questions)` via its `provider=` constructor argument, so no real network
    call, MLX process, or credential lookup is ever needed to unit test the
    Engine class itself.
    """

    def __init__(self, *, model=None, answers=None, provenance=None, raises=None):
        self.calls = []
        self._model = model
        self._answers = answers
        self._provenance = provenance
        self._raises = raises

    def evaluate(self, p, state, questions):
        self.calls.append(SimpleNamespace(policy=p, state=state, questions=questions))
        if self._raises is not None:
            raise self._raises
        model = self._model or _MODEL_BY_PROVIDER.get(p["provider"], "laya-mlx@fake")
        answers = self._answers if self._answers is not None else _one_hot_answers(questions)
        result = {"model": model, "answers": answers, "usage": {"input_tokens": 3, "output_tokens": 3}}
        if self._provenance is not None:
            result["provenance"] = self._provenance
        return result

    def warmup(self, p, wait=False):
        if not isinstance(p.get("mlx"), dict):
            raise AutoError("MLX_NOT_CONFIGURED")
        return {"ready": True, "provider": "laya-mlx"}


class FakeProvidersWithReadyHook(FakeProviders):
    """Exercises Engine.judge()'s `hasattr(self.providers, 'ready_for_request')` branch."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.ready_calls = 0

    def ready_for_request(self, effective_policy):
        self.ready_calls += 1


class _EngineTestCase(unittest.TestCase):
    """Shared fixture: an isolated project directory and an isolated state root.

    `Engine.__init__` reads a policy immediately (via `self.store.prune(...)`),
    so every test needs one enrolled before constructing an `Engine`. `enroll()`
    writes it with `save_policy_new`, matching how a real setup wizard would.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.project = root / "project"
        self.project.mkdir()
        env_patch = patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state-home")})
        env_patch.start()
        self.addCleanup(env_patch.stop)

    def enroll(self, provider="typesafe", **overrides):
        policy = make_policy(self.project, provider, **overrides)
        save_policy_new(self.project, policy)
        return policy

    def build_engine(self, providers=None, base=None):
        return Engine(self.project, base=base, provider=providers if providers is not None else FakeProviders())


# --------------------------------------------------------------------------
# Engine.judge() -- the shared decision path behind every recipe
# --------------------------------------------------------------------------

class EngineJudgeCoreTests(_EngineTestCase):
    """`judge()` is the one method every dispatch op eventually funnels through.

    These tests drive it directly so its budget, cache, single-flight, and
    consent checks are covered without needing a real provider or a running
    broker, and so a provider bug (wrong model, wrong shape, an exception) is
    proven to surface as the documented AutoError code rather than crash.
    """

    def test_first_call_is_a_live_decision_and_is_cached_on_repeat(self):
        self.enroll(case_ids=["c1"])
        engine = self.build_engine()
        questions = {"pick": {"type": "choice", "instructions": "pick one",
                               "criteria": {"a": "opt a", "b": "opt b"}}}
        first = engine.judge("c1", {"x": 1}, questions)
        self.assertFalse(first["cache_hit"])
        self.assertIn("receipt_id", first)
        second = engine.judge("c1", {"x": 1}, questions)
        self.assertTrue(second["cache_hit"])
        self.assertEqual(len(engine.providers.calls), 1, "the second identical call must not re-invoke the provider")

    def test_recipe_not_enrolled_is_rejected_before_any_provider_call(self):
        self.enroll(case_ids=["c1"])
        engine = self.build_engine()
        with self.assertRaises(AutoError) as ctx:
            engine.judge("never-enrolled", {"x": 1}, {"q": {"type": "noul", "instructions": "x"}})
        self.assertEqual(str(ctx.exception), "RECIPE_NOT_ENROLLED")
        self.assertEqual(engine.providers.calls, [])

    def test_generic_recipe_requires_generic_query_enabled(self):
        self.enroll(case_ids=[], generic_query_enabled=False)
        engine = self.build_engine()
        with self.assertRaises(AutoError) as ctx:
            engine.judge("generic", {"x": 1}, {"q": {"type": "noul", "instructions": "x"}})
        self.assertEqual(str(ctx.exception), "RECIPE_NOT_ENROLLED")

    def test_stale_policy_snapshot_is_rejected_as_policy_changed(self):
        policy = self.enroll(case_ids=["c1"])
        engine = self.build_engine()
        save_policy(self.project, {**policy, "max_calls_per_day": policy["max_calls_per_day"] + 1})
        with self.assertRaises(AutoError) as ctx:
            engine.judge("c1", {"x": 1}, {"q": {"type": "noul", "instructions": "x"}}, policy)
        self.assertEqual(str(ctx.exception), "POLICY_CHANGED")

    def test_a_policy_edit_landing_while_the_provider_call_is_in_flight_is_rejected(self):
        # Distinct from the stale-snapshot case above: here the policy is
        # edited *during* the provider call (after budget reservation, before
        # the receipt is written), which judge()'s post-call re-check must
        # still catch even though the pre-call snapshot was fresh.
        self.enroll(case_ids=["c1"])
        project = self.project

        class ChangesPolicyMidFlight(FakeProviders):
            def evaluate(self, p, state, questions):
                save_policy(project, {**load_policy(project), "max_calls_per_day":
                                       load_policy(project)["max_calls_per_day"] + 1})
                return super().evaluate(p, state, questions)

        engine = self.build_engine(ChangesPolicyMidFlight())
        with self.assertRaises(AutoError) as ctx:
            engine.judge("c1", {"a": 1}, {"q": {"type": "noul", "instructions": "x"}})
        self.assertEqual(str(ctx.exception), "POLICY_CHANGED_DURING_REQUEST")

    def test_sensitive_state_is_never_sent_to_the_provider(self):
        self.enroll(case_ids=["c1"])
        engine = self.build_engine()
        with self.assertRaises(AutoError) as ctx:
            engine.judge("c1", {"note": "password: hunter2sekrit"}, {"q": {"type": "noul", "instructions": "x"}})
        self.assertEqual(str(ctx.exception), "SENSITIVE_PAYLOAD_NOT_SENT")
        self.assertEqual(engine.providers.calls, [], "screening must happen before any provider call")

    def test_a_provider_answer_that_embeds_a_secret_is_still_screened_on_the_way_out(self):
        # The criteria text is caller-supplied and becomes `legend` in a score
        # answer; require_clean(result) must still catch it even though the
        # provider itself only ever saw the closed set of criteria.
        self.enroll(case_ids=["c1"])
        engine = self.build_engine()
        questions = {"risk": {"type": "score", "instructions": "rate",
                               "criteria": ["fine", "sk-ant-01234567890123456789", "bad"]}}
        with self.assertRaises(AutoError) as ctx:
            engine.judge("c1", {"a": 1}, questions)
        self.assertEqual(str(ctx.exception), "SENSITIVE_PAYLOAD_NOT_SENT")

    def test_provider_autoerror_propagates_unchanged(self):
        self.enroll(case_ids=["c1"])
        engine = self.build_engine(FakeProviders(raises=AutoError("SYNTHETIC_PROVIDER_ERROR")))
        with self.assertRaises(AutoError) as ctx:
            engine.judge("c1", {"a": 1}, {"q": {"type": "noul", "instructions": "x"}})
        self.assertEqual(str(ctx.exception), "SYNTHETIC_PROVIDER_ERROR")

    def test_provider_generic_exception_is_wrapped_not_leaked(self):
        self.enroll(case_ids=["c1"])
        engine = self.build_engine(FakeProviders(raises=RuntimeError("network is on fire")))
        with self.assertRaises(AutoError) as ctx:
            engine.judge("c1", {"a": 1}, {"q": {"type": "noul", "instructions": "x"}})
        self.assertEqual(str(ctx.exception), "DECISION_FAILED_OR_UNAVAILABLE")

    def test_provenance_metadata_is_merged_into_the_result_when_present(self):
        self.enroll(case_ids=["c1"])
        engine = self.build_engine(FakeProviders(provenance={"provider": "typesafe", "confidence_kind": "provider-reported"}))
        result = engine.judge("c1", {"a": 1}, {"q": {"type": "noul", "instructions": "x"}})
        self.assertEqual(result["provider_metadata"], {"provider": "typesafe", "confidence_kind": "provider-reported"})

    def test_ready_for_request_hook_is_called_only_when_the_provider_defines_it(self):
        self.enroll(case_ids=["c1"])
        with_hook = FakeProvidersWithReadyHook()
        engine = self.build_engine(with_hook)
        engine.judge("c1", {"a": 1}, {"q": {"type": "noul", "instructions": "x"}})
        self.assertEqual(with_hook.ready_calls, 1)

        without_hook = FakeProviders()
        self.assertFalse(hasattr(without_hook, "ready_for_request"))
        engine2 = self.build_engine(without_hook)
        engine2.judge("c1", {"a": 1}, {"q": {"type": "noul", "instructions": "x"}})  # must not raise

    def test_request_byte_budget_is_enforced(self):
        self.enroll(case_ids=["c1"], max_request_bytes=1000)
        engine = self.build_engine()
        oversized = {"q": {"type": "noul", "instructions": "x" * 2000}}
        with self.assertRaises(AutoError) as ctx:
            engine.judge("c1", {}, oversized)
        self.assertEqual(str(ctx.exception), "REQUEST_BYTE_BUDGET")

    def test_daily_call_budget_is_enforced_across_distinct_requests(self):
        self.enroll(case_ids=["c1"], max_calls_per_day=1)
        engine = self.build_engine()
        engine.judge("c1", {"a": 1}, {"q": {"type": "noul", "instructions": "first"}})
        with self.assertRaises(AutoError) as ctx:
            engine.judge("c1", {"a": 1}, {"q": {"type": "noul", "instructions": "second, distinct key"}})
        self.assertEqual(str(ctx.exception), "AUTO_DAILY_BUDGET")

    def test_concurrent_identical_requests_are_coalesced_into_one_provider_call(self):
        self.enroll(case_ids=["c1"])
        started = threading.Event()
        release = threading.Event()

        class SlowFakeProviders(FakeProviders):
            def evaluate(self, p, state, questions):
                started.set()
                release.wait(5)
                return super().evaluate(p, state, questions)

        fake = SlowFakeProviders()
        engine = self.build_engine(fake)
        results = {}

        def call(name):
            results[name] = engine.judge("c1", {"same": 1}, {"q": {"type": "noul", "instructions": "x"}})

        first = threading.Thread(target=call, args=("first",))
        first.start()
        self.assertTrue(started.wait(5), "the owner thread never reached the provider call")
        second = threading.Thread(target=call, args=("second",))
        second.start()
        time.sleep(0.2)  # give the joiner a chance to register with SingleFlight
        release.set()
        first.join(5)
        second.join(5)
        self.assertEqual(len(fake.calls), 1, "SingleFlight must coalesce concurrent identical requests")
        self.assertFalse(results["first"]["cache_hit"])
        self.assertTrue(results["second"]["cache_hit"])
        self.assertTrue(results["second"]["coalesced"])


class EngineProviderOverrideConsentTests(_EngineTestCase):
    """The `provider_override='laya-mlx'` escape hatch is consent-gated.

    `evaluate_typed` is the only real caller and only ever passes
    `recipe='generic'`, so these branches are exercised here directly against
    `judge()` -- still Engine's own public method and part of its contract.
    """

    def test_override_without_local_consent_is_refused(self):
        self.enroll(case_ids=["c1"], local_laya_enabled=False)
        engine = self.build_engine()
        with self.assertRaises(AutoError) as ctx:
            engine.judge("c1", {"a": 1}, {"q": {"type": "noul", "instructions": "x"}}, provider_override="laya-mlx")
        self.assertEqual(str(ctx.exception), "PROCESSOR_SWITCH_NEEDS_CONSENT")

    def test_override_for_a_non_generic_recipe_is_refused_even_with_consent(self):
        mlx_cfg = {"repository": "aac6fef/laya-mlx"}
        policy = self.enroll(case_ids=["c1"], local_laya_enabled=True, mlx=mlx_cfg)
        engine = self.build_engine()
        with self.assertRaises(AutoError) as ctx:
            engine.judge("c1", {"a": 1}, {"q": {"type": "noul", "instructions": "x"}}, policy,
                         provider_override="laya-mlx")
        self.assertEqual(str(ctx.exception), "PROCESSOR_SWITCH_NEEDS_CONSENT")

    def test_override_with_full_consent_and_the_generic_recipe_switches_provider(self):
        mlx_cfg = {"repository": "aac6fef/laya-mlx"}
        policy = self.enroll(case_ids=[], generic_query_enabled=True, local_laya_enabled=True, mlx=mlx_cfg)
        fake = FakeProviders(model="laya-mlx@deadbeef")
        engine = self.build_engine(fake)
        result = engine.judge("generic", {"a": 1}, {"q": {"type": "noul", "instructions": "x"}}, policy,
                               provider_override="laya-mlx")
        self.assertEqual(fake.calls[0].policy["provider"], "laya-mlx")
        self.assertFalse(result["cache_hit"])


# --------------------------------------------------------------------------
# Engine.effective_policy()
# --------------------------------------------------------------------------

class EngineEffectivePolicyTests(_EngineTestCase):
    """`effective_policy` resolves a per-recipe provider route.

    Every real caller passes an already-`validate_policy`-checked policy, and
    `validate_policy` already restricts `routes` values to the same three
    providers `effective_policy` re-checks -- so its own `PROVIDER_ROUTE` guard
    is unreachable through any real call path. It is still part of the
    method's contract (the method does not itself call validate_policy), so it
    is tested directly here against a hand-built dict.
    """

    def test_recipe_without_a_route_falls_back_to_the_policy_default_provider(self):
        policy = self.enroll(provider="openrouter", case_ids=["c1"])
        engine = self.build_engine()
        effective = engine.effective_policy(policy, "c1")
        self.assertEqual(effective["provider"], "openrouter")

    def test_recipe_with_an_explicit_route_uses_it(self):
        policy = self.enroll(provider="typesafe", case_ids=["c1"], routes={"c1": "openrouter"})
        engine = self.build_engine()
        effective = engine.effective_policy(policy, "c1")
        self.assertEqual(effective["provider"], "openrouter")

    def test_a_route_naming_an_unsupported_provider_is_refused(self):
        policy = self.enroll(case_ids=["c1"])
        engine = self.build_engine()
        hand_built = {**policy, "routes": {"c1": "not-a-real-provider"}}
        with self.assertRaises(AutoError) as ctx:
            engine.effective_policy(hand_built, "c1")
        self.assertEqual(str(ctx.exception), "PROVIDER_ROUTE")


# --------------------------------------------------------------------------
# Engine.dispatch() -- the ops with no other module behind them
# --------------------------------------------------------------------------

class EngineDispatchBasicOpsTests(_EngineTestCase):
    """health/stats/recall/prepare_runtime/warmup/probe/set_goal/prepare and
    the two request-shape guards (`REQUEST_OBJECT`, `UNKNOWN_OPERATION`)."""

    def test_health_reports_the_frozen_broker_handshake_version(self):
        self.enroll()
        engine = self.build_engine()
        health = engine.dispatch({"op": "health"})
        self.assertEqual(health, {"version": "1.0.0", "active": True, "provider": "typesafe",
                                   "native_browser_verified": False, "actual_host_savings_measured": False})

    def test_health_reports_inactive_once_the_policy_is_revoked(self):
        # Engine needs *a* policy to construct at all (store.prune reads
        # retention_days), so enroll first, then revoke, then ask health --
        # the one path where an Engine legitimately outlives its own policy.
        self.enroll()
        engine = self.build_engine()
        revoke(self.project)
        health = engine.dispatch({"op": "health"})
        self.assertEqual(health["active"], False)
        self.assertIsNone(health["provider"])

    def test_stats_reports_the_documented_shape(self):
        self.enroll()
        engine = self.build_engine()
        stats = engine.dispatch({"op": "stats"})
        self.assertEqual(set(stats), {"budget_rows", "evidence_records", "proposed_tool_characters_withheld",
                                       "host_tokens_saved", "host_cost_saved", "measurement_note"})

    def test_recall_of_a_source_record_slices_by_line_range(self):
        self.enroll()
        engine = self.build_engine()
        receipt_id = engine.store.put({"kind": "source", "text": "l1\nl2\nl3\nl4"})
        sliced = engine.dispatch({"op": "recall", "receipt_id": receipt_id, "start": 2, "end": 3})
        self.assertEqual(sliced, {"receipt_id": receipt_id, "start": 2, "end": 3, "text": "l2\nl3"})

    def test_recall_of_a_non_source_record_returns_the_whole_detail(self):
        self.enroll()
        engine = self.build_engine()
        receipt_id = engine.store.put({"kind": "decision", "foo": "bar"})
        detail = engine.dispatch({"op": "recall", "receipt_id": receipt_id})
        self.assertEqual(detail, {"receipt_id": receipt_id, "detail": {"kind": "decision", "foo": "bar"}})

    def test_recall_rejects_a_key_that_is_not_a_lowercase_hex_digest(self):
        self.enroll()
        engine = self.build_engine()
        with self.assertRaises(AutoError) as ctx:
            engine.dispatch({"op": "recall", "receipt_id": "not-a-real-key"})
        self.assertEqual(str(ctx.exception), "EVIDENCE_ID")

    def test_recall_rejects_an_out_of_bound_range(self):
        self.enroll()
        engine = self.build_engine()
        with self.assertRaises(AutoError) as ctx:
            engine.dispatch({"op": "recall", "receipt_id": "a" * 64, "start": 1, "end": 5000})
        self.assertEqual(str(ctx.exception), "RECALL_RANGE")

    def test_prepare_runtime_reports_ready_without_mlx_configured(self):
        self.enroll()
        engine = self.build_engine()
        self.assertEqual(engine.dispatch({"op": "prepare_runtime"}), {"ready": True, "provider": "typesafe"})

    def test_prepare_runtime_delegates_to_provider_warmup_without_waiting_when_mlx_is_configured(self):
        self.enroll(mlx={"repository": "aac6fef/laya-mlx"})
        engine = self.build_engine()
        self.assertEqual(engine.dispatch({"op": "prepare_runtime"}), {"ready": True, "provider": "laya-mlx"})

    def test_warmup_without_mlx_configured_raises(self):
        self.enroll()
        engine = self.build_engine()
        with self.assertRaises(AutoError) as ctx:
            engine.dispatch({"op": "warmup"})
        self.assertEqual(str(ctx.exception), "MLX_NOT_CONFIGURED")

    def test_probe_returns_the_expected_choice_from_a_deterministic_fixture(self):
        self.enroll(case_ids=[], local_recipe_ids=["probe"])
        fake = FakeProviders(answers={"department": {"type": "choice", "choice": "billing", "confidence": 0.95,
                                                       "probabilities": {"billing": 1.0, "technical": 0.0, "other": 0.0}}})
        engine = self.build_engine(fake)
        result = engine.dispatch({"op": "probe", "nonce": 42})
        self.assertEqual(result["expected_choice"], "billing")
        self.assertEqual(result["answers"]["department"]["choice"], "billing")
        self.assertEqual(result["mode"], "actual_model_inference")

    def test_set_goal_stores_a_bounded_goal_for_a_session(self):
        self.enroll()
        engine = self.build_engine()
        self.assertEqual(engine.dispatch({"op": "set_goal", "session": "s1", "goal": "Investigate the failing build"}),
                         {"stored": True})

    def test_set_goal_rejects_an_oversized_session_id(self):
        self.enroll()
        engine = self.build_engine()
        with self.assertRaises(AutoError) as ctx:
            engine.dispatch({"op": "set_goal", "session": "s" * 300, "goal": "x"})
        self.assertEqual(str(ctx.exception), "GOAL_SHAPE")

    def test_set_goal_screens_a_sensitive_goal(self):
        self.enroll()
        engine = self.build_engine()
        with self.assertRaises(AutoError) as ctx:
            engine.dispatch({"op": "set_goal", "session": "s1", "goal": "api_key: abcdef123456"})
        self.assertEqual(str(ctx.exception), "SENSITIVE_PAYLOAD_NOT_SENT")

    def test_prepare_returns_a_no_op_reason_for_a_short_goal(self):
        self.enroll()
        engine = self.build_engine()
        result = engine.dispatch({"op": "prepare", "goal": "fix it"})
        self.assertEqual(result, {"packet": "", "selected": [], "reason": "preparation_not_needed"})

    def test_prepare_screens_a_sensitive_goal(self):
        self.enroll()
        engine = self.build_engine()
        with self.assertRaises(AutoError) as ctx:
            engine.dispatch({"op": "prepare", "goal": "please refactor and fix this: password: hunter2sekrit12"})
        self.assertEqual(str(ctx.exception), "SENSITIVE_PAYLOAD_NOT_SENT")

    def test_unknown_operation_is_refused(self):
        self.enroll()
        engine = self.build_engine()
        with self.assertRaises(AutoError) as ctx:
            engine.dispatch({"op": "not_a_real_operation"})
        self.assertEqual(str(ctx.exception), "UNKNOWN_OPERATION")

    def test_a_non_dict_request_is_refused_before_reading_any_op(self):
        self.enroll()
        engine = self.build_engine()
        with self.assertRaises(AutoError) as ctx:
            engine.dispatch("not-a-dict")
        self.assertEqual(str(ctx.exception), "REQUEST_OBJECT")


# --------------------------------------------------------------------------
# Engine.evaluate_case() -- the legacy jevkit fixture path
# --------------------------------------------------------------------------

class EngineEvaluateCaseTests(_EngineTestCase):
    """`evaluate_case` bridges to the shipped `jevkit` use-case fixtures.

    "01-skill-routing" is a real, shipped use case (not synthesized by this
    test) so this doubles as a check that the fixture, the question compiler,
    and Engine's own receipt path still agree end to end.
    """

    def test_a_route_decision_produces_a_compact_recommend_receipt(self):
        self.enroll(case_ids=["01-skill-routing"])
        fake = FakeProviders(answers={"decision": {"type": "choice", "choice": "testing", "confidence": 0.95,
                                                     "probabilities": {"testing": 1.0, "docs": 0.0, "browser": 0.0, "none": 0.0}}})
        engine = self.build_engine(fake)
        state = {"request": "Run and diagnose the checkout regression tests.",
                  "skills": {"testing": "unit and regression tests", "docs": "edit documentation",
                             "browser": "inspect browser UI"}}
        receipt = engine.dispatch({"op": "evaluate", "case_id": "01-skill-routing", "state": state})
        self.assertEqual(receipt["status"], "RECOMMEND")
        self.assertEqual(receipt["recommendation"], "testing")
        self.assertEqual(receipt["version"], "1.0.0")
        self.assertFalse(receipt["execution_authorized"])

    def test_an_unknown_case_id_leaks_an_untyped_jevkit_error_instead_of_autoerror(self):
        """PRE-EXISTING DEFECT (report only; do not patch):

        FILE: jev_auto/engine.py:65, inside Engine.evaluate_case.
        `spec(case)` (imported from jevkit.engine) raises `jevkit.security.
        SafeError('UNKNOWN_CASE')` for an unenrolled/unknown case id.
        SafeError is NOT a subclass of jev_auto.common.AutoError (verified:
        `issubclass(jevkit.security.SafeError, jev_auto.common.AutoError)` is
        False). Every *other* Engine method -- judge, recall, browser, the
        whole evaluate_typed family -- raises AutoError with a stable code, and
        jev_auto/server.py's Handler only special-cases AutoError; an
        uncaught SafeError instead falls into its generic
        `except Exception: {'ok': False, 'error': 'BROKER_INTERNAL_ERROR'}`,
        so a caller loses the specific, actionable 'UNKNOWN_CASE' code and
        cannot distinguish it from an actual internal crash.
        CURRENT: dispatch(op='evaluate') with an unenrolled case_id raises
        jevkit.security.SafeError.
        CORRECT (per every sibling method's contract): it should raise
        jev_auto.common.AutoError so the broker can report a stable code.
        CONCRETE FAILING INPUT: case_ids=['not-a-real-case'] enrolled, then
        dispatch({'op': 'evaluate', 'case_id': 'not-a-real-case', 'state': {}}).
        """
        self.enroll(case_ids=["not-a-real-case"])
        engine = self.build_engine()
        with self.assertRaises(AutoError):
            engine.dispatch({"op": "evaluate", "case_id": "not-a-real-case", "state": {}})

    # test_an_unknown_case_id_leaks_an_untyped_jevkit_error_instead_of_autoerror: defect fixed in 1.0.7; kept as a regression guard.


# --------------------------------------------------------------------------
# Engine.evaluate_typed() and its five callers
# --------------------------------------------------------------------------

class EngineEvaluateTypedFamilyTests(_EngineTestCase):
    """typed_query, verify, rerank, route, recipe_try and review_diff all
    funnel through evaluate_typed -> src.adl.queries.typed.prepare_query.

    `prepare_query` reads the policy itself (see
    `EngineBaseParameterNotForwardedTests`), so every test here enrolls with
    `generic_query_enabled=True` and lets Engine use the default `base=None`,
    which this file's `XDG_STATE_HOME` redirection makes safe.
    """

    def test_typed_query_happy_path(self):
        self.enroll(generic_query_enabled=True)
        engine = self.build_engine()
        result = engine.dispatch({"op": "typed_query", "state": {"a": 1},
                                   "questions": {"q": {"type": "noul", "instructions": "x"}},
                                   "provider": "typesafe", "data_classification": "public"})
        self.assertEqual(result["status"], "ADVISORY")
        self.assertFalse(result["execution_authorized"])

    def test_typed_query_maps_a_compiler_rejection_to_a_stable_autoerror(self):
        # provider='openrouter' while the enrolled default is 'typesafe' and no
        # local override is configured -> typed.py's own PROCESSOR_SWITCH_NEEDS_CONSENT.
        self.enroll(provider="typesafe", generic_query_enabled=True)
        engine = self.build_engine()
        with self.assertRaises(AutoError) as ctx:
            engine.dispatch({"op": "typed_query", "state": {"a": 1},
                              "questions": {"q": {"type": "noul", "instructions": "x"}},
                              "provider": "openrouter", "data_classification": "public"})
        self.assertEqual(str(ctx.exception), "PROCESSOR_SWITCH_NEEDS_CONSENT")

    def test_typed_query_over_the_local_laya_route_with_an_attested_model(self):
        model_dir, manifest_path, revision, weight_sha256 = _write_attested_mlx_fixture(self._tmp.name)
        mlx_cfg = {"repository": "aac6fef/laya-mlx", "revision": revision, "weight_sha256": weight_sha256,
                   "model_dir": str(model_dir), "artifact_manifest": str(manifest_path)}
        self.enroll(generic_query_enabled=True, local_laya_enabled=True, mlx=mlx_cfg)
        fake = FakeProviders(model=f"laya-mlx@{revision}")
        engine = self.build_engine(fake)
        result = engine.dispatch({"op": "typed_query", "state": {"a": 1},
                                   "questions": {"q": {"type": "noul", "instructions": "x"}},
                                   "provider": "laya-mlx", "data_classification": "restricted"})
        self.assertEqual(result["provider"], "laya-mlx")
        self.assertEqual(result["model"], f"laya-mlx@{revision}")

    def test_typed_query_over_the_local_laya_route_rejects_a_model_identity_mismatch(self):
        model_dir, manifest_path, revision, weight_sha256 = _write_attested_mlx_fixture(self._tmp.name)
        mlx_cfg = {"repository": "aac6fef/laya-mlx", "revision": revision, "weight_sha256": weight_sha256,
                   "model_dir": str(model_dir), "artifact_manifest": str(manifest_path)}
        self.enroll(generic_query_enabled=True, local_laya_enabled=True, mlx=mlx_cfg)
        fake = FakeProviders(model="totally-not-the-attested-model")
        engine = self.build_engine(fake)
        with self.assertRaises(AutoError) as ctx:
            engine.dispatch({"op": "typed_query", "state": {"a": 1},
                              "questions": {"q": {"type": "noul", "instructions": "x"}},
                              "provider": "laya-mlx", "data_classification": "restricted"})
        self.assertEqual(str(ctx.exception), "MODEL_MISMATCH")

    def test_typed_query_truncates_then_empties_a_very_wide_answer_set(self):
        # 60 choice questions (the protocol maximum) with two 120-character
        # labels each: small enough to clear typed.py's 20_000-byte question
        # cap, but the *stripped* projection (type/choice/confidence only,
        # dropping `probabilities`) is still large enough on its own to blow
        # through evaluate_typed's second 8_000-byte cutoff, which empties
        # `answers` outright.
        self.enroll(generic_query_enabled=True)
        engine = self.build_engine(FakeProviders())
        wide_questions = {
            f"q{i}": {"type": "choice", "instructions": "pick one",
                      "criteria": {(str(label) + "o" * 120)[:120]: "d" for label in range(2)}}
            for i in range(60)
        }
        result = engine.dispatch({"op": "typed_query", "state": {"a": 1}, "questions": wide_questions,
                                   "provider": "typesafe", "data_classification": "public"})
        self.assertTrue(result["detail_available"])
        self.assertEqual(result["answers"], {})

    def test_verify_extraction_rejects_an_out_of_range_or_boolean_threshold(self):
        self.enroll(generic_query_enabled=True)
        engine = self.build_engine()
        for bad_threshold in (2.0, 0.0, True):
            with self.subTest(threshold=bad_threshold):
                with self.assertRaises(AutoError) as ctx:
                    engine.dispatch({"op": "verify", "source_text": "The total is 500.",
                                      "extraction": {"total": "500"}, "threshold": bad_threshold,
                                      "data_classification": "public"})
                self.assertEqual(str(ctx.exception), "VERIFY_THRESHOLD_INVALID")

    def test_verify_extraction_reports_a_trustworthy_field(self):
        self.enroll(generic_query_enabled=True)
        fake = FakeProviders(answers={"f0": {"type": "noul", "noul": 0.05}})
        engine = self.build_engine(fake)
        result = engine.dispatch({"op": "verify", "source_text": "The invoice total is $500.",
                                   "extraction": {"total": "500"}, "data_classification": "public"})
        self.assertTrue(result["trustworthy"])
        self.assertEqual(result["suspect_fields"], [])

    def test_rerank_memories_reports_should_abstain_when_nothing_is_usable(self):
        self.enroll(generic_query_enabled=True)
        fake = FakeProviders(answers={"m0": {"type": "score", "score": 0, "confidence": 0.9,
                                              "probabilities": {"0": 1.0, "1": 0.0, "2": 0.0, "3": 0.0}},
                                       "set_answers_question": {"type": "noul", "noul": 0.05}})
        engine = self.build_engine(fake)
        result = engine.dispatch({"op": "rerank", "query": "what is the total?",
                                   "memories": [{"fact_id": "m1", "content": "unrelated content", "score": 0.5}],
                                   "data_classification": "public"})
        self.assertTrue(result["should_abstain"])

    def test_route_selects_a_candidate_from_a_closed_set(self):
        self.enroll(generic_query_enabled=True)
        fake = FakeProviders(answers={"selected": {"type": "choice", "choice": "pytest", "confidence": 0.9,
                                                    "probabilities": {"pytest": 1.0, "jest": 0.0, "unknown": 0.0}}})
        engine = self.build_engine(fake)
        result = engine.dispatch({"op": "route", "kind": "tool", "task": "Pick a tool to run tests",
                                   "candidates": [{"id": "pytest", "description": "Run python tests"},
                                                  {"id": "jest", "description": "Run js tests"}],
                                   "data_classification": "public"})
        self.assertEqual(result["candidate_id"], "pytest")
        self.assertEqual(result["status"], "ADVISORY_UNCALIBRATED")

    def test_route_abstains_when_the_model_picks_the_closed_sets_unknown_option(self):
        self.enroll(generic_query_enabled=True)
        fake = FakeProviders(answers={"selected": {"type": "choice", "choice": "unknown", "confidence": 0.1,
                                                    "probabilities": {"pytest": 0.0, "jest": 0.0, "unknown": 1.0}}})
        engine = self.build_engine(fake)
        result = engine.dispatch({"op": "route", "kind": "tool", "task": "Pick a tool to run tests",
                                   "candidates": [{"id": "pytest", "description": "Run python tests"},
                                                  {"id": "jest", "description": "Run js tests"}],
                                   "data_classification": "public"})
        self.assertEqual(result["status"], "ABSTAIN_UNKNOWN")
        self.assertIsNone(result["candidate_id"])

    def test_try_recipe_runs_a_shipped_public_recipe(self):
        self.enroll(generic_query_enabled=True)
        fake = FakeProviders(answers={"decision": {"type": "score", "score": 0, "confidence": 0.9,
                                                    "probabilities": {"0": 1.0, "1": 0.0, "2": 0.0}}})
        engine = self.build_engine(fake)
        result = engine.dispatch({"op": "recipe_try", "recipe_id": "qualixar.brief-fit",
                                   "input": {"query": "Explain the idea plainly",
                                             "candidate": "This paragraph explains the idea plainly."},
                                   "data_classification": "public"})
        self.assertEqual(result["status"], "EXPERIMENTAL_ADVISORY")
        self.assertEqual(result["recipe_id"], "qualixar.brief-fit")

    def test_review_diff_returns_a_risk_score_and_a_closed_focus_choice(self):
        self.enroll(generic_query_enabled=True)
        fake = FakeProviders(answers={
            "risk": {"type": "score", "score": 1, "confidence": 0.9,
                     "probabilities": {"0": 0.0, "1": 1.0, "2": 0.0}},
            "focus": {"type": "choice", "choice": "correctness", "confidence": 0.9,
                      "probabilities": {"correctness": 1.0, "security": 0.0, "compatibility": 0.0,
                                         "test_gap": 0.0, "documentation": 0.0, "unknown": 0.0}},
        })
        engine = self.build_engine(fake)
        result = engine.dispatch({"op": "review_diff", "goal": "Review this change",
                                   "diff": "--- a/f.py\n+++ b/f.py\n-old\n+new\n",
                                   "data_classification": "public"})
        self.assertEqual(result["focus"], "correctness")
        self.assertEqual(result["status"], "ADVISORY_NOT_REVIEW_VERDICT")
        self.assertIn("independent_code_review", result["required_followup"])


class EngineResultShapeGuardTests(_EngineTestCase):
    """route/try_recipe/review_diff each re-check their provider answer's shape
    (ROUTE_RESULT_INVALID, RECIPE_RESULT_INVALID, REVIEW_RESULT_INVALID) on
    top of what protocol.validate_response already guarantees.

    OBSERVATION (not a defect -- reported for visibility, not fixed): through
    the real pipeline (evaluate_typed -> judge -> validate_response), both
    guards are unreachable. validate_response already guarantees `set(answers)
    == set(questions)` (ANSWER_IDS) and `answer['type'] == question['type']`
    for every key (ANSWER_TYPE) before try_recipe/review_diff ever see the
    result, so their own `answer.get('type') != ...` / `risk.get('type') !=
    'score'` re-checks can only fire for a shape the real pipeline can no
    longer produce. This is the same pattern as
    EngineEffectivePolicyTests.test_a_route_naming_an_unsupported_provider_is_refused
    (routes are already constrained to PROVIDERS by validate_policy before
    effective_policy ever runs) and
    EngineProviderOverrideConsentTests.test_override_for_a_non_generic_recipe_is_refused_even_with_consent
    (evaluate_typed only ever calls judge with recipe='generic'). None of the
    three cause incorrect behaviour; they are defense in depth against a
    caller that does not go through the guarantees the real pipeline provides.
    These tests reach the lines by monkeypatching `evaluate_typed` directly on
    the Engine instance, which is the only way to hand try_recipe/review_diff
    a shape their real caller can no longer produce.
    """

    def test_route_error_table(self):
        self.enroll(generic_query_enabled=True)
        base_candidates = [{"id": "a", "description": "d"}, {"id": "b", "description": "d2"}]
        cases = {
            "wrong answer type": {"selected": {"type": "noul", "noul": 0.1}},
            "choice not among the offered ids": {"selected": {"type": "choice", "choice": "not-an-offered-id",
                                                                 "confidence": 0.9,
                                                                 "probabilities": {"a": 0.5, "b": 0.3, "unknown": 0.2}}},
            "probabilities keys do not match the offered ids": {"selected": {"type": "choice", "choice": "a",
                                                                              "confidence": 0.9,
                                                                              "probabilities": {"a": 1.0}}},
        }
        for name, answers in cases.items():
            with self.subTest(name=name):
                engine = self.build_engine()
                engine.evaluate_typed = lambda *a, _answers=answers, **k: {
                    "answers": _answers, "provider": "typesafe", "model": "jev-1.13.0",
                    "receipt_id": "x", "cache_hit": False, "calibration_status": "NOT_EVALUATED",
                }
                with self.assertRaises(AutoError) as ctx:
                    engine.dispatch({"op": "route", "kind": "tool", "task": "pick",
                                      "candidates": base_candidates, "data_classification": "public"})
                self.assertEqual(str(ctx.exception), "ROUTE_RESULT_INVALID")

    def test_recipe_try_rejects_a_result_missing_its_decision_answer(self):
        self.enroll(generic_query_enabled=True)
        engine = self.build_engine()
        engine.evaluate_typed = lambda *a, **k: {
            "answers": {}, "provider": "typesafe", "model": "jev-1.13.0",
            "receipt_id": "x", "cache_hit": False, "calibration_status": "NOT_EVALUATED",
        }
        with self.assertRaises(AutoError) as ctx:
            engine.dispatch({"op": "recipe_try", "recipe_id": "qualixar.brief-fit",
                              "input": {"query": "q", "candidate": "c"}, "data_classification": "public"})
        self.assertEqual(str(ctx.exception), "RECIPE_RESULT_INVALID")

    def test_review_diff_rejects_a_risk_answer_that_is_not_a_score(self):
        self.enroll(generic_query_enabled=True)
        engine = self.build_engine()
        engine.evaluate_typed = lambda *a, **k: {
            "answers": {"risk": {"type": "choice", "choice": "x", "confidence": 0.9, "probabilities": {"x": 1.0}},
                        "focus": {"type": "choice", "choice": "correctness", "confidence": 0.9,
                                  "probabilities": {"correctness": 1.0}}},
            "provider": "typesafe", "model": "jev-1.13.0", "receipt_id": "x", "cache_hit": False,
            "calibration_status": "NOT_EVALUATED",
        }
        with self.assertRaises(AutoError) as ctx:
            engine.dispatch({"op": "review_diff", "goal": "review", "diff": "--- a\n+++ b\n",
                              "data_classification": "public"})
        self.assertEqual(str(ctx.exception), "REVIEW_RESULT_INVALID")


def _write_attested_mlx_fixture(tmp_root):
    """Write a model.safetensors + artifact_manifest pair whose hashes agree,
    satisfying src.adl.queries.typed's local-model attestation check."""
    import hashlib

    root = Path(tmp_root)
    model_dir = root / "mlx-model"
    model_dir.mkdir(exist_ok=True)
    weights = model_dir / "model.safetensors"
    weights.write_bytes(b"fake-attested-weights")
    weight_sha256 = hashlib.sha256(weights.read_bytes()).hexdigest()
    revision = "a" * 40
    manifest_path = root / "manifest.json"
    manifest_path.write_text(json.dumps({"repository": "aac6fef/laya-mlx", "revision": revision,
                                          "files": {"model.safetensors": weight_sha256}}))
    manifest_path.chmod(0o600)
    return model_dir, manifest_path, revision, weight_sha256


class EngineBrowserOpTests(_EngineTestCase):
    """`browser()` is implemented directly in engine.py (no delegate module),
    so its five validation guards and its success path are this file's job."""

    def _enrolled_engine(self):
        self.enroll(browser_origins=["https://example.com"], browser_max_steps=5)
        return self.build_engine()

    def test_browser_success_returns_the_chosen_action_with_its_probability(self):
        engine = self._enrolled_engine()
        request = {"op": "browser", "origin": "https://example.com", "step_index": 0, "goal": "g",
                   "state": {"url": "https://example.com"},
                   "actions": [{"id": "a1", "op": "click", "description": "d"}]}
        result = engine.dispatch(request)
        self.assertEqual(result["choice"], "a1")
        self.assertFalse(result["execution_authorized"])

    def test_browser_error_table(self):
        engine = self._enrolled_engine()
        base = {"op": "browser", "origin": "https://example.com", "step_index": 0, "goal": "g",
                "state": {"url": "https://example.com"},
                "actions": [{"id": "a1", "op": "click", "description": "d"}]}
        cases = {
            "BROWSER_ORIGIN_NOT_ENROLLED": {**base, "origin": "https://not-enrolled.example"},
            "BROWSER_STEP_BUDGET (wrong type)": {**base, "step_index": "0"},
            "BROWSER_STEP_BUDGET (out of range)": {**base, "step_index": 99},
            "BROWSER_CANDIDATE_COUNT (not a list)": {**base, "actions": "nope"},
            "BROWSER_CANDIDATE_COUNT (too many)": {**base, "actions": [
                {"id": f"a{i}", "op": "click", "description": "d"} for i in range(40)]},
            "BROWSER_ACTION_IDS (duplicate)": {**base, "actions": [
                {"id": "a1", "op": "click", "description": "d"}, {"id": "a1", "op": "scroll", "description": "d2"}]},
            "BROWSER_ACTION_IDS (bad prefix)": {**base, "actions": [{"id": "x1", "op": "click", "description": "d"}]},
            "BROWSER_ACTION_TYPE (bad op)": {**base, "actions": [{"id": "a1", "op": "teleport", "description": "d"}]},
        }
        expected = {
            "BROWSER_ORIGIN_NOT_ENROLLED": "BROWSER_ORIGIN_NOT_ENROLLED",
            "BROWSER_STEP_BUDGET (wrong type)": "BROWSER_STEP_BUDGET",
            "BROWSER_STEP_BUDGET (out of range)": "BROWSER_STEP_BUDGET",
            "BROWSER_CANDIDATE_COUNT (not a list)": "BROWSER_CANDIDATE_COUNT",
            "BROWSER_CANDIDATE_COUNT (too many)": "BROWSER_CANDIDATE_COUNT",
            "BROWSER_ACTION_IDS (duplicate)": "BROWSER_ACTION_IDS",
            "BROWSER_ACTION_IDS (bad prefix)": "BROWSER_ACTION_IDS",
            "BROWSER_ACTION_TYPE (bad op)": "BROWSER_ACTION_TYPE",
        }
        for name, request in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(AutoError) as ctx:
                    engine.dispatch(request)
                self.assertEqual(str(ctx.exception), expected[name])


class EngineSieveAndUnknownRecipeOpTests(_EngineTestCase):
    """dispatch(op='sieve') itself is one delegate call inside engine.py; the
    internals of jev_auto.sieve are a different module's coverage target."""

    def test_sieve_delegates_and_returns_the_delegate_shape(self):
        self.enroll(local_recipe_ids=["sieve"])
        engine = self.build_engine()
        result = engine.dispatch({"op": "sieve", "goal": "Understand this log",
                                   "text": "short text under the min_chars bypass", "tool": "read_file",
                                   "tool_input": {"path": "x.log"}})
        self.assertIn("changed", result)
        self.assertIn("text", result)


# --------------------------------------------------------------------------
# The confirmed base= isolation defect
# --------------------------------------------------------------------------

class EngineBaseParameterNotForwardedTests(_EngineTestCase):
    """PRE-EXISTING DEFECT (report only; do not patch):

    FILE: jev_auto/engine.py:74, inside Engine.evaluate_typed:
        compiled = prepare_query(self.workspace, state, questions,
                                  provider=provider, data_classification=data_classification)
    This omits `base=self.base`. `src.adl.queries.typed.prepare_query` then
    calls `jev_auto.settings.load_policy(workspace_path)` with its `base`
    parameter defaulting to `None`, i.e. the *default* `home_root()`
    (`$XDG_STATE_HOME` or `~/.local/state`) -- regardless of what `base` this
    Engine was constructed with. `Engine.judge` (via `self.policy()`) and
    `Engine.__init__` (via `self.store`) both correctly use `self.base`.

    `base` is not a theoretical parameter: `jev_auto/server.py:main` parses a
    real `--state-base` CLI flag into it, and `jev_auto/ipc.py:ensure` passes
    `--state-base` to the broker subprocess whenever its own caller supplied a
    non-default `base` -- an isolation mechanism (used for exactly this kind
    of test) that silently stops working for six of Engine's fourteen ops
    (typed_query, verify, rerank, route, recipe_try, review_diff -- everything
    that funnels through evaluate_typed) the moment `base` is not the process
    default.
    CURRENT: with an Engine constructed against a custom `base` directory that
    holds a validly enrolled, generic_query_enabled policy, and with the
    process's default state root left with no policy for that workspace at
    all, `typed_query` raises AutoError('GENERIC_QUERY_NOT_ENROLLED') even
    though `engine.policy()` (using the same `self.base`) succeeds.
    CORRECT: it should succeed, exactly as `judge()`-driven ops do for the
    same Engine and the same `base`.
    CONCRETE FAILING INPUT: see body below -- a fresh temp dir used only as
    `base`, and $XDG_STATE_HOME pointed at a second, deliberately empty temp
    dir so the failure is the clean 'GENERIC_QUERY_NOT_ENROLLED' rather than a
    cross-contaminated 'POLICY_CHANGED' from a stray policy at the default
    location.
    """

    def test_typed_query_should_honor_the_engines_own_base_directory(self):
        with tempfile.TemporaryDirectory() as isolated_default_root:
            env_patch = patch.dict(os.environ, {"XDG_STATE_HOME": str(Path(isolated_default_root) / "empty")})
            env_patch.start()
            self.addCleanup(env_patch.stop)
            custom_base = Path(self._tmp.name) / "custom-base"
            policy = make_policy(self.project, "typesafe", generic_query_enabled=True)
            save_policy_new(self.project, policy, base=custom_base)
            engine = Engine(self.project, base=custom_base, provider=FakeProviders())
            self.assertEqual(engine.policy()["provider"], "typesafe", "sanity: judge()-driven ops see the enrolled policy")
            result = engine.dispatch({"op": "typed_query", "state": {"a": 1},
                                       "questions": {"q": {"type": "noul", "instructions": "x"}},
                                       "provider": "typesafe", "data_classification": "public"})
            self.assertEqual(result["status"], "ADVISORY")

    # test_typed_query_should_honor_the_engines_own_base_directory: defect fixed in 1.0.7; kept as a regression guard.


# --------------------------------------------------------------------------
# jev_auto/settings.py -- the persisted policy document
# --------------------------------------------------------------------------

class SettingsPolicyValidationTests(unittest.TestCase):
    """Every AutoError code `make_policy`/`validate_policy` can raise, plus
    the one successful shape each guard's happy path implies."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.project = Path(self._tmp.name) / "project"
        self.project.mkdir()

    def test_make_policy_success_shape(self):
        policy = make_policy(self.project, "typesafe", case_ids=["c1"])
        self.assertEqual(policy["provider"], "typesafe")
        self.assertEqual(policy["schema_version"], 1)
        self.assertIn("policy_id", policy)

    def test_make_policy_rejects_an_unsupported_provider(self):
        with self.assertRaises(AutoError) as ctx:
            make_policy(self.project, "not-a-real-provider")
        self.assertEqual(str(ctx.exception), "POLICY_CONFIG")

    def test_make_policy_rejects_an_out_of_range_days_value(self):
        with self.assertRaises(AutoError) as ctx:
            make_policy(self.project, "typesafe", days=0)
        self.assertEqual(str(ctx.exception), "POLICY_CONFIG")
        with self.assertRaises(AutoError) as ctx:
            make_policy(self.project, "typesafe", days=400)
        self.assertEqual(str(ctx.exception), "POLICY_CONFIG")

    def test_validate_policy_rejects_a_workspace_id_that_does_not_match_the_path(self):
        policy = make_policy(self.project, "typesafe")
        other_project = Path(self._tmp.name) / "other-project"
        other_project.mkdir()
        with self.assertRaises(AutoError) as ctx:
            validate_policy(policy, other_project)
        self.assertEqual(str(ctx.exception), "POLICY_WORKSPACE_MISMATCH")

    def test_make_policy_rejects_a_disabled_policy(self):
        with self.assertRaises(AutoError) as ctx:
            make_policy(self.project, "typesafe", enabled=False)
        self.assertEqual(str(ctx.exception), "AUTO_DISABLED_OR_EXPIRED")

    def test_validate_policy_rejects_an_expired_policy(self):
        policy = make_policy(self.project, "typesafe")
        with self.assertRaises(AutoError) as ctx:
            validate_policy(policy, self.project, now=policy["expires_at"] + 1)
        self.assertEqual(str(ctx.exception), "AUTO_DISABLED_OR_EXPIRED")

    def test_make_policy_rejects_an_out_of_range_integer_field(self):
        with self.assertRaises(AutoError) as ctx:
            make_policy(self.project, "typesafe", max_calls_per_day=0)
        self.assertEqual(str(ctx.exception), "POLICY_RANGE")

    def test_make_policy_rejects_an_out_of_range_float_field(self):
        with self.assertRaises(AutoError) as ctx:
            make_policy(self.project, "typesafe", timeout_seconds=999)
        self.assertEqual(str(ctx.exception), "POLICY_RANGE")

    def test_make_policy_rejects_an_unsupported_data_classification(self):
        with self.assertRaises(AutoError) as ctx:
            make_policy(self.project, "typesafe", data_classification="bogus")
        self.assertEqual(str(ctx.exception), "DATA_CLASSIFICATION")

    def test_make_policy_accepts_a_consistent_decision_mode(self):
        policy = make_policy(self.project, "typesafe", decision_mode="jev-public",
                              data_classification="public", local_laya_enabled=False)
        self.assertEqual(policy["decision_mode"], "jev-public")

    def test_make_policy_rejects_a_decision_mode_whose_shape_does_not_match(self):
        with self.assertRaises(AutoError) as ctx:
            make_policy(self.project, "typesafe", decision_mode="jev-public", data_classification="restricted")
        self.assertEqual(str(ctx.exception), "DECISION_MODE_INVALID")

    def test_make_policy_rejects_an_unsupported_credential_store(self):
        with self.assertRaises(AutoError) as ctx:
            make_policy(self.project, "typesafe", credential_store="other")
        self.assertEqual(str(ctx.exception), "CREDENTIAL_STORE")

    def test_make_policy_rejects_a_non_boolean_toggle(self):
        with self.assertRaises(AutoError) as ctx:
            make_policy(self.project, "typesafe", native_output_rewrite="yes")
        self.assertEqual(str(ctx.exception), "POLICY_BOOLEAN")

    def test_make_policy_rejects_a_non_boolean_local_laya_enabled(self):
        with self.assertRaises(AutoError) as ctx:
            make_policy(self.project, "typesafe", local_laya_enabled="yes")
        self.assertEqual(str(ctx.exception), "POLICY_BOOLEAN")

    def test_make_policy_rejects_a_non_boolean_generic_query_enabled(self):
        with self.assertRaises(AutoError) as ctx:
            make_policy(self.project, "typesafe", generic_query_enabled="yes")
        self.assertEqual(str(ctx.exception), "POLICY_BOOLEAN")

    def test_make_policy_rejects_auto_prepare_without_generic_consent(self):
        with self.assertRaises(AutoError) as ctx:
            make_policy(self.project, "typesafe", auto_prepare_jev=True, generic_query_enabled=False)
        self.assertEqual(str(ctx.exception), "AUTO_PREPARE_REQUIRES_GENERIC_CONSENT")

    def test_make_policy_rejects_auto_prepare_over_the_local_route(self):
        with self.assertRaises(AutoError) as ctx:
            make_policy(self.project, "laya-mlx", auto_prepare_jev=True, generic_query_enabled=True)
        self.assertEqual(str(ctx.exception), "AUTO_PREPARE_HOSTED_ONLY")

    def test_make_policy_rejects_local_laya_enabled_without_an_attested_config(self):
        with self.assertRaises(AutoError) as ctx:
            make_policy(self.project, "typesafe", local_laya_enabled=True)
        self.assertEqual(str(ctx.exception), "LOCAL_ROUTE_NOT_ATTESTED")

    def test_make_policy_rejects_a_non_list_or_oversized_id_list(self):
        with self.assertRaises(AutoError) as ctx:
            make_policy(self.project, "typesafe", case_ids=[1, 2, 3])
        self.assertEqual(str(ctx.exception), "POLICY_LIST")

    def test_make_policy_rejects_a_malformed_browser_origin(self):
        with self.assertRaises(AutoError) as ctx:
            make_policy(self.project, "typesafe", browser_origins=["ftp://example.com"])
        self.assertEqual(str(ctx.exception), "BROWSER_ORIGIN")

    def test_make_policy_rejects_a_route_naming_an_unsupported_provider(self):
        with self.assertRaises(AutoError) as ctx:
            make_policy(self.project, "typesafe", routes={"generic": "not-a-real-provider"})
        self.assertEqual(str(ctx.exception), "POLICY_ROUTES")

    def test_make_policy_rejects_a_route_that_conflicts_with_the_decision_mode(self):
        with self.assertRaises(AutoError) as ctx:
            make_policy(self.project, "typesafe", decision_mode="jev-public", data_classification="public",
                        routes={"generic": "openrouter"})
        self.assertEqual(str(ctx.exception), "DECISION_MODE_ROUTE_CONFLICT")

    def test_make_policy_rejects_an_oversized_policy_document(self):
        with self.assertRaises(AutoError) as ctx:
            make_policy(self.project, "typesafe", padding="x" * 20_000)
        self.assertEqual(str(ctx.exception), "POLICY_SIZE")


class SettingsPersistenceLifecycleTests(unittest.TestCase):
    """load/save/replace/transition/revoke -- the on-disk policy lifecycle.

    Every method here calls a settings.py function with `base=None` (the
    default), which resolves to `home_root()` -- so, unlike
    SettingsPolicyValidationTests (which only ever calls the pure, in-memory
    `make_policy`/`validate_policy`), this class must redirect
    `XDG_STATE_HOME` itself or it will write real per-workspace state under
    the actual `~/.local/state/qualixar-jev-decision-layer`.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.project = root / "project"
        self.project.mkdir()
        env_patch = patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state-home")})
        env_patch.start()
        self.addCleanup(env_patch.stop)

    def test_load_policy_before_enrollment_is_refused(self):
        with self.assertRaises(AutoError) as ctx:
            load_policy(self.project)
        self.assertEqual(str(ctx.exception), "WORKSPACE_NOT_ENROLLED")

    def test_save_policy_new_then_load_policy_round_trips(self):
        policy = make_policy(self.project, "typesafe")
        save_policy_new(self.project, policy)
        self.assertEqual(load_policy(self.project)["policy_id"], policy["policy_id"])

    def test_save_policy_new_refuses_to_overwrite_an_existing_enrollment(self):
        policy = make_policy(self.project, "typesafe")
        save_policy_new(self.project, policy)
        with self.assertRaises(AutoError) as ctx:
            save_policy_new(self.project, policy)
        self.assertEqual(str(ctx.exception), "WORKSPACE_ALREADY_ENROLLED")

    def test_save_policy_overwrites_in_place(self):
        policy = make_policy(self.project, "typesafe")
        save_policy_new(self.project, policy)
        save_policy(self.project, {**policy, "max_calls_per_day": 42})
        self.assertEqual(load_policy(self.project)["max_calls_per_day"], 42)

    def test_replace_reviewed_policy_succeeds_then_refuses_a_stale_expectation(self):
        pending = {**make_policy(self.project, "typesafe"), "setup_origin": "local_wizard",
                   "setup_state": "ready", "setup_choice_digest": "x"}
        save_policy(self.project, pending)
        reviewed = make_policy(self.project, "openrouter")
        replace_reviewed_policy(self.project, pending, reviewed)
        self.assertEqual(load_policy(self.project)["provider"], "openrouter")
        with self.assertRaises(AutoError) as ctx:
            replace_reviewed_policy(self.project, pending, reviewed)
        self.assertEqual(str(ctx.exception), "SETUP_RECOVERY_CONFLICT")

    def test_transition_setup_policy_ready_succeeds_then_refuses_a_stale_expectation(self):
        pending = {**make_policy(self.project, "typesafe"), "setup_origin": "local_wizard",
                   "setup_state": "pending", "setup_choice_digest": "y"}
        save_policy(self.project, pending)
        transition_setup_policy_ready(self.project, pending)
        self.assertEqual(load_policy(self.project)["setup_state"], "ready")
        with self.assertRaises(AutoError) as ctx:
            transition_setup_policy_ready(self.project, pending)
        self.assertEqual(str(ctx.exception), "SETUP_RECOVERY_CONFLICT")

    def test_revoke_disables_the_policy_in_place(self):
        policy = make_policy(self.project, "typesafe")
        save_policy_new(self.project, policy)
        revoke(self.project)
        with self.assertRaises(AutoError) as ctx:
            load_policy(self.project)
        self.assertEqual(str(ctx.exception), "AUTO_DISABLED_OR_EXPIRED")

    def test_a_world_readable_pre_existing_lock_file_is_refused(self):
        # _policy_lock only sets mode 0o600 when it *creates* the lock file;
        # opening a pre-existing, too-permissive one must still be refused.
        from jev_auto.common import home_root, workspace_id

        policy = make_policy(self.project, "typesafe")
        state_dir_path = home_root() / workspace_id(self.project)
        state_dir_path.mkdir(parents=True, mode=0o700)
        lock_path = state_dir_path / ".policy.lock"
        lock_path.write_text("")
        lock_path.chmod(0o644)
        with self.assertRaises(AutoError) as ctx:
            save_policy_new(self.project, policy)
        self.assertEqual(str(ctx.exception), "POLICY_LOCK_UNSAFE")

    def test_replace_reviewed_policy_on_a_never_enrolled_workspace_is_a_conflict(self):
        # read_private raises FileNotFoundError (an OSError) before any content
        # comparison happens; that must map to the same stable conflict code as
        # a content mismatch, not leak a bare OSError.
        never_saved = make_policy(self.project, "typesafe")
        with self.assertRaises(AutoError) as ctx:
            replace_reviewed_policy(self.project, never_saved, never_saved)
        self.assertEqual(str(ctx.exception), "SETUP_RECOVERY_CONFLICT")

    def test_transition_setup_policy_ready_on_a_never_enrolled_workspace_is_a_conflict(self):
        never_saved = make_policy(self.project, "typesafe")
        with self.assertRaises(AutoError) as ctx:
            transition_setup_policy_ready(self.project, never_saved)
        self.assertEqual(str(ctx.exception), "SETUP_RECOVERY_CONFLICT")


class SettingsRenameExclusiveTests(unittest.TestCase):
    """`_rename_exclusive` has a Darwin path (`renamex_np`, exclusive rename
    without a TOCTOU race) and a fallback `os.link` path for every other OS.

    This machine is Darwin, so the fallback is only reachable by patching
    `platform.system()` -- the rename itself still runs for real against the
    real filesystem, only the OS-detection branch is faked. Also covers the
    one real (non-mocked) Darwin failure mode that is not "destination
    already exists": `renamex_np` failing because the destination's parent
    directory does not exist.

    The last test also calls `save_policy_new` with `base=None`, so this
    class needs the same `XDG_STATE_HOME` redirection as
    SettingsPersistenceLifecycleTests to avoid writing real state.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        env_patch = patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "state-home")})
        env_patch.start()
        self.addCleanup(env_patch.stop)

    def test_darwin_renamex_np_failure_that_is_not_eexist_is_mapped_to_policy_write_failed(self):
        from jev_auto.settings import _rename_exclusive

        source = self.root / "source"
        source.write_text("hello")
        with self.assertRaises(AutoError) as ctx:
            _rename_exclusive(source, self.root / "no-such-parent-dir" / "dest")
        self.assertEqual(str(ctx.exception), "POLICY_WRITE_FAILED")

    def test_non_darwin_fallback_uses_a_real_hardlink_rename(self):
        from jev_auto.settings import _rename_exclusive

        source = self.root / "source"
        source.write_text("hello")
        destination = self.root / "dest"
        with patch("jev_auto.settings.platform.system", return_value="Linux"):
            _rename_exclusive(source, destination)
            self.assertEqual(destination.read_text(), "hello")

            second_source = self.root / "source-2"
            second_source.write_text("world")
            with self.assertRaises(AutoError) as ctx:
                _rename_exclusive(second_source, destination)  # destination already exists
            self.assertEqual(str(ctx.exception), "WORKSPACE_ALREADY_ENROLLED")

            with self.assertRaises(AutoError) as ctx:
                _rename_exclusive(second_source, self.root / "no-such-parent-dir" / "dest")
            self.assertEqual(str(ctx.exception), "POLICY_WRITE_FAILED")

    def test_an_fsync_failure_during_save_policy_new_is_mapped_to_policy_write_failed(self):
        project = self.root / "project"
        project.mkdir()
        policy = make_policy(project, "typesafe")
        with patch("os.fsync", side_effect=OSError("synthetic disk full")):
            with self.assertRaises(AutoError) as ctx:
                save_policy_new(project, policy)
            self.assertEqual(str(ctx.exception), "POLICY_WRITE_FAILED")
        # The failed attempt must not leave a stray temp file blocking a retry.
        save_policy_new(project, policy)
        self.assertEqual(load_policy(project)["policy_id"], policy["policy_id"])


# --------------------------------------------------------------------------
# jev_auto/protocol.py -- the request/response contract
# --------------------------------------------------------------------------

class ProtocolValidateQuestionsTests(unittest.TestCase):
    """Direct coverage of validate_questions' own shape guards."""

    def test_error_table(self):
        cases = {
            "QUESTION_COUNT (empty)": ({}, "QUESTION_COUNT"),
            "QUESTION_COUNT (too many)": ({f"q{i}": {"type": "noul", "instructions": "x"} for i in range(61)}, "QUESTION_COUNT"),
            "QUESTION_SHAPE (non-str key)": ({1: {"type": "noul", "instructions": "x"}}, "QUESTION_SHAPE"),
            "QUESTION_SHAPE (question not a dict)": ({"a": "not-a-dict"}, "QUESTION_SHAPE"),
            "QUESTION_TYPE": ({"a": {"type": "bogus", "instructions": "x"}}, "QUESTION_TYPE"),
            "QUESTION_INSTRUCTIONS (blank)": ({"a": {"type": "noul", "instructions": "  "}}, "QUESTION_INSTRUCTIONS"),
            "CHOICE_OPTIONS (too few)": ({"a": {"type": "choice", "instructions": "x", "criteria": {"only": "one"}}}, "CHOICE_OPTIONS"),
            "SCORE_LEVELS (not a list)": ({"a": {"type": "score", "instructions": "x", "criteria": "nope"}}, "SCORE_LEVELS"),
        }
        for name, (questions, code) in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(AutoError) as ctx:
                    validate_questions(questions)
                self.assertEqual(str(ctx.exception), code)

    def test_question_bytes_cap_is_enforced(self):
        with self.assertRaises(AutoError) as ctx:
            validate_questions({"a": {"type": "noul", "instructions": "x" * 21_000}})
        self.assertEqual(str(ctx.exception), "QUESTION_BYTES")

    def test_a_well_formed_question_set_passes(self):
        validate_questions({"a": {"type": "noul", "instructions": "x"},
                             "b": {"type": "choice", "instructions": "x", "criteria": {"y": "z", "w": "v"}},
                             "c": {"type": "score", "instructions": "x", "criteria": ["lo", "hi"]}})


class ProtocolValidateResponseTests(unittest.TestCase):
    """Direct coverage of validate_response's answer-shape and consistency checks."""

    def setUp(self):
        self.choice_questions = {"pick": {"type": "choice", "instructions": "pick",
                                           "criteria": {"a": "opt a", "b": "opt b"}}}

    def test_happy_path_choice(self):
        raw = {"model": "m1", "answers": {"pick": {"type": "choice", "choice": "a", "confidence": 0.9,
                                                     "probabilities": {"a": 1.0, "b": 0.0}}},
               "usage": {"input_tokens": 1, "output_tokens": 1}}
        out = validate_response(raw, self.choice_questions)
        self.assertEqual(out["answers"]["pick"]["choice"], "a")

    def test_expected_model_mismatch(self):
        raw = {"model": "m1", "answers": {"pick": {"type": "choice", "choice": "a", "confidence": 0.9,
                                                     "probabilities": {"a": 1.0, "b": 0.0}}}, "usage": {}}
        with self.assertRaises(AutoError) as ctx:
            validate_response(raw, self.choice_questions, expected_model="other-model")
        self.assertEqual(str(ctx.exception), "MODEL_MISMATCH")

    def test_error_table(self):
        base_answer = {"type": "choice", "choice": "a", "confidence": 0.9, "probabilities": {"a": 1.0, "b": 0.0}}
        # Each case maps a description -> (raw response, expected code). The
        # code is carried explicitly rather than parsed out of the
        # description string, so a purely cosmetic rename of a case can never
        # silently change what the test actually asserts.
        cases = {
            "not a dict at all": ("not-a-dict", "RESPONSE_MODEL"),
            "model field missing": ({"answers": {}, "usage": {}}, "RESPONSE_MODEL"),
            "answers key set does not match the questions": ({"model": "m1", "answers": {}, "usage": {}}, "ANSWER_IDS"),
            "an answer's type does not match its question's type": (
                {"model": "m1", "answers": {"pick": {"type": "score", "score": 0}}, "usage": {}}, "ANSWER_TYPE"),
            "probabilities missing a label": (
                {"model": "m1", "answers": {"pick": {**base_answer, "probabilities": {"a": 1.0}}}, "usage": {}},
                "PROBABILITIES"),
            "probabilities do not sum to one": (
                {"model": "m1", "answers": {"pick": {**base_answer, "probabilities": {"a": 0.5, "b": 0.2}}}, "usage": {}},
                "PROBABILITY_SUM"),
            "confidence out of range": (
                {"model": "m1", "answers": {"pick": {**base_answer, "confidence": 5}}, "usage": {}}, "CONFIDENCE_RANGE"),
            "chosen label is not the argmax": (
                {"model": "m1", "answers": {"pick": {**base_answer, "choice": "b", "probabilities": {"a": 0.9, "b": 0.1}}}, "usage": {}},
                "CHOICE_ARGMAX"),
            "usage is a truthy non-dict": (
                {"model": "m1", "answers": {"pick": base_answer}, "usage": [1, 2, 3]}, "USAGE_SHAPE"),
            "usage token count is negative": (
                {"model": "m1", "answers": {"pick": base_answer}, "usage": {"input_tokens": -1}}, "USAGE_VALUE"),
        }
        for name, (raw, expected_code) in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(AutoError) as ctx:
                    validate_response(raw, self.choice_questions)
                self.assertEqual(str(ctx.exception), expected_code)

    def test_noul_happy_path_and_range_check(self):
        noul_questions = {"n": {"type": "noul", "instructions": "x"}}
        ok = validate_response({"model": "m1", "answers": {"n": {"type": "noul", "noul": 0.4}}, "usage": {}}, noul_questions)
        self.assertEqual(ok["answers"]["n"]["noul"], 0.4)
        with self.assertRaises(AutoError) as ctx:
            validate_response({"model": "m1", "answers": {"n": {"type": "noul", "noul": 5}}, "usage": {}}, noul_questions)
        self.assertEqual(str(ctx.exception), "NOUL_RANGE")

    def test_score_expectation_for_the_jev_model_uses_rounded_probability_tolerance(self):
        score_questions = {"decision": {"type": "score", "instructions": "rate fit",
                                         "criteria": ["No fit", "Some fit", "Direct fit"]}}
        raw = {"model": "jev-1.13.0", "answers": {"decision": {"type": "score", "score": 1.69, "confidence": 0.7,
               "probabilities": {"0": 0.01, "1": 0.28, "2": 0.71}}}, "usage": {}}
        self.assertEqual(validate_response(raw, score_questions)["answers"]["decision"]["score"], 1.69)
        with self.assertRaises(AutoError) as ctx:
            validate_response({**raw, "answers": {"decision": {**raw["answers"]["decision"], "score": 1.4}}}, score_questions)
        self.assertEqual(str(ctx.exception), "SCORE_EXPECTATION")

    def test_score_expectation_for_a_non_jev_model_uses_a_simple_weighted_average(self):
        score_questions = {"s": {"type": "score", "instructions": "x", "criteria": ["lo", "mid", "hi"]}}
        raw = {"model": "some-other-model", "answers": {"s": {"type": "score", "score": 1.0, "confidence": 0.9,
               "probabilities": {"0": 0.0, "1": 1.0, "2": 0.0}}}, "usage": {}}
        self.assertEqual(validate_response(raw, score_questions)["answers"]["s"]["score"], 1.0)
        with self.assertRaises(AutoError) as ctx:
            validate_response({**raw, "answers": {"s": {**raw["answers"]["s"], "score": 0.0}}}, score_questions)
        self.assertEqual(str(ctx.exception), "SCORE_EXPECTATION")


class ProtocolCompactReceiptTests(unittest.TestCase):
    """compact_receipt's allowlist projection and its two size-truncation levels."""

    def test_a_small_route_record_keeps_reason_codes_and_recommendation(self):
        record = {"case_id": "c", "record_sha256": "a" * 64, "mode": "live", "cache_hit": False,
                  "policy": {"status": "RECOMMEND", "recommendation": "go", "reasons": ["a bounded reason"]}}
        out = compact_receipt(record)
        self.assertEqual(out["recommendation"], "go")
        self.assertEqual(out["reason_codes"], ["a bounded reason"])
        self.assertEqual(out["version"], "1.0.0")

    def test_a_record_with_large_items_drops_items_first(self):
        record = {"case_id": "c", "record_sha256": "a" * 64, "mode": "live", "cache_hit": False,
                  "policy": {"status": "REVIEW", "recommendation": "rec",
                             "items": [{"n": i, "pad": "x" * 40} for i in range(200)],
                             "reasons": ["short reason"]}}
        out = compact_receipt(record)
        self.assertNotIn("items", out)
        self.assertTrue(out["detail_available"])
        self.assertIn("reason_codes", out, "the first truncation must not also drop reason_codes")

    def test_a_record_still_too_large_after_dropping_items_also_drops_reasons(self):
        record = {"case_id": "c", "record_sha256": "a" * 64, "mode": "live", "cache_hit": False,
                  "policy": {"status": "REVIEW", "recommendation": "rec",
                             "reasons": ["r" * 3000, "r" * 3000, "r" * 3000]}}
        out = compact_receipt(record)
        self.assertNotIn("reason_codes", out)
        self.assertIsNone(out["recommendation"])

    def test_only_the_first_three_reasons_are_ever_offered(self):
        record = {"case_id": "c", "record_sha256": "a" * 64, "mode": "live", "cache_hit": False,
                  "policy": {"status": "REVIEW", "recommendation": "rec", "reasons": ["one", "two", "three", "four"]}}
        out = compact_receipt(record)
        self.assertEqual(out["reason_codes"], ["one", "two", "three"])


# --------------------------------------------------------------------------
# jev_auto/review.py
# --------------------------------------------------------------------------

class ReviewCompileReviewTests(unittest.TestCase):
    """Every guard in compile_review, plus its two-question success shape."""

    def test_error_table(self):
        # Each case carries its expected code explicitly (not parsed out of
        # the description), so renaming a case can never silently change what
        # gets asserted.
        cases = {
            "goal is not a string": (123, "--- a\n+++ b\n", "REVIEW_GOAL_INVALID"),
            "goal is blank": ("   ", "--- a\n+++ b\n", "REVIEW_GOAL_INVALID"),
            "goal is too long": ("g" * 1001, "--- a\n+++ b\n", "REVIEW_GOAL_INVALID"),
            "diff is not a string": ("goal", None, "REVIEW_DIFF_INVALID"),
            "diff is empty": ("goal", "", "REVIEW_DIFF_INVALID"),
            "diff is too long": ("goal", "x" * 16_001, "REVIEW_DIFF_INVALID"),
            "diff is missing both '--- ' and '+++ ' markers": ("goal", "no diff markers here at all", "REVIEW_DIFF_INVALID"),
        }
        for name, (goal, diff, expected_code) in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(AutoError) as ctx:
                    compile_review(goal, diff)
                self.assertEqual(str(ctx.exception), expected_code)

    def test_success_shape_is_a_score_and_a_closed_choice(self):
        state, questions = compile_review("Review this change", "--- a/file.py\n+++ b/file.py\n-old\n+new\n")
        self.assertEqual(state["goal"], "Review this change")
        self.assertEqual({key: value["type"] for key, value in questions.items()}, {"risk": "score", "focus": "choice"})
        self.assertIn("unknown", questions["focus"]["criteria"])


# --------------------------------------------------------------------------
# jev_auto/agy_hook.py
# --------------------------------------------------------------------------

class AgyHookHandleTests(unittest.TestCase):
    """handle()'s shape guards and its one advisory success shape."""

    def test_only_the_zeroth_invocation_of_an_enrolled_workspace_gets_a_hint(self):
        with tempfile.TemporaryDirectory() as directory:
            event = {"invocationNum": 0, "workspacePaths": [directory]}
            first = agy_handle(event, policy_loader=lambda _path: {"enabled": True})
            second = agy_handle({**event, "invocationNum": 1}, policy_loader=lambda _path: {"enabled": True})
        self.assertEqual(set(first), {"injectSteps"})
        self.assertIn("Qualixar Jev", first["injectSteps"][0]["ephemeralMessage"])
        self.assertEqual(second, {})

    def test_malformed_or_unenrolled_events_are_all_silently_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            cases = {
                "invocationNum missing": {"workspacePaths": [directory]},
                "invocationNum not an int": {"invocationNum": "0", "workspacePaths": [directory]},
                "workspacePaths not a list": {"invocationNum": 0, "workspacePaths": directory},
                "workspacePaths empty": {"invocationNum": 0, "workspacePaths": []},
                "workspacePaths too many": {"invocationNum": 0, "workspacePaths": [directory] * 17},
                "workspacePaths[0] not a string": {"invocationNum": 0, "workspacePaths": [123]},
            }
            for name, event in cases.items():
                with self.subTest(name=name):
                    self.assertEqual(agy_handle(event, policy_loader=lambda _path: {"enabled": True}), {})

            not_enrolled = agy_handle({"invocationNum": 0, "workspacePaths": [directory]},
                                       policy_loader=lambda _path: (_ for _ in ()).throw(RuntimeError("no policy")))
            self.assertEqual(not_enrolled, {})
            disabled = agy_handle({"invocationNum": 0, "workspacePaths": [directory]},
                                   policy_loader=lambda _path: {"enabled": False})
            self.assertEqual(disabled, {})


class AgyHookMainTests(unittest.TestCase):
    """main()'s three outcomes: skip-oversized, exception-swallowed, and a
    real end-to-end run through the actual settings.load_policy default."""

    def _run_main(self, raw_bytes):
        out = io.StringIO()
        with patch("sys.stdin", SimpleNamespace(buffer=io.BytesIO(raw_bytes))), patch("sys.stdout", out):
            agy_main()
        return out.getvalue()

    def test_oversized_input_is_skipped_without_attempting_to_decode(self):
        self.assertEqual(self._run_main(b"x" * 70_000), "{}\n")

    def test_invalid_json_is_swallowed_by_the_broad_except(self):
        self.assertEqual(self._run_main(b"not-json{{{"), "{}\n")

    def test_a_real_enrolled_workspace_produces_the_advisory_hint(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            project.mkdir()
            env_patch = patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state-home")})
            env_patch.start()
            self.addCleanup(env_patch.stop)
            save_policy_new(project, make_policy(project, "typesafe"))
            event = json.dumps({"invocationNum": 0, "workspacePaths": [str(project)]}).encode()
            output = json.loads(self._run_main(event))
        self.assertEqual(set(output), {"injectSteps"})


# --------------------------------------------------------------------------
# jev_auto/vendor/winnow_chunk.py
# --------------------------------------------------------------------------

class WinnowChunkTests(unittest.TestCase):
    """chunk()'s empty input, blank-line early flush, size-growth-on-overflow,
    non-positive block_lines clamp, and the final-remainder flush."""

    def test_empty_text_produces_no_blocks(self):
        self.assertEqual(chunk(""), [])

    def test_rejoining_block_texts_reproduces_the_input_exactly(self):
        text = "first\nsecond\nthird\nfourth\nfifth"
        blocks = chunk(text, block_lines=2, max_blocks=200)
        self.assertEqual("\n".join(block.text for block in blocks), text)

    def test_a_blank_line_flushes_a_block_early_once_it_is_at_least_half_full(self):
        blocks = chunk("a\nb\n\nc\nd\ne\nf", block_lines=4, max_blocks=200)
        self.assertEqual([(b.start, b.end) for b in blocks], [(1, 3), (4, 7)])

    def test_more_blocks_than_the_cap_forces_the_block_size_to_grow(self):
        text = "\n".join(str(i) for i in range(50))
        blocks = chunk(text, block_lines=1, max_blocks=3)
        self.assertLessEqual(len(blocks), 3)
        self.assertEqual("\n".join(block.text for block in blocks), text)

    def test_a_non_positive_block_lines_is_clamped_to_one(self):
        blocks = chunk("a\nb\nc", block_lines=0, max_blocks=200)
        self.assertEqual(len(blocks), 3)

    def test_block_n_lines_property(self):
        block = Block("b001", 0, 3, 7, "text")
        self.assertEqual(block.n_lines, 5)


class WinnowGroupContiguousTests(unittest.TestCase):
    """group_contiguous groups by consecutive .index, regardless of input order."""

    def test_a_gap_in_index_starts_a_new_group(self):
        blocks = [Block("b1", 0, 1, 1, "x"), Block("b2", 1, 2, 2, "y"), Block("b4", 3, 4, 4, "z")]
        groups = group_contiguous(blocks)
        self.assertEqual([[b.id for b in group] for group in groups], [["b1", "b2"], ["b4"]])

    def test_out_of_order_input_is_sorted_by_index_first(self):
        blocks = [Block("b2", 1, 2, 2, "y"), Block("b1", 0, 1, 1, "x")]
        groups = group_contiguous(blocks)
        self.assertEqual(len(groups), 1)
        self.assertEqual([b.id for b in groups[0]], ["b1", "b2"])


if __name__ == "__main__":
    unittest.main()
