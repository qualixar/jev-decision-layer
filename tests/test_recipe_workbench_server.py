"""Loopback HTTP security and route tests for the recipe workbench."""

from __future__ import annotations

import http.client
import json
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "plugins" / "qualixar-jev-decision-layer" / "runtime"
sys.path.insert(0, str(RUNTIME))

from jev_auto.recipe_workbench import RecipeWorkbench  # noqa: E402
from src.adl.api.recipe_workbench_server import WorkbenchServer  # noqa: E402


class WorkbenchServerTests(unittest.TestCase):
    def setUp(self):
        self.workspace = Path(tempfile.mkdtemp())
        self.service = RecipeWorkbench(self.workspace, engine_factory=Mock())
        self.server = WorkbenchServer(("127.0.0.1", 0), self.service)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.origin = f"http://127.0.0.1:{self.server.server_port}"
        self.cookie = None
        self.csrf = None

    def tearDown(self):
        if self.thread.is_alive():
            self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def get_page(self):
        with urlopen(self.origin + "/", timeout=2) as response:
            body = response.read()
            headers = response.headers
            self.cookie = headers.get("Set-Cookie").split(";", 1)[0]
            self.csrf = body.decode().split('name="jev-csrf" content="', 1)[1].split('"', 1)[0]
            return response.status, headers, body

    def post(self, path, payload, *, origin=None, csrf=None, cookie=None):
        headers = {
            "Content-Type": "application/json",
            "Cookie": cookie or self.cookie,
            "Origin": origin or self.origin,
            "X-CSRF-Token": csrf or self.csrf,
        }
        request = Request(self.origin + path, data=json.dumps(payload).encode(), headers=headers, method="POST")
        try:
            with urlopen(request, timeout=2) as response:
                return response.status, response.headers, json.loads(response.read())
        except HTTPError as response:
            return response.code, response.headers, json.loads(response.read())

    def test_only_loopback_and_ephemeral_binding_are_accepted(self):
        with self.assertRaises(ValueError):
            WorkbenchServer(("0.0.0.0", 0), self.service)
        self.assertGreater(self.server.server_port, 0)

    def test_root_sets_strict_session_cookie_and_security_headers(self):
        status, headers, body = self.get_page()
        self.assertEqual(status, 200)
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", headers["Set-Cookie"])
        self.assertIn("Path=/", headers["Set-Cookie"])
        self.assertNotIn("Domain=", headers["Set-Cookie"])
        self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIn(b"One small decision", body)

    def test_catalog_and_fixture_routes_are_session_bound_and_offline(self):
        self.get_page()
        request = Request(self.origin + "/api/catalog", headers={"Cookie": self.cookie})
        with urlopen(request, timeout=2) as response:
            catalog = json.loads(response.read())
        self.assertEqual(len(catalog["recipes"]), 38)
        status, _, outcome = self.post("/api/offline-example", {"recipe_id": "qualixar.brief-fit", "variant": "nominal"})
        self.assertEqual(status, 200)
        self.assertEqual(outcome["mode"], "fixture")
        self.assertEqual(outcome["data_classification"], "synthetic")
        self.service.engine_factory.assert_not_called()

    def test_unrelated_browser_cookies_are_allowed_but_duplicate_session_is_rejected(self):
        self.get_page()
        request = Request(self.origin + "/api/catalog", headers={
            "Cookie": f"browser_preference=light; {self.cookie}",
        })
        with urlopen(request, timeout=2) as response:
            self.assertEqual(response.status, 200)
        duplicate = Request(self.origin + "/api/catalog", headers={
            "Cookie": f"{self.cookie}; jev_workbench=not-the-session",
        })
        with self.assertRaises(HTTPError) as failure:
            urlopen(duplicate, timeout=2)
        self.assertEqual(failure.exception.code, 403)

    def test_post_rejects_wrong_origin_and_missing_csrf_before_service_call(self):
        self.get_page()
        status, _, _ = self.post("/api/offline-example", {"recipe_id": "qualixar.brief-fit", "variant": "nominal"}, origin="null")
        self.assertEqual(status, 403)
        status, _, _ = self.post("/api/offline-example", {"recipe_id": "qualixar.brief-fit", "variant": "nominal"}, csrf="bad-token")
        self.assertEqual(status, 403)

    def test_wrong_host_is_rejected(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        connection.request("GET", "/", headers={"Host": "localhost"})
        response = connection.getresponse()
        self.assertEqual(response.status, 403)
        connection.close()

    def test_oversized_or_duplicate_key_json_is_rejected(self):
        self.get_page()
        status, _, _ = self.post("/api/offline-example", {"recipe_id": "qualixar.brief-fit", "variant": "nominal", "extra": True})
        self.assertEqual(status, 400)
        body = b'{"recipe_id":"qualixar.brief-fit","recipe_id":"other","variant":"nominal"}'
        request = Request(self.origin + "/api/offline-example", data=body, headers={
            "Content-Type": "application/json", "Cookie": self.cookie, "Origin": self.origin,
            "X-CSRF-Token": self.csrf,
        }, method="POST")
        with self.assertRaises(HTTPError) as caught:
            urlopen(request, timeout=2)
        self.assertEqual(caught.exception.code, 400)

    def test_oversized_json_body_is_rejected_before_service_invocation(self):
        self.get_page()
        request = Request(self.origin + "/api/offline-example", data=b" " * 48_001, headers={
            "Content-Type": "application/json", "Cookie": self.cookie, "Origin": self.origin,
            "X-CSRF-Token": self.csrf,
        }, method="POST")
        with self.assertRaises(HTTPError) as caught:
            urlopen(request, timeout=2)
        self.assertEqual(caught.exception.code, 413)
        self.service.engine_factory.assert_not_called()

    def test_malformed_body_closes_connection_before_pipelined_request(self):
        self.get_page()
        self.service.list_cards = Mock(wraps=self.service.list_cards)
        request = (
            f"POST /api/offline-example HTTP/1.1\r\nHost: 127.0.0.1:{self.server.server_port}\r\n"
            f"Cookie: {self.cookie}\r\nOrigin: {self.origin}\r\nX-CSRF-Token: {self.csrf}\r\n"
            "Content-Type: application/json\r\nContent-Length: 120\r\n\r\n{}"
            f"GET /api/catalog HTTP/1.1\r\nHost: 127.0.0.1:{self.server.server_port}\r\n"
            f"Cookie: {self.cookie}\r\nConnection: close\r\n\r\n"
        ).encode()
        connection = socket.create_connection(("127.0.0.1", self.server.server_port), timeout=2)
        connection.sendall(request)
        chunks = []
        while True:
            chunk = connection.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
        connection.close()
        response = b"".join(chunks)
        self.assertIn(b"400 Bad Request", response)
        self.assertIn(b"Connection: close", response)
        self.assertEqual(response.count(b"HTTP/1.1 "), 1)
        self.service.list_cards.assert_not_called()

    def test_partial_body_and_enormous_content_length_are_rejected_and_closed(self):
        self.get_page()
        connection = socket.create_connection(("127.0.0.1", self.server.server_port), timeout=2)
        partial = (
            f"POST /api/offline-example HTTP/1.1\r\nHost: 127.0.0.1:{self.server.server_port}\r\n"
            f"Cookie: {self.cookie}\r\nOrigin: {self.origin}\r\nX-CSRF-Token: {self.csrf}\r\n"
            "Content-Type: application/json\r\nContent-Length: 12\r\n\r\n{}"
        ).encode()
        connection.sendall(partial)
        connection.shutdown(socket.SHUT_WR)
        response = b""
        while True:
            chunk = connection.recv(4096)
            if not chunk:
                break
            response += chunk
        connection.close()
        self.assertIn(b"400 Bad Request", response)
        self.assertIn(b"Connection: close", response)

        connection = socket.create_connection(("127.0.0.1", self.server.server_port), timeout=2)
        enormous = (
            f"POST /api/offline-example HTTP/1.1\r\nHost: 127.0.0.1:{self.server.server_port}\r\n"
            f"Cookie: {self.cookie}\r\nOrigin: {self.origin}\r\nX-CSRF-Token: {self.csrf}\r\n"
            "Content-Type: application/json\r\nContent-Length: 9" + "9" * 5000 + "\r\n\r\n"
        ).encode()
        connection.sendall(enormous)
        response = connection.recv(4096)
        connection.close()
        self.assertIn(b"400 Bad Request", response)
        self.assertIn(b"Connection: close", response)

    def test_review_nonce_is_consumed_by_exactly_one_run(self):
        self.get_page()
        reviewed_request = {"recipe_id": "qualixar.brief-fit", "values": {"query": "q", "candidate": "c"},
                            "data_classification": "public"}
        self.service.preview_live = Mock(return_value={
            "review": {"requires_external_scope_confirmation": False, "route_kind": "hosted"},
            "reviewed_request": reviewed_request,
        })
        self.service.run_live = Mock(return_value={"execution_authorized": False, "receipt_id": "a" * 64})
        status, _, response = self.post("/api/review", {
            "recipe_id": "qualixar.brief-fit", "values": reviewed_request["values"], "data_classification": "public",
        })
        self.assertEqual(status, 200)
        nonce = response["review_nonce"]
        status, _, result = self.post("/api/run", {"review_nonce": nonce, "confirm": True})
        self.assertEqual(status, 200)
        self.assertFalse(result["execution_authorized"])
        status, _, error = self.post("/api/run", {"review_nonce": nonce, "confirm": True})
        self.assertEqual(status, 400)
        self.assertEqual(error["error"]["code"], "WORKBENCH_REVIEW_EXPIRED")
        self.service.run_live.assert_called_once()

    def test_session_expiry_stops_serve_loop_and_closes_listener(self):
        self.server.session.expires_at = time.monotonic() + 0.15
        self.thread.join(timeout=2)
        self.assertFalse(self.thread.is_alive())
        self.assertEqual(self.server.socket.fileno(), -1)

    def test_missing_restricted_scope_acknowledgement_does_not_consume_review(self):
        self.get_page()
        reviewed_request = {"recipe_id": "qualixar.brief-fit", "values": {"query": "q", "candidate": "c"},
                            "data_classification": "restricted"}
        self.service.preview_live = Mock(return_value={
            "review": {"requires_external_scope_confirmation": True, "route_kind": "hosted"},
            "reviewed_request": reviewed_request,
        })
        self.service.run_live = Mock(return_value={"execution_authorized": False, "receipt_id": "b" * 64})
        status, _, response = self.post("/api/review", {
            "recipe_id": "qualixar.brief-fit", "values": reviewed_request["values"], "data_classification": "restricted",
        })
        self.assertEqual(status, 200)
        nonce = response["review_nonce"]
        status, _, error = self.post("/api/run", {"review_nonce": nonce, "confirm": True})
        self.assertEqual(status, 400)
        self.assertEqual(error["error"]["code"], "EXTERNAL_SCOPE_CONFIRMATION_REQUIRED")
        status, _, result = self.post("/api/run", {
            "review_nonce": nonce, "confirm": True, "external_scope_confirmation": True,
        })
        self.assertEqual(status, 200)
        self.assertFalse(result["execution_authorized"])
        self.service.run_live.assert_called_once()

    def test_receipt_path_requires_a_lowercase_sha256_id(self):
        self.get_page()
        request = Request(self.origin + "/api/receipt/NOT-AN-ID", headers={"Cookie": self.cookie})
        with self.assertRaises(HTTPError) as caught:
            urlopen(request, timeout=2)
        self.assertEqual(caught.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
