"""Contract tests for the local recipe workbench facade."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "plugins" / "qualixar-jev-decision-layer" / "runtime"

# Derived from the recipe sources, so adding a recipe never means editing a count here.
RECIPE_COUNT = len(list((ROOT / "recipes").rglob("*.json")))
sys.path.insert(0, str(RUNTIME))

from jev_auto.recipe_fixtures import run_fixture  # noqa: E402
from jev_auto.common import AutoError  # noqa: E402
from jev_auto.recipe_workbench import RecipeWorkbench  # noqa: E402
from jevkit.engine import catalog as legacy_catalog  # noqa: E402


class WorkbenchCatalogTests(unittest.TestCase):
    def setUp(self):
        self.workbench = RecipeWorkbench(Path(tempfile.mkdtemp()))

    def test_catalog_contains_only_public_recipe_contracts(self):
        cards = self.workbench.list_cards()
        source = json.loads((RUNTIME / "recipe_catalog.json").read_text())
        self.assertEqual(len(cards), RECIPE_COUNT)
        self.assertEqual({card["id"] for card in cards}, {item["id"] for item in source["recipes"]})
        for card in cards:
            self.assertEqual(set(card), {"id", "title", "audience", "input_schema", "questions", "status", "limitations", "featured_for"})
            self.assertNotIn("gates", card)
            self.assertNotIn("fixtures", card)
        self.assertEqual(
            {card["id"] for card in cards if card["featured_for"]},
            {"qualixar.meeting-action-routing", "qualixar.brief-fit", "qualixar.test-selection"},
        )

    def test_offline_example_replays_the_real_fixture_and_is_labeled_synthetic(self):
        expected = run_fixture("qualixar.brief-fit", "uncertain")
        observed = self.workbench.offline_example("qualixar.brief-fit", "uncertain")
        self.assertEqual(observed["outcome"], expected)
        self.assertEqual(observed["mode"], "fixture")
        self.assertEqual(observed["data_classification"], "synthetic")
        self.assertIn("not accuracy evidence", observed["disclaimer"].lower())

    def test_offline_example_never_constructs_engine_or_reads_policy(self):
        engine_factory = Mock(side_effect=AssertionError("engine must not be created for a fixture"))
        policy_loader = Mock(side_effect=AssertionError("fixture must not load workspace policy"))
        workbench = RecipeWorkbench(Path(tempfile.mkdtemp()), engine_factory=engine_factory, policy_loader=policy_loader)
        workbench.offline_example("qualixar.brief-fit", "nominal")
        engine_factory.assert_not_called()
        policy_loader.assert_not_called()


class WorkbenchReviewTests(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp())
        self.policy = {
            "schema_version": 1,
            "enabled": True,
            "provider": "typesafe",
            "routes": {},
            "data_classification": "public",
            "decision_mode": "jev-public",
            "generic_query_enabled": True,
            "local_laya_enabled": False,
            "max_calls_per_day": 20,
            "max_bytes_per_day": 100_000,
            "max_request_bytes": 48_000,
            "expires_at": 4_000_000_000,
            "policy_id": "test-policy",
            # This is the shape produced by the setup wizard: legacy case IDs,
            # which do not overlap with the separate recipe catalog.
            "case_ids": [case["id"] for case in legacy_catalog()],
            "local_recipe_ids": [],
        }
        from jev_auto.common import digest

        self.policy_digest = digest(self.policy)
        self.preparer = Mock(return_value=Mock(
            provider="typesafe", expected_model="jev-1.13.0", request_sha256="a" * 64,
            payload_json='{"fixture":"payload"}', calibration_status="NOT_EVALUATED",
            policy_sha256=self.policy_digest, policy_expires_at=4_000_000_000,
        ))
        self.engine_factory = Mock(side_effect=AssertionError("preview cannot call engine"))
        self.workbench = RecipeWorkbench(
            self.path,
            engine_factory=self.engine_factory,
            policy_loader=Mock(return_value=self.policy),
            query_preparer=self.preparer,
        )

    def test_review_shows_route_scope_limits_and_returns_private_run_binding(self):
        self.assertEqual(len(self.policy["case_ids"]), 20)
        self.assertEqual(self.policy["case_ids"], [case["id"] for case in legacy_catalog()])
        self.assertFalse(any(recipe_id.startswith("qualixar.") for recipe_id in self.policy["case_ids"]))
        values = {"query": "A public synthetic project brief.", "candidate": "A short draft."}
        reviewed = self.workbench.preview_live(
            self.path, "qualixar.brief-fit", values, "public"
        )
        self.assertEqual(reviewed["review"]["provider"], "typesafe")
        self.assertEqual(reviewed["review"]["model"], "jev-1.13.0")
        self.assertEqual(reviewed["review"]["route_kind"], "hosted")
        self.assertEqual(reviewed["review"]["data_classification"], "public")
        self.assertEqual(reviewed["review"]["max_request_bytes"], 48_000)
        self.assertTrue(reviewed["review"]["receipt_persistence"])
        self.assertEqual(reviewed["reviewed_request"]["recipe_id"], "qualixar.brief-fit")
        self.assertEqual(reviewed["reviewed_request"]["values"], values)
        self.assertEqual(reviewed["reviewed_request"]["policy_digest"], self.policy_digest)
        self.engine_factory.assert_not_called()

    def test_preview_uses_generic_query_consent_instead_of_legacy_case_ids(self):
        self.assertNotIn("qualixar.brief-fit", self.policy["case_ids"])
        reviewed = self.workbench.preview_live(
            self.path, "qualixar.brief-fit", {"query": "brief", "candidate": "draft"}, "public"
        )
        self.assertEqual(reviewed["reviewed_request"]["recipe_id"], "qualixar.brief-fit")
        self.preparer.assert_called_once()

    def test_preview_rejects_when_generic_query_consent_is_disabled(self):
        policy = {**self.policy, "generic_query_enabled": False}
        preparer = Mock(side_effect=AssertionError("disabled generic query must fail before compilation"))
        workbench = RecipeWorkbench(
            self.path,
            engine_factory=self.engine_factory,
            policy_loader=Mock(return_value=policy),
            query_preparer=preparer,
        )
        with self.assertRaisesRegex(AutoError, "GENERIC_QUERY_DISABLED"):
            workbench.preview_live(
                self.path, "qualixar.brief-fit", {"query": "brief", "candidate": "draft"}, "public"
            )
        preparer.assert_not_called()
        self.engine_factory.assert_not_called()

    def test_run_rejects_if_generic_query_consent_was_revoked_after_review(self):
        reviewed = self.workbench.preview_live(
            self.path, "qualixar.brief-fit", {"query": "brief", "candidate": "draft"}, "public"
        )["reviewed_request"]
        self.preparer.reset_mock()
        disabled_policy = {**self.policy, "generic_query_enabled": False}
        workbench = RecipeWorkbench(
            self.path,
            engine_factory=self.engine_factory,
            policy_loader=Mock(return_value=disabled_policy),
            query_preparer=self.preparer,
        )
        with self.assertRaisesRegex(AutoError, "GENERIC_QUERY_DISABLED"):
            workbench.run_live(self.path, reviewed, confirmation=True)
        self.preparer.assert_not_called()
        self.engine_factory.assert_not_called()

    def test_review_rejects_workspace_substitution(self):
        with self.assertRaises(AutoError):
            self.workbench.preview_live(self.path.parent, "qualixar.brief-fit", {"brief": "x", "draft": "y"}, "public")


class WorkbenchLiveRunTests(unittest.TestCase):
    def test_live_run_requires_server_confirmation_and_returns_advisory_only(self):
        path = Path(tempfile.mkdtemp())
        policy = {"schema_version": 1, "enabled": True, "provider": "typesafe", "routes": {},
                  "data_classification": "public", "decision_mode": "jev-public",
                  "generic_query_enabled": True, "local_laya_enabled": False,
                  "max_calls_per_day": 20, "max_bytes_per_day": 100_000,
                  "max_request_bytes": 48_000, "expires_at": 4_000_000_000,
                  "policy_id": "test-policy", "case_ids": ["qualixar.brief-fit"], "local_recipe_ids": []}
        from jev_auto.common import digest

        policy_digest = digest(policy)
        result = {"status": "EXPERIMENTAL_ADVISORY", "recipe_id": "qualixar.brief-fit",
                  "title": "Brief fit", "audience": "Creators", "answer": {"type": "score", "score": 2.0},
                  "provider": "typesafe", "model": "jev-1.13.0", "receipt_id": "c" * 64,
                  "cache_hit": False, "calibration_status": "NOT_EVALUATED",
                  "recipe_status": "SPECIFICATION_NOT_MODEL_EVALUATED", "gate_status": "REVIEW",
                  "host_action": "verify", "reason": "Review the result.", "recommendation": "verify",
                  "selected_label": None,
                  "policy_receipt_id": "d" * 64, "execution_authorized": False,
                  "provider_profile": {"untrusted": "do not expose"}}
        engine = Mock()
        engine.providers.close = Mock()
        engine.policy.return_value = policy
        engine.try_recipe.return_value = result
        workbench = RecipeWorkbench(path, engine_factory=Mock(return_value=engine), policy_loader=Mock(return_value=policy),
                                    query_preparer=Mock(return_value=Mock(policy_sha256=policy_digest,
                                                                          request_sha256="a" * 64,
                                                                          provider="typesafe",
                                                                          expected_model="jev-1.13.0")))
        reviewed = {"recipe_id": "qualixar.brief-fit", "values": {"query": "Brief", "candidate": "Draft"},
                    "data_classification": "public", "policy_digest": policy_digest,
                    "request_digest": "a" * 64, "provider": "typesafe", "model": "jev-1.13.0",
                    "questions": {"decision": {"type": "score"}}, "title": "Brief fit",
                    "audience": "Creators", "recipe_status": "SPECIFICATION_NOT_MODEL_EVALUATED"}
        with self.assertRaises(AutoError):
            workbench.run_live(path, reviewed, confirmation=False)
        output = workbench.run_live(path, reviewed, confirmation=True)
        engine.try_recipe.assert_called_once_with({"recipe_id": "qualixar.brief-fit", "input": reviewed["values"],
                                                   "data_classification": "public",
                                                   "expected_policy_digest": policy_digest})
        engine.providers.close.assert_called_once()
        self.assertEqual(output["answer"], result["answer"])
        self.assertIn("selected_label", output, "the chosen label must reach the browser")
        self.assertEqual(output["host_action"], "verify")
        self.assertFalse(output["execution_authorized"])
        self.assertNotIn("provider_profile", output)

    def test_policy_change_after_review_is_rejected_by_engine_before_provider_call(self):
        from jev_auto.common import digest
        from jev_auto.engine import Engine
        from jev_auto.settings import make_policy, save_policy

        path = Path(tempfile.mkdtemp())
        state_base = Path(tempfile.mkdtemp())
        policy_b = make_policy(
            path,
            "typesafe",
            generic_query_enabled=True,
            case_ids=["qualixar.brief-fit"],
        )
        save_policy(path, policy_b, state_base)
        policy_a = {**policy_b, "policy_id": "reviewed-policy-A"}
        reviewed_digest = digest(policy_a)

        class CountingProvider:
            def __init__(self):
                self.calls = 0
                self.closes = 0

            def evaluate(self, _policy, _state, _questions):
                self.calls += 1
                raise AssertionError("provider must not run with a changed policy")

            def close(self):
                self.closes += 1

        provider = CountingProvider()
        query = Mock(
            policy_sha256=reviewed_digest,
            request_sha256="a" * 64,
            provider="typesafe",
            expected_model="jev-1.13.0",
            payload_json='{"model":"jev-1.13.0"}',
            calibration_status="NOT_EVALUATED",
        )
        workbench = RecipeWorkbench(
            path,
            base=state_base,
            engine_factory=lambda workspace_path, base: Engine(workspace_path, base, provider=provider),
            policy_loader=Mock(return_value=policy_a),
            query_preparer=Mock(return_value=query),
        )
        request = workbench.preview_live(
            path,
            "qualixar.brief-fit",
            {"query": "brief", "candidate": "draft"},
            "public",
        )["reviewed_request"]

        with self.assertRaisesRegex(AutoError, "POLICY_CHANGED"):
            workbench.run_live(path, request, confirmation=True)
        self.assertEqual(provider.calls, 0)
        self.assertEqual(provider.closes, 1)


if __name__ == "__main__":
    unittest.main()
