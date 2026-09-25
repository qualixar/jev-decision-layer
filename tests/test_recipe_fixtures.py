"""Offline fixtures: the layer must be provable without spending a token.

A host deciding whether to adopt this layer should not have to pay a provider
call, or host-model context, to find out whether the gate holds. These tests
assert the shipped fixtures actually exercise that gate rather than decorate it.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "plugins" / "qualixar-jev-decision-layer" / "runtime"
sys.path.insert(0, str(RUNTIME))

from jev_auto.common import AutoError  # noqa: E402
from jev_auto.recipe_fixtures import VARIANTS, run_fixture, selftest, validate_fixtures  # noqa: E402
from jev_auto.recipe_gate import ACT, evaluate  # noqa: E402

CATALOG = json.loads((RUNTIME / "recipe_catalog.json").read_text())


class ShippedFixtures(unittest.TestCase):
    def test_every_recipe_ships_all_three_variants(self):
        self.assertEqual(len(CATALOG["fixtures"]), len(CATALOG["recipes"]))
        self.assertEqual({entry["id"] for entry in CATALOG["fixtures"]},
                         {recipe["id"] for recipe in CATALOG["recipes"]})
        for entry in CATALOG["fixtures"]:
            self.assertEqual([case["variant"] for case in entry["cases"]], list(VARIANTS), entry["id"])

    def test_the_whole_suite_replays_clean(self):
        result = selftest()
        self.assertTrue(result["all_passed"], result["failures"])
        self.assertEqual(result["cases"], 96)
        self.assertEqual(result["passed"], 96)

    def test_fixtures_are_not_shipped_inside_the_model_facing_recipes(self):
        for recipe in CATALOG["recipes"]:
            self.assertNotIn("fixtures", recipe, "a worked example would steer the answer it checks")

    def test_every_case_is_labelled_synthetic(self):
        for entry in CATALOG["fixtures"]:
            for case in entry["cases"]:
                self.assertEqual(case["data_classification"], "synthetic", case["fixture_id"])
                self.assertIn("Not a Jev prediction", case["disclaimer"])


class VariantsMeanSomething(unittest.TestCase):
    """A variant label that does not constrain the verdict is decoration."""

    def test_a_clear_cut_input_is_cleared_so_the_host_acts_once(self):
        for entry in CATALOG["fixtures"]:
            with self.subTest(entry["id"]):
                self.assertEqual(run_fixture(entry["id"], "nominal")["observed"]["host_action"], ACT)

    def test_an_ambiguous_input_is_never_cleared(self):
        for entry in CATALOG["fixtures"]:
            with self.subTest(entry["id"]):
                self.assertNotEqual(run_fixture(entry["id"], "uncertain")["observed"]["host_action"], ACT)

    def test_an_embedded_instruction_never_buys_authorisation(self):
        for entry in CATALOG["fixtures"]:
            with self.subTest(entry["id"]):
                observed = run_fixture(entry["id"], "adversarial")["observed"]
                self.assertNotEqual(observed["host_action"], ACT)
                self.assertEqual(observed["status"], "REVIEW")


class CleanCostsTheHostNothing(unittest.TestCase):
    """The soul, stated as a test.

    Five recipes used to answer `request_review` in BOTH directions: a document
    with no drift, a listing with no conflict and copy that already matched the
    style guide all sent the host to a review it did not need. The cheap model
    had settled the question and the host paid anyway.
    """

    def test_no_recipe_returns_the_same_outcome_in_both_directions(self):
        for gate in CATALOG["gates"]:
            policy = gate["policy"]
            with self.subTest(gate["id"]):
                self.assertNotEqual(policy["positive_outcome"], policy["negative_outcome"],
                                    "a gate that cannot distinguish clean from dirty is not a gate")

    def test_a_confident_clean_noul_reports_a_passed_check_not_a_review(self):
        for gate in CATALOG["gates"]:
            policy = gate["policy"]
            if policy["kind"] != "noul" or policy["positive_outcome"] != "request_review":
                continue
            with self.subTest(gate["id"]):
                result = evaluate(policy, {"type": "noul", "noul": 0.03})
                self.assertEqual(result["host_action"], ACT)
                self.assertEqual(result["recommendation"], "mark_check_passed")

    def test_copy_that_already_matches_the_brief_is_not_sent_for_review(self):
        for gate in CATALOG["gates"]:
            policy = gate["policy"]
            if policy["kind"] != "score" or policy["negative_outcome"] != "request_review":
                continue
            if policy["positive_outcome"] not in {"mark_check_passed", "select_candidates"}:
                continue
            with self.subTest(gate["id"]):
                result = evaluate(policy, {"type": "score", "score": 3.0, "confidence": 0.84})
                self.assertEqual(result["host_action"], ACT)
                self.assertNotEqual(result["recommendation"], "request_review")


class Validation(unittest.TestCase):
    def test_a_missing_variant_is_rejected(self):
        entry = json.loads(json.dumps(CATALOG["fixtures"][0]))
        entry["cases"] = entry["cases"][:2]
        with self.assertRaises(AutoError):
            validate_fixtures([entry])

    def test_a_case_claiming_to_be_real_data_is_rejected(self):
        entry = json.loads(json.dumps(CATALOG["fixtures"][0]))
        entry["cases"][0]["data_classification"] = "customer"
        with self.assertRaises(AutoError):
            validate_fixtures([entry])

    def test_an_unknown_variant_name_is_refused(self):
        with self.assertRaises(AutoError):
            run_fixture(CATALOG["fixtures"][0]["id"], "optimistic")

    def test_an_unknown_recipe_is_refused(self):
        with self.assertRaises(AutoError):
            run_fixture("qualixar.not-a-recipe", "nominal")


if __name__ == "__main__":
    unittest.main()
