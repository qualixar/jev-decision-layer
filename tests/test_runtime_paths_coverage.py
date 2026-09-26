"""Coverage-raising tests for six under-tested jev_auto modules.

Targets (see the coverage run this file was written against):
  sieve.py            bounded_blocks() fallback partition, render(), reduce_text()
  store.py            Store (budgets/cache/evidence/goals/events) and SingleFlight
  recipe_runtime.py   _validate_catalog, catalog_document, _catalog, prepare_recipe
  prepare.py          candidates() git/skill discovery, prepare() provider branches
  recipe_fixtures.py  validate_fixtures, _document/_gate guardrails, selftest resilience
  routing.py          compile_route() input validation

House style: stdlib only, unittest.TestCase, subTest for tables,
tempfile.TemporaryDirectory for anything touching disk. Every class docstring
says what breaks in production if the class's claim is wrong.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "plugins" / "qualixar-jev-decision-layer" / "runtime"
sys.path.insert(0, str(RUNTIME))

from jev_auto.common import AutoError, canonical, digest  # noqa: E402
from jev_auto import sieve  # noqa: E402
from jev_auto.sieve import bounded_blocks, render  # noqa: E402
from jev_auto.vendor.winnow_chunk import Block, chunk  # noqa: E402
from jev_auto.store import Store, SingleFlight  # noqa: E402
from jev_auto import prepare as prepare_module  # noqa: E402
from jev_auto import recipe_runtime  # noqa: E402
from jev_auto.recipe_runtime import (  # noqa: E402
    _validate_catalog, catalog_document, catalog_preview, prepare_recipe,
)
from jev_auto import recipe_fixtures  # noqa: E402
from jev_auto.recipe_fixtures import VARIANTS, run_fixture, selftest, validate_fixtures  # noqa: E402
from jev_auto.recipe_gate import ACT, VERIFY  # noqa: E402
from jev_auto.routing import compile_route  # noqa: E402
from jev_auto.prepare import candidates, prepare  # noqa: E402

CATALOG = json.loads((RUNTIME / "recipe_catalog.json").read_text())


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

def _line(n: int) -> str:
    """Neutral filler: no ERRORS/CONSTRAINTS/SENSITIVE trigger words."""
    return f"alpha bravo charlie delta line {n} of synthetic filler content for coverage testing"


def _pair_text(n_blocks: int) -> str:
    """2*n_blocks lines, no blank lines. With block_lines=2 this yields exactly
    n_blocks blocks of 2 lines each (verified empirically: no blank line means
    chunk()'s early-flush-on-blank never fires, so it's a plain fixed group)."""
    return "\n".join(_line(i) for i in range(2 * n_blocks))


def _quad_text(n_blocks: int) -> str:
    """4*n_blocks lines, for the LOCAL (laya-mlx) sieve path only: reduce_text
    hardcodes block_lines=4 for that path regardless of policy['block_lines']."""
    return "\n".join(_line(i) for i in range(4 * n_blocks))


BASE_SIEVE_POLICY = {
    "min_chars": 10, "max_blocks": 48, "block_lines": 2,
    "timeout_seconds": 5, "drop_probability": 0.10, "min_reduction": 0.05,
}


def make_budget_policy(**overrides):
    base = {"max_request_bytes": 1000, "enabled": True, "expires_at": time.time() + 86400,
            "policy_id": "policy-A", "max_calls_per_day": 5, "max_bytes_per_day": 5000}
    return {**base, **overrides}


def _valid_recipe(id_="test.valid-recipe", **overrides):
    base = {
        "id": id_, "title": "Test recipe", "audience": "business",
        "input_schema": {
            "type": "object", "additionalProperties": False,
            "properties": {"note": {"type": "string", "minLength": 1, "maxLength": 100}},
            "required": ["note"],
        },
        "questions": {"decision": {"type": "noul", "instructions": "Decide."}},
        "status": "SPECIFICATION_NOT_MODEL_EVALUATED",
        "limitations": "None.",
    }
    return {**base, **overrides}


def _valid_case(variant, **overrides):
    base = {
        "fixture_id": f"test.fixture:{variant}", "variant": variant, "data_classification": "synthetic",
        "state": {"note": "hi"}, "mock_answer": {"type": "noul", "noul": 0.9 if variant == "nominal" else 0.5},
        "expected_status": "RECOMMEND" if variant == "nominal" else "REVIEW",
        "expected_host_action": ACT if variant == "nominal" else VERIFY,
        "expected_recommendation": "outcome-a" if variant == "nominal" else None,
        "disclaimer": "Not a Jev prediction; synthetic fixture for offline contract testing.",
    }
    return {**base, **overrides}


def _valid_entry(id_="test.valid-entry"):
    return {"id": id_, "cases": [_valid_case(v) for v in VARIANTS]}


# ---------------------------------------------------------------------------
# sieve.py -- bounded_blocks()
# ---------------------------------------------------------------------------

class SieveBoundedBlocksTests(unittest.TestCase):
    """bounded_blocks() is sieve.py's only entry point into the vendored
    chunker. When that chunker's own paragraph-boundary heuristic still emits
    more blocks than the caller's cap (a real, observed upstream behaviour,
    not a hypothetical -- see the module docstring), this function must fall
    back to a fixed partition that keeps every original byte and line order.
    If this regresses, a host could silently receive fewer or reordered
    lines relative to the source it asked to reduce.
    """

    def test_within_cap_delegates_directly_to_the_vendored_chunker(self):
        text = _pair_text(3)
        self.assertEqual(bounded_blocks(text, lines=2, maximum=48),
                          chunk(text, block_lines=2, max_blocks=48))

    def test_over_cap_falls_back_to_a_fixed_partition_preserving_every_byte(self):
        lines = []
        for i in range(6):
            lines += [f"line{i}", f"line{i}", ""]
        text = "\n".join(lines)
        overflowing = chunk(text, block_lines=4, max_blocks=3)
        self.assertGreater(len(overflowing), 3)  # pins the upstream behaviour this fallback exists for

        blocks = bounded_blocks(text, lines=4, maximum=3)
        self.assertEqual(len(blocks), 3)
        self.assertEqual([b.id for b in blocks], ["b001", "b002", "b003"])
        self.assertEqual("\n".join(b.text for b in blocks), text)
        self.assertEqual([(b.start, b.end) for b in blocks], [(1, 6), (7, 12), (13, 18)])


# ---------------------------------------------------------------------------
# sieve.py -- render()
# ---------------------------------------------------------------------------

class SieveRenderTests(unittest.TestCase):
    """render() is what a host actually sees after a reduction: every hidden
    run of blocks must collapse into exactly one recoverable-omission marker
    spanning the true start/end lines, and every visible block must appear
    verbatim. A bug here either leaks nothing back (over-verbose, defeats the
    point) or, worse, drops visible text the model still needed.
    """

    BLOCKS = [
        Block("b001", 0, 1, 2, "alpha\nbravo"),
        Block("b002", 1, 3, 4, "charlie\ndelta"),
        Block("b003", 2, 5, 6, "echo\nfoxtrot"),
        Block("b004", 3, 7, 8, "golf\nhotel"),
    ]
    KEY = "k" * 64

    def test_no_hidden_blocks_reproduces_the_source_exactly(self):
        expected = "\n".join(b.text for b in self.BLOCKS)
        self.assertEqual(render(self.BLOCKS, hidden=set(), key=self.KEY), expected)

    def test_a_contiguous_hidden_run_collapses_into_one_marker(self):
        out = render(self.BLOCKS, hidden={"b002", "b003"}, key=self.KEY)
        self.assertIn("alpha\nbravo", out)
        self.assertIn("golf\nhotel", out)
        self.assertNotIn("charlie", out)
        self.assertNotIn("echo", out)
        self.assertEqual(out.count("jev-auto omitted lines"), 1)
        self.assertIn(
            f"jev-auto omitted lines 3-6; recover exact text with jev_recall receipt_id={self.KEY} start=3 end=6",
            out)

    def test_non_contiguous_hidden_blocks_produce_separate_markers(self):
        out = render(self.BLOCKS, hidden={"b001", "b003"}, key=self.KEY)
        self.assertEqual(out.count("jev-auto omitted lines"), 2)
        self.assertIn("charlie\ndelta", out)  # b002 stays visible between the two hidden blocks
        self.assertNotIn("alpha", out)
        self.assertNotIn("echo", out)

    def test_a_hidden_block_at_the_very_end_is_still_flushed(self):
        out = render(self.BLOCKS, hidden={"b004"}, key=self.KEY)
        self.assertTrue(out.rstrip().endswith("start=7 end=8]"))


# ---------------------------------------------------------------------------
# sieve.py -- reduce_text() guardrails (every "unchanged(...)" early exit)
# ---------------------------------------------------------------------------

class SieveReduceTextGuardrailTests(unittest.TestCase):
    """Every early exit in reduce_text returns the untouched original text
    under one of a fixed set of typed reasons. A workspace must never receive
    a rewritten payload when a guardrail disagrees -- not even a byte of it --
    so each of these asserts the FULL returned dict, not just the reason.
    """

    def test_non_string_text_is_rejected_before_any_policy_lookup(self):
        with self.assertRaises(AutoError):
            sieve.reduce_text(SimpleNamespace(), BASE_SIEVE_POLICY, "goal", 12345)

    def test_text_outside_the_size_window_bypasses_reduction(self):
        engine = SimpleNamespace()
        cases = {
            "too short": (dict(BASE_SIEVE_POLICY, min_chars=1000), "short text"),
            "too long": (BASE_SIEVE_POLICY, "x" * 100_001),
        }
        for label, (policy, text) in cases.items():
            with self.subTest(label):
                result = sieve.reduce_text(engine, policy, "Please debug the router.", text)
                self.assertEqual(result, {"changed": False, "text": text,
                                           "reason": "size_bypass", "withheld_chars": 0})

    def test_protected_tool_or_instructions_bypasses_reduction(self):
        engine = SimpleNamespace()
        text = _pair_text(3)
        result = sieve.reduce_text(engine, BASE_SIEVE_POLICY, "Please debug the router.", text,
                                    tool="Read", tool_input={"file_path": "AGENTS.md"})
        self.assertEqual(result, {"changed": False, "text": text,
                                   "reason": "protected_tool_or_instructions", "withheld_chars": 0})

    def test_text_carrying_an_error_signature_is_preserved_verbatim(self):
        engine = SimpleNamespace()
        text = _pair_text(3) + "\nTraceback (most recent call last):\n"
        result = sieve.reduce_text(engine, BASE_SIEVE_POLICY, "Please debug the router.", text)
        self.assertEqual(result, {"changed": False, "text": text,
                                   "reason": "error_preserved", "withheld_chars": 0})

    def test_missing_goal_bypasses_reduction(self):
        engine = SimpleNamespace()
        text = _pair_text(3)
        result = sieve.reduce_text(engine, BASE_SIEVE_POLICY, "", text)
        self.assertEqual(result, {"changed": False, "text": text,
                                   "reason": "goal_unknown", "withheld_chars": 0})

    def test_sensitive_payload_is_never_sent_for_a_reduction_decision(self):
        engine = SimpleNamespace()
        text = _pair_text(3)
        goal = "Rotate this fake credential AKIA" + "X" * 16 + " before merging."
        result = sieve.reduce_text(engine, BASE_SIEVE_POLICY, goal, text)
        self.assertEqual(result, {"changed": False, "text": text,
                                   "reason": "sensitive_data_not_sent", "withheld_chars": 0})

    def test_require_clean_is_best_effort_not_comprehensive_dlp(self):
        """Documented limitation, not a bug: text that LOOKS sensitive to a
        human but matches none of the fixed SENSITIVE patterns in common.py
        sails through this gate. Pinning this stops someone from "fixing"
        sieve.py with ad hoc heuristics that belong, if anywhere, in
        common.screen() -- and documents that this is a known boundary.
        """
        engine = SimpleNamespace(effective_policy=lambda p, recipe: {**p, "provider": "typesafe"})
        goal = "Please review this internal note before it goes out."
        text = ("Internal codename Nightingale: Q3 rollout plan is still confidential.\n"
                "Second line of context for the reviewer.")
        result = sieve.reduce_text(engine, BASE_SIEVE_POLICY, goal, text)
        self.assertNotEqual(result["reason"], "sensitive_data_not_sent")

    def test_fewer_than_three_blocks_bypasses_reduction(self):
        engine = SimpleNamespace(effective_policy=lambda p, recipe: {**p, "provider": "typesafe"})
        text = _pair_text(1)
        result = sieve.reduce_text(engine, BASE_SIEVE_POLICY, "Please debug the router.", text)
        self.assertEqual(result, {"changed": False, "text": text,
                                   "reason": "few_blocks", "withheld_chars": 0})


# ---------------------------------------------------------------------------
# sieve.py -- reduce_text() local (laya-mlx) provider path
# ---------------------------------------------------------------------------

class SieveReduceTextLocalProviderTests(unittest.TestCase):
    """The on-device (laya-mlx) path asks one small yes/no question per middle
    block, on a strict per-call deadline, hardcoding block_lines=4 regardless
    of policy. An unjudged block (call failed, or the deadline already
    passed) must be PRESERVED, never assumed droppable -- silently dropping
    unjudged content would be worse than not reducing at all.
    """

    def test_judges_each_middle_block_and_keeps_everything_when_all_score_high(self):
        text = _quad_text(4)  # 16 lines -> 4 fixed-size-4 blocks -> middle = b002, b003
        calls = []

        def judge(recipe, state, questions, p):
            calls.append(state["block"])
            return {"answers": {"needed": {"noul": 0.95}}}

        engine = SimpleNamespace(effective_policy=lambda p, recipe: {**p, "provider": "laya-mlx"}, judge=judge)
        result = sieve.reduce_text(engine, BASE_SIEVE_POLICY, "Please debug the router.", text)
        self.assertEqual(len(calls), 2)
        self.assertEqual(result["reason"], "nothing_confidently_irrelevant")
        self.assertFalse(result["changed"])

    def test_preserves_a_block_whose_judgment_call_fails(self):
        text = _quad_text(4)
        blocks = bounded_blocks(text, 4, BASE_SIEVE_POLICY["max_blocks"])
        failing_block, ok_block = blocks[1:-1]

        def judge(recipe, state, questions, p):
            if state["block"] == failing_block.text:
                raise AutoError("PROVIDER_UNAVAILABLE")
            return {"answers": {"needed": {"noul": 0.01}}}

        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            engine = SimpleNamespace(effective_policy=lambda p, recipe: {**p, "provider": "laya-mlx"},
                                      judge=judge, store=store)
            policy = {**BASE_SIEVE_POLICY, "min_reduction": 0.01}
            result = sieve.reduce_text(engine, policy, "Please debug the router.", text)
        self.assertTrue(result["changed"])
        self.assertIn(failing_block.text, result["text"])   # unjudged -> preserved
        self.assertNotIn(ok_block.text, result["text"])     # judged confidently low -> hidden

    def test_stops_asking_once_the_deadline_has_already_passed(self):
        text = _quad_text(4)
        calls = []

        def judge(recipe, state, questions, p):
            calls.append(1)
            return {"answers": {"needed": {"noul": 0.01}}}

        engine = SimpleNamespace(effective_policy=lambda p, recipe: {**p, "provider": "laya-mlx"}, judge=judge)
        policy = {**BASE_SIEVE_POLICY, "timeout_seconds": -1}
        result = sieve.reduce_text(engine, policy, "Please debug the router.", text)
        self.assertEqual(calls, [])
        self.assertEqual(result["reason"], "nothing_confidently_irrelevant")


# ---------------------------------------------------------------------------
# sieve.py -- reduce_text() hosted provider path
# ---------------------------------------------------------------------------

class SieveReduceTextHostedProviderTests(unittest.TestCase):
    """The hosted path asks one batched question covering every block plus an
    'error' signal, and integrates with the real Store for the success path:
    a hide decision must be persisted as recoverable evidence and reflected
    in stats(), not just returned in the response dict.
    """

    def test_preserves_everything_when_the_error_signal_is_uncertain(self):
        text = _pair_text(3)  # exactly one middle block, b002

        def judge(recipe, state, questions, p):
            answers = {"error": {"noul": 0.5}}
            answers.update({k: {"noul": 0.01} for k in questions if k != "error"})
            return {"answers": answers}

        engine = SimpleNamespace(effective_policy=lambda p, recipe: {**p, "provider": "typesafe"}, judge=judge)
        result = sieve.reduce_text(engine, BASE_SIEVE_POLICY, "Please debug the router.", text)
        self.assertEqual(result, {"changed": False, "text": text,
                                   "reason": "error_or_constraint_uncertain", "withheld_chars": 0})

    def test_preserves_original_text_when_the_judge_call_fails(self):
        text = _pair_text(3)

        def judge(recipe, state, questions, p):
            raise AutoError("PROVIDER_UNAVAILABLE")

        engine = SimpleNamespace(effective_policy=lambda p, recipe: {**p, "provider": "typesafe"}, judge=judge)
        result = sieve.reduce_text(engine, BASE_SIEVE_POLICY, "Please debug the router.", text)
        self.assertEqual(result, {"changed": False, "text": text,
                                   "reason": "decision_unavailable_original_preserved", "withheld_chars": 0})

    def test_keeps_everything_when_all_blocks_score_confidently_relevant(self):
        text = _pair_text(4)

        def judge(recipe, state, questions, p):
            answers = {"error": {"noul": 0.0}}
            answers.update({k: {"noul": 0.95} for k in questions if k != "error"})
            return {"answers": answers}

        engine = SimpleNamespace(effective_policy=lambda p, recipe: {**p, "provider": "typesafe"}, judge=judge)
        result = sieve.reduce_text(engine, BASE_SIEVE_POLICY, "Please debug the router.", text)
        self.assertEqual(result, {"changed": False, "text": text,
                                   "reason": "nothing_confidently_irrelevant", "withheld_chars": 0})

    def test_rejects_a_hide_whose_marker_overhead_erases_the_savings(self):
        text = _pair_text(3)

        def judge(recipe, state, questions, p):
            answers = {"error": {"noul": 0.0}}
            answers.update({k: {"noul": 0.01} for k in questions if k != "error"})
            return {"answers": answers}

        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            engine = SimpleNamespace(effective_policy=lambda p, recipe: {**p, "provider": "typesafe"},
                                      judge=judge, store=store)
            policy = {**BASE_SIEVE_POLICY, "min_reduction": 0.99}
            result = sieve.reduce_text(engine, policy, "Please debug the router.", text)
        self.assertEqual(result, {"changed": False, "text": text,
                                   "reason": "below_net_reduction", "withheld_chars": 0})


# ---------------------------------------------------------------------------
# store.py -- Store lifecycle / constructor safety
# ---------------------------------------------------------------------------

class StoreLifecycleTests(unittest.TestCase):
    """Store is opened by every host process sharing this workspace's local
    state. The constructor is the one place that refuses to reuse a database
    file with unsafe permissions or an unexpected hard link -- on a shared
    machine, skipping this check would let one user's cache/budget data be
    read or corrupted by another.
    """

    def test_fresh_store_creates_a_private_directory_and_full_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "state"
            store = Store(root)
            self.assertTrue(store.path.exists())
            self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(store.path.stat().st_mode), 0o600)
            with store.connection() as c:
                tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertEqual(tables, {"budgets", "cache", "evidence", "goals", "events"})

    def test_a_preexisting_group_or_world_readable_database_file_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "state"
            root.mkdir(mode=0o700)
            db = root / "auto.sqlite3"
            db.write_bytes(b"")
            db.chmod(0o644)  # group/other readable: unsafe
            with self.assertRaises(AutoError):
                Store(root)

    def test_a_hardlinked_database_file_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "state"
            root.mkdir(mode=0o700)
            db = root / "auto.sqlite3"
            db.write_bytes(b"")
            db.chmod(0o600)
            os.link(db, root / "auto.sqlite3.extra-link")
            with self.assertRaises(AutoError):
                Store(root)


# ---------------------------------------------------------------------------
# store.py -- reserve() budgets
# ---------------------------------------------------------------------------

class StoreBudgetReserveTests(unittest.TestCase):
    """reserve() is the only spend control between a workspace and its daily
    call/byte cap. Every one of these is a way a cap could silently stop
    applying: a malformed byte count, a disabled/expired grant, a day that
    never rolls over, or (see StoreConcurrentBudgetTests) a lost update under
    concurrent writers.
    """

    def test_reserve_rejects_malformed_byte_counts(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            policy = make_budget_policy(max_request_bytes=100)
            for n in (-1, 101, 1.5, True, "10"):
                with self.subTest(n=n):
                    with self.assertRaises(AutoError):
                        store.reserve(policy, n)

    def test_reserve_rejects_disabled_or_expired_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            for label, overrides in {"disabled": {"enabled": False},
                                      "expired": {"expires_at": time.time() - 10}}.items():
                with self.subTest(label):
                    with self.assertRaises(AutoError):
                        store.reserve(make_budget_policy(**overrides), 10)

    def test_reserve_denies_when_daily_call_count_or_byte_total_would_be_exceeded(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            now = time.time()
            calls_policy = make_budget_policy(max_calls_per_day=1, max_bytes_per_day=1_000_000, policy_id="p-calls")
            store.reserve(calls_policy, 10, now=now)
            with self.assertRaises(AutoError):
                store.reserve(calls_policy, 10, now=now)

            bytes_policy = make_budget_policy(max_calls_per_day=1000, max_bytes_per_day=15, policy_id="p-bytes")
            store.reserve(bytes_policy, 10, now=now)
            with self.assertRaises(AutoError):
                store.reserve(bytes_policy, 10, now=now)  # 10 + 10 = 20 > 15

    def test_reserve_increments_calls_and_bytes_across_repeated_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            now = time.time()
            policy = make_budget_policy(max_calls_per_day=10, max_bytes_per_day=1000, policy_id="p-inc")
            store.reserve(policy, 10, now=now)
            store.reserve(policy, 15, now=now)
            with store.connection() as c:
                row = c.execute("SELECT calls, bytes FROM budgets WHERE policy=?", ("p-inc",)).fetchone()
            self.assertEqual(row, (2, 25))

    def test_reserve_starts_a_fresh_budget_row_when_the_utc_day_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            policy = make_budget_policy(max_calls_per_day=1, policy_id="p-day")
            day1 = 1_700_000_000.0
            day2 = day1 + 86_400
            store.reserve(policy, 5, now=day1)
            with self.assertRaises(AutoError):
                store.reserve(policy, 5, now=day1)  # exhausted for day1
            store.reserve(policy, 5, now=day2)  # new UTC day -> fresh row, must succeed
            with store.connection() as c:
                rows = c.execute("SELECT day, calls FROM budgets WHERE policy=? ORDER BY day", ("p-day",)).fetchall()
            self.assertEqual([r[1] for r in rows], [1, 1])
            self.assertEqual(len({r[0] for r in rows}), 2)


class StoreConcurrentBudgetTests(unittest.TestCase):
    """store.py's own docstring: 'Same-user local state only' -- but many host
    PROCESSES for that same user share one sqlite file. reserve() uses
    BEGIN IMMEDIATE specifically so that N processes racing the same
    day+policy row never lose an increment. If this regressed to a plain
    SELECT-then-UPDATE, two processes could both read calls=0 and both write
    calls=1, silently doubling the real spend against a cap meant to bound it.
    """

    def test_concurrent_reserves_from_two_store_instances_never_lose_an_increment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "shared"
            store_a = Store(root)
            store_b = Store(root)  # a second handle on the same on-disk database
            policy = make_budget_policy(max_calls_per_day=1000, max_bytes_per_day=1_000_000, policy_id="p-race")
            now = time.time()
            n_workers = 12

            def reserve_once(index):
                (store_a if index % 2 == 0 else store_b).reserve(policy, 10, now=now)

            with ThreadPoolExecutor(max_workers=n_workers) as pool:
                futures = [pool.submit(reserve_once, i) for i in range(n_workers)]
                for f in futures:
                    f.result(timeout=10)

            with store_a.connection() as c:
                rows = c.execute("SELECT calls, bytes FROM budgets WHERE policy=?", ("p-race",)).fetchall()
            self.assertEqual(rows, [(n_workers, 10 * n_workers)])


# ---------------------------------------------------------------------------
# store.py -- cache / evidence / goal / event / prune
# ---------------------------------------------------------------------------

class StoreCacheTests(unittest.TestCase):
    """cache()/cached() back the provider single-flight/response cache; a
    stale hit that is never expired would silently serve an old decision, and
    a wrongly-expired hit would silently double a paid provider call.
    """

    def test_cache_roundtrips_and_expires_by_ttl(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            store.cache("fresh", {"value": 42}, ttl=60)
            self.assertEqual(store.cached("fresh"), {"value": 42})
            store.cache("already-expired", {"value": 1}, ttl=-5)
            self.assertIsNone(store.cached("already-expired"))
            self.assertIsNone(store.cached("never-written"))


class StoreEvidenceTests(unittest.TestCase):
    """put()/get() are the content-addressed evidence store behind every
    sieve receipt_id. A host must be able to trust that get() either returns
    exactly what was put, or fails typed -- never returns silently-corrupted
    content.
    """

    def test_put_is_content_addressed_and_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            key1 = store.put({"a": 1})
            key2 = store.put({"a": 1})
            self.assertEqual(key1, key2)
            self.assertEqual(store.get(key1), {"a": 1})
            with store.connection() as c:
                count = c.execute("SELECT count(*) FROM evidence").fetchone()[0]
            self.assertEqual(count, 1)  # INSERT OR IGNORE: identical content stored once

    def test_get_rejects_malformed_or_unknown_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            for bad_id in ("short", "g" * 64, "A" * 64):
                with self.subTest(bad_id=bad_id):
                    with self.assertRaises(AutoError):
                        store.get(bad_id)
            with self.assertRaises(AutoError):
                store.get("0" * 64)  # well-formed hex, never stored

    def test_get_detects_tampered_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            key = store.put({"a": 1})
            with store.connection() as c:
                c.execute("UPDATE evidence SET body=? WHERE k=?", (canonical({"a": 999}), key))
            with self.assertRaises(AutoError):
                store.get(key)


class StoreGoalTests(unittest.TestCase):
    """set_goal()/goal() carry the session's working goal into sieve/prepare
    decisions; a goal that fails to truncate could blow the provider payload
    cap, and a goal that never expires could leak yesterday's task into
    today's judging.
    """

    def test_goal_roundtrips_and_truncates_to_4000_chars(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            long_text = "g" * 5000
            store.set_goal("session-1", long_text)
            got = store.goal("session-1")
            self.assertEqual(len(got), 4000)
            self.assertEqual(got, long_text[:4000])

    def test_goal_returns_empty_string_for_unknown_or_stale_session(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            self.assertEqual(store.goal("never-seen"), "")
            with store.connection() as c:
                c.execute("INSERT INTO goals VALUES (?,?,?)",
                          ("stale-session", time.time() - 90_000, "bye"))
            self.assertEqual(store.goal("stale-session"), "")


class StoreEventStatsPruneTests(unittest.TestCase):
    """event()/stats() feed the only honesty check a host gets on how much
    sieve withheld, and prune() is what keeps the four tables bounded. Each
    must touch only what it claims to: stats must not count non-sieve
    events, and prune must not delete data still inside the retention window.
    """

    def test_stats_aggregates_sieve_withheld_chars_and_ignores_other_kinds(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            store.event("sieve", {"withheld_chars": 100, "reason": "extractive_reduction"})
            store.event("sieve", {"withheld_chars": 50, "reason": "extractive_reduction"})
            store.event("provider_failure", {"withheld_chars": 999_999})  # must not be counted
            store.put({"anything": True})
            stats = store.stats()
            self.assertEqual(stats["proposed_tool_characters_withheld"], 150)
            self.assertEqual(stats["evidence_records"], 1)
            self.assertIsNone(stats["host_tokens_saved"])
            self.assertIsNone(stats["host_cost_saved"])

    def test_stats_budget_rows_report_the_correct_day_calls_and_bytes(self):
        """proposed_tool_characters_withheld is derived from the same
        stats() call as budget_rows, but nothing anywhere else in this suite
        asserts budget_rows' own key names or day/calls/bytes mapping -- a
        swapped field or renamed key here would previously have gone
        unnoticed despite 100% line coverage of the surrounding statement.
        """
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            policy = make_budget_policy(policy_id="p-stats", max_calls_per_day=10, max_bytes_per_day=1000)
            now = time.time()
            store.reserve(policy, 30, now=now)
            store.reserve(policy, 20, now=now)
            rows = store.stats()["budget_rows"]
            self.assertEqual(len(rows), 1)
            with store.connection() as c:
                (expected_day,) = c.execute("SELECT day FROM budgets WHERE policy=?", ("p-stats",)).fetchone()
            self.assertEqual(rows[0], {"utc_day": expected_day, "reserved_attempts": 2, "reserved_payload_bytes": 50})

    def test_prune_removes_only_data_older_than_the_retention_window(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            now = time.time()
            old_ts = now - 10 * 86_400

            store.cache("fresh-cache", "v", ttl=3600)
            store.cache("expired-cache", "v", ttl=-10)  # expired cache is pruned regardless of `days`
            fresh_evidence = store.put({"tag": "fresh"})
            old_evidence_key = digest({"tag": "old"})
            with store.connection() as c:
                c.execute("INSERT OR IGNORE INTO evidence VALUES (?,?,?)",
                          (old_evidence_key, old_ts, canonical({"tag": "old"})))
                c.execute("INSERT INTO events(created, kind, body) VALUES (?,?,?)",
                          (old_ts, "sieve", canonical({"withheld_chars": 2})))
                c.execute("INSERT INTO goals VALUES (?,?,?)", ("old-session", old_ts, "bye"))
            store.event("sieve", {"withheld_chars": 1})
            store.set_goal("fresh-session", "hi")

            store.prune(days=7)

            self.assertIsNone(store.cached("expired-cache"))
            self.assertEqual(store.cached("fresh-cache"), "v")
            self.assertEqual(store.get(fresh_evidence), {"tag": "fresh"})
            with self.assertRaises(AutoError):
                store.get(old_evidence_key)
            self.assertEqual(store.goal("fresh-session"), "hi")
            self.assertEqual(store.goal("old-session"), "")
            self.assertEqual(store.stats()["proposed_tool_characters_withheld"], 1)


# ---------------------------------------------------------------------------
# store.py -- SingleFlight
# ---------------------------------------------------------------------------

class SingleFlightTests(unittest.TestCase):
    """Multiple threads (standing in for multiple host requests) racing the
    same cache-miss key must trigger the underlying, possibly-paid
    computation exactly once, with every joiner receiving the same result.
    A regression here silently multiplies provider calls under load, and a
    key that is never cleared would wedge every future call for that key.
    """

    def test_runs_once_and_returns_the_value(self):
        flight = SingleFlight()
        self.assertEqual(flight.run("k", lambda: 42), 42)

    def test_key_is_cleared_after_completion_so_the_next_call_runs_fresh(self):
        flight = SingleFlight()
        calls = []
        self.assertEqual(flight.run("k", lambda: calls.append(1) or "a"), "a")
        self.assertEqual(flight.run("k", lambda: calls.append(2) or "b"), "b")
        self.assertEqual(calls, [1, 2])  # second call actually ran fn again

    def test_concurrent_callers_of_the_same_key_share_one_computation(self):
        flight = SingleFlight()
        started = threading.Event()
        release = threading.Event()
        lock = threading.Lock()
        call_count = 0

        def slow():
            nonlocal call_count
            with lock:
                call_count += 1
            started.set()
            release.wait(timeout=5)
            return "shared-result"

        results = []

        def worker():
            results.append(flight.run("shared-key", slow))

        threads = [threading.Thread(target=worker) for _ in range(5)]
        threads[0].start()
        self.assertTrue(started.wait(timeout=5))
        for t in threads[1:]:
            t.start()
        time.sleep(0.2)  # let joiners register behind the in-flight owner
        release.set()
        for t in threads:
            t.join(timeout=5)

        self.assertEqual(call_count, 1)
        self.assertEqual(results, ["shared-result"] * 5)

    def test_on_join_transforms_the_value_for_joiners_only(self):
        flight = SingleFlight()
        release = threading.Event()

        def slow():
            release.wait(timeout=5)
            return {"raw": 1}

        owner_result = []
        owner_thread = threading.Thread(target=lambda: owner_result.append(flight.run("k2", slow)))
        owner_thread.start()
        time.sleep(0.1)

        joined = []
        joiner_thread = threading.Thread(
            target=lambda: joined.append(flight.run("k2", slow, on_join=lambda v: {"transformed": v["raw"]})))
        joiner_thread.start()
        time.sleep(0.1)

        release.set()
        owner_thread.join(timeout=5)
        joiner_thread.join(timeout=5)
        self.assertEqual(owner_result, [{"raw": 1}])
        self.assertEqual(joined, [{"transformed": 1}])

    def test_owner_exception_propagates_to_joiners_and_the_key_is_still_cleared(self):
        flight = SingleFlight()
        release = threading.Event()

        def failing():
            release.wait(timeout=5)
            raise RuntimeError("boom")

        errors = []

        def owner_worker():
            try:
                flight.run("k3", failing)
            except RuntimeError as e:
                errors.append(("owner", str(e)))

        def joiner_worker():
            time.sleep(0.1)
            try:
                flight.run("k3", failing)
            except RuntimeError as e:
                errors.append(("joiner", str(e)))

        t1 = threading.Thread(target=owner_worker)
        t2 = threading.Thread(target=joiner_worker)
        t1.start()
        t2.start()
        time.sleep(0.2)
        release.set()
        t1.join(timeout=5)
        t2.join(timeout=5)

        self.assertEqual(len(errors), 2)
        self.assertTrue(all(msg == "boom" for _, msg in errors))
        # key cleared after the failure -> a fresh call for the same key runs again, not hangs
        self.assertEqual(flight.run("k3", lambda: "ok"), "ok")


# ---------------------------------------------------------------------------
# recipe_runtime.py -- _validate_catalog()
# ---------------------------------------------------------------------------

class RecipeCatalogValidationTests(unittest.TestCase):
    """_validate_catalog is the only gate between a hand-authored or
    generated recipe_catalog.json and every model-facing recipe in the
    product. Each case here is a shape that must never reach a real host.
    """

    def test_top_level_container_shape(self):
        cases = {"not a list": "nope", "empty": [],
                 "too many": [_valid_recipe(f"test.r{i}") for i in range(129)]}
        for label, recipes in cases.items():
            with self.subTest(label):
                with self.assertRaises(AutoError):
                    _validate_catalog(recipes)

    def test_each_recipe_must_be_a_dict_with_exactly_the_public_fields(self):
        missing_field = _valid_recipe()
        del missing_field["limitations"]
        extra_field = {**_valid_recipe(), "secret_sauce": True}
        cases = {"not a dict": "nope", "missing field": missing_field, "extra field": extra_field}
        for label, recipe in cases.items():
            with self.subTest(label):
                with self.assertRaises(AutoError):
                    _validate_catalog([recipe])

    def test_id_must_match_the_allowed_pattern(self):
        for bad_id in ("", "1starts-with-digit", "has space", "x" * 129):
            with self.subTest(bad_id=bad_id):
                with self.assertRaises(AutoError):
                    _validate_catalog([_valid_recipe(bad_id)])

    def test_field_shape_and_status_checks(self):
        cases = {
            "title too long": _valid_recipe(title="x" * 151),
            "empty title": _valid_recipe(title=""),
            "audience too long": _valid_recipe(audience="x" * 41),
            "wrong status": _valid_recipe(status="DRAFT"),
            "limitations not a string": _valid_recipe(limitations=123),
            "input_schema not a dict": _valid_recipe(input_schema="nope"),
            "questions not a dict": _valid_recipe(questions="nope"),
        }
        for label, recipe in cases.items():
            with self.subTest(label):
                with self.assertRaises(AutoError):
                    _validate_catalog([recipe])

    def test_duplicate_ids_are_rejected(self):
        with self.assertRaises(AutoError):
            _validate_catalog([_valid_recipe("test.dup"), _valid_recipe("test.dup")])

    def test_a_well_formed_catalog_is_returned_unchanged(self):
        recipes = [_valid_recipe("test.ok-one"), _valid_recipe("test.ok-two")]
        self.assertEqual(_validate_catalog(recipes), recipes)


# ---------------------------------------------------------------------------
# recipe_runtime.py -- catalog_document()
# ---------------------------------------------------------------------------

class RecipeCatalogDocumentTests(unittest.TestCase):
    """catalog_document() is the sole gate on the packaged recipe_catalog.json
    before its contents are trusted: an oversized file, a symlink swapped in
    after packaging, or truncated JSON must all fail typed and closed, not be
    silently accepted or crash the host with a raw exception. `__file__` is
    patched (not the real packaged file) so the shared repo's real catalog
    is never touched.
    """

    @staticmethod
    def _fake_runtime(tmp):
        fake_runtime = Path(tmp) / "runtime"
        (fake_runtime / "jev_auto").mkdir(parents=True)
        return fake_runtime, fake_runtime / "jev_auto" / "recipe_runtime.py"

    def test_missing_packaged_catalog_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, fake_file = self._fake_runtime(tmp)
            with patch.object(recipe_runtime, "__file__", str(fake_file)):
                self.assertIsNone(recipe_runtime.catalog_document())

    def test_symlinked_packaged_catalog_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_runtime, fake_file = self._fake_runtime(tmp)
            real = fake_runtime / "real.json"
            real.write_text(json.dumps({"schema_version": 1, "recipes": []}))
            (fake_runtime / "recipe_catalog.json").symlink_to(real)
            with patch.object(recipe_runtime, "__file__", str(fake_file)):
                with self.assertRaises(AutoError):
                    recipe_runtime.catalog_document()

    def test_oversized_packaged_catalog_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_runtime, fake_file = self._fake_runtime(tmp)
            (fake_runtime / "recipe_catalog.json").write_bytes(b"x" * 512_001)
            with patch.object(recipe_runtime, "__file__", str(fake_file)):
                with self.assertRaises(AutoError):
                    recipe_runtime.catalog_document()

    def test_malformed_json_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_runtime, fake_file = self._fake_runtime(tmp)
            (fake_runtime / "recipe_catalog.json").write_text("{not valid json")
            with patch.object(recipe_runtime, "__file__", str(fake_file)):
                with self.assertRaises(AutoError):
                    recipe_runtime.catalog_document()

    def test_wrong_shape_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_runtime, fake_file = self._fake_runtime(tmp)
            cases = {
                "not a dict": json.dumps([1, 2, 3]),
                "wrong schema_version": json.dumps({"schema_version": 2, "recipes": []}),
                "recipes not a list": json.dumps({"schema_version": 1, "recipes": {}}),
            }
            for label, payload in cases.items():
                with self.subTest(label):
                    (fake_runtime / "recipe_catalog.json").write_text(payload)
                    with patch.object(recipe_runtime, "__file__", str(fake_file)):
                        with self.assertRaises(AutoError):
                            recipe_runtime.catalog_document()

    def test_well_formed_packaged_catalog_is_returned(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_runtime, fake_file = self._fake_runtime(tmp)
            payload = {"schema_version": 1, "recipes": [], "gates": [], "fixtures": []}
            (fake_runtime / "recipe_catalog.json").write_text(json.dumps(payload))
            with patch.object(recipe_runtime, "__file__", str(fake_file)):
                self.assertEqual(recipe_runtime.catalog_document(), payload)


class CatalogMissingFailsTypedTests(unittest.TestCase):
    """FIXED DURING THIS SESSION -- keep this note, it explains why the test
    looks the way it does. _catalog() used to have a source-only fallback
    (`from src.adl.recipes.registry import load_registry`) for a workspace
    with no packaged recipe_catalog.json. That module never existed anywhere
    in this repository, so the fallback raised an unhandled
    ModuleNotFoundError instead of a typed error. This was reported as a
    defect (originally covered here by an `expectedFailure` test) and has
    since been fixed at the source: `_catalog()` now raises a typed
    `AutoError("RECIPE_CATALOG_MISSING")` instead of attempting a fallback
    that could never work. This test asserts that fixed, typed behaviour --
    it is a normal (non-xfail) test again.
    """

    def test_a_missing_packaged_catalog_fails_typed_not_with_a_raw_import_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_runtime = Path(tmp) / "runtime"
            (fake_runtime / "jev_auto").mkdir(parents=True)
            fake_file = fake_runtime / "jev_auto" / "recipe_runtime.py"
            with patch.object(recipe_runtime, "__file__", str(fake_file)):
                with self.assertRaises(AutoError) as ctx:
                    recipe_runtime._catalog()
        self.assertEqual(str(ctx.exception), "RECIPE_CATALOG_MISSING")


# ---------------------------------------------------------------------------
# recipe_runtime.py -- prepare_recipe()
# ---------------------------------------------------------------------------

class PrepareRecipeValidationTests(unittest.TestCase):
    """prepare_recipe() is the last typed check before a model ever sees an
    input. Each failure mode here is a shape the shipped 36-recipe catalog
    never exercises today, so a regression in the deeper schema/questions
    checks would ship silently.
    """

    def test_malformed_recipe_id_is_rejected_without_touching_the_catalog(self):
        for bad_id in ("", "1bad", "has space", 42):
            with self.subTest(bad_id=bad_id):
                with self.assertRaises(AutoError):
                    prepare_recipe(bad_id, {})

    def test_unknown_recipe_id_is_rejected(self):
        with self.assertRaises(AutoError):
            prepare_recipe("qualixar.does-not-exist", {})

    def test_invalid_input_schema_shape_is_rejected(self):
        broken = _valid_recipe("test.broken-schema", input_schema={"type": "array"})
        with patch("jev_auto.recipe_runtime._catalog", return_value=[broken]):
            with self.assertRaises(AutoError):
                prepare_recipe("test.broken-schema", {"note": "hi"})

    def test_non_dict_values_or_schema_key_mismatch_is_rejected(self):
        recipe = _valid_recipe("test.values-shape")
        with patch("jev_auto.recipe_runtime._catalog", return_value=[recipe]):
            cases = {"values not a dict": "nope", "wrong keys": {"other": "x"}}
            for label, values in cases.items():
                with self.subTest(label):
                    with self.assertRaises(AutoError):
                        prepare_recipe("test.values-shape", values)

    def test_field_level_validation_failures_are_rejected(self):
        recipe = _valid_recipe("test.field-shape")
        with patch("jev_auto.recipe_runtime._catalog", return_value=[recipe]):
            cases = {"not a string": {"note": 123}, "blank after strip": {"note": "   "},
                     "too long": {"note": "x" * 101}}
            for label, values in cases.items():
                with self.subTest(label):
                    with self.assertRaises(AutoError):
                        prepare_recipe("test.field-shape", values)

    def test_invalid_questions_shape_is_rejected(self):
        broken = _valid_recipe("test.broken-questions", questions={"decision": {}, "extra": {}})
        with patch("jev_auto.recipe_runtime._catalog", return_value=[broken]):
            with self.assertRaises(AutoError):
                prepare_recipe("test.broken-questions", {"note": "hi"})


# ---------------------------------------------------------------------------
# prepare.py -- candidates() git-tracked file discovery
# ---------------------------------------------------------------------------

class PrepareCandidatesGitDiscoveryTests(unittest.TestCase):
    """candidates() ranks git-TRACKED files by goal-term overlap so the packet
    can point at real repo files -- but must never surface secrets/lockfiles
    even when their names score high. Every other existing test builds a
    workspace explicitly WITHOUT a git repo, so this path has never actually
    run before this test.

    `jev_auto.prepare.__file__` is patched to a path under the tempdir for
    every test in this class. candidates() also scans
    `Path(__file__).resolve().parents[2] / 'skills'` -- the REAL plugin's own
    skills/ directory -- and without this patch these tests would silently
    depend on that directory's current, independently-evolving contents (it
    has 3 real skills at the time of writing) rather than only the synthetic
    workspace each test builds.
    """

    @staticmethod
    def _isolate_plugin_root(tmp):
        fake_prepare = Path(tmp) / "isolated-plugin-root" / "runtime" / "jev_auto" / "prepare.py"
        return patch.object(prepare_module, "__file__", str(fake_prepare))

    def test_git_tracked_files_are_ranked_and_secrets_or_lockfiles_are_excluded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "router.py").write_text("# router")
            (root / "router_test.py").write_text("# router test")
            (root / "router_credential_store.py").write_text("# holds a fake credential store")
            (root / "router.lock.json").write_text("{}")
            subprocess.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "-C", str(root), "add", "-A"], check=True, capture_output=True)
            with patch.object(Path, "home", return_value=root), self._isolate_plugin_root(directory):
                found = candidates(root, "Please review the router test implementation carefully.")
            ids = [item["id"] for item in found]
        self.assertIn("file:router.py", ids)
        self.assertIn("file:router_test.py", ids)
        self.assertNotIn("file:router_credential_store.py", ids)
        self.assertNotIn("file:router.lock.json", ids)
        self.assertEqual(ids[0], "file:router_test.py")  # scores 2 (router+test) vs router.py's 1

    def test_a_failing_git_invocation_is_swallowed_not_raised(self):
        """git may be absent, may time out, or may emit undecodable bytes on a
        broken checkout; none of that may crash prompt preparation -- it
        should just mean zero file: candidates, with skill/guidance discovery
        still attempted.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(Path, "home", return_value=root), self._isolate_plugin_root(directory), \
                 patch("jev_auto.prepare.subprocess.run", side_effect=OSError("git not found")):
                found = candidates(root, "Please review the router implementation carefully.")
            self.assertEqual(found, [])


class PrepareCandidatesSkillGuidanceExceptionTests(unittest.TestCase):
    """A skill/guidance file whose own text fails the sensitive-content screen
    must be silently skipped -- not crash prompt preparation, and not leak
    the guidance's content into the shortlist regardless.
    """

    def test_a_guidance_file_whose_own_text_fails_the_sensitive_screen_is_skipped(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            project.mkdir()
            skills = project / ".agents" / "skills"
            good = skills / "router-review"
            good.mkdir(parents=True)
            (good / "SKILL.md").write_text(
                "---\nname: router-review\ndescription: Review router implementation changes.\n---\n")
            bad = skills / "router-secrets"
            bad.mkdir(parents=True)
            (bad / "SKILL.md").write_text(
                "---\nname: router-secrets\ndescription: Review router implementation. "
                "api_key: AKIA" + "X" * 16 + "\n---\n")
            with patch.object(Path, "home", return_value=root), \
                 PrepareCandidatesGitDiscoveryTests._isolate_plugin_root(directory):
                found = candidates(project, "Please review the router implementation carefully.")
            ids = {item["id"] for item in found}
        self.assertIn("skill:router-review", ids)
        self.assertNotIn("skill:router-secrets", ids)


# ---------------------------------------------------------------------------
# prepare.py -- prepare() orchestration branches
# ---------------------------------------------------------------------------

class PrepareOrchestrationGuardrailTests(unittest.TestCase):
    """prepare() must not spend a candidate search, let alone a provider
    call, on a goal that is too short or off-topic, and must not fabricate a
    shortlist when zero candidates exist.
    """

    @staticmethod
    def _must_not_be_called(*_args, **_kwargs):
        raise AssertionError("engine.judge must not be called on this path")

    def test_short_or_off_topic_goal_skips_preparation_entirely(self):
        engine = SimpleNamespace(judge=self._must_not_be_called)
        policy = {"prepare_context": True}
        for goal in ("fix", "Please handle this ordinary conversational request without any special keyword here."):
            with self.subTest(goal=goal):
                result = prepare(engine, policy, goal)
                self.assertEqual(result, {"packet": "", "selected": [], "reason": "preparation_not_needed"})

    def test_prepare_context_disabled_skips_preparation(self):
        engine = SimpleNamespace(judge=self._must_not_be_called)
        policy = {"prepare_context": False}
        result = prepare(engine, policy, "Please fix and test the router implementation thoroughly today.")
        self.assertEqual(result["reason"], "preparation_not_needed")

    def test_no_matching_candidates_short_circuits_before_any_provider_call(self):
        engine = SimpleNamespace(workspace=Path("/synthetic"), judge=self._must_not_be_called)
        policy = {"prepare_context": True}
        goal = "Please fix and test the router implementation thoroughly today."
        with patch("jev_auto.prepare.candidates", return_value=[]):
            result = prepare(engine, policy, goal)
        self.assertEqual(result, {"packet": "", "selected": [], "reason": "no_candidates"})


class PrepareOrchestrationLocalLayaTests(unittest.TestCase):
    """When the workspace is routed to the on-device Laya provider, prepare()
    must score EVERY candidate itself (no jev/hosted shortcut) and only
    surface items that clear both the score and confidence bar.
    """

    def test_laya_mlx_provider_scores_every_candidate_and_filters_by_threshold(self):
        items = [
            {"id": "file:a.py", "kind": "file", "description": "a.py"},
            {"id": "file:b.py", "kind": "file", "description": "b.py"},
        ]

        def judge(recipe, state, questions, policy):
            return {"answers": {"c0": {"score": 2.0, "confidence": 0.9},
                                "c1": {"score": 1.0, "confidence": 0.9}},
                    "receipt_id": "b" * 64}

        engine = SimpleNamespace(workspace=Path("/synthetic"), judge=judge,
                                  effective_policy=lambda policy, recipe: {"provider": "laya-mlx"})
        policy = {"prepare_context": True, "auto_prepare_jev": True, "generic_query_enabled": True}
        goal = "Please fix and test the router implementation thoroughly today across every module."
        with patch("jev_auto.prepare.candidates", return_value=items):
            result = prepare(engine, policy, goal)
        self.assertEqual(result["selected"], ["file:a.py"])
        self.assertEqual(result["reason"], "candidate_selection")
        self.assertEqual(result["calibration_status"], "NOT_EVALUATED")
        self.assertIn("Local Laya shortlist", result["packet"])
        self.assertNotIn("file:b.py", result["packet"])


# ---------------------------------------------------------------------------
# recipe_fixtures.py -- validate_fixtures() direct
# ---------------------------------------------------------------------------

class RecipeFixturesValidationDirectTests(unittest.TestCase):
    """validate_fixtures is structural-only by design (its own docstring:
    'Never trusts the file to be well-formed'). Each case here pins one shape
    it must still catch before a fixture ever reaches run_fixture/selftest.
    """

    def test_top_level_container_shape(self):
        cases = {"not a list": "nope", "empty": [],
                 "too many": [_valid_entry(f"test.e{i}") for i in range(129)]}
        for label, entries in cases.items():
            with self.subTest(label):
                with self.assertRaises(AutoError):
                    validate_fixtures(entries)

    def test_entry_shape_and_duplicate_ids(self):
        cases = {
            "not a dict": "nope",
            "missing cases key": {"id": "test.x"},
            "extra key": {**_valid_entry("test.x"), "extra": 1},
        }
        for label, entry in cases.items():
            with self.subTest(label):
                with self.assertRaises(AutoError):
                    validate_fixtures([entry])
        with self.assertRaises(AutoError):
            validate_fixtures([_valid_entry("test.dup"), _valid_entry("test.dup")])

    def test_non_string_identifier_is_rejected(self):
        bad = _valid_entry()
        bad["id"] = 123
        with self.assertRaises(AutoError):
            validate_fixtures([bad])

    def test_state_and_mock_answer_must_be_dicts(self):
        for field in ("state", "mock_answer"):
            entry = _valid_entry("test.field-shape")
            entry["cases"][0] = _valid_case("nominal", **{field: "not-a-dict"})
            with self.subTest(field=field):
                with self.assertRaises(AutoError):
                    validate_fixtures([entry])

    def test_fixture_id_and_disclaimer_must_be_strings(self):
        for field in ("fixture_id", "disclaimer"):
            entry = _valid_entry("test.string-shape")
            entry["cases"][0] = _valid_case("nominal", **{field: 123})
            with self.subTest(field=field):
                with self.assertRaises(AutoError):
                    validate_fixtures([entry])

    def test_a_well_formed_entry_list_is_returned_unchanged(self):
        entries = [_valid_entry("test.ok")]
        self.assertEqual(validate_fixtures(entries), entries)


# ---------------------------------------------------------------------------
# recipe_fixtures.py -- _document() / _gate() guardrails
# ---------------------------------------------------------------------------

class RecipeFixturesDocumentGuardrailTests(unittest.TestCase):
    """_document() and _gate() are the last checks before a fixture replay
    trusts the packaged catalog's fixtures/gates. A workspace whose packaged
    catalog is absent, or whose fixtures/gates have drifted out of sync with
    each other, must fail typed rather than KeyError or silently mis-grade a
    fixture. `catalog_document` is patched (not the real packaged file) so
    the shared repo's real catalog is never touched.
    """

    def test_unavailable_fixtures_or_gates_is_rejected(self):
        cases = {
            "catalog missing entirely": None,
            "missing gates key": {"fixtures": []},
            "missing fixtures key": {"gates": []},
            "not a dict": ["nope"],
        }
        for label, fake_document in cases.items():
            with self.subTest(label):
                with patch("jev_auto.recipe_fixtures.catalog_document", return_value=fake_document):
                    with self.assertRaises(AutoError):
                        run_fixture("anything", "nominal")

    def test_a_fixture_with_no_matching_gate_is_refused_not_mis_evaluated(self):
        real_gate = next(g for g in CATALOG["gates"] if g["id"] == "qualixar.catalog-quality")
        orphan_entry = _valid_entry("test.orphan-no-gate")
        fake_document = {"fixtures": [orphan_entry], "gates": [real_gate]}
        with patch("jev_auto.recipe_fixtures.catalog_document", return_value=fake_document):
            with self.assertRaises(AutoError):
                run_fixture("test.orphan-no-gate", "nominal")


# ---------------------------------------------------------------------------
# recipe_fixtures.py -- selftest() partial-failure resilience
# ---------------------------------------------------------------------------

class SelftestPartialFailureResilienceTests(unittest.TestCase):
    """selftest()'s documented purpose: 'A single broken case also no longer
    destroys the proof.' A fixture whose gate has gone missing must be
    recorded as a failure and the replay must continue -- the healthy
    fixture riding alongside it must still be graded and must still pass.
    Reuses the REAL 'qualixar.catalog-quality' fixture+gate as the healthy
    control (already proven correct by test_recipe_fixtures.py) so this test
    is purely about the resilience behaviour, not about re-proving one more
    recipe's gate math.
    """

    def test_a_broken_sibling_fixture_does_not_take_down_the_healthy_one(self):
        healthy_id = "qualixar.catalog-quality"
        healthy_fixture = next(e for e in CATALOG["fixtures"] if e["id"] == healthy_id)
        healthy_gate = next(g for g in CATALOG["gates"] if g["id"] == healthy_id)
        orphan_entry = _valid_entry("test.synthetic-orphan-fixture")
        fake_document = {"fixtures": [healthy_fixture, orphan_entry], "gates": [healthy_gate]}

        with patch("jev_auto.recipe_fixtures.catalog_document", return_value=fake_document):
            result = selftest()

        self.assertEqual(result["cases"], 6)
        self.assertEqual(result["recipes"], 2)
        self.assertFalse(result["all_passed"])
        self.assertEqual(result["passed"], 3)
        self.assertEqual(len(result["failures"]), 3)
        for failure in result["failures"]:
            self.assertEqual(failure["recipe_id"], "test.synthetic-orphan-fixture")
            self.assertFalse(failure["passed"])
            self.assertIn("RECIPE_NOT_FOUND", failure["error"])
        self.assertNotIn(healthy_id, {f["recipe_id"] for f in result["failures"]})


# ---------------------------------------------------------------------------
# routing.py -- compile_route()
# ---------------------------------------------------------------------------

class RoutingCompileRouteValidationTests(unittest.TestCase):
    """compile_route's own input checks are the last line of defense before a
    provider ever sees a routing question; a hole here lets malformed
    kind/task/candidate data reach the judge layer instead of failing fast
    and typed.
    """

    @staticmethod
    def _two_candidates():
        """A fresh list every call -- compile_route must not mutate its input,
        but a shared mutable class attribute would mask it silently if it did.
        """
        return [{"id": "a", "description": "A"}, {"id": "b", "description": "B"}]

    def test_invalid_kind_or_task_is_rejected(self):
        cases = {
            "unknown kind": ("not-a-kind", "Check something", self._two_candidates()),
            "empty task": ("tool", "", self._two_candidates()),
            "task too long": ("tool", "x" * 4001, self._two_candidates()),
            "non-string task": ("tool", 12345, self._two_candidates()),
        }
        for label, (kind, task, candidates_) in cases.items():
            with self.subTest(label):
                with self.assertRaises(AutoError):
                    compile_route(kind, task, candidates_)

    def test_invalid_candidates_container_is_rejected(self):
        cases = {
            "not a list": "nope",
            "too few": [{"id": "a", "description": "A"}],
            "too many": [{"id": f"c{i}", "description": "D"} for i in range(13)],
        }
        for label, candidates_ in cases.items():
            with self.subTest(label):
                with self.assertRaises(AutoError):
                    compile_route("tool", "Choose one", candidates_)

    def test_malformed_candidate_shape_is_rejected(self):
        cases = {
            "not a dict": ["not-a-dict", {"id": "b", "description": "B"}],
            "extra key": [{"id": "a", "description": "A", "extra": 1}, {"id": "b", "description": "B"}],
            "missing description": [{"id": "a"}, {"id": "b", "description": "B"}],
        }
        for label, candidates_ in cases.items():
            with self.subTest(label):
                with self.assertRaises(AutoError):
                    compile_route("tool", "Choose one", candidates_)

    def test_reserved_unknown_id_cannot_be_claimed_by_a_candidate(self):
        with self.assertRaises(AutoError):
            compile_route("tool", "Choose one", [
                {"id": "unknown", "description": "Should not be allowed"},
                {"id": "b", "description": "B"},
            ])


if __name__ == "__main__":
    unittest.main()

    def test_hides_low_relevance_blocks_but_keeps_a_constrained_one_and_persists_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            lines = [_line(i) for i in range(8)]
            lines[4] = "This particular block must be kept regardless of its relevance score."
            text = "\n".join(lines)
            blocks = bounded_blocks(text, 2, BASE_SIEVE_POLICY["max_blocks"])
            b002, b003 = blocks[1:-1]
            self.assertIn("must be kept", b003.text)  # sanity: the constraint word lands in b003

            def judge(recipe, state, questions, p):
                answers = {"error": {"noul": 0.0}}
                answers.update({k: {"noul": 0.02} for k in questions if k != "error"})
                return {"answers": answers}

            engine = SimpleNamespace(effective_policy=lambda p, recipe: {**p, "provider": "typesafe"},
                                      judge=judge, store=store)
            policy = {**BASE_SIEVE_POLICY, "min_reduction": 0.01}
            result = sieve.reduce_text(engine, policy, "Investigate the router.", text)

            self.assertTrue(result["changed"])
            self.assertEqual(result["reason"], "extractive_reduction")
            self.assertIn("must be kept", result["text"])       # CONSTRAINTS-protected despite low score
            self.assertNotIn(b002.text, result["text"])         # low score, no constraint -> hidden
            self.assertIn(f"jev_recall receipt_id={result['receipt_id']}", result["text"])

            stored = store.get(result["receipt_id"])
            self.assertEqual(stored["text"], text)
            self.assertEqual(store.stats()["proposed_tool_characters_withheld"], result["withheld_chars"])
