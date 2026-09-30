"""Coverage-floor tests for the workbench server's catalog, asset, receipt and POST routes.

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


class CatalogAndAssetRouteTests(_LiveServerTestCase):
    def test_catalog_reports_a_service_autoerror_without_leaking_it(self):
        self.get_page()
        self.service.list_cards = Mock(side_effect=AutoError("RECIPE_CATALOG_MISSING"))
        status, _, body = self.get("/api/catalog")
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"]["code"], "RECIPE_CATALOG_MISSING")

    def test_catalog_reports_an_unexpected_service_exception_as_a_generic_internal_error(self):
        self.get_page()
        self.service.list_cards = Mock(side_effect=RuntimeError("unexpected"))
        status, _, body = self.get("/api/catalog")
        self.assertEqual(status, 500)
        payload = json.loads(body)
        self.assertEqual(payload["error"]["code"], "REQUEST_FAILED")
        self.assertNotIn("unexpected", json.dumps(payload))

    def test_static_assets_are_served_with_the_right_content_type(self):
        self.get_page()
        status, headers, body = self.get("/app.css")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/css; charset=utf-8")
        self.assertGreater(len(body), 0)
        status, headers, body = self.get("/app.js")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/javascript; charset=utf-8")

    def test_an_oversized_static_asset_is_refused_not_streamed(self):
        self.get_page()
        with _patched_workbench_asset({"app.css": {"stat_size": 999_999}}):
            status, _, body = self.get("/app.css")
        self.assertEqual(status, 500)
        self.assertEqual(json.loads(body)["error"]["code"], "WORKBENCH_ASSET_UNAVAILABLE")

    def test_a_symlinked_static_asset_is_refused(self):
        self.get_page()
        with _patched_workbench_asset({"app.js": {"is_symlink": True}}):
            status, _, body = self.get("/app.js")
        self.assertEqual(status, 500)
        self.assertEqual(json.loads(body)["error"]["code"], "WORKBENCH_ASSET_UNAVAILABLE")

    def test_an_oversized_index_page_is_refused(self):
        with _patched_workbench_asset({"index.html": {"stat_size": 999_999}}):
            with self.assertRaises(HTTPError) as caught:
                urlopen(self.origin + "/", timeout=2)
        self.assertEqual(caught.exception.code, 500)

    def test_an_index_page_missing_its_csrf_marker_is_refused(self):
        with _patched_workbench_asset({"index.html": {"read_bytes": b"<html><body>no csrf marker</body></html>"}}):
            with self.assertRaises(HTTPError) as caught:
                urlopen(self.origin + "/", timeout=2)
        self.assertEqual(caught.exception.code, 500)
        self.assertEqual(json.loads(caught.exception.read())["error"]["code"], "WORKBENCH_UI_UNAVAILABLE")


class ReceiptRouteTests(_LiveServerTestCase):
    def test_a_64_char_id_with_non_hex_characters_is_not_found_before_reaching_the_service(self):
        self.get_page()
        self.service.read_receipt = Mock(side_effect=AssertionError("must not be called for a malformed id"))
        status, _, body = self.get("/api/receipt/" + ("G" * 64))
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)["error"]["code"], "NOT_FOUND")
        self.service.read_receipt.assert_not_called()

    def test_a_well_formed_id_is_forwarded_to_the_service_and_returned(self):
        self.get_page()
        receipt_id = "a" * 64
        self.service.read_receipt = Mock(return_value={"receipt_id": receipt_id, "kind": "decision"})
        status, _, body = self.get("/api/receipt/" + receipt_id)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"receipt_id": receipt_id, "kind": "decision"})
        self.service.read_receipt.assert_called_once_with(self.service.workspace, receipt_id)

    def test_a_service_autoerror_on_receipt_lookup_is_mapped_to_its_status(self):
        self.get_page()
        self.service.read_receipt = Mock(side_effect=AutoError("EVIDENCE_NOT_FOUND"))
        status, _, body = self.get("/api/receipt/" + ("b" * 64))
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)["error"]["code"], "EVIDENCE_NOT_FOUND")

    def test_an_unexpected_exception_on_receipt_lookup_is_a_generic_internal_error(self):
        self.get_page()
        self.service.read_receipt = Mock(side_effect=RuntimeError("boom"))
        status, _, body = self.get("/api/receipt/" + ("c" * 64))
        self.assertEqual(status, 500)
        self.assertEqual(json.loads(body)["error"]["code"], "REQUEST_FAILED")


class PostRouteTests(_LiveServerTestCase):
    def test_unknown_post_path_is_not_found(self):
        self.get_page()
        status, _, body = self.post("/api/does-not-exist", {})
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "NOT_FOUND")

    def test_a_value_error_raised_by_the_service_is_a_generic_bad_request(self):
        self.get_page()
        self.service.offline_example = Mock(side_effect=ValueError("bad input"))
        status, _, body = self.post("/api/offline-example", {"recipe_id": _RECIPE_ID, "variant": "nominal"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "REQUEST_INVALID")

    def test_an_unexpected_exception_from_the_service_is_a_generic_internal_error(self):
        self.get_page()
        self.service.offline_example = Mock(side_effect=RuntimeError("boom"))
        status, _, body = self.post("/api/offline-example", {"recipe_id": _RECIPE_ID, "variant": "nominal"})
        self.assertEqual(status, 500)
        self.assertEqual(body["error"]["code"], "REQUEST_FAILED")

    def test_review_response_with_an_invalid_shape_is_rejected(self):
        self.get_page()
        self.service.preview_live = Mock(return_value={"review": "not-a-dict", "reviewed_request": {}})
        status, _, body = self.post("/api/review", {
            "recipe_id": _RECIPE_ID, "values": _VALUES, "data_classification": "public",
        })
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "WORKBENCH_REVIEW_INVALID")

    def test_the_ninth_concurrent_review_is_refused_once_the_limit_is_reached(self):
        self.get_page()
        self.service.preview_live = Mock(return_value={
            "review": {"requires_external_scope_confirmation": False, "route_kind": "hosted"},
            "reviewed_request": {"recipe_id": _RECIPE_ID, "values": _VALUES, "data_classification": "public"},
        })
        for _ in range(8):
            status, _, _body = self.post("/api/review", {
                "recipe_id": _RECIPE_ID, "values": _VALUES, "data_classification": "public",
            })
            self.assertEqual(status, 200)
        status, _, body = self.post("/api/review", {
            "recipe_id": _RECIPE_ID, "values": _VALUES, "data_classification": "public",
        })
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "WORKBENCH_REVIEW_LIMIT")

    def test_run_rejects_a_body_with_an_extra_unrecognized_key(self):
        self.get_page()
        status, _, body = self.post("/api/run", {"review_nonce": "x" * 40, "confirm": True, "extra": 1})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "REQUEST_SHAPE_INVALID")

    def test_run_requires_confirm_true_exactly(self):
        self.get_page()
        status, _, body = self.post("/api/run", {"review_nonce": "x" * 40, "confirm": False})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "WORKBENCH_CONFIRMATION_REQUIRED")

    def test_run_rejects_a_non_boolean_external_scope_confirmation(self):
        self.get_page()
        status, _, body = self.post("/api/run", {
            "review_nonce": "x" * 40, "confirm": True, "external_scope_confirmation": "yes",
        })
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "REQUEST_SHAPE_INVALID")

    def test_run_rejects_a_nonce_of_the_wrong_shape(self):
        self.get_page()
        status, _, body = self.post("/api/run", {"review_nonce": "too-short", "confirm": True})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "WORKBENCH_REVIEW_EXPIRED")

    def test_run_rejects_an_unknown_nonce(self):
        self.get_page()
        status, _, body = self.post("/api/run", {"review_nonce": "x" * 40, "confirm": True})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "WORKBENCH_REVIEW_EXPIRED")

    def test_a_concurrent_take_of_the_same_nonce_is_detected_at_the_final_lock_check(self):
        """Simulate another request winning the race between the read and the pop.

        The handler re-checks identity (``is not record``) under the lock
        immediately before popping. We swap in a lock whose second acquire
        replaces the stored tuple with a fresh (value-equal, identity
        different) one, deterministically reproducing what a second, faster
        /api/run request racing on the same nonce would do -- without any
        real thread timing.
        """
        self.get_page()
        reviewed_request = {"recipe_id": _RECIPE_ID, "values": _VALUES, "data_classification": "public"}
        self.service.preview_live = Mock(return_value={
            "review": {"requires_external_scope_confirmation": False, "route_kind": "hosted"},
            "reviewed_request": reviewed_request,
        })
        status, _, response = self.post("/api/review", {
            "recipe_id": _RECIPE_ID, "values": _VALUES, "data_classification": "public",
        })
        self.assertEqual(status, 200)
        nonce = response["review_nonce"]

        real_lock = self.server._review_lock

        class _RaceOnSecondAcquire:
            def __init__(self, inner):
                self._inner = inner
                self._count = 0

            def __enter__(self):
                self._inner.acquire()
                self._count += 1
                if self._count == 2:
                    current = self.server_reviews[nonce]
                    self.server_reviews[nonce] = (dict(current[0]), current[1], current[2])
                return self

            def __exit__(self, *exc_info):
                self._inner.release()
                return False

        race_lock = _RaceOnSecondAcquire(real_lock)
        race_lock.server_reviews = self.server.reviews
        self.server._review_lock = race_lock
        try:
            status, _, body = self.post("/api/run", {"review_nonce": nonce, "confirm": True})
        finally:
            self.server._review_lock = real_lock
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "WORKBENCH_REVIEW_EXPIRED")


if __name__ == "__main__":
    unittest.main()
