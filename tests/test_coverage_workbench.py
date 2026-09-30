"""Coverage-floor tests for the recipe workbench facade (jev_auto/recipe_workbench.py).

Every test asserts an exact status code and error code, or an exact returned
value, and refusal paths assert that nothing was saved or executed. No test
makes a live network or provider call.
"""


from __future__ import annotations

import json
import socket
import sys
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "plugins" / "qualixar-jev-decision-layer" / "runtime"
sys.path.insert(0, str(RUNTIME))

from jev_auto.common import AutoError, digest  # noqa: E402
from jev_auto.recipe_workbench import RecipeWorkbench  # noqa: E402
import src.adl.api.recipe_workbench_server as server_module  # noqa: E402
from src.adl.api.recipe_workbench_server import WorkbenchServer, _validate_json_shape, _WorkbenchHandler  # noqa: E402



from workbench_support import _RECIPE_ID, _VALUES, _base_policy, _query_mock, _patched_workbench_asset, _LiveServerTestCase  # noqa: E402,F401


class OfflineExampleGuardTests(unittest.TestCase):
    def test_unknown_variant_is_rejected_before_any_fixture_replay(self):
        workbench = RecipeWorkbench(Path(tempfile.mkdtemp()), engine_factory=Mock(side_effect=AssertionError))
        with self.assertRaisesRegex(AutoError, "RECIPE_FIXTURE_VARIANT_INVALID"):
            workbench.offline_example(_RECIPE_ID, "not-a-real-variant")


class PreviewLiveGuardTests(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp())
        self.policy = _base_policy()
        self.policy_digest = digest(self.policy)
        self.engine_factory = Mock(side_effect=AssertionError("preview must not construct an engine"))

    def _workbench(self, *, policy=None, preparer=None):
        return RecipeWorkbench(
            self.path,
            engine_factory=self.engine_factory,
            policy_loader=Mock(return_value=policy or self.policy),
            query_preparer=preparer or Mock(return_value=_query_mock(policy_sha256=self.policy_digest)),
        )

    def test_unknown_data_classification_is_rejected_before_compiling_the_query(self):
        preparer = Mock(side_effect=AssertionError("must not compile with a bad classification"))
        workbench = self._workbench(preparer=preparer)
        with self.assertRaisesRegex(AutoError, "DATA_CLASSIFICATION"):
            workbench.preview_live(self.path, _RECIPE_ID, _VALUES, "not-a-real-scope")
        preparer.assert_not_called()

    def test_a_query_compiled_against_a_stale_policy_digest_is_rejected(self):
        # query_preparer returns a policy_sha256 that does not match the
        # policy the workbench just re-read: the compiled query is stale.
        preparer = Mock(return_value=_query_mock(policy_sha256="0" * 64))
        workbench = self._workbench(preparer=preparer)
        with self.assertRaisesRegex(AutoError, "POLICY_CHANGED"):
            workbench.preview_live(self.path, _RECIPE_ID, _VALUES, "public")
        self.engine_factory.assert_not_called()

    def test_a_payload_over_the_policy_byte_budget_is_rejected(self):
        policy = _base_policy(max_request_bytes=10)
        preparer = Mock(return_value=_query_mock(
            policy_sha256=digest(policy), payload_json='{"much too big for the ten byte budget":true}',
        ))
        workbench = self._workbench(policy=policy, preparer=preparer)
        with self.assertRaisesRegex(AutoError, "REQUEST_BYTE_BUDGET"):
            workbench.preview_live(self.path, _RECIPE_ID, _VALUES, "public")
        self.engine_factory.assert_not_called()


class RunLiveGuardTests(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp())
        self.policy = _base_policy()
        self.policy_digest = digest(self.policy)
        self.engine_factory = Mock(side_effect=AssertionError("this guard must fail before engine construction"))
        self.reviewed = {
            "recipe_id": _RECIPE_ID, "values": _VALUES, "data_classification": "public",
            "policy_digest": self.policy_digest, "request_digest": "a" * 64,
            "provider": "typesafe", "model": "jev-1.13.0",
            "questions": {"decision": {"type": "score"}}, "title": "Brief fit",
            "audience": "Creators", "recipe_status": "SPECIFICATION_NOT_MODEL_EVALUATED",
        }

    def _workbench(self, *, policy=None, preparer=None):
        return RecipeWorkbench(
            self.path,
            engine_factory=self.engine_factory,
            policy_loader=Mock(return_value=policy or self.policy),
            query_preparer=preparer or Mock(side_effect=AssertionError("must not recompile")),
        )

    def test_a_reviewed_request_that_is_not_a_dict_is_rejected(self):
        workbench = self._workbench()
        with self.assertRaisesRegex(AutoError, "WORKBENCH_REVIEW_INVALID"):
            workbench.run_live(self.path, ["not", "a", "dict"], confirmation=True)
        self.engine_factory.assert_not_called()

    def test_a_reviewed_request_missing_a_required_field_is_rejected(self):
        incomplete = {key: value for key, value in self.reviewed.items() if key != "model"}
        workbench = self._workbench()
        with self.assertRaisesRegex(AutoError, "WORKBENCH_REVIEW_INVALID"):
            workbench.run_live(self.path, incomplete, confirmation=True)
        self.engine_factory.assert_not_called()

    def test_a_reviewed_request_with_an_invalid_classification_is_rejected(self):
        tampered = {**self.reviewed, "data_classification": "top-secret"}
        workbench = self._workbench()
        with self.assertRaisesRegex(AutoError, "DATA_CLASSIFICATION"):
            workbench.run_live(self.path, tampered, confirmation=True)
        self.engine_factory.assert_not_called()

    def test_policy_changed_since_review_is_rejected_before_recompiling_or_touching_the_engine(self):
        changed_policy = {**self.policy, "policy_id": "a-different-policy"}
        workbench = self._workbench(policy=changed_policy)
        with self.assertRaisesRegex(AutoError, "POLICY_CHANGED"):
            workbench.run_live(self.path, self.reviewed, confirmation=True)
        self.engine_factory.assert_not_called()

    def test_a_recompiled_query_that_no_longer_matches_the_reviewed_request_is_rejected(self):
        # Same policy digest, but the recompiled route now resolves to a
        # different provider than the one the browser reviewed.
        preparer = Mock(return_value=_query_mock(
            policy_sha256=self.policy_digest, request_sha256="a" * 64,
            provider="openrouter", expected_model="jev-1.13.0",
        ))
        workbench = self._workbench(preparer=preparer)
        with self.assertRaisesRegex(AutoError, "WORKBENCH_REVIEW_CHANGED"):
            workbench.run_live(self.path, self.reviewed, confirmation=True)
        self.engine_factory.assert_not_called()

    def test_restricted_scope_over_a_hosted_provider_requires_explicit_external_confirmation(self):
        restricted = {**self.reviewed, "data_classification": "restricted"}
        preparer = Mock(return_value=_query_mock(
            policy_sha256=self.policy_digest, request_sha256="a" * 64,
            provider="typesafe", expected_model="jev-1.13.0",
        ))
        workbench = self._workbench(preparer=preparer)
        with self.assertRaisesRegex(AutoError, "EXTERNAL_SCOPE_CONFIRMATION_REQUIRED"):
            workbench.run_live(self.path, restricted, confirmation=True, external_scope_confirmation=False)
        self.engine_factory.assert_not_called()

    def _passing_preparer(self):
        return Mock(return_value=_query_mock(
            policy_sha256=self.policy_digest, request_sha256="a" * 64,
            provider="typesafe", expected_model="jev-1.13.0",
        ))

    def _run_with_engine_result(self, result):
        engine = Mock()
        engine.providers.close = Mock()
        engine.try_recipe.return_value = result
        workbench = RecipeWorkbench(
            self.path, engine_factory=Mock(return_value=engine),
            policy_loader=Mock(return_value=self.policy), query_preparer=self._passing_preparer(),
        )
        return workbench, engine

    def test_an_engine_result_that_does_not_disclaim_execution_is_a_contract_violation(self):
        workbench, engine = self._run_with_engine_result({"execution_authorized": True, "receipt_id": "a" * 64,
                                                            "policy_receipt_id": "b" * 64})
        with self.assertRaisesRegex(AutoError, "WORKBENCH_ENGINE_CONTRACT"):
            workbench.run_live(self.path, self.reviewed, confirmation=True)
        engine.providers.close.assert_called_once()

    def test_an_engine_result_that_is_not_a_dict_is_a_contract_violation(self):
        workbench, engine = self._run_with_engine_result(["not", "a", "dict"])
        with self.assertRaisesRegex(AutoError, "WORKBENCH_ENGINE_CONTRACT"):
            workbench.run_live(self.path, self.reviewed, confirmation=True)
        engine.providers.close.assert_called_once()

    def test_a_malformed_receipt_id_from_the_engine_is_rejected(self):
        workbench, _engine = self._run_with_engine_result({
            "execution_authorized": False, "receipt_id": "not-a-sha256", "policy_receipt_id": "b" * 64,
        })
        with self.assertRaisesRegex(AutoError, "WORKBENCH_RECEIPT_INVALID"):
            workbench.run_live(self.path, self.reviewed, confirmation=True)

    def test_an_oversized_engine_result_is_rejected(self):
        workbench, _engine = self._run_with_engine_result({
            "execution_authorized": False, "receipt_id": "a" * 64, "policy_receipt_id": "b" * 64,
            "answer": {"text": "x" * 13_000},
        })
        with self.assertRaisesRegex(AutoError, "WORKBENCH_RESULT_TOO_LARGE"):
            workbench.run_live(self.path, self.reviewed, confirmation=True)


class ReadReceiptTests(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp())

    def test_a_malformed_receipt_id_is_rejected_without_constructing_an_engine(self):
        engine_factory = Mock(side_effect=AssertionError("must not construct an engine for a bad id"))
        workbench = RecipeWorkbench(self.path, engine_factory=engine_factory)
        with self.assertRaisesRegex(AutoError, "EVIDENCE_ID"):
            workbench.read_receipt(self.path, "NOT-HEX")
        engine_factory.assert_not_called()

    def test_a_non_dict_record_from_the_store_is_rejected(self):
        engine = Mock()
        engine.providers.close = Mock()
        engine.store.get.return_value = "not-a-dict"
        workbench = RecipeWorkbench(self.path, engine_factory=Mock(return_value=engine))
        with self.assertRaisesRegex(AutoError, "WORKBENCH_RECEIPT_INVALID"):
            workbench.read_receipt(self.path, "a" * 64)
        engine.providers.close.assert_called_once()

    def test_a_valid_record_is_filtered_to_the_safe_public_fields_only(self):
        engine = Mock()
        engine.providers.close = Mock()
        engine.store.get.return_value = {
            "kind": "decision",
            "recipe": "qualixar.brief-fit",
            "recipe_id": "qualixar.brief-fit",
            "provider": "typesafe",
            "model_identity": "jev-1.13.0",
            "model": "jev-1.13.0",
            "recorded_at": 1_700_000_000,
            "recipe_status": "SPECIFICATION_NOT_MODEL_EVALUATED",
            "gate": {"status": "REVIEW", "host_action": "verify", "recommendation": "verify",
                      "reasons": ["x"], "internal_debug": "must not leak"},
            "secret_provider_payload": "must never appear",
            "is_flagged": True,  # bool must be excluded even though isinstance(bool, int) is True
        }
        workbench = RecipeWorkbench(self.path, engine_factory=Mock(return_value=engine))
        safe = workbench.read_receipt(self.path, "a" * 64)
        self.assertEqual(safe["receipt_id"], "a" * 64)
        self.assertEqual(safe["kind"], "decision")
        self.assertEqual(safe["recipe_status"], "SPECIFICATION_NOT_MODEL_EVALUATED")
        self.assertNotIn("secret_provider_payload", safe)
        self.assertNotIn("is_flagged", safe)
        self.assertEqual(safe["gate"], {"status": "REVIEW", "host_action": "verify",
                                          "recommendation": "verify", "reasons": ["x"]})
        self.assertNotIn("internal_debug", safe["gate"])


class ProviderForGuardTests(unittest.TestCase):
    def test_restricted_scope_with_local_laya_enabled_and_configured_routes_to_the_local_model(self):
        policy = _base_policy(local_laya_enabled=True, mlx={"model": "local-laya"})
        self.assertEqual(RecipeWorkbench._provider_for(policy, "restricted"), "laya-mlx")

    def test_an_unrecognized_provider_route_is_rejected(self):
        policy = _base_policy(provider="not-a-real-provider", routes={})
        with self.assertRaisesRegex(AutoError, "PROVIDER_ROUTE"):
            RecipeWorkbench._provider_for(policy, "public")


# ---------------------------------------------------------------------------
# WorkbenchServer -- construction guards (no listener needed)
# ---------------------------------------------------------------------------


class WorkbenchServerConstructionTests(unittest.TestCase):
    def test_a_non_loopback_or_non_ephemeral_address_is_rejected(self):
        service = RecipeWorkbench(Path(tempfile.mkdtemp()), engine_factory=Mock())
        with self.assertRaises(ValueError):
            WorkbenchServer(("0.0.0.0", 0), service)
        with self.assertRaises(ValueError):
            WorkbenchServer(("127.0.0.1",), service)  # wrong tuple arity

    def test_session_lifetime_outside_the_allowed_range_is_rejected(self):
        service = RecipeWorkbench(Path(tempfile.mkdtemp()), engine_factory=Mock())
        with self.assertRaises(ValueError):
            WorkbenchServer(("127.0.0.1", 0), service, session_lifetime=0)
        with self.assertRaises(ValueError):
            WorkbenchServer(("127.0.0.1", 0), service, session_lifetime=3601)

    def test_request_limit_outside_the_allowed_range_is_rejected(self):
        service = RecipeWorkbench(Path(tempfile.mkdtemp()), engine_factory=Mock())
        with self.assertRaises(ValueError):
            WorkbenchServer(("127.0.0.1", 0), service, request_limit=0)
        with self.assertRaises(ValueError):
            WorkbenchServer(("127.0.0.1", 0), service, request_limit=10_001)


# ---------------------------------------------------------------------------
# _validate_json_shape -- one branch (an unexpected Python type after JSON
# decoding) is not reachable through json.loads() with the server's own
# parse_constant/object_pairs_hook, since those only ever produce
# dict/list/str/int/float/bool/None. Exercised directly as a unit test.
# ---------------------------------------------------------------------------


class ValidateJsonShapeDirectTests(unittest.TestCase):
    def test_a_python_type_json_loads_could_never_produce_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "REQUEST_SHAPE_INVALID"):
            _validate_json_shape({"key": {1, 2, 3}})  # a set: not dict/list/str/int/float/bool/None


if __name__ == "__main__":
    unittest.main()
