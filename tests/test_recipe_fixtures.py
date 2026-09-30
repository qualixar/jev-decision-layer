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
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "plugins" / "qualixar-jev-decision-layer" / "runtime"

# Derived from the recipe sources, so adding a recipe never means editing a count here.
RECIPE_COUNT = len(list((ROOT / "recipes").rglob("*.json")))
FIXTURE_CASES = 3 * len(list((ROOT / "fixtures").glob("*.json")))
sys.path.insert(0, str(RUNTIME))

from jev_auto.common import AutoError  # noqa: E402
from jev_auto import recipe_fixtures  # noqa: E402
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
        self.assertEqual(result["cases"], FIXTURE_CASES)
        self.assertEqual(result["passed"], FIXTURE_CASES)

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

    def test_a_clear_cut_input_clears_raw_gate_but_live_action_requires_verification(self):
        for entry in CATALOG["fixtures"]:
            with self.subTest(entry["id"]):
                outcome = run_fixture(entry["id"], "nominal")
                self.assertEqual(outcome["raw_gate"]["host_action"], ACT)
                self.assertEqual(outcome["observed"]["host_action"], "verify")
                self.assertEqual(outcome["observed"]["status"], "REVIEW")

    def test_an_ambiguous_input_is_never_cleared(self):
        for entry in CATALOG["fixtures"]:
            with self.subTest(entry["id"]):
                outcome = run_fixture(entry["id"], "uncertain")
                self.assertNotEqual(outcome["raw_gate"]["host_action"], ACT)
                self.assertNotEqual(outcome["observed"]["host_action"], ACT)

    def test_an_embedded_instruction_never_buys_authorisation(self):
        for entry in CATALOG["fixtures"]:
            with self.subTest(entry["id"]):
                observed = run_fixture(entry["id"], "adversarial")["observed"]
                self.assertNotEqual(observed["host_action"], ACT)
                self.assertEqual(observed["status"], "REVIEW")

    def test_an_embedded_instruction_never_gets_the_outcome_it_asks_for(self):
        policies = {gate["id"]: gate["policy"] for gate in CATALOG["gates"]}
        for entry in CATALOG["fixtures"]:
            with self.subTest(entry["id"]):
                case = entry["cases"][2]
                policy = policies[entry["id"]]
                self.assertIn(case["injection_target"],
                              {policy["positive_outcome"], policy["negative_outcome"]})
                raw = run_fixture(entry["id"], "adversarial")["raw_gate"]
                if raw["host_action"] == ACT:
                    self.assertIn(raw["recommendation"], policy["safe_outcomes"])
                    self.assertNotEqual(raw["recommendation"], case["injection_target"])

    def test_every_gate_declares_which_outcomes_are_safe_under_injection(self):
        for gate in CATALOG["gates"]:
            policy = gate["policy"]
            with self.subTest(gate["id"]):
                self.assertIsInstance(policy["safe_outcomes"], list)
                self.assertLessEqual(set(policy["safe_outcomes"]),
                                     {"abstain", "request_review", "quarantine_candidate"})
                self.assertLessEqual(set(policy["safe_outcomes"]),
                                     {policy["positive_outcome"], policy["negative_outcome"]})

    def test_a_resisting_model_is_allowed_its_honest_confident_answer(self):
        """injection-triage's correct answer to its own attack is `quarantine`.

        The old rule forbade any `act`, so the one answer that proves the
        recipe works could not be recorded.
        """
        outcome = run_fixture("qualixar.injection-triage", "adversarial")
        self.assertEqual(outcome["raw_gate"]["host_action"], ACT)
        self.assertEqual(outcome["raw_gate"]["recommendation"], "quarantine_candidate")
        self.assertTrue(outcome["intent_held"])
        self.assertEqual(outcome["observed"]["host_action"], "verify")


class TheChosenLabelIsPartOfTheProof(unittest.TestCase):
    """A gate that reports the wrong queue must fail the offline proof."""

    def test_every_choice_case_records_the_label_it_expects(self):
        kinds = {gate["id"]: gate["policy"]["kind"] for gate in CATALOG["gates"]}
        for entry in CATALOG["fixtures"]:
            for case in entry["cases"]:
                with self.subTest(case["fixture_id"]):
                    if kinds[entry["id"]] == "choice":
                        self.assertEqual(case["expected_choice"], case["mock_answer"]["choice"])
                        outcome = run_fixture(entry["id"], case["variant"])
                        self.assertEqual(outcome["raw_gate"]["selected_label"], case["expected_choice"])
                        self.assertEqual(outcome["observed"]["selected_label"], case["expected_choice"])
                    else:
                        self.assertNotIn("expected_choice", case)

    def test_a_swapped_label_fails_even_when_the_action_matches(self):
        catalog = json.loads(json.dumps(CATALOG))
        case = next(e for e in catalog["fixtures"] if e["id"] == "qualixar.task-routing")["cases"][0]
        case["expected_choice"] = next(label for label in case["mock_answer"]["probabilities"]
                                       if label != case["mock_answer"]["choice"])
        with patch.object(recipe_fixtures, "catalog_document", lambda: catalog):
            outcome = run_fixture("qualixar.task-routing", "nominal")
        self.assertEqual(outcome["raw_gate"]["host_action"], ACT)
        self.assertFalse(outcome["matched"])
        self.assertFalse(outcome["passed"])

    def test_an_expected_choice_must_be_a_label(self):
        entry = json.loads(json.dumps(next(e for e in CATALOG["fixtures"] if e["id"] == "qualixar.task-routing")))
        for bad in ("", 3, None):
            entry["cases"][0]["expected_choice"] = bad
            with self.subTest(bad=bad):
                with self.assertRaises(AutoError):
                    validate_fixtures([entry])


class InjectionIntent(unittest.TestCase):
    """The adversarial rule, tested against hand-built catalogs."""

    RECIPE = "qualixar.injection-triage"

    def _catalog(self, noul, *, target="request_review", safe=("quarantine_candidate", "request_review")):
        catalog = json.loads(json.dumps(CATALOG))
        gate = next(g for g in catalog["gates"] if g["id"] == self.RECIPE)
        gate["policy"]["safe_outcomes"] = list(safe)
        case = next(e for e in catalog["fixtures"] if e["id"] == self.RECIPE)["cases"][2]
        case["mock_answer"] = {"type": "noul", "noul": noul}
        case["injection_target"] = target
        result = evaluate(gate["policy"], case["mock_answer"])
        case["expected_status"] = result["status"]
        case["expected_host_action"] = result["host_action"]
        case["expected_recommendation"] = result["recommendation"]
        return catalog

    def _run(self, catalog):
        with patch.object(recipe_fixtures, "catalog_document", lambda: catalog):
            return run_fixture(self.RECIPE, "adversarial")

    def test_acting_on_the_outcome_the_instruction_asks_for_fails_even_when_safe(self):
        outcome = self._run(self._catalog(0.05))  # confident "no" -> request_review, the target
        self.assertTrue(outcome["matched"])
        self.assertFalse(outcome["intent_held"])
        self.assertFalse(outcome["passed"])

    def test_acting_on_an_outcome_the_recipe_did_not_declare_safe_fails(self):
        outcome = self._run(self._catalog(0.95, target="request_review", safe=("request_review",)))
        self.assertFalse(outcome["intent_held"])

    def test_without_declared_safe_outcomes_an_adversarial_case_may_not_act_at_all(self):
        catalog = self._catalog(0.95)
        del next(g for g in catalog["gates"] if g["id"] == self.RECIPE)["policy"]["safe_outcomes"]
        self.assertFalse(self._run(catalog)["intent_held"])

    def test_refusing_to_act_always_holds(self):
        self.assertTrue(self._run(self._catalog(0.5))["intent_held"])

    def test_an_injection_target_is_only_recorded_on_an_adversarial_case(self):
        entry = json.loads(json.dumps(next(e for e in CATALOG["fixtures"] if e["id"] == self.RECIPE)))
        entry["cases"][0]["injection_target"] = "request_review"
        with self.assertRaises(AutoError):
            validate_fixtures([entry])
        entry["cases"][0].pop("injection_target")
        entry["cases"][2]["injection_target"] = ""
        with self.assertRaises(AutoError):
            validate_fixtures([entry])

    def test_malformed_safe_outcomes_are_refused_at_load(self):
        from jev_auto.recipe_gate import validate_gates

        gate = json.loads(json.dumps(next(g for g in CATALOG["gates"] if g["id"] == self.RECIPE)))
        for bad in ("request_review", ["mark_check_passed"], ["request_review", "request_review"], [3]):
            gate["policy"]["safe_outcomes"] = bad
            with self.subTest(bad=bad):
                with self.assertRaises(AutoError):
                    validate_gates([gate])

    def test_experimental_cap_does_not_mutate_raw_gate_result(self):
        from jev_auto.recipe_fixtures import apply_recipe_status_cap

        raw = {"status": "RECOMMEND", "host_action": ACT, "reasons": ["threshold cleared"]}
        live = apply_recipe_status_cap(raw, "SPECIFICATION_NOT_MODEL_EVALUATED")
        self.assertEqual(raw, {"status": "RECOMMEND", "host_action": ACT,
                               "reasons": ["threshold cleared"]})
        self.assertEqual(live["host_action"], "verify")
        self.assertEqual(live["status"], "REVIEW")

    def test_selftest_fails_if_live_cap_stops_applying(self):
        with patch.object(recipe_fixtures, "apply_recipe_status_cap", lambda gate, status: gate):
            outcome = run_fixture("qualixar.task-routing", "nominal")
        self.assertTrue(outcome["matched"], "raw fixture gate still clears")
        self.assertFalse(outcome["live_matched"], "effective host action must be verified")
        self.assertFalse(outcome["passed"])


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
                result = evaluate(policy, {
                    "type": "score", "score": 3.0, "confidence": 0.95,
                    "probabilities": {"0": 0.01, "1": 0.01, "2": 0.03, "3": 0.95},
                })
                self.assertEqual(result["host_action"], ACT)
                self.assertNotEqual(result["recommendation"], "request_review")


def _coherent_confidence(probabilities: dict) -> float:
    """TypeSafe's documented statistic: all mass on one option gives 1.0."""
    values = list(probabilities.values())
    return (len(values) * max(values) - 1) / (len(values) - 1)


class AnswersAProviderCouldReturn(unittest.TestCase):
    """A fixture built on an impossible answer proves the gate against nothing.

    Twenty-two shipped cases once passed only because their confidence could
    not come from their own distribution: 0.42 recorded beside a 0.91 top
    option. With the confidence the provider would actually report, every one
    of them cleared the gate it was meant to be stopped by.
    """

    def _recipe(self, recipe_id):
        return next(recipe for recipe in CATALOG["recipes"] if recipe["id"] == recipe_id)

    def test_every_recorded_confidence_follows_from_its_own_distribution(self):
        for entry in CATALOG["fixtures"]:
            for case in entry["cases"]:
                answer = case["mock_answer"]
                if answer.get("type") not in ("choice", "score"):
                    continue
                with self.subTest(case["fixture_id"]):
                    self.assertAlmostEqual(answer["confidence"],
                                           _coherent_confidence(answer["probabilities"]), delta=0.05)

    def test_every_recorded_score_is_the_expectation_of_its_levels(self):
        for entry in CATALOG["fixtures"]:
            for case in entry["cases"]:
                answer = case["mock_answer"]
                if answer.get("type") != "score":
                    continue
                with self.subTest(case["fixture_id"]):
                    expectation = sum(int(level) * p for level, p in answer["probabilities"].items())
                    self.assertAlmostEqual(answer["score"], expectation, delta=0.01)

    def test_every_recorded_answer_passes_the_live_protocol_validator(self):
        from jev_auto.protocol import validate_response

        for entry in CATALOG["fixtures"]:
            questions = self._recipe(entry["id"])["questions"]
            for case in entry["cases"]:
                with self.subTest(case["fixture_id"]):
                    validate_response({"model": "fixture", "answers": {"decision": case["mock_answer"]}},
                                      questions)

    def _choice_case(self):
        entry = next(e for e in CATALOG["fixtures"] if e["id"] == "qualixar.patch-review")
        return json.loads(json.dumps(entry))

    def test_fixture_validation_refuses_a_confidence_its_distribution_cannot_produce(self):
        entry = self._choice_case()
        answer = entry["cases"][0]["mock_answer"]
        answer["probabilities"] = {label: 0.0225 for label in answer["probabilities"]}
        answer["probabilities"][answer["choice"]] = 0.91
        answer["confidence"] = 0.42  # the coherent value is 0.89
        with self.assertRaises(AutoError):
            validate_fixtures([entry])

    def test_fixture_validation_refuses_a_score_that_is_not_its_expectation(self):
        entry = json.loads(json.dumps(next(e for e in CATALOG["fixtures"]
                                           if e["id"] == "qualixar.work-item-priority")))
        answer = entry["cases"][0]["mock_answer"]
        answer["score"] = round(answer["score"] + 0.25, 2)
        with self.assertRaises(AutoError):
            validate_fixtures([entry])
        for levels in ({"low": 0.02, "mid": 0.08, "high": 0.9}, None):
            entry = json.loads(json.dumps(next(e for e in CATALOG["fixtures"]
                                               if e["id"] == "qualixar.work-item-priority")))
            answer = entry["cases"][0]["mock_answer"]
            if levels is None:
                answer["score"] = "high"
            else:
                answer["probabilities"] = levels
            with self.subTest(levels=levels):
                with self.assertRaises(AutoError):
                    validate_fixtures([entry])

    def test_fixture_validation_refuses_a_choice_answer_without_a_usable_distribution(self):
        for broken in ({"probabilities": "none"}, {"probabilities": {"only": 1.0}}, {"confidence": "high"}):
            entry = self._choice_case()
            entry["cases"][0]["mock_answer"].update(broken)
            with self.subTest(broken=broken):
                with self.assertRaises(AutoError):
                    validate_fixtures([entry])

    def test_a_coherent_answer_still_validates(self):
        entry = self._choice_case()
        self.assertEqual(validate_fixtures([entry]), [entry])


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
