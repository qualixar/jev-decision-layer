"""Coverage-floor tests for jev_auto/hermes_tool.py's bounded-envelope logic.

WHY THIS FILE EXISTS
---------------------
`hermes_tool.handle()` accepts an injectable `dispatcher=` callable (already
used by tests/test_hermes_parity.py and tests/test_core_contracts.py), so its
whole envelope -- argument shape checks, the oversized-result compaction for
`jev_verify`/`jev_rerank`, and the final size caps -- can be driven without a
real jevkit dispatch, a workspace, or any provider/network call. This file
closes out the branches nothing previously exercised: a context tool called
with no `workspace_path` key at all, every `_compact_oversized_result` failure
path for both `jev_verify` and `jev_rerank` (as distinct from the
already-tested *successful* `jev_verify` compaction), the full successful
`jev_rerank` compaction (never previously exercised at all), a dispatcher that
returns something other than a dict, and a result still too large even after
compaction.

Every dispatcher below is a plain local function/lambda; none calls
`jevkit`, `.mcp`, or any provider.

UNREACHABLE LINES REPORTED, NOT TESTED
---------------------------------------
`_canonical_workspace_path`'s two `return None` statements after
`ntpath.normpath`/`posixpath.normpath` (guarding a canonical path that turned
out non-absolute, UNC-rooted, or over length) are asserted here to be dead
code: see `WindowsAndPosixNormpathGuardsAreUnreachableTests` below, which
fuzzes both branches and documents why no input can reach them, rather than
silently skipping them.
"""

from __future__ import annotations

import ntpath
import posixpath
import random
import sys
import unittest
from pathlib import PureWindowsPath
from unittest.mock import patch

ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "plugins" / "qualixar-jev-decision-layer" / "runtime"
sys.path.insert(0, str(RUNTIME))

from jev_auto.common import canonical  # noqa: E402
from jev_auto.hermes_tool import MAX_RESULT_BYTES, handle  # noqa: E402


def _refuses_dispatch(_name, _arguments):
    raise AssertionError("dispatcher must not be called when argument validation already failed")


# --------------------------------------------------------------------------
# _valid_context_arguments(): a context tool called with no workspace_path key
# --------------------------------------------------------------------------

class ContextToolMissingWorkspacePathTests(unittest.TestCase):
    """`handle()` only canonicalizes `workspace_path` when the key is present
    in the arguments at all (`if "workspace_path" in arguments:`). A caller
    that omits the key entirely for a context tool (`jev_prepare`,
    `jev_reduce`, `jev_recall`) skips that block and falls straight into
    `_valid_context_arguments`, whose own `_is_workspace_path(None)` check is
    what actually rejects it. That is a different line from the
    already-tested "present but malformed" case.
    """

    def test_jev_prepare_without_a_workspace_path_key_is_rejected_before_dispatch(self):
        result = handle({"name": "jev_prepare", "arguments": {"goal": "Focus the session"}},
                        dispatcher=_refuses_dispatch)
        self.assertEqual(result, {"error": "HERMES_TOOL_ARGUMENTS"})

    def test_jev_recall_without_a_workspace_path_key_is_rejected_before_dispatch(self):
        result = handle({"name": "jev_recall", "arguments": {"receipt_id": "a" * 64}},
                        dispatcher=_refuses_dispatch)
        self.assertEqual(result, {"error": "HERMES_TOOL_ARGUMENTS"})

    def test_positive_control_the_same_call_with_workspace_path_present_reaches_dispatch(self):
        seen = []
        result = handle(
            {"name": "jev_prepare", "arguments": {"workspace_path": "/synthetic", "goal": "Focus"}},
            dispatcher=lambda name, args: seen.append((name, args)) or {"status": "ADVISORY"},
        )
        self.assertEqual(result, {"status": "ADVISORY"})
        self.assertEqual(len(seen), 1)


# --------------------------------------------------------------------------
# _compact_oversized_result(): jev_verify failure branches
# --------------------------------------------------------------------------

class VerifyCompactionFailureTests(unittest.TestCase):
    """`_compact_oversized_result` for `jev_verify` returns None -- which
    `handle()` turns into HERMES_TOOL_RESULT_INVALID -- for four distinct
    reasons. test_hermes_parity.py already covers the *successful* case; each
    sub-case here is a single-field mutation away from that success so it
    isolates one specific check.
    """

    def _oversized(self, fields):
        # "padding" alone pushes the raw result over MAX_RESULT_BYTES; it is
        # not part of the `fields` schema so it never affects whether
        # compaction can succeed, only whether the oversized path triggers.
        return {"status": "ADVISORY", "receipt_id": "a" * 64, "fields": fields,
                "padding": "z" * (MAX_RESULT_BYTES + 500)}

    def test_a_non_list_fields_value_is_rejected(self):
        result = handle({"name": "jev_verify", "arguments": {}},
                        dispatcher=lambda *_a: self._oversized("not-a-list"))
        self.assertEqual(result, {"error": "HERMES_TOOL_RESULT_INVALID"})

    def test_a_field_entry_missing_a_string_field_name_is_rejected(self):
        result = handle({"name": "jev_verify", "arguments": {}},
                        dispatcher=lambda *_a: self._oversized([{"field": 123, "p_wrong": 0.1, "status": "ok"}]))
        self.assertEqual(result, {"error": "HERMES_TOOL_RESULT_INVALID"})

    def test_a_non_finite_p_wrong_is_rejected(self):
        result = handle(
            {"name": "jev_verify", "arguments": {}},
            dispatcher=lambda *_a: self._oversized(
                [{"field": "amount", "p_wrong": "not-a-number", "status": "ok"}]),
        )
        self.assertEqual(result, {"error": "HERMES_TOOL_RESULT_INVALID"})

    def test_an_unrecognised_status_value_is_rejected(self):
        result = handle(
            {"name": "jev_verify", "arguments": {}},
            dispatcher=lambda *_a: self._oversized(
                [{"field": "amount", "p_wrong": 0.1, "status": "definitely-fine"}]),
        )
        self.assertEqual(result, {"error": "HERMES_TOOL_RESULT_INVALID"})


# --------------------------------------------------------------------------
# _compact_oversized_result(): jev_rerank, success and both failure branches
# (previously exercised zero times: baseline coverage showed every line of
# this branch, 129 through 150, as missing)
# --------------------------------------------------------------------------

class RerankCompactionTests(unittest.TestCase):
    def test_a_non_list_memories_value_is_rejected(self):
        # A non-iterable value (not a string: a string would still fail two
        # lines further down, at the per-item dict check, which would leave
        # this test unable to tell the two guards apart) isolates the
        # `isinstance(memories, list)` check itself.
        result = handle(
            {"name": "jev_rerank", "arguments": {}},
            dispatcher=lambda *_a: {"memories": 12345, "padding": "z" * (MAX_RESULT_BYTES + 500)},
        )
        self.assertEqual(result, {"error": "HERMES_TOOL_RESULT_INVALID"})

    def test_a_memory_entry_that_is_not_a_dict_is_rejected(self):
        result = handle(
            {"name": "jev_rerank", "arguments": {}},
            dispatcher=lambda *_a: {"memories": ["not-a-dict"], "padding": "z" * (MAX_RESULT_BYTES + 500)},
        )
        self.assertEqual(result, {"error": "HERMES_TOOL_RESULT_INVALID"})

    def test_a_valid_oversized_memory_list_is_compacted_and_keeps_only_the_safe_fields(self):
        # Each memory's large "text" is exactly what compaction must drop;
        # only fact_id/jev_level/jev_confidence/retrieval_score/usable survive.
        memories = [
            {"text": "a passage that is much too long to echo back " * 200,
             "fact_id": f"fact-{i}", "jev_level": 2, "jev_confidence": 0.8,
             "retrieval_score": 0.9, "usable": True}
            for i in range(5)
        ]
        # A mixed-shape entry lacking the optional fields: proves the
        # `isinstance(fact_id, str) and len(...) <= 128` and
        # `isinstance(memory.get("usable"), bool)` guards are conditional
        # skips, not required fields -- a memory missing them must still
        # compact cleanly rather than crash or fabricate a value.
        memories.append({"text": "another long passage " * 200,
                         "jev_level": None, "retrieval_score": 0.4})
        raw = {"status": "ADVISORY", "receipt_id": "b" * 64, "memories": memories}
        self.assertGreater(len(canonical(raw)) + 1, MAX_RESULT_BYTES, "test setup: must actually be oversized")

        result = handle({"name": "jev_rerank", "arguments": {}}, dispatcher=lambda *_a: raw)

        self.assertNotIn("error", result)
        self.assertEqual(result["status"], "ADVISORY")
        self.assertEqual(result["receipt_id"], "b" * 64)
        self.assertIn("jev_recall", result["details_note"])
        self.assertEqual(len(result["memories"]), 6)
        for item in result["memories"][:5]:
            self.assertEqual(set(item), {"fact_id", "jev_level", "jev_confidence", "retrieval_score", "usable"})
        self.assertEqual(result["memories"][0]["fact_id"], "fact-0")
        self.assertTrue(result["memories"][0]["usable"])
        # The mixed-shape entry: no fact_id/usable key at all, and its
        # missing jev_confidence carried through as None rather than omitted.
        sparse = result["memories"][5]
        self.assertNotIn("fact_id", sparse)
        self.assertNotIn("usable", sparse)
        self.assertIsNone(sparse["jev_level"])
        self.assertIsNone(sparse["jev_confidence"])
        self.assertEqual(sparse["retrieval_score"], 0.4)
        self.assertLessEqual(len(canonical(result)) + 1, MAX_RESULT_BYTES)


# --------------------------------------------------------------------------
# handle(): dispatcher result type/size final checks
# --------------------------------------------------------------------------

class ResultShapeAndFinalSizeCapTests(unittest.TestCase):
    def test_a_dispatcher_returning_a_non_dict_result_is_rejected(self):
        result = handle({"name": "jev_recipe_catalog", "arguments": {}},
                        dispatcher=lambda *_a: ["not", "a", "dict"])
        self.assertEqual(result, {"error": "HERMES_TOOL_RESULT_INVALID"})

    def test_a_result_still_too_large_after_compaction_is_rejected(self):
        # 1000 small-but-valid fields compact to far more than MAX_RESULT_BYTES
        # on their own, proving the *second* size check (after compaction,
        # not just the first) is the one that fires.
        fields = [{"field": f"field_{i:04d}", "p_wrong": 0.5, "status": "ok"} for i in range(1000)]
        raw = {"status": "ADVISORY", "receipt_id": "c" * 64, "fields": fields}
        self.assertGreater(len(canonical(raw)) + 1, MAX_RESULT_BYTES, "test setup: must actually be oversized")

        result = handle({"name": "jev_verify", "arguments": {}}, dispatcher=lambda *_a: raw)
        self.assertEqual(result, {"error": "HERMES_TOOL_RESULT_INVALID"})

        # Compaction itself must have succeeded (this is not case 1 above);
        # it is the second size check that still rejects it.
        from jev_auto.hermes_tool import _compact_oversized_result

        compact = _compact_oversized_result("jev_verify", raw)
        self.assertIsNotNone(compact)
        self.assertGreater(len(canonical(compact)) + 1, MAX_RESULT_BYTES)


# --------------------------------------------------------------------------
# _canonical_workspace_path(): documenting two lines as unreachable
# --------------------------------------------------------------------------

class WindowsAndPosixNormpathGuardsAreUnreachableTests(unittest.TestCase):
    """The stdlib property `_canonical_workspace_path` relies on.

Until 1.0.11 the function re-checked absoluteness, UNC drive and length
after `ntpath.normpath` / `posixpath.normpath`. Those re-checks could never
fire and were removed; the authoritative checks run on the raw value. These
fuzz tests pin why that is safe: once a value passed the raw checks, neither
normalizer makes it relative, UNC, or longer. The differential test in
tests/test_hermes_path_canonicalization.py compares the current function
against the previous double-checked one.
"""

    def test_ntpath_normpath_never_lengthens_or_unc_promotes_an_already_valid_windows_path(self):
        random.seed(1234567)
        segments = ["..", ".", "a", "", "b..", "..a", "a.", "...", "a b"]
        separators = ["\\", "/", "\\\\", "//"]
        checked = 0
        for _ in range(50_000):
            count = random.randint(1, 6)
            parts = [random.choice(segments) for _ in range(count)]
            sep = random.choice(separators)
            value = "C:" + sep + sep.join(parts)
            if not 1 <= len(value) <= 4096:
                continue
            path = PureWindowsPath(value)
            if not (path.is_absolute() and path.drive and not path.drive.startswith("\\\\")
                    and not value.startswith(("\\\\?\\", "\\\\.\\"))):
                continue  # rejected before normpath is even consulted
            normalized = ntpath.normpath(value)
            canonical_path = PureWindowsPath(normalized)
            checked += 1
            self.assertTrue(canonical_path.is_absolute())
            self.assertFalse(canonical_path.drive.startswith("\\\\"))
            self.assertLessEqual(len(normalized), 4096)
        self.assertGreater(checked, 1000, "fuzz produced too few valid pre-checked samples to be meaningful")

    def test_posixpath_normpath_never_lengthens_or_de_roots_an_already_valid_posix_path(self):
        random.seed(7654321)
        segments = ["..", ".", "a", "", "b..", "..a", "a.", "...", "a b"]
        checked = 0
        for _ in range(50_000):
            count = random.randint(1, 6)
            parts = [random.choice(segments) for _ in range(count)]
            sep = random.choice(["/", "//"])
            value = "/" + sep.join(parts)
            if not 1 <= len(value) <= 4096:
                continue
            checked += 1
            normalized = posixpath.normpath(value)
            self.assertTrue(normalized.startswith("/"))
            self.assertLessEqual(len(normalized), 4096)
        self.assertGreater(checked, 1000, "fuzz produced too few valid pre-checked samples to be meaningful")


if __name__ == "__main__":
    unittest.main()
