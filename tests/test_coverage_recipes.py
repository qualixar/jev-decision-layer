"""Coverage-floor tests for the recipe gate/runtime/fixtures/calibration modules.

WHY THIS FILE EXISTS
---------------------
`jev_auto/recipe_gate.py` (91%), `jev_auto/recipe_runtime.py` (96%),
`jev_auto/recipe_fixtures.py` (98%), and `jev_auto/provider_calibration.py`
(95%) all have their remaining gaps in fail-closed validation code: a
malformed gate catalog, an answer whose selected label is not the most
probable option, an ACT recommendation the policy forgot to name, an
installation whose packaged catalog is missing or self-inconsistent. These
are exactly the paths a host must trust NOT to silently pass a bad answer, so
each test here asserts the precise `AutoError` code or return value the
function's docstring promises, not just "it didn't crash".

No test touches the real packaged `recipe_catalog.json`. `recipe_runtime`
tests redirect its own `Path(__file__).resolve().parents[1] /
"recipe_catalog.json"` lookup at a temporary directory via a small stand-in
for `pathlib.Path`, patched only as `jev_auto.recipe_runtime.Path` (never the
real `pathlib.Path` class, and never global state). `recipe_fixtures` tests
patch `jev_auto.recipe_fixtures.catalog_document` and
`jev_auto.recipe_fixtures.prepare_recipe` directly -- the exact names that
module imported into its own namespace -- so a crafted, self-inconsistent
catalog document can be fed to `run_fixture()` without depending on (or
being broken by future edits to) the real shipped catalog.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "plugins" / "qualixar-jev-decision-layer" / "runtime"
sys.path.insert(0, str(RUNTIME))

from jev_auto.common import AutoError  # noqa: E402


# --------------------------------------------------------------------------
# recipe_gate.validate_gates() -- catalog-load-time structural checks
# --------------------------------------------------------------------------

def _valid_gate(identifier="sample", **overrides):
    policy = {"kind": "noul", "yes": 0.9, "no": 0.1,
              "positive_outcome": "ok", "negative_outcome": "no", **overrides}
    return {"id": identifier, "policy": policy}


class ValidateGatesStructuralTests(unittest.TestCase):
    """Every branch here is fail-closed: a gate catalog that cannot be
    enforced must refuse to load rather than let a decision fall through
    ungated later. Each sub-case is independent (single-field mutation from
    an otherwise-valid gate list), so a passing test proves that specific
    field is actually checked.
    """

    def test_rejects_a_non_list_or_out_of_range_gate_count(self):
        from jev_auto.recipe_gate import validate_gates

        for bad in (None, "not-a-list", [], [_valid_gate(f"g{i}") for i in range(129)]):
            with self.subTest(bad=type(bad).__name__ if not isinstance(bad, list) else len(bad)):
                with self.assertRaisesRegex(AutoError, "RECIPE_GATES_INVALID"):
                    validate_gates(bad)

    def test_rejects_a_gate_entry_that_is_not_a_dict_with_exactly_id_and_policy(self):
        from jev_auto.recipe_gate import validate_gates

        for bad_entry in ("not-a-dict", {"id": "sample"}, {**_valid_gate(), "extra": 1}):
            with self.subTest(bad_entry=bad_entry):
                with self.assertRaisesRegex(AutoError, "RECIPE_GATES_INVALID"):
                    validate_gates([bad_entry] if not isinstance(bad_entry, str) else [bad_entry])

    def test_rejects_a_non_string_or_duplicate_gate_id(self):
        from jev_auto.recipe_gate import validate_gates

        with self.subTest("non-string id"):
            with self.assertRaisesRegex(AutoError, "RECIPE_GATES_INVALID"):
                validate_gates([_valid_gate(123)])
        with self.subTest("duplicate id"):
            with self.assertRaisesRegex(AutoError, "RECIPE_GATES_INVALID"):
                validate_gates([_valid_gate("dup"), _valid_gate("dup")])

    def test_rejects_a_policy_with_an_unknown_or_missing_kind(self):
        from jev_auto.recipe_gate import validate_gates

        with self.assertRaisesRegex(AutoError, "RECIPE_GATES_INVALID"):
            validate_gates([_valid_gate(kind="unknown-kind")])
        with self.assertRaisesRegex(AutoError, "RECIPE_GATES_INVALID"):
            validate_gates([{"id": "sample", "policy": "not-a-dict"}])

    def test_rejects_a_policy_missing_a_positive_or_negative_outcome_label(self):
        from jev_auto.recipe_gate import validate_gates

        with self.subTest("missing positive_outcome"):
            gate = _valid_gate()
            del gate["policy"]["positive_outcome"]
            with self.assertRaisesRegex(AutoError, "RECIPE_GATES_INVALID"):
                validate_gates([gate])
        with self.subTest("empty negative_outcome"):
            with self.assertRaisesRegex(AutoError, "RECIPE_GATES_INVALID"):
                validate_gates([_valid_gate(negative_outcome="")])

    def test_a_fully_valid_gate_list_round_trips_unchanged(self):
        # Positive control: proves the rejections above are about the
        # specific broken field, not about validate_gates() rejecting
        # everything.
        from jev_auto.recipe_gate import validate_gates

        gates = [_valid_gate()]
        self.assertEqual(validate_gates(gates), gates)


# --------------------------------------------------------------------------
# recipe_gate._probability_of() -- self-contradiction and malformed-label checks
# --------------------------------------------------------------------------

class ProbabilityOfTests(unittest.TestCase):
    """`_probability_of` is the one place a self-contradictory or malformed
    choice answer is caught before a gate can act on it. Each sub-case
    returns None for a documented reason; a positive control shows the same
    function returns the actual probability for a coherent answer, so a test
    that always returned None (e.g. from an accidental TypeError elsewhere)
    would not pass silently.
    """

    def test_an_unhashable_selected_label_is_treated_as_unusable(self):
        from jev_auto.recipe_gate import _probability_of

        answer = {"choice": ["not", "hashable"], "probabilities": {"a": 0.5, "b": 0.5}}
        self.assertIsNone(_probability_of(answer, answer["choice"]))

    def test_a_non_unit_probability_value_makes_the_whole_distribution_unusable(self):
        from jev_auto.recipe_gate import _probability_of

        answer = {"choice": "a", "probabilities": {"a": "not-a-number", "b": 0.5}}
        self.assertIsNone(_probability_of(answer, "a"))

    def test_a_label_with_lower_mass_than_another_option_is_self_contradictory(self):
        from jev_auto.recipe_gate import _probability_of

        answer = {"choice": "b", "probabilities": {"a": 0.7, "b": 0.3}}
        self.assertIsNone(_probability_of(answer, "b"))

    def test_a_label_absent_from_the_distribution_is_unusable(self):
        from jev_auto.recipe_gate import _probability_of

        answer = {"choice": "c", "probabilities": {"a": 0.5, "b": 0.5}}
        self.assertIsNone(_probability_of(answer, "c"))

    def test_positive_control_a_coherent_top_ranked_label_returns_its_mass(self):
        from jev_auto.recipe_gate import _probability_of

        answer = {"choice": "a", "probabilities": {"a": 0.7, "b": 0.3}}
        self.assertEqual(_probability_of(answer, "a"), 0.7)


# --------------------------------------------------------------------------
# recipe_gate.evaluate() -- the ACT-needs-a-recommendation cap, and the
# non-dict-answer / unknown-kind fail-closed branches
# --------------------------------------------------------------------------

class EvaluateCapAndFailClosedTests(unittest.TestCase):
    def test_an_act_recommendation_the_policy_did_not_name_is_capped_to_verify(self):
        from jev_auto.recipe_gate import ACT, VERIFY, evaluate

        # A noul policy that clears the "yes" band but never declared what
        # to recommend: `evaluate` must not let ACT through with no target.
        policy = {"kind": "noul", "yes": 0.8, "no": 0.2}
        result = evaluate(policy, {"noul": 0.95})
        self.assertEqual(result["status"], "REVIEW")
        self.assertEqual(result["host_action"], VERIFY)
        self.assertIsNone(result["recommendation"])
        self.assertIn("no outcome to act on", result["reasons"][-1])
        self.assertNotEqual(result["host_action"], ACT)

    def test_a_non_dict_answer_is_reported_as_review_not_a_crash(self):
        from jev_auto.recipe_gate import VERIFY, evaluate

        for bad_answer in (None, "not-a-dict", 42):
            with self.subTest(bad_answer=bad_answer):
                result = evaluate({"kind": "noul", "yes": 0.8, "no": 0.2,
                                   "positive_outcome": "ok", "negative_outcome": "no"}, bad_answer)
                self.assertEqual(result["status"], "REVIEW")
                self.assertEqual(result["host_action"], VERIFY)

    def test_an_unrecognised_policy_kind_fails_closed_to_review(self):
        from jev_auto.recipe_gate import VERIFY, evaluate

        result = evaluate({"kind": "some-future-kind"}, {"anything": True})
        self.assertEqual(result["status"], "REVIEW")
        self.assertEqual(result["host_action"], VERIFY)
        self.assertIn("some-future-kind", result["reasons"][-1])

    def test_a_score_kind_with_an_unusable_score_value_fails_closed(self):
        from jev_auto.recipe_gate import VERIFY, evaluate

        policy = {"kind": "score", "min_confidence": 0.5, "min_score": 1,
                  "min_selected_probability": 0.5,
                  "positive_outcome": "ok", "negative_outcome": "no"}
        answer = {"confidence": 0.9, "score": "not-a-number",
                  "probabilities": {"0": 0.5, "1": 0.5}}
        result = evaluate(policy, answer)
        self.assertEqual(result["status"], "REVIEW")
        self.assertEqual(result["host_action"], VERIFY)
        self.assertEqual(result["reasons"][-1], "Score was unusable.")

    def test_positive_control_a_clean_score_answer_clears_the_gate(self):
        # Proves the previous test failed the gate on the score value, not on
        # something else in the policy/answer shape.
        from jev_auto.recipe_gate import ACT, evaluate

        policy = {"kind": "score", "min_confidence": 0.5, "min_score": 1,
                  "min_selected_probability": 0.5,
                  "positive_outcome": "ok", "negative_outcome": "no"}
        answer = {"confidence": 0.9, "score": 1, "probabilities": {"0": 0.1, "1": 0.9}}
        result = evaluate(policy, answer)
        self.assertEqual(result["host_action"], ACT)
        self.assertEqual(result["recommendation"], "ok")


# --------------------------------------------------------------------------
# recipe_runtime: redirecting the packaged-catalog lookup to a temp file
# --------------------------------------------------------------------------

class _RedirectPath:
    """Stand-in for the `Path(__file__).resolve().parents[1] / "..."` chain.

    Only the three operations `catalog_document()` performs on it are
    implemented: `.resolve()`, `.parents[N]`, and `/`. Patched in as
    `jev_auto.recipe_runtime.Path` for the life of one `with` block so the
    module's own file-lookup logic runs unmodified against a directory this
    test controls, instead of the real installed `recipe_catalog.json`.
    """

    def __init__(self, target_dir):
        self._target_dir = target_dir

    def resolve(self):
        return self

    def __getitem__(self, _index):
        return self

    @property
    def parents(self):
        return self

    def __truediv__(self, other):
        return Path(self._target_dir) / other


def _redirected_path(tmp_dir):
    return lambda _arg: _RedirectPath(tmp_dir)


class RecipeRuntimeCatalogDocumentTests(unittest.TestCase):
    def test_a_recipe_entry_without_a_string_id_makes_the_whole_catalog_invalid(self):
        from jev_auto import recipe_runtime

        with tempfile.TemporaryDirectory() as tmp_dir:
            (Path(tmp_dir) / "recipe_catalog.json").write_text(json.dumps(
                {"schema_version": 1, "recipes": [{"id": 123}], "gates": []}))
            with patch.object(recipe_runtime, "Path", new=_redirected_path(tmp_dir)):
                with self.assertRaisesRegex(AutoError, "RECIPE_CATALOG_INVALID"):
                    recipe_runtime.catalog_document()

    def test_gate_policy_reports_catalog_missing_when_no_packaged_file_exists(self):
        from jev_auto import recipe_runtime

        with tempfile.TemporaryDirectory() as tmp_dir:  # deliberately empty: no recipe_catalog.json
            with patch.object(recipe_runtime, "Path", new=_redirected_path(tmp_dir)):
                self.assertIsNone(recipe_runtime.catalog_document())
                with self.assertRaisesRegex(AutoError, "RECIPE_CATALOG_MISSING"):
                    recipe_runtime.gate_policy("anything")

    def test_gate_policy_reports_gates_invalid_when_the_recipe_id_has_no_gate(self):
        from jev_auto import recipe_runtime

        with tempfile.TemporaryDirectory() as tmp_dir:
            (Path(tmp_dir) / "recipe_catalog.json").write_text(json.dumps(
                {"schema_version": 1, "recipes": [{"id": "sample"}],
                 "gates": [_valid_gate("sample")]}))
            with patch.object(recipe_runtime, "Path", new=_redirected_path(tmp_dir)):
                # Positive control: the catalog itself loads fine ...
                document = recipe_runtime.catalog_document()
                self.assertEqual(document["recipes"][0]["id"], "sample")
                # ... but a recipe id no gate names must not be silently ungated.
                with self.assertRaisesRegex(AutoError, "RECIPE_GATES_INVALID"):
                    recipe_runtime.gate_policy("no-such-recipe")


# --------------------------------------------------------------------------
# recipe_fixtures.run_fixture() -- a fixture/gate entry whose recipe entry is
# missing from the catalog's own `recipes` list
# --------------------------------------------------------------------------

def _fixture_case(variant):
    return {
        "fixture_id": f"sample-{variant}", "variant": variant, "data_classification": "synthetic",
        "state": {}, "mock_answer": {"noul": 0.5}, "expected_status": "REVIEW",
        "expected_host_action": "verify", "expected_recommendation": None,
        "disclaimer": "synthetic fixture built for a coverage test, not a real recipe",
    }


class RecipeFixturesMissingRecipeEntryTests(unittest.TestCase):
    """`run_fixture()` requires the recipe id to exist as its own entry in
    `data["recipes"]`, distinct from (and checked after) both the gate and
    fixture lookups. `catalog_document()` and `prepare_recipe` are patched on
    `recipe_fixtures`'s own namespace (the names it imported), so this test
    exercises `recipe_fixtures`'s bookkeeping without depending on -- or
    validating -- the real, separately-tested `recipe_runtime.catalog_document`.
    """

    def test_a_fixture_and_gate_with_no_matching_recipe_entry_is_reported_as_recipe_not_found(self):
        from jev_auto import recipe_fixtures

        data = {
            "recipes": [],  # deliberately missing "sample"
            "gates": [_valid_gate("sample")],
            "fixtures": [{"id": "sample", "cases": [_fixture_case(v) for v in recipe_fixtures.VARIANTS]}],
        }
        with patch.object(recipe_fixtures, "catalog_document", return_value=data), \
             patch.object(recipe_fixtures, "prepare_recipe", return_value=None):
            with self.assertRaisesRegex(AutoError, "RECIPE_NOT_FOUND"):
                recipe_fixtures.run_fixture("sample", "nominal")

    def test_positive_control_the_same_fixture_with_a_matching_recipe_entry_runs_to_completion(self):
        from jev_auto import recipe_fixtures

        data = {
            "recipes": [{"id": "sample", "status": "SPECIFICATION_NOT_MODEL_EVALUATED"}],
            "gates": [_valid_gate("sample")],
            "fixtures": [{"id": "sample", "cases": [_fixture_case(v) for v in recipe_fixtures.VARIANTS]}],
        }
        with patch.object(recipe_fixtures, "catalog_document", return_value=data), \
             patch.object(recipe_fixtures, "prepare_recipe", return_value=None):
            outcome = recipe_fixtures.run_fixture("sample", "nominal")
        self.assertEqual(outcome["recipe_id"], "sample")
        self.assertIn("observed", outcome)


# --------------------------------------------------------------------------
# provider_calibration.describe()
# --------------------------------------------------------------------------

class ProviderCalibrationDescribeTests(unittest.TestCase):
    def test_describe_lists_the_default_and_every_named_profile_as_unvalidated(self):
        from jev_auto.provider_calibration import DEFAULT, PROFILES, UNVALIDATED, describe

        entries = describe()
        self.assertEqual(entries[0], {
            "provider": "(default)", "min_confidence": None,
            "calibration_status": UNVALIDATED, "sample_size": 0, "note": DEFAULT.note,
        })
        self.assertEqual({entry["provider"] for entry in entries}, {"(default)", *PROFILES})
        for entry in entries:
            with self.subTest(provider=entry["provider"]):
                self.assertEqual(entry["calibration_status"], UNVALIDATED)


if __name__ == "__main__":
    unittest.main()
