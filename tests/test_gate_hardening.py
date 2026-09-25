"""The gate must fail closed, and the proof must not be tautological.

Every case here is a defect an external audit found in the shipped 1.0.1 gate
and this session reproduced before fixing. They are kept as tests because each
one produced `act` — the promise that the host need not think again — on an
answer no one should act on. That is the single most expensive thing this
layer can get wrong.
"""

from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "plugins" / "qualixar-jev-decision-layer" / "runtime"
sys.path.insert(0, str(RUNTIME))

from jev_auto import recipe_fixtures  # noqa: E402
from jev_auto.common import AutoError  # noqa: E402
from jev_auto.host_mcp import TARGETS, merge, plan, render  # noqa: E402
from jev_auto.recipe_gate import ACT, evaluate, validate_gates  # noqa: E402

NOUL = {"kind": "noul", "yes": 0.8, "no": 0.2,
        "positive_outcome": "Y", "negative_outcome": "N"}
CHOICE = {"kind": "choice", "min_confidence": 0.7, "min_selected_probability": 0.8,
          "positive_outcome": "P", "negative_outcome": "N"}
SCORE = {"kind": "score", "min_confidence": 0.7, "min_score": 1.5,
         "positive_outcome": "P", "negative_outcome": "N"}


class ValuesOutsideTheirOwnDomain(unittest.TestCase):
    def test_a_noul_above_one_or_below_zero_is_not_an_answer(self):
        for value in (1.5, -2, 2.0, -0.01):
            with self.subTest(value=value):
                self.assertNotEqual(evaluate(NOUL, {"noul": value})["host_action"], ACT)

    def test_a_confidence_above_one_cannot_clear_a_floor(self):
        """999 > 0.7 is true, and that is exactly the bug."""
        out = evaluate(CHOICE, {"choice": "a", "probabilities": {"a": 0.9}, "confidence": 999})
        self.assertNotEqual(out["host_action"], ACT)
        self.assertNotEqual(evaluate(SCORE, {"score": 2.0, "confidence": 5.0})["host_action"], ACT)

    def test_a_probability_outside_zero_to_one_is_refused(self):
        for probability in (1.5, -0.5):
            with self.subTest(probability=probability):
                out = evaluate(CHOICE, {"choice": "a", "probabilities": {"a": probability},
                                        "confidence": 0.9})
                self.assertNotEqual(out["host_action"], ACT)


class SelfContradictoryAnswers(unittest.TestCase):
    def test_a_label_the_model_scored_lower_than_another_is_refused(self):
        """The probability bar is set low on purpose.

        With a bar of 0.8 this case fails the bar, so the test would pass with
        the argmax check deleted — a mutation run caught exactly that. At 0.2
        the selected label clears the bar and only its rank can refuse it.
        """
        policy = {**CHOICE, "min_selected_probability": 0.2}
        out = evaluate(policy, {"choice": "a", "probabilities": {"a": 0.3, "b": 0.65},
                                "confidence": 0.95})
        self.assertNotEqual(out["host_action"], ACT, "0.3 cleared the bar; 'b' outranks it")

    def test_a_distribution_holding_more_than_one_unit_of_mass_is_refused(self):
        out = evaluate(CHOICE, {"choice": "a", "probabilities": {"a": 0.9, "b": 0.9},
                                "confidence": 0.95})
        self.assertNotEqual(out["host_action"], ACT)

    def test_a_label_that_is_not_a_string_is_refused(self):
        for label in (True, 123, None, ""):
            with self.subTest(label=label):
                out = evaluate(CHOICE, {"choice": label, "probabilities": {label: 0.9},
                                        "confidence": 0.9})
                self.assertNotEqual(out["host_action"], ACT)


class FailClosed(unittest.TestCase):
    """An unreadable threshold is a broken gate, not an absent one."""

    def test_a_malformed_floor_does_not_silently_drop_the_gate(self):
        out = evaluate({"kind": "choice", "min_confidence": "high", "min_selected_probability": 0.8,
                        "positive_outcome": "P"},
                       {"choice": "a", "probabilities": {"a": 0.9}, "confidence": 0.1})
        self.assertNotEqual(out["host_action"], ACT)
        self.assertIn("thresholds", " ".join(out["reasons"]))

    def test_a_missing_probability_bar_does_not_admit_an_impossible_distribution(self):
        out = evaluate({"kind": "choice", "min_confidence": 0.5, "positive_outcome": "P"},
                       {"choice": "a", "probabilities": {"a": 0.0}, "confidence": 0.95})
        self.assertNotEqual(out["host_action"], ACT)

    def test_a_missing_score_bar_fails_closed(self):
        out = evaluate({"kind": "score", "min_confidence": 0.5, "positive_outcome": "P"},
                       {"score": 99.0, "confidence": 0.95})
        self.assertNotEqual(out["host_action"], ACT)

    def test_act_is_never_returned_without_something_to_act_on(self):
        out = evaluate({"kind": "choice", "min_confidence": 0.5, "min_selected_probability": 0.8},
                       {"choice": "a", "probabilities": {"a": 0.9}, "confidence": 0.9})
        self.assertNotEqual(out["host_action"], ACT)
        self.assertIsNone(out["recommendation"])


class NeverRaisesOnContent(unittest.TestCase):
    """The docstring promises this. It was not true."""

    def test_an_unhashable_label_returns_a_verdict_rather_than_crashing(self):
        for label in (["x"], {"x": 1}, {1, 2}):
            with self.subTest(label=type(label).__name__):
                out = evaluate(CHOICE, {"choice": label, "probabilities": {"a": 0.9},
                                        "confidence": 0.9})
                self.assertNotEqual(out["host_action"], ACT)

    def test_a_policy_that_is_not_a_dict_returns_a_verdict(self):
        for policy in (None, [], "choice", 3):
            with self.subTest(policy=repr(policy)):
                self.assertNotEqual(evaluate(policy, {})["host_action"], ACT)


class GatesRejectedAtLoad(unittest.TestCase):
    def _gate(self, policy):
        return [{"id": "x", "policy": policy}]

    def test_inverted_noul_bands_are_refused(self):
        with self.assertRaises(AutoError):
            validate_gates(self._gate({**NOUL, "yes": 0.3, "no": 0.7}))

    def test_a_malformed_threshold_is_refused(self):
        with self.assertRaises(AutoError):
            validate_gates(self._gate({**CHOICE, "min_confidence": "high"}))

    def test_a_missing_threshold_is_refused(self):
        with self.assertRaises(AutoError):
            validate_gates(self._gate({"kind": "choice", "min_confidence": 0.7,
                                       "positive_outcome": "P", "negative_outcome": "N"}))

    def test_a_confidence_floor_on_a_noul_is_refused(self):
        """A Noul answer carries no confidence field to compare against."""
        with self.assertRaises(AutoError):
            validate_gates(self._gate({**NOUL, "min_confidence": 0.5}))

    def test_a_gate_that_cannot_tell_clean_from_dirty_is_refused(self):
        with self.assertRaises(AutoError):
            validate_gates(self._gate({**CHOICE, "negative_outcome": "P"}))

    def test_the_shipped_catalog_still_loads(self):
        catalog = json.loads((RUNTIME / "recipe_catalog.json").read_text())
        self.assertEqual(len(validate_gates(catalog["gates"])), 36)


class TheProofIsNotTautological(unittest.TestCase):
    """A fixture must not pass merely by agreeing with itself."""

    def _catalog_with_lying_uncertain_case(self):
        catalog = copy.deepcopy(json.loads((RUNTIME / "recipe_catalog.json").read_text()))
        entry = next(e for e in catalog["fixtures"] if e["id"] == "qualixar.task-routing")
        case = next(c for c in entry["cases"] if c["variant"] == "uncertain")
        case["mock_answer"]["confidence"] = 0.99
        case["expected_status"] = "RECOMMEND"
        case["expected_host_action"] = "act"
        case["expected_recommendation"] = "route_to_queue"
        return catalog

    def test_an_uncertain_case_that_clears_the_gate_fails_even_when_it_matches(self):
        with patch.object(recipe_fixtures, "catalog_document", self._catalog_with_lying_uncertain_case):
            outcome = recipe_fixtures.run_fixture("qualixar.task-routing", "uncertain")
        self.assertTrue(outcome["matched"], "it does agree with its own expectation")
        self.assertFalse(outcome["intent_held"], "but an uncertain case must not clear the gate")
        self.assertFalse(outcome["passed"])

    def test_the_suite_reports_that_failure(self):
        with patch.object(recipe_fixtures, "catalog_document", self._catalog_with_lying_uncertain_case):
            result = recipe_fixtures.selftest()
        self.assertFalse(result["all_passed"])
        self.assertEqual(len(result["failures"]), 1)

    def test_one_broken_case_does_not_destroy_the_whole_proof(self):
        catalog = copy.deepcopy(json.loads((RUNTIME / "recipe_catalog.json").read_text()))
        entry = next(e for e in catalog["fixtures"] if e["id"] == "qualixar.task-routing")
        entry["cases"][0]["state"] = {"wrong_field": "no longer type-checks"}
        with patch.object(recipe_fixtures, "catalog_document", lambda: catalog):
            result = recipe_fixtures.selftest()
        self.assertEqual(result["cases"], 108, "every other case still ran")
        self.assertEqual(len(result["failures"]), 1)


class FixtureValidation(unittest.TestCase):
    def test_a_null_case_is_a_typed_error_not_a_crash(self):
        with self.assertRaises(AutoError):
            recipe_fixtures.validate_fixtures([{"id": "r", "cases": [None, None, None]}])

    def test_cases_out_of_order_are_refused(self):
        catalog = json.loads((RUNTIME / "recipe_catalog.json").read_text())
        entry = copy.deepcopy(catalog["fixtures"][0])
        entry["cases"].reverse()
        with self.assertRaises(AutoError):
            recipe_fixtures.validate_fixtures([entry])


class HostShapes(unittest.TestCase):
    """Each host disagrees about the shape, and every wrong shape is silent."""

    def test_each_host_gets_the_key_it_actually_reads(self):
        expected = {"vscode": "servers", "antigravity": "mcpServers", "claude-desktop": "mcpServers"}
        for host, key in expected.items():
            with self.subTest(host):
                self.assertEqual(list(render(host, Path("/opt/jev"))), [key])

    def test_only_vscode_carries_a_type_field(self):
        for host in TARGETS:
            entry = render(host, Path("/opt/jev"))[TARGETS[host].key]["qualixar-jev"]
            with self.subTest(host):
                self.assertEqual("type" in entry, host == "vscode")

    def test_a_plan_never_returns_another_server_secret(self):
        """A printed plan used to include every key in the user's config."""
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            config = workspace / TARGETS["vscode"].workspace_relative
            config.parent.mkdir(parents=True)
            config.write_text(json.dumps({"servers": {
                "other": {"command": "x", "env": {"API_KEY": "sk-live-do-not-print"}}}}))
            outcome = plan("vscode", workspace, Path("/opt/jev"))
        self.assertNotIn("sk-live-do-not-print", json.dumps(outcome))
        self.assertEqual(outcome["preserved_servers"], ["other"])
        self.assertNotIn("document", outcome)

    def test_a_secret_under_our_own_name_is_masked_before_it_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            config = workspace / TARGETS["vscode"].workspace_relative
            config.parent.mkdir(parents=True)
            config.write_text(json.dumps({"servers": {
                "qualixar-jev": {"command": "/old", "env": {"TOKEN": "sk-live-secret"}}}}))
            outcome = plan("vscode", workspace, Path("/opt/jev"))
        self.assertNotIn("sk-live-secret", json.dumps(outcome))
        self.assertEqual(outcome["replaced_entry"]["env"], {"TOKEN": "[REDACTED]"})

    def test_a_malformed_key_is_refused_rather_than_reinterpreted(self):
        for bad in ({"servers": "oops"}, {"servers": None}, {"servers": ["a"]}, "not a dict"):
            with self.subTest(bad=repr(bad)[:24]):
                with self.assertRaises(AutoError):
                    merge("vscode", bad)

    def test_a_workspace_host_without_a_workspace_is_refused(self):
        with self.assertRaises(AutoError):
            plan("vscode", None)

    def test_an_unknown_host_is_refused(self):
        with self.assertRaises(AutoError):
            plan("emacs", None)


if __name__ == "__main__":
    unittest.main()
