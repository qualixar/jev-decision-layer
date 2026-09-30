"""Gate evaluation: a typed answer must arrive with a local policy verdict.

These tests check gate mechanics; they do not establish provider accuracy,
calibration, or measured token and money savings.
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
from jev_auto.recipe_gate import ACT, IGNORE, VERIFY, evaluate, validate_gates  # noqa: E402

CHOICE = {
    "kind": "choice",
    "min_confidence": 0.55,
    "min_selected_probability": 0.8,
    "positive_outcome": "route_to_queue",
    "negative_outcome": "request_review",
    "unknown_choice": "unknown",
}
NOUL = {"kind": "noul", "yes": 0.8, "no": 0.2,
        "positive_outcome": "mark_check_passed", "negative_outcome": "request_review"}
SCORE = {"kind": "score", "min_confidence": 0.55, "min_score": 1.5,
         "min_selected_probability": 0.8,
         "positive_outcome": "route_to_queue", "negative_outcome": "request_review"}


def choice(label, confidence, probability):
    return {"type": "choice", "choice": label, "confidence": confidence,
            "probabilities": {label: probability, "other": round(1 - probability, 6)}}


class ChoiceGate(unittest.TestCase):
    def test_clearing_both_bars_tells_the_host_to_act(self):
        out = evaluate(CHOICE, choice("billing", 0.91, 0.93))
        self.assertEqual(out["status"], "RECOMMEND")
        self.assertEqual(out["host_action"], ACT)
        self.assertEqual(out["recommendation"], "route_to_queue")

    def test_reported_confidence_below_policy_floor_does_not_pass(self):
        """Check the configured floor without claiming independent calibration."""
        out = evaluate(CHOICE, choice("technical", 0.45, 0.85))
        self.assertEqual(out["status"], "REVIEW")
        self.assertEqual(out["host_action"], VERIFY)
        self.assertIn("configured floor", " ".join(out["reasons"]))

    def test_confident_but_split_distribution_does_not_pass(self):
        out = evaluate(CHOICE, choice("billing", 0.95, 0.55))
        self.assertEqual(out["status"], "REVIEW")
        self.assertIn("0.55", " ".join(out["reasons"]))

    def test_unknown_selection_tells_the_host_to_stop_asking(self):
        out = evaluate(CHOICE, choice("unknown", 0.99, 0.99))
        self.assertEqual(out["host_action"], IGNORE)

    def test_missing_confidence_is_never_treated_as_confident(self):
        answer = {"type": "choice", "choice": "billing",
                  "probabilities": {"billing": 0.99, "other": 0.01}}
        out = evaluate(CHOICE, answer)
        self.assertEqual(out["host_action"], VERIFY)
        self.assertIn("No usable reported confidence", " ".join(out["reasons"]))

    def test_missing_alternatives_or_probability_mass_cannot_clear(self):
        for probabilities in ({"billing": 0.9}, {"billing": 0.9, "other": 0.0},
                              {"billing": 0.9, "other": 0.13}):
            with self.subTest(probabilities=probabilities):
                out = evaluate(CHOICE, {"choice": "billing", "confidence": 0.95,
                                        "probabilities": probabilities})
                self.assertEqual(out["host_action"], VERIFY)
                self.assertIn("distribution", " ".join(out["reasons"]))

    def test_small_rounding_error_is_accepted(self):
        out = evaluate(CHOICE, {"choice": "billing", "confidence": 0.95,
                                "probabilities": {"billing": 0.9, "other": 0.095}})
        self.assertEqual(out["host_action"], ACT)

    def test_choice_and_score_apply_the_same_configured_confidence_floor(self):
        choice_out = evaluate(CHOICE, choice("billing", 0.72, 0.9))
        score_out = evaluate(SCORE, {"score": 2.0, "confidence": 0.72,
                                     "probabilities": {"0": 0.0, "1": 0.1, "2": 0.9}})
        self.assertEqual(choice_out["host_action"], ACT)
        self.assertEqual(score_out["host_action"], ACT)
        self.assertFalse(choice_out["thresholds_calibrated"])
        self.assertFalse(score_out["thresholds_calibrated"])

    def test_nan_confidence_cannot_slip_through(self):
        out = evaluate(CHOICE, choice("billing", float("nan"), 0.99))
        self.assertNotEqual(out["host_action"], ACT)

    def test_the_chosen_label_travels_with_the_verdict(self):
        """`route_to_queue` alone does not say which queue.

        Every non-unknown label of a choice recipe clears to the same outcome,
        so a verdict without the label left the host to dig it out of the raw
        answer, and a gate that reported the wrong label went unnoticed.
        """
        self.assertEqual(evaluate(CHOICE, choice("billing", 0.91, 0.93))["selected_label"], "billing")
        self.assertEqual(evaluate(CHOICE, choice("technical", 0.45, 0.85))["selected_label"], "technical")
        self.assertEqual(evaluate(CHOICE, choice("unknown", 0.99, 0.99))["selected_label"], "unknown")
        for label in (None, 3, ""):
            with self.subTest(label=label):
                self.assertIsNone(evaluate(CHOICE, {"choice": label, "confidence": 0.9,
                                                    "probabilities": {"a": 0.9, "b": 0.1}})["selected_label"])

    def test_noul_and_score_verdicts_carry_no_label(self):
        self.assertIsNone(evaluate(NOUL, {"type": "noul", "noul": 0.95})["selected_label"])
        self.assertIsNone(evaluate(SCORE, {"type": "score", "score": 2.0, "confidence": 0.99,
                                           "probabilities": {"0": 0.0, "1": 0.0, "2": 1.0}})["selected_label"])
        self.assertIsNone(evaluate(None, None)["selected_label"])

    def test_confidence_exactly_at_the_floor_clears(self):
        """The floor is inclusive: `>=` passes, and only `<` refuses."""
        out = evaluate(CHOICE, choice("billing", 0.55, 0.93))
        self.assertEqual(out["host_action"], ACT)
        score = evaluate(SCORE, {"type": "score", "score": 2.0, "confidence": 0.55,
                                 "probabilities": {"0": 0.0, "1": 0.0, "2": 1.0}})
        self.assertEqual(score["host_action"], ACT)

    def test_a_noul_exactly_on_a_band_edge_is_a_decision(self):
        self.assertEqual(evaluate(NOUL, {"type": "noul", "noul": 0.8})["recommendation"], "mark_check_passed")
        self.assertEqual(evaluate(NOUL, {"type": "noul", "noul": 0.2})["recommendation"], "request_review")

    def test_a_single_option_distribution_is_never_a_choice(self):
        """One label with all the mass sums to one and ranks first, so only
        the option-count check refuses it."""
        out = evaluate(CHOICE, {"type": "choice", "choice": "billing", "confidence": 1.0,
                                "probabilities": {"billing": 1.0}})
        self.assertNotEqual(out["host_action"], ACT)
        self.assertIn("distribution", " ".join(out["reasons"]))

    def test_thresholds_are_reported_so_the_host_need_not_guess(self):
        out = evaluate(CHOICE, choice("billing", 0.91, 0.93))
        self.assertEqual(out["thresholds_applied"],
                         {"min_confidence": 0.55, "min_selected_probability": 0.8})
        self.assertFalse(out["thresholds_calibrated"])
        self.assertFalse(out["execution_authorized"])


class NoulGate(unittest.TestCase):
    def test_noul_needs_no_confidence_field(self):
        out = evaluate(NOUL, {"type": "noul", "noul": 0.95})
        self.assertEqual(out["host_action"], ACT)
        self.assertEqual(out["recommendation"], "mark_check_passed")
        self.assertAlmostEqual(out["confidence"], 0.9)  # derived, not returned

    def test_confident_no_is_also_actionable(self):
        out = evaluate(NOUL, {"type": "noul", "noul": 0.05})
        self.assertEqual(out["host_action"], ACT)
        self.assertEqual(out["recommendation"], "request_review")

    def test_the_middle_band_is_not_a_decision(self):
        out = evaluate(NOUL, {"type": "noul", "noul": 0.5})
        self.assertEqual(out["host_action"], VERIFY)
        self.assertAlmostEqual(out["confidence"], 0.0)


class ScoreGate(unittest.TestCase):
    def test_score_requires_confidence_too(self):
        out = evaluate(SCORE, {"type": "score", "score": 2.0,
                               "probabilities": {"0": 0.0, "1": 0.0, "2": 1.0}})
        self.assertEqual(out["host_action"], VERIFY)

    def test_low_confidence_score_does_not_pass(self):
        out = evaluate(SCORE, {"type": "score", "score": 2.0, "confidence": 0.40,
                               "probabilities": {"0": 0.0, "1": 0.0, "2": 1.0}})
        self.assertEqual(out["host_action"], VERIFY)

    def test_positive_score_requires_probability_mass_on_passing_levels(self):
        out = evaluate(SCORE, {"type": "score", "score": 1.5, "confidence": 0.99,
                               "probabilities": {"0": 0.25, "1": 0.0, "2": 0.75}})
        self.assertEqual(out["host_action"], VERIFY)
        self.assertEqual(out["recommendation"], None)
        self.assertAlmostEqual(out["outcome_probability"], 0.75)
        self.assertIn("below 0.8", " ".join(out["reasons"]))

    def test_positive_score_at_probability_bar_is_actionable(self):
        out = evaluate(SCORE, {"type": "score", "score": 1.6, "confidence": 0.99,
                               "probabilities": {"0": 0.2, "1": 0.0, "2": 0.8}})
        self.assertEqual(out["host_action"], ACT)
        self.assertEqual(out["recommendation"], "route_to_queue")
        self.assertAlmostEqual(out["outcome_probability"], 0.8)

    def test_negative_score_requires_probability_mass_on_failing_levels(self):
        out = evaluate(SCORE, {"type": "score", "score": 0.4, "confidence": 0.99,
                               "probabilities": {"0": 0.75, "1": 0.0, "2": 0.25}})
        self.assertEqual(out["host_action"], VERIFY)
        self.assertEqual(out["recommendation"], None)
        self.assertAlmostEqual(out["outcome_probability"], 0.75)

    def test_negative_score_at_probability_bar_is_actionable(self):
        out = evaluate(SCORE, {"type": "score", "score": 0.4, "confidence": 0.99,
                               "probabilities": {"0": 0.8, "1": 0.0, "2": 0.2}})
        self.assertEqual(out["host_action"], ACT)
        self.assertEqual(out["recommendation"], "request_review")
        self.assertAlmostEqual(out["outcome_probability"], 0.8)

    def test_malformed_score_probability_distributions_fail_closed(self):
        malformed = [
            None,
            {},
            {"0": 0.1, "1": 0.1, "2": 0.1},
            {"0": 1.1, "1": 0.0, "2": 0.0},
            {"0": 0.2, "2": 0.8},
            {"low": 0.2, "1": 0.0, "2": 0.8},
        ]
        for probabilities in malformed:
            with self.subTest(probabilities=probabilities):
                out = evaluate(SCORE, {"type": "score", "score": 2.0, "confidence": 0.99,
                                       "probabilities": probabilities})
                self.assertEqual(out["host_action"], VERIFY)

    def test_score_gate_requires_the_probability_threshold(self):
        invalid = {key: value for key, value in SCORE.items() if key != "min_selected_probability"}
        with self.assertRaises(AutoError):
            validate_gates([{"id": "x", "policy": invalid}])


class InputContract(unittest.TestCase):
    """A recipe's declared input limits are enforced, not decorative."""

    def test_an_input_one_character_over_its_declared_limit_is_refused(self):
        from jev_auto.recipe_runtime import prepare_recipe

        catalog = json.loads((RUNTIME / "recipe_catalog.json").read_text())
        recipe = next(r for r in catalog["recipes"] if r["id"] == "qualixar.patch-review")
        properties = recipe["input_schema"]["properties"]
        values = {name: "x" * rule["maxLength"] for name, rule in properties.items()}
        self.assertEqual(prepare_recipe(recipe["id"], values)["recipe_id"], recipe["id"])
        first = next(iter(properties))
        values[first] += "x"
        with self.assertRaisesRegex(AutoError, "RECIPE_INPUT_INVALID"):
            prepare_recipe(recipe["id"], values)

    def test_no_recipe_can_accept_more_than_the_hosted_query_takes(self):
        """The provider step refuses any string over 8,000 characters.

        A recipe declaring 12,000 once passed local validation for text the
        provider then refused, so the runtime caps at the provider's limit
        whatever a catalog declares.
        """
        from unittest.mock import patch

        from jev_auto import recipe_runtime

        catalog = json.loads((RUNTIME / "recipe_catalog.json").read_text())
        recipe = json.loads(json.dumps(next(r for r in catalog["recipes"] if r["id"] == "qualixar.patch-review")))
        for rule in recipe["input_schema"]["properties"].values():
            rule["maxLength"] = 12_000
        names = list(recipe["input_schema"]["properties"])
        with patch.object(recipe_runtime, "_catalog", return_value=[recipe]):
            accepted = recipe_runtime.prepare_recipe(recipe["id"], {name: "x" * 8_000 for name in names})
            self.assertEqual(accepted["recipe_id"], recipe["id"])
            with self.assertRaisesRegex(AutoError, "RECIPE_INPUT_INVALID"):
                recipe_runtime.prepare_recipe(recipe["id"], {name: "x" * 8_001 for name in names})


class GateCatalog(unittest.TestCase):
    def test_every_shipped_recipe_has_a_loadable_gate(self):
        catalog = json.loads((RUNTIME / "recipe_catalog.json").read_text())
        gates = validate_gates(catalog["gates"])
        self.assertEqual(len(gates), len(catalog["recipes"]))
        self.assertEqual({g["id"] for g in gates}, {r["id"] for r in catalog["recipes"]})

    def test_gates_are_not_shipped_inside_the_model_facing_recipes(self):
        catalog = json.loads((RUNTIME / "recipe_catalog.json").read_text())
        for recipe in catalog["recipes"]:
            self.assertNotIn("policy", recipe, "a threshold is not evidence for the model")

    def test_confidence_floor_only_where_a_confidence_field_exists(self):
        catalog = json.loads((RUNTIME / "recipe_catalog.json").read_text())
        for gate in catalog["gates"]:
            policy = gate["policy"]
            if policy["kind"] == "noul":
                self.assertNotIn("min_confidence", policy,
                                 f"{gate['id']}: a Noul answer has no confidence field")
            else:
                self.assertIn("min_confidence", policy, gate["id"])


if __name__ == "__main__":
    unittest.main()
