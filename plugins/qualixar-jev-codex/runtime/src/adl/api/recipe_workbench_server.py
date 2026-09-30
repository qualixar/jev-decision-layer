"""Single-user, loopback-only HTTP boundary for the local recipe workbench."""

from __future__ import annotations

import hmac
import json
import secrets
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from jev_auto.common import AutoError


_MAX_BODY = 48_000
_MAX_RESPONSE = 512_000
_MAX_REQUESTS = 240
_MAX_REVIEWS = 8
_REVIEW_LIFETIME = 120
_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
    ),
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
}
_ERROR_STATUS = {
    "WORKSPACE_NOT_ENROLLED": 409,
    "AUTO_DISABLED_OR_EXPIRED": 409,
    "RECIPE_NOT_ENROLLED": 409,
    "POLICY_CHANGED": 409,
    "WORKBENCH_REVIEW_CHANGED": 409,
    "REQUEST_BYTE_BUDGET": 413,
    "REQUEST_TOO_LARGE": 413,
    "EVIDENCE_NOT_FOUND": 404,
    "RECIPE_NOT_FOUND": 404,
    "RECIPE_INPUT_INVALID": 400,
    "DATA_CLASSIFICATION": 400,
    "EXTERNAL_SCOPE_CONFIRMATION_REQUIRED": 400,
    "WORKBENCH_CONFIRMATION_REQUIRED": 400,
}


@dataclass
class _BrowserSession:
    token: str
    csrf: str
    expires_at: float


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in pairs:
        if key in output:
            raise ValueError("DUPLICATE_JSON_KEY")
        output[key] = value
    return output


def _reject_constant(_value: str) -> None:
    raise ValueError("NONFINITE_JSON")


def _validate_json_shape(value: Any) -> None:
    pending = [(value, 0)]
    nodes = 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if nodes > 20_000 or depth > 32:
            raise ValueError("REQUEST_SHAPE_INVALID")
        if isinstance(item, dict):
            if any(not isinstance(key, str) or len(key) > 128 for key in item):
                raise ValueError("REQUEST_SHAPE_INVALID")
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)
        elif isinstance(item, str):
            if len(item) > 12_000:
                raise ValueError("REQUEST_TOO_LARGE")
        elif item is not None and not isinstance(item, (int, float, bool)):
            raise ValueError("REQUEST_SHAPE_INVALID")


class WorkbenchServer(HTTPServer):
    """Bind a workbench service to one exact loopback browser session."""

    allow_reuse_address = False

    def __init__(
        self,
        address: tuple[str, int],
        service: Any,
        *,
        session_lifetime: int = 600,
        request_limit: int = _MAX_REQUESTS,
    ) -> None:
        if not isinstance(address, tuple) or len(address) != 2 or address[0] != "127.0.0.1":
            raise ValueError("WORKBENCH_LOOPBACK_ONLY")
        if not isinstance(session_lifetime, int) or isinstance(session_lifetime, bool) or not 1 <= session_lifetime <= 3600:
            raise ValueError("WORKBENCH_SESSION_LIFETIME")
        if not isinstance(request_limit, int) or isinstance(request_limit, bool) or not 1 <= request_limit <= 10_000:
            raise ValueError("WORKBENCH_REQUEST_LIMIT")
        self.service = service
        self.session_lifetime = session_lifetime
        self.request_limit = request_limit
        self.request_count = 0
        self.reviews: dict[str, tuple[dict[str, Any], float, bool]] = {}
        self._review_lock = threading.Lock()
        super().__init__(address, _WorkbenchHandler)
        self.session = _BrowserSession(
            token=secrets.token_urlsafe(32),
            csrf=secrets.token_urlsafe(32),
            expires_at=time.monotonic() + session_lifetime,
        )
        # 256-bit, single use: the first GET of the launch link gets the cookie.
        self._launch_token = secrets.token_urlsafe(32)
        self._launch_used = False
        self._launch_lock = threading.Lock()

    @property
    def launch_path(self) -> str:
        return f"/?t={self._launch_token}"

    @property
    def launch_url(self) -> str:
        """The private link for the browser opener. Never print it to a pipe."""
        return f"http://127.0.0.1:{self.server_port}{self.launch_path}"

    def redeem_launch_token(self, supplied: str) -> bool:
        """True exactly once, for the exact token; every replay is refused."""
        if not isinstance(supplied, str) or not supplied.isascii():
            return False
        with self._launch_lock:
            if self._launch_used or not hmac.compare_digest(supplied.encode("ascii"), self._launch_token.encode("ascii")):
                return False
            self._launch_used = True
            return True

    def server_close(self) -> None:
        with self._review_lock:
            self.reviews.clear()
        super().server_close()

    def serve_forever(self, poll_interval: float = 0.5) -> None:
        """Stop serving and close the listener when this browser session expires."""
        stop_watcher = threading.Event()

        def expire_session() -> None:
            while True:
                remaining = self.session.expires_at - time.monotonic()
                if remaining <= 0:
                    self.shutdown()
                    self.server_close()
                    return
                if stop_watcher.wait(min(remaining, 0.25)):
                    return

        watcher = threading.Thread(target=expire_session, daemon=True, name="jev-workbench-session-expiry")
        watcher.start()
        try:
            super().serve_forever(poll_interval=poll_interval)
        finally:
            stop_watcher.set()
            watcher.join(timeout=1)


class _WorkbenchHandler(BaseHTTPRequestHandler):
    server: WorkbenchServer
    protocol_version = "HTTP/1.1"

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(5)

    def log_message(self, _format: str, *_args: object) -> None:
        # Request paths, headers, bodies and browser content stay out of logs.
        return

    def handle_expect_100(self) -> bool:
        self.close_connection = True
        self.send_error(417)
        return False

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        # Parser and unsupported-method errors must keep the same no-detail,
        # no-log response policy as application routes.
        self.close_connection = True
        self._send_json(code, {"error": {"code": "HTTP_REQUEST_REJECTED", "message": "The local workbench rejected this request."}}, extra={"Connection": "close"})

    def do_GET(self) -> None:
        if not self._begin_request() or not self._host_ok():
            return
        path = self.path
        route, separator, query = path.partition("?")
        if route == "/":
            # The browser holding the cookie may reload; anyone else needs the
            # unused launch link. Nothing else gets the cookie or the CSRF value.
            if not self._cookie_token_ok() and not self._redeem_launch_link(separator, query):
                self._send_json(403, {"error": {"code": "SESSION_REJECTED", "message": "Restart the local workbench and open its new address."}})
                return
            if self._count_request():
                self._send_page()
            return
        if not self._session_ok(require_csrf=False):
            return
        if path == "/api/catalog":
            try:
                self._send_json(200, {"recipes": self.server.service.list_cards()})
            except AutoError as error:
                self._send_error_code(str(error))
            except Exception:
                self._send_internal_error()
            return
        if path in ("/app.css", "/app.js"):
            self._send_asset(path)
            return
        prefix = "/api/receipt/"
        if path.startswith(prefix) and len(path) == len(prefix) + 64:
            receipt_id = path[len(prefix):]
            if not all(char in "0123456789abcdef" for char in receipt_id):
                self._send_json(404, {"error": {"code": "NOT_FOUND", "message": "That receipt was not found."}})
                return
            try:
                receipt = self.server.service.read_receipt(self.server.service.workspace, receipt_id)
            except AutoError as error:
                self._send_error_code(str(error))
                return
            except Exception:
                self._send_internal_error()
                return
            self._send_json(200, receipt)
            return
        self._send_json(404, {"error": {"code": "NOT_FOUND", "message": "That page was not found."}})

    def do_POST(self) -> None:
        if not self._begin_request() or not self._host_ok():
            return
        if not self._session_ok(require_csrf=True):
            return
        body = self._read_json()
        if body is None:
            return
        try:
            if self.path == "/api/offline-example":
                self._require_keys(body, {"recipe_id", "variant"})
                result = self.server.service.offline_example(body["recipe_id"], body["variant"])
                self._send_json(200, result)
                return
            if self.path == "/api/review":
                self._require_keys(body, {"recipe_id", "values", "data_classification"})
                result = self.server.service.preview_live(
                    self.server.service.workspace,
                    body["recipe_id"],
                    body["values"],
                    body["data_classification"],
                )
                reviewed_request = result.get("reviewed_request")
                review = result.get("review")
                if not isinstance(reviewed_request, dict) or not isinstance(review, dict):
                    raise AutoError("WORKBENCH_REVIEW_INVALID")
                nonce = secrets.token_urlsafe(32)
                requires_external = review.get("requires_external_scope_confirmation") is True
                with self.server._review_lock:
                    now = time.monotonic()
                    self.server.reviews = {key: value for key, value in self.server.reviews.items() if value[1] > now}
                    if len(self.server.reviews) >= _MAX_REVIEWS:
                        raise AutoError("WORKBENCH_REVIEW_LIMIT")
                    self.server.reviews[nonce] = (reviewed_request, now + _REVIEW_LIFETIME, requires_external)
                self._send_json(200, {"review": review, "review_nonce": nonce, "expires_in_seconds": _REVIEW_LIFETIME})
                return
            if self.path == "/api/run":
                allowed = {"review_nonce", "confirm", "external_scope_confirmation"}
                if not set(body) <= allowed or not {"review_nonce", "confirm"} <= set(body):
                    raise AutoError("REQUEST_SHAPE_INVALID")
                if body["confirm"] is not True:
                    raise AutoError("WORKBENCH_CONFIRMATION_REQUIRED")
                if "external_scope_confirmation" in body and not isinstance(body["external_scope_confirmation"], bool):
                    raise AutoError("REQUEST_SHAPE_INVALID")
                nonce = body["review_nonce"]
                if not isinstance(nonce, str) or not 32 <= len(nonce) <= 128:
                    raise AutoError("WORKBENCH_REVIEW_EXPIRED")
                with self.server._review_lock:
                    record = self.server.reviews.get(nonce)
                if record is None or record[1] <= time.monotonic():
                    with self.server._review_lock:
                        self.server.reviews.pop(nonce, None)
                    raise AutoError("WORKBENCH_REVIEW_EXPIRED")
                reviewed_request, _expires_at, requires_external = record
                external_confirm = body.get("external_scope_confirmation") is True
                if requires_external and not external_confirm:
                    raise AutoError("EXTERNAL_SCOPE_CONFIRMATION_REQUIRED")
                with self.server._review_lock:
                    if self.server.reviews.get(nonce) is not record:
                        raise AutoError("WORKBENCH_REVIEW_EXPIRED")
                    self.server.reviews.pop(nonce)
                result = self.server.service.run_live(
                    self.server.service.workspace,
                    reviewed_request,
                    confirmation=True,
                    external_scope_confirmation=external_confirm,
                )
                self._send_json(200, result)
                return
            self._send_json(404, {"error": {"code": "NOT_FOUND", "message": "That action was not found."}})
        except AutoError as error:
            self._send_error_code(str(error))
        except (ValueError, TypeError, KeyError, RecursionError):
            self._send_json(400, {"error": {"code": "REQUEST_INVALID", "message": "Check the request and try again."}})
        except Exception:
            self._send_internal_error()

    def do_PUT(self) -> None:
        self._method_not_allowed()

    def do_DELETE(self) -> None:
        self._method_not_allowed()

    def do_PATCH(self) -> None:
        self._method_not_allowed()

    def _method_not_allowed(self) -> None:
        if self._begin_request() and self._host_ok():
            self.close_connection = True
            self._send_json(405, {"error": {"code": "METHOD_NOT_ALLOWED", "message": "That method is not available."}}, extra={"Allow": "GET, POST", "Connection": "close"})

    def _count_request(self) -> bool:
        """Count only requests that carry this browser's session, so another
        local program cannot use up the limit and lock the user out."""
        self.server.request_count += 1
        if self.server.request_count > self.server.request_limit:
            self.close_connection = True
            self._send_json(429, {"error": {"code": "REQUEST_LIMIT", "message": "Restart the local workbench to continue."}}, extra={"Connection": "close"})
            return False
        return True

    def _begin_request(self) -> bool:
        if time.monotonic() >= self.server.session.expires_at:
            self.close_connection = True
            self._send_json(403, {"error": {"code": "SESSION_EXPIRED", "message": "This workbench session expired. Restart it to continue."}}, extra={"Connection": "close"})
            return False
        return True

    def _host_ok(self) -> bool:
        hosts = self.headers.get_all("Host", [])
        expected = f"127.0.0.1:{self.server.server_port}"
        if len(hosts) != 1 or hosts[0] != expected:
            self.close_connection = True
            self._send_json(403, {"error": {"code": "HOST_REJECTED", "message": "Open the local workbench from its original address."}}, extra={"Connection": "close"})
            return False
        return True

    def _cookie_token_ok(self) -> bool:
        cookies = self.headers.get_all("Cookie", [])
        tokens = []
        for header in cookies:
            for part in header.split(";"):
                name, separator, value = part.strip().partition("=")
                if name == "jev_workbench":
                    tokens.append(value if separator else "")
        token = tokens[0] if len(tokens) == 1 else None
        return (isinstance(token, str) and token.isascii() and bool(self.server.session.token)
                and hmac.compare_digest(token.encode("ascii"), self.server.session.token.encode("ascii")))

    def _redeem_launch_link(self, separator: str, query: str) -> bool:
        """Accept only `/?t=<token>`, once."""
        if not separator:
            return False
        try:
            fields = parse_qs(query, keep_blank_values=True, strict_parsing=True, max_num_fields=1)
        except ValueError:
            return False
        tokens = fields.get("t", [])
        return set(fields) == {"t"} and len(tokens) == 1 and self.server.redeem_launch_token(tokens[0])

    def _session_ok(self, *, require_csrf: bool) -> bool:
        if not self._cookie_token_ok():
            if require_csrf:
                self.close_connection = True
            self._send_json(
                403,
                {"error": {"code": "SESSION_REJECTED", "message": "Restart the local workbench and open its new address."}},
                extra={"Connection": "close"} if require_csrf else None,
            )
            return False
        if require_csrf:
            origins = self.headers.get_all("Origin", [])
            csrf_values = self.headers.get_all("X-CSRF-Token", [])
            fetch_sites = self.headers.get_all("Sec-Fetch-Site", [])
            expected_origin = f"http://127.0.0.1:{self.server.server_port}"
            if (
                len(origins) != 1 or origins[0] != expected_origin
                or len(csrf_values) != 1
                or not csrf_values[0].isascii()
                or not hmac.compare_digest(csrf_values[0].encode("ascii"), self.server.session.csrf.encode("ascii"))
                or len(fetch_sites) > 1
                or (fetch_sites and fetch_sites[0] != "same-origin")
            ):
                self.close_connection = True
                self._send_json(403, {"error": {"code": "ORIGIN_REJECTED", "message": "The request did not come from this workbench page."}}, extra={"Connection": "close"})
                return False
        return self._count_request()

    def _read_json(self) -> dict[str, Any] | None:
        transfer = self.headers.get_all("Transfer-Encoding", [])
        lengths = self.headers.get_all("Content-Length", [])
        content_types = self.headers.get_all("Content-Type", [])
        if transfer or len(lengths) != 1 or len(content_types) != 1 or content_types[0].lower() != "application/json":
            self._reject_body(400, "REQUEST_HEADERS_INVALID", "The request format is not supported.")
            return None
        length_text = lengths[0]
        if not length_text.isascii() or not length_text.isdecimal() or len(length_text) > 8:
            self._reject_body(400, "REQUEST_LENGTH_INVALID", "The request length is invalid.")
            return None
        try:
            length = int(length_text)
        except ValueError:
            self._reject_body(400, "REQUEST_LENGTH_INVALID", "The request length is invalid.")
            return None
        if length > _MAX_BODY:
            self._reject_body(413, "REQUEST_TOO_LARGE", "This request is too large for the workbench.")
            return None
        try:
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError("BODY_LENGTH_MISMATCH")
            decoded = raw.decode("utf-8", errors="strict")
            value = json.loads(decoded, object_pairs_hook=_reject_duplicate_keys, parse_constant=_reject_constant)
            _validate_json_shape(value)
            if not isinstance(value, dict):
                raise ValueError("REQUEST_SHAPE_INVALID")
            return value
        except (UnicodeError, ValueError, RecursionError, OSError):
            self._reject_body(400, "REQUEST_INVALID", "Check the request and try again.")
            return None

    def _reject_body(self, status: int, code: str, message: str) -> None:
        self.close_connection = True
        self._send_json(status, {"error": {"code": code, "message": message}}, extra={"Connection": "close"})

    @staticmethod
    def _require_keys(body: dict[str, Any], required: set[str]) -> None:
        if set(body) != required:
            raise AutoError("REQUEST_SHAPE_INVALID")

    def _send_page(self) -> None:
        page_path = Path(__file__).resolve().parent / "workbench" / "index.html"
        try:
            if page_path.is_symlink() or page_path.stat().st_size > 32_000:
                raise OSError
            body = page_path.read_bytes()
            marker = b'<meta name="jev-csrf" content="">'
            if body.count(marker) != 1:
                raise OSError
            body = body.replace(marker, f'<meta name="jev-csrf" content="{self.server.session.csrf}">'.encode())
        except OSError:
            self._send_json(500, {"error": {"code": "WORKBENCH_UI_UNAVAILABLE", "message": "The local workbench page could not be loaded."}})
            return
        self._send(200, body, "text/html; charset=utf-8", cookie=True)

    def _send_asset(self, route: str) -> None:
        allowed = {"/app.css": ("app.css", "text/css; charset=utf-8", 40_000),
                   "/app.js": ("app.js", "text/javascript; charset=utf-8", 40_000)}
        name, content_type, limit = allowed[route]
        asset = Path(__file__).resolve().parent / "workbench" / name
        try:
            if asset.is_symlink() or asset.stat().st_size > limit:
                raise OSError
            body = asset.read_bytes()
        except OSError:
            self._send_json(500, {"error": {"code": "WORKBENCH_ASSET_UNAVAILABLE", "message": "A local workbench asset could not be loaded."}})
            return
        self._send(200, body, content_type)

    def _send_error_code(self, code: str) -> None:
        if not code or len(code) > 64 or any(char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_" for char in code):
            code = "REQUEST_FAILED"
        status = _ERROR_STATUS.get(code, 400)
        message = {
            "WORKSPACE_NOT_ENROLLED": "This workspace has not been enrolled in Jev yet.",
            "AUTO_DISABLED_OR_EXPIRED": "Workspace permission is disabled or expired.",
            "POLICY_CHANGED": "Workspace settings changed. Review the request again.",
            "WORKBENCH_REVIEW_CHANGED": "The reviewed request changed. Review it again before running.",
            "WORKBENCH_CONFIRMATION_REQUIRED": "Confirm this one request to continue.",
            "EXTERNAL_SCOPE_CONFIRMATION_REQUIRED": "Confirm the hosted data scope before continuing.",
            "RECIPE_INPUT_INVALID": "Complete each required field within its stated length.",
            "REQUEST_BYTE_BUDGET": "This request exceeds the workspace request limit.",
            "REQUEST_TOO_LARGE": "This request is too large for the workbench.",
            "EVIDENCE_NOT_FOUND": "That receipt was not found in this workspace.",
        }.get(code, "The request could not be completed. Review it and try again.")
        self._send_json(status, {"error": {"code": code, "message": message}})

    def _send_internal_error(self) -> None:
        self._send_json(500, {"error": {"code": "REQUEST_FAILED", "message": "The local workbench could not complete this request."}})

    def _send_json(self, status: int, value: Any, *, extra: dict[str, str] | None = None) -> None:
        try:
            body = json.dumps(value, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        except (TypeError, ValueError, RecursionError):
            status = 500
            body = b'{"error":{"code":"RESPONSE_INVALID","message":"The local response could not be displayed."}}'
        if len(body) > _MAX_RESPONSE:
            status = 500
            body = b'{"error":{"code":"RESPONSE_TOO_LARGE","message":"The local response could not be displayed."}}'
        self._send(status, body, "application/json; charset=utf-8", extra=extra)

    def _send(self, status: int, body: bytes, content_type: str, *, cookie: bool = False,
              extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in _SECURITY_HEADERS.items():
            self.send_header(key, value)
        if cookie:
            max_age = max(0, int(self.server.session.expires_at - time.monotonic()))
            self.send_header("Set-Cookie", f"jev_workbench={self.server.session.token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={max_age}")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            return
