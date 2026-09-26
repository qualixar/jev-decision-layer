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


class MiscalibratedUpstream(unittest.TestCase):
    """A distribution sharper than the model's own belief is not a basis to act.

    laya-mlx records that its checkpoint ships a fitted calibration temperature
    of 0.1006 for the `choice:11+` bucket, and clamps temperatures to
    [0.5, 5.0] before use because 0.1006 "would sharpen logits ~10x and report
    a coin flip as near-certainty".

    Measured against this gate before the fix: a genuine coin flip -- two
    options separated by 0.30 logits, honest probability 0.574 -- sharpened at
    that temperature reported probability 0.9518. With the confidence floor at
    0.55, BOTH an inflated confidence (0.9518) and an HONEST one (0.5744)
    cleared the gate and returned `act`. Every individual check behaved as
    specified; the two thresholds simply were not independent, and the only
    evidence of the problem was their disagreement, which nothing read.

    Two changes close it: the floor moved to 0.70, above the band a two-option
    coin flip can produce, and a shortfall between the distribution's peak and
    the model's own confidence is now refused.
    """

    COIN_FLIP_SHARPENED = 0.9518   # honest p 0.5744, sharpened at T=0.1006
    HONEST_CONFIDENCE = 0.5744

    def policy(self) -> dict:
        catalog = json.loads((RUNTIME / "recipe_catalog.json").read_text())
        for gate in catalog["gates"]:
            policy = gate.get("policy", gate)
            if policy.get("kind") == "choice":
                return policy
        self.fail("the catalog ships no choice gate")

    def answer(self, probability: float, confidence: float) -> dict:
        return {"kind": "choice", "choice": "a", "confidence": confidence,
                "probabilities": {"a": probability, "b": round(1 - probability, 6)}}

    def test_a_sharpened_coin_flip_with_honest_confidence_never_acts(self):
        verdict = evaluate(self.policy(),
                           self.answer(self.COIN_FLIP_SHARPENED, self.HONEST_CONFIDENCE))
        self.assertNotEqual(verdict["host_action"], "act",
                            "a coin flip reached `act` because a bad temperature sharpened it")

    def test_every_shipped_choice_gate_rejects_a_coin_flips_honest_confidence(self):
        """The floor must sit above what a two-option coin flip can report."""
        catalog = json.loads((RUNTIME / "recipe_catalog.json").read_text())
        for gate in catalog["gates"]:
            policy = gate.get("policy", gate)
            if policy.get("kind") != "choice":
                continue
            with self.subTest(recipe=gate.get("recipe_id") or gate.get("id")):
                verdict = evaluate(policy, self.answer(self.COIN_FLIP_SHARPENED,
                                                       self.HONEST_CONFIDENCE))
                self.assertNotEqual(verdict["host_action"], "act")

    def test_the_shortfall_check_is_the_only_thing_standing_between_act_and_verify(self):
        """Both thresholds clear individually; only their disagreement objects.

        Without this case the shortfall check could be deleted and the suite
        would still pass, because the confidence floor catches the cases above.
        """
        policy = self.policy()
        probability, confidence = 0.99, 0.72
        self.assertGreaterEqual(confidence, policy["min_confidence"],
                                "this case must clear the confidence floor on its own")
        self.assertGreaterEqual(probability, policy["min_selected_probability"],
                                "this case must clear the probability bar on its own")
        verdict = evaluate(policy, self.answer(probability, confidence))
        self.assertEqual(verdict["host_action"], "verify")
        self.assertIn("sharper than the model", verdict["reasons"][-1])

    def test_a_normally_confident_answer_still_clears(self):
        """The bar must not punish ordinary answers.

        The shipped fixtures' confident answers are probability 0.98 against
        confidence 0.95. If this ever fails, the shortfall bar is too tight and
        the gate has started refusing answers it should act on.
        """
        verdict = evaluate(self.policy(), self.answer(0.98, 0.95))
        self.assertEqual(verdict["host_action"], "act", verdict["reasons"])

    def test_the_confidence_floor_does_work_the_shortfall_check_cannot(self):
        """A mildly sharpened answer slips under the shortfall bar.

        The shortfall check only sees a LARGE disagreement. An answer at
        probability 0.82 with confidence 0.68 disagrees by 0.14, under the bar,
        and clears the 0.8 probability requirement -- so the confidence floor
        is the only thing left. At the old floor of 0.55 this returned `act` on
        a model reporting it was 68% sure.

        Written after reverting the floor to 0.55 in a worktree and finding the
        other cases in this class still passed: they were being caught by the
        shortfall check, so nothing actually pinned the floor.
        """
        policy = self.policy()
        self.assertGreaterEqual(policy["min_confidence"], 0.7,
                                "the floor must stay above the band a coin flip can report")
        probability, confidence = 0.82, 0.68
        self.assertGreaterEqual(probability, policy["min_selected_probability"])
        self.assertLessEqual(probability - confidence, 0.2,
                             "this case must slip under the shortfall bar to isolate the floor")
        verdict = evaluate(policy, self.answer(probability, confidence))
        self.assertEqual(verdict["host_action"], "verify")
        self.assertIn("below", verdict["reasons"][-1])

    def test_every_choice_gate_keeps_a_floor_above_the_coin_flip_band(self):
        catalog = json.loads((RUNTIME / "recipe_catalog.json").read_text())
        for gate in catalog["gates"]:
            policy = gate.get("policy", gate)
            if policy.get("kind") not in ("choice", "score"):
                continue
            with self.subTest(recipe=gate.get("recipe_id") or gate.get("id")):
                self.assertGreaterEqual(policy["min_confidence"], 0.7)

    def test_the_shortfall_boundary_is_exact(self):
        for probability, confidence, expected in (
            (0.95, 0.75, "act"),      # shortfall exactly 0.20, allowed
            (0.951, 0.75, "verify"),  # shortfall 0.201, refused
        ):
            with self.subTest(shortfall=round(probability - confidence, 3)):
                verdict = evaluate(self.policy(), self.answer(probability, confidence))
                self.assertEqual(verdict["host_action"], expected)


class ProviderCalibration(unittest.TestCase):
    """Confidence does not mean the same thing in two different models.

    One threshold set was applied to every provider until 1.0.7, which held
    only while no provider had been measured. Driving laya-mlx 0.2.0 through
    this package's own contracts produced confidence with a median of 0.2861
    against a probability median far above it -- so a floor chosen for a
    hosted model refused almost everything Laya said, most of it correct.

    Both routes ship. Both have to work on one machine.
    """

    # Real answers, laya-mlx 0.2.0 / aac6fef/laya-mlx, measured 2026-09-26 on
    # this package's own twenty decision contracts. (case, probability, confidence)
    MEASURED = (
        ("01-skill-routing", 0.8902, 0.6647), ("02-task-routing", 0.8324, 0.5675),
        ("03-tool-selection", 0.6471, 0.2596), ("06-injection-triage", 0.8853, 0.6186),
        ("09-patch-review", 0.2946, 0.0085), ("11-failure-classification", 0.2896, 0.1818),
        ("13-issue-triage", 0.6483, 0.3126), ("17-security-review-routing", 0.6617, 0.3289),
        ("18-support-triage", 0.9307, 0.7872), ("20-memory-admission", 0.8433, 0.5631),
    )

    def policy(self) -> dict:
        return {"kind": "choice", "min_confidence": 0.7, "min_selected_probability": 0.8,
                "positive_outcome": "route", "negative_outcome": "request_review",
                "unknown_choice": "unknown"}

    def answer(self, probability: float, confidence: float) -> dict:
        return {"kind": "choice", "choice": "a", "confidence": confidence,
                "probabilities": {"a": probability, "b": round(1 - probability, 6)}}

    def acted(self, provider) -> int:
        return sum(1 for _, p, c in self.MEASURED
                   if evaluate(self.policy(), self.answer(p, c),
                               provider=provider)["host_action"] == "act")

    def test_the_local_route_is_usable_again_under_its_own_profile(self):
        """Without this the local route sends nearly everything to review.

        That is the defect 1.0.1 fixed in another form: a verified-clean
        answer costing the host a review it did not need.
        """
        self.assertGreaterEqual(self.acted("laya-mlx"), 5)

    def test_the_hosted_route_keeps_the_strict_floor(self):
        self.assertLessEqual(self.acted(None), 1)
        self.assertLessEqual(self.acted("typesafe"), 1)

    def test_an_unrecognised_provider_never_loosens_the_gate(self):
        """A name nobody has measured must not buy a weaker threshold."""
        for provider in ("totally-unknown", "", 7, None, {"provider": "laya-mlx"}):
            with self.subTest(provider=provider):
                self.assertEqual(self.acted(provider), self.acted(None))

    def test_the_result_names_the_profile_it_applied(self):
        """A caller must be able to see which calibration decided its answer."""
        verdict = evaluate(self.policy(), self.answer(0.98, 0.95), provider="laya-mlx")
        self.assertEqual(verdict["provider_profile"]["provider"], "laya-mlx")
        self.assertEqual(verdict["provider_profile"]["sample_size"], 14)
        self.assertIn("NOT_CALIBRATED", verdict["provider_profile"]["calibration_status"])

    def test_no_profile_claims_to_be_calibrated(self):
        """Fourteen samples on one checkpoint is a measurement, not calibration."""
        from jev_auto.provider_calibration import describe

        for row in describe():
            with self.subTest(provider=row["provider"]):
                self.assertNotEqual(row["calibration_status"], "CALIBRATED")
                self.assertTrue(row["calibration_status"].endswith("NOT_CALIBRATED")
                                or row["calibration_status"] == "UNVALIDATED_DEMONSTRATION_DEFAULT")

    def test_a_broken_recipe_threshold_still_fails_closed_under_any_profile(self):
        """A profile may move a floor. It may never replace a missing one."""
        broken = {**self.policy(), "min_confidence": "high"}
        for provider in (None, "laya-mlx"):
            with self.subTest(provider=provider):
                verdict = evaluate(broken, self.answer(0.99, 0.99), provider=provider)
                self.assertEqual(verdict["host_action"], "verify")

    def test_the_local_profile_cannot_separate_its_own_spread_from_a_bad_temperature(self):
        """A documented limitation, pinned so it is not mistaken for a guarantee.

        Laya's normal shortfall runs to 0.42, so a coin flip sharpened by the
        checkpoint's uncalibrated 0.1006 temperature sits inside its ordinary
        range and clears the local profile. The defence for that is upstream:
        laya-mlx clamps the temperature at load and says so. Our gate cannot
        see it from the answer alone, and this test records that rather than
        implying the profile catches it.
        """
        sharpened = self.answer(0.9518, 0.5744)
        self.assertEqual(evaluate(self.policy(), sharpened, provider=None)["host_action"],
                         "verify")
        self.assertEqual(evaluate(self.policy(), sharpened, provider="laya-mlx")["host_action"],
                         "act")


if __name__ == "__main__":
    unittest.main()
