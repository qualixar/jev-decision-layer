"""Source-only local management contract; no HTTP mutation route exists yet.

The model-facing entrypoint is read-only. A future HTTP server must derive UI
identity from its own transport and keep SetupSession private, never accept a
caller label or session authority object from request data.
"""

from __future__ import annotations

import hmac
import secrets
import time
from dataclasses import dataclass
from typing import Any


ALLOWED_ORIGIN = "http://127.0.0.1"
MCP_FORBIDDEN = {"/v1/consents", "/v1/recipes/activate", "/v1/install/apply"}


@dataclass(frozen=True)
class SetupSession:
    """Server-held browser session; never returned by MCP or status responses."""

    port: int
    session: str
    csrf: str
    expires_at: float

    @classmethod
    def create(cls, *, port: int, lifetime_seconds: int = 600) -> SetupSession:
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            raise ValueError("INVALID_SETUP_PORT")
        if not isinstance(lifetime_seconds, int) or not 1 <= lifetime_seconds <= 3600:
            raise ValueError("INVALID_SETUP_LIFETIME")
        return cls(port, secrets.token_urlsafe(32), secrets.token_urlsafe(32), time.monotonic() + lifetime_seconds)

    def matches(self, *, session: str | None, csrf: str) -> bool:
        return (
            time.monotonic() < self.expires_at
            and isinstance(session, str)
            and isinstance(csrf, str)
            and hmac.compare_digest(session, self.session)
            and hmac.compare_digest(csrf, self.csrf)
        )


def _canonical_path(path: str) -> bool:
    return (
        isinstance(path, str)
        and path.startswith("/v1/")
        and not path.endswith("/")
        and "//" not in path
        and not any(char in path for char in ("%", "\\", "?", "#", ";"))
    )


def _status() -> dict[str, Any]:
    return {"status": 200, "runtime": "local", "credentials": "not_exposed", "browser_storage": []}


def request(
    method: str,
    path: str,
    *,
    origin: str,
    host: str = "127.0.0.1",
    csrf: str,
    session: str | None = None,
) -> dict[str, Any]:
    """Model-facing read-only facade; caller cannot claim to be the UI."""
    if not _canonical_path(path):
        return {"status": 403, "code": "NON_CANONICAL_PATH"}
    if origin != ALLOWED_ORIGIN or host != "127.0.0.1" or not isinstance(csrf, str) or not csrf:
        return {"status": 403, "code": "CROSS_ORIGIN_REJECTED"}
    if path in MCP_FORBIDDEN or method != "GET":
        return {"status": 403, "code": "MCP_CANNOT_CREATE_CONSENT"}
    if not session:
        return {"status": 401, "code": "SESSION_REQUIRED"}
    return _status() if path == "/v1/status" else {"status": 404, "code": "ROUTE_NOT_FOUND"}


def request_ui(
    method: str,
    path: str,
    *,
    origin: str,
    host: str = "127.0.0.1",
    csrf: str,
    session: str | None = None,
    authority: SetupSession | None = None,
) -> dict[str, Any]:
    """Future browser-server entrypoint; valid POSTs still have no route."""
    if not _canonical_path(path):
        return {"status": 403, "code": "NON_CANONICAL_PATH"}
    expected_origin = f"http://127.0.0.1:{authority.port}" if authority is not None else ALLOWED_ORIGIN
    expected_host = f"127.0.0.1:{authority.port}" if authority is not None else "127.0.0.1"
    if origin != expected_origin or host != expected_host or not isinstance(csrf, str) or not csrf:
        return {"status": 403, "code": "CROSS_ORIGIN_REJECTED"}
    if not session:
        return {"status": 401, "code": "SESSION_REQUIRED"}
    if authority is None:
        return {"status": 403, "code": "SESSION_AUTHORITY_REQUIRED"}
    if not authority.matches(session=session, csrf=csrf):
        return {"status": 403, "code": "SESSION_INVALID"}
    return _status() if method == "GET" and path == "/v1/status" else {"status": 404, "code": "ROUTE_NOT_FOUND"}
