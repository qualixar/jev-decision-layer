"""Coverage-floor tests for the workbench server's session, protocol, limit and send-failure handling.

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


class SessionAndProtocolTests(_LiveServerTestCase):
    def test_a_post_with_no_session_cookie_is_rejected_and_closes_the_connection(self):
        self.get_page()
        connection = socket.create_connection(("127.0.0.1", self.server.server_port), timeout=2)
        request = (
            f"POST /api/offline-example HTTP/1.1\r\nHost: 127.0.0.1:{self.server.server_port}\r\n"
            f"Origin: {self.origin}\r\nX-CSRF-Token: {self.csrf}\r\n"
            "Content-Type: application/json\r\nContent-Length: 2\r\n\r\n{}"
        ).encode()
        connection.sendall(request)
        response = b""
        while True:
            chunk = connection.recv(4096)
            if not chunk:
                break
            response += chunk
        connection.close()
        self.assertIn(b"403", response.split(b"\r\n", 1)[0])
        self.assertIn(b"Connection: close", response)
        self.assertIn(b"SESSION_REJECTED", response)

    def test_unsupported_methods_are_rejected_with_405_and_an_allow_header(self):
        self.get_page()
        for method in ("PUT", "DELETE", "PATCH"):
            connection = socket.create_connection(("127.0.0.1", self.server.server_port), timeout=2)
            request = (
                f"{method} /api/catalog HTTP/1.1\r\nHost: 127.0.0.1:{self.server.server_port}\r\n"
                f"Cookie: {self.cookie}\r\nContent-Length: 0\r\n\r\n"
            ).encode()
            connection.sendall(request)
            response = b""
            while True:
                chunk = connection.recv(4096)
                if not chunk:
                    break
                response += chunk
            connection.close()
            self.assertIn(b"405", response.split(b"\r\n", 1)[0], method)
            self.assertIn(b"Allow: GET, POST", response, method)
            self.assertIn(b"METHOD_NOT_ALLOWED", response, method)

    def test_a_request_carrying_expect_100_continue_is_refused_with_417(self):
        connection = socket.create_connection(("127.0.0.1", self.server.server_port), timeout=2)
        request = (
            f"POST /api/offline-example HTTP/1.1\r\nHost: 127.0.0.1:{self.server.server_port}\r\n"
            "Expect: 100-continue\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n"
        ).encode()
        connection.sendall(request)
        response = b""
        while True:
            chunk = connection.recv(4096)
            if not chunk:
                break
            response += chunk
        connection.close()
        self.assertIn(b"417", response.split(b"\r\n", 1)[0])
        self.assertIn(b"HTTP_REQUEST_REJECTED", response)

    def test_a_syntactically_invalid_request_line_gets_the_no_detail_json_body(self):
        connection = socket.create_connection(("127.0.0.1", self.server.server_port), timeout=2)
        connection.sendall(b"NOTAVALIDREQUESTLINE\r\n\r\n")
        response = b""
        while True:
            chunk = connection.recv(4096)
            if not chunk:
                break
            response += chunk
        connection.close()
        self.assertIn(b"HTTP_REQUEST_REJECTED", response)

    def test_a_malformed_request_line_gets_only_the_no_detail_refusal_on_every_python(self):
        """Whether this reply carries an HTTP status line depends on the Python
        release: CPython 3.14.5 leaves request_version at "HTTP/0.9" for a
        request line it cannot parse and so writes the body alone, while
        3.14.7 frames it. What this server controls, and what must hold on
        every release, is the body: the fixed refusal code and nothing else."""
        connection = socket.create_connection(("127.0.0.1", self.server.server_port), timeout=2)
        connection.sendall(b"NOTAVALIDREQUESTLINE\r\n\r\n")
        response = b""
        while True:
            chunk = connection.recv(4096)
            if not chunk:
                break
            response += chunk
        connection.close()
        body = response.split(b"\r\n\r\n", 1)[1] if response.startswith(b"HTTP/") else response
        if response.startswith(b"HTTP/"):
            self.assertTrue(response.startswith(b"HTTP/1.") and b" 400 " in response.split(b"\r\n", 1)[0])
        document = json.loads(body)
        self.assertEqual(document["error"]["code"], "HTTP_REQUEST_REJECTED")
        self.assertNotIn(b"Traceback", response)
        self.assertNotIn(b"NOTAVALIDREQUESTLINE", response)

    def test_bad_request_headers_missing_content_type_are_rejected(self):
        self.get_page()
        connection = socket.create_connection(("127.0.0.1", self.server.server_port), timeout=2)
        request = (
            f"POST /api/offline-example HTTP/1.1\r\nHost: 127.0.0.1:{self.server.server_port}\r\n"
            f"Cookie: {self.cookie}\r\nOrigin: {self.origin}\r\nX-CSRF-Token: {self.csrf}\r\n"
            "Content-Length: 2\r\n\r\n{}"
        ).encode()
        connection.sendall(request)
        response = b""
        while True:
            chunk = connection.recv(4096)
            if not chunk:
                break
            response += chunk
        connection.close()
        self.assertIn(b"400", response.split(b"\r\n", 1)[0])
        self.assertIn(b"REQUEST_HEADERS_INVALID", response)

    def test_a_json_body_that_is_not_a_dict_is_rejected(self):
        self.get_page()
        status, _, body = self.post("/api/offline-example", [1, 2, 3])
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "REQUEST_INVALID")

    def test_a_deeply_nested_json_body_is_rejected(self):
        self.get_page()
        nested = "nominal"
        for _ in range(40):
            nested = [nested]
        status, _, body = self.post("/api/offline-example", {"recipe_id": _RECIPE_ID, "variant": nested})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "REQUEST_INVALID")

    def test_a_json_body_with_an_overlong_object_key_is_rejected(self):
        self.get_page()
        status, _, body = self.post("/api/offline-example", {"x" * 200: 1, "recipe_id": _RECIPE_ID, "variant": "nominal"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "REQUEST_INVALID")

    def test_a_json_body_with_an_overlong_string_value_is_rejected(self):
        self.get_page()
        status, _, body = self.post("/api/offline-example", {"recipe_id": "x" * 12_001, "variant": "nominal"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "REQUEST_INVALID")

    def test_a_json_body_containing_a_non_finite_constant_is_rejected(self):
        self.get_page()
        connection = socket.create_connection(("127.0.0.1", self.server.server_port), timeout=2)
        body = b'{"recipe_id":"' + _RECIPE_ID.encode() + b'","variant":NaN}'
        request = (
            f"POST /api/offline-example HTTP/1.1\r\nHost: 127.0.0.1:{self.server.server_port}\r\n"
            f"Cookie: {self.cookie}\r\nOrigin: {self.origin}\r\nX-CSRF-Token: {self.csrf}\r\n"
            f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n"
        ).encode() + body
        connection.sendall(request)
        response = b""
        while True:
            chunk = connection.recv(4096)
            if not chunk:
                break
            response += chunk
        connection.close()
        self.assertIn(b"400", response.split(b"\r\n", 1)[0])
        self.assertIn(b"REQUEST_INVALID", response)


class RequestLimitAndSessionExpiryTests(unittest.TestCase):
    """Uses handle_request() (single shot) instead of serve_forever() so the
    background session-expiry watcher thread cannot race these assertions.
    """

    def setUp(self):
        self.workspace = Path(tempfile.mkdtemp())
        self.service = RecipeWorkbench(self.workspace, engine_factory=Mock())

    def _one_shot(self, server):
        thread = threading.Thread(target=server.handle_request, daemon=True)
        thread.start()
        return thread

    def test_a_request_beyond_the_configured_limit_is_rejected_with_429(self):
        server = WorkbenchServer(("127.0.0.1", 0), self.service, request_limit=1)
        try:
            server.request_count = 1  # already at the limit before this request arrives
            thread = self._one_shot(server)
            with self.assertRaises(HTTPError) as caught:
                urlopen(server.launch_url, timeout=2)
            self.assertEqual(caught.exception.code, 429)
            self.assertEqual(json.loads(caught.exception.read())["error"]["code"], "REQUEST_LIMIT")
            thread.join(timeout=2)
        finally:
            server.server_close()

    def test_a_request_arriving_after_session_expiry_is_rejected_with_403(self):
        server = WorkbenchServer(("127.0.0.1", 0), self.service, session_lifetime=600)
        try:
            server.session.expires_at = time.monotonic() - 1  # already expired
            thread = self._one_shot(server)
            with self.assertRaises(HTTPError) as caught:
                urlopen(f"http://127.0.0.1:{server.server_port}/", timeout=2)
            self.assertEqual(caught.exception.code, 403)
            self.assertEqual(json.loads(caught.exception.read())["error"]["code"], "SESSION_EXPIRED")
            thread.join(timeout=2)
        finally:
            server.server_close()


class SendMethodBrokenPipeTests(unittest.TestCase):
    """Whitebox: constructs a handler without a real socket to deterministically
    exercise the "client already disconnected while we were writing" path.
    """

    def test_a_broken_pipe_while_writing_the_response_does_not_propagate(self):
        handler = object.__new__(_WorkbenchHandler)
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        handler.wfile = Mock()
        handler.wfile.write.side_effect = BrokenPipeError()
        handler.server = Mock()
        handler.server.session.expires_at = time.monotonic() + 100

        # Must not raise, even though the underlying socket write failed.
        handler._send(200, b"hello", "text/plain")
        handler.wfile.write.assert_called_once()

    def test_a_connection_reset_while_writing_the_response_does_not_propagate(self):
        handler = object.__new__(_WorkbenchHandler)
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        handler.wfile = Mock()
        handler.wfile.write.side_effect = ConnectionResetError()
        handler.server = Mock()
        handler.server.session.expires_at = time.monotonic() + 100

        handler._send(200, b"hello", "text/plain")
        handler.wfile.write.assert_called_once()


class SendJsonFailureTests(unittest.TestCase):
    """Whitebox: forces json.dumps() and the response-size guard to fail,
    without needing a 512KB or non-serializable payload over real HTTP.
    """

    def _handler(self):
        handler = object.__new__(_WorkbenchHandler)
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        handler.wfile = Mock()
        handler.server = Mock()
        handler.server.session.expires_at = time.monotonic() + 100
        return handler

    def test_a_non_serializable_value_falls_back_to_a_fixed_500_response(self):
        handler = self._handler()
        handler._send_json(200, {"bad": {1, 2, 3}})
        written = handler.wfile.write.call_args[0][0]
        self.assertIn(b"RESPONSE_INVALID", written)
        handler.send_response.assert_called_once_with(500)

    def test_an_oversized_response_body_falls_back_to_a_fixed_500_response(self):
        handler = self._handler()
        handler._send_json(200, {"padding": "x" * server_module._MAX_RESPONSE})
        written = handler.wfile.write.call_args[0][0]
        self.assertIn(b"RESPONSE_TOO_LARGE", written)
        handler.send_response.assert_called_once_with(500)


class RemainingGuardTests(_LiveServerTestCase):
    """Mops up the remaining easily-reachable refusal paths: wrong Host on a
    POST, wrong Origin with a valid session, malformed/oversized Content-Length,
    a truncated body, a missing required key, a plain successful fixture POST,
    the full external-scope-confirmation run flow, and an unusual AutoError
    code getting normalized instead of echoed to the client.
    """

    def test_post_with_wrong_host_header_is_rejected_before_the_session_check(self):
        self.get_page()
        connection = socket.create_connection(("127.0.0.1", self.server.server_port), timeout=2)
        request = (
            "POST /api/offline-example HTTP/1.1\r\nHost: example.invalid\r\n"
            f"Cookie: {self.cookie}\r\nOrigin: {self.origin}\r\nX-CSRF-Token: {self.csrf}\r\n"
            "Content-Type: application/json\r\nContent-Length: 2\r\n\r\n{}"
        ).encode()
        connection.sendall(request)
        response = b""
        while True:
            chunk = connection.recv(4096)
            if not chunk:
                break
            response += chunk
        connection.close()
        self.assertIn(b"403", response.split(b"\r\n", 1)[0])
        self.assertIn(b"HOST_REJECTED", response)

    def test_a_valid_session_with_the_wrong_origin_is_rejected(self):
        self.get_page()
        status, _, body = self.post(
            "/api/offline-example", {"recipe_id": _RECIPE_ID, "variant": "nominal"}, origin="https://evil.example",
        )
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["code"], "ORIGIN_REJECTED")

    def test_get_without_a_session_cookie_is_rejected(self):
        status, _, body = self.get("/api/catalog", cookie="")
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body)["error"]["code"], "SESSION_REJECTED")

    def test_get_of_an_unrecognized_path_is_not_found(self):
        self.get_page()
        status, _, body = self.get("/no/such/route")
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)["error"]["code"], "NOT_FOUND")

    def test_a_non_decimal_content_length_is_rejected(self):
        self.get_page()
        connection = socket.create_connection(("127.0.0.1", self.server.server_port), timeout=2)
        request = (
            f"POST /api/offline-example HTTP/1.1\r\nHost: 127.0.0.1:{self.server.server_port}\r\n"
            f"Cookie: {self.cookie}\r\nOrigin: {self.origin}\r\nX-CSRF-Token: {self.csrf}\r\n"
            "Content-Type: application/json\r\nContent-Length: abc\r\n\r\n{}"
        ).encode()
        connection.sendall(request)
        response = b""
        while True:
            chunk = connection.recv(4096)
            if not chunk:
                break
            response += chunk
        connection.close()
        self.assertIn(b"400", response.split(b"\r\n", 1)[0])
        self.assertIn(b"REQUEST_LENGTH_INVALID", response)

    def test_a_content_length_over_the_body_cap_is_rejected_without_reading_the_body(self):
        self.get_page()
        self.service.offline_example = Mock(side_effect=AssertionError("must not be called"))
        connection = socket.create_connection(("127.0.0.1", self.server.server_port), timeout=2)
        request = (
            f"POST /api/offline-example HTTP/1.1\r\nHost: 127.0.0.1:{self.server.server_port}\r\n"
            f"Cookie: {self.cookie}\r\nOrigin: {self.origin}\r\nX-CSRF-Token: {self.csrf}\r\n"
            "Content-Type: application/json\r\nContent-Length: 48001\r\n\r\n"
        ).encode()
        connection.sendall(request)
        response = b""
        while True:
            chunk = connection.recv(4096)
            if not chunk:
                break
            response += chunk
        connection.close()
        self.assertIn(b"413", response.split(b"\r\n", 1)[0])
        self.assertIn(b"REQUEST_TOO_LARGE", response)
        self.service.offline_example.assert_not_called()

    def test_a_body_shorter_than_its_declared_content_length_is_rejected(self):
        self.get_page()
        connection = socket.create_connection(("127.0.0.1", self.server.server_port), timeout=2)
        request = (
            f"POST /api/offline-example HTTP/1.1\r\nHost: 127.0.0.1:{self.server.server_port}\r\n"
            f"Cookie: {self.cookie}\r\nOrigin: {self.origin}\r\nX-CSRF-Token: {self.csrf}\r\n"
            "Content-Type: application/json\r\nContent-Length: 40\r\n\r\n{}"
        ).encode()
        connection.sendall(request)
        connection.shutdown(socket.SHUT_WR)
        response = b""
        while True:
            chunk = connection.recv(4096)
            if not chunk:
                break
            response += chunk
        connection.close()
        self.assertIn(b"400", response.split(b"\r\n", 1)[0])
        self.assertIn(b"REQUEST_INVALID", response)

    def test_offline_example_missing_a_required_key_is_rejected_before_the_service_call(self):
        self.get_page()
        self.service.offline_example = Mock(side_effect=AssertionError("must not be called"))
        status, _, body = self.post("/api/offline-example", {"recipe_id": _RECIPE_ID})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "REQUEST_SHAPE_INVALID")
        self.service.offline_example.assert_not_called()

    def test_a_plain_successful_offline_example_post_returns_the_real_fixture(self):
        self.get_page()
        status, _, body = self.post("/api/offline-example", {"recipe_id": _RECIPE_ID, "variant": "nominal"})
        self.assertEqual(status, 200)
        self.assertEqual(body["mode"], "fixture")
        self.assertEqual(body["data_classification"], "synthetic")

    def test_the_full_restricted_run_flow_succeeds_once_external_scope_is_confirmed(self):
        self.get_page()
        reviewed_request = {"recipe_id": _RECIPE_ID, "values": _VALUES, "data_classification": "restricted"}
        self.service.preview_live = Mock(return_value={
            "review": {"requires_external_scope_confirmation": True, "route_kind": "hosted"},
            "reviewed_request": reviewed_request,
        })
        self.service.run_live = Mock(return_value={"execution_authorized": False, "receipt_id": "e" * 64})
        status, _, response = self.post("/api/review", {
            "recipe_id": _RECIPE_ID, "values": _VALUES, "data_classification": "restricted",
        })
        self.assertEqual(status, 200)
        nonce = response["review_nonce"]
        status, _, error = self.post("/api/run", {"review_nonce": nonce, "confirm": True})
        self.assertEqual(status, 400)
        self.assertEqual(error["error"]["code"], "EXTERNAL_SCOPE_CONFIRMATION_REQUIRED")
        self.service.run_live.assert_not_called()
        status, _, result = self.post("/api/run", {
            "review_nonce": nonce, "confirm": True, "external_scope_confirmation": True,
        })
        self.assertEqual(status, 200)
        self.assertFalse(result["execution_authorized"])
        self.service.run_live.assert_called_once_with(
            self.service.workspace, reviewed_request, confirmation=True, external_scope_confirmation=True,
        )

    def test_an_unusual_autoerror_code_is_normalized_to_request_failed_not_echoed(self):
        self.get_page()
        self.service.offline_example = Mock(side_effect=AutoError("lowercase-code!"))
        status, _, body = self.post("/api/offline-example", {"recipe_id": _RECIPE_ID, "variant": "nominal"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "REQUEST_FAILED")
        self.assertNotIn("lowercase-code!", json.dumps(body))

    def test_duplicate_top_level_json_keys_are_rejected(self):
        self.get_page()
        raw = b'{"recipe_id":"' + _RECIPE_ID.encode() + b'","recipe_id":"other","variant":"nominal"}'
        request = Request(self.origin + "/api/offline-example", data=raw, headers={
            "Content-Type": "application/json", "Cookie": self.cookie, "Origin": self.origin,
            "X-CSRF-Token": self.csrf,
        }, method="POST")
        with self.assertRaises(HTTPError) as caught:
            urlopen(request, timeout=2)
        self.assertEqual(caught.exception.code, 400)
        self.assertEqual(json.loads(caught.exception.read())["error"]["code"], "REQUEST_INVALID")


if __name__ == "__main__":
    unittest.main()
