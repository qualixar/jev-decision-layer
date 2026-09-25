"""Gate evaluation: a typed answer must arrive at the host pre-gated.

The economy this protects: a Jev token is ~free, a host-model token is not.
Every case here is one where the host would otherwise have to re-derive
trust in its own expensive context.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "plugins" / "qualixar-jev-decision-layer" / "runtime"
sys.path.insert(0, str(RUNTIME))

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
         "positive_outcome": "route_to_queue", "negative_outcome": "request_review"}


def choice(label, confidence, probability):
    return {"type": "choice", "choice": label, "confidence": confidence,
            "probabilities": {label: probability}}


class ChoiceGate(unittest.TestCase):
    def test_clearing_both_bars_tells_the_host_to_act(self):
        out = evaluate(CHOICE, choice("billing", 0.91, 0.93))
        self.assertEqual(out["status"], "RECOMMEND")
        self.assertEqual(out["host_action"], ACT)
        self.assertEqual(out["recommendation"], "route_to_queue")

    def test_decisive_distribution_with_poor_confidence_does_not_pass(self):
        """The measured jev-1.13 case: p=0.85 but confidence=0.45.

        Probability alone would authorise this. Confidence is the signal that
        says the answer is not trustworthy, and acting on it costs the host
        far more than the call saved.
        """
        out = evaluate(CHOICE, choice("technical", 0.45, 0.85))
        self.assertEqual(out["status"], "REVIEW")
        self.assertEqual(out["host_action"], VERIFY)
        self.assertIn("not calibrated", " ".join(out["reasons"]))

    def test_confident_but_split_distribution_does_not_pass(self):
        out = evaluate(CHOICE, choice("billing", 0.95, 0.55))
        self.assertEqual(out["status"], "REVIEW")
        self.assertIn("0.55", " ".join(out["reasons"]))

    def test_unknown_selection_tells_the_host_to_stop_asking(self):
        out = evaluate(CHOICE, choice("unknown", 0.99, 0.99))
        self.assertEqual(out["host_action"], IGNORE)

    def test_missing_confidence_is_never_treated_as_confident(self):
        answer = {"type": "choice", "choice": "billing", "probabilities": {"billing": 0.99}}
        out = evaluate(CHOICE, answer)
        self.assertEqual(out["host_action"], VERIFY)
        self.assertIn("probability alone is not a gate", " ".join(out["reasons"]))

    def test_nan_confidence_cannot_slip_through(self):
        out = evaluate(CHOICE, choice("billing", float("nan"), 0.99))
        self.assertNotEqual(out["host_action"], ACT)

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
        out = evaluate(SCORE, {"type": "score", "score": 2.0})
        self.assertEqual(out["host_action"], VERIFY)

    def test_low_confidence_score_does_not_pass(self):
        out = evaluate(SCORE, {"type": "score", "score": 2.0, "confidence": 0.40})
        self.assertEqual(out["host_action"], VERIFY)


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
