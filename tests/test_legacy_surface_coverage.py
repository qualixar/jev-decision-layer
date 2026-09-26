"""Coverage-closing tests for the legacy jevkit live path and the first-run setup wizard.

Targets (baseline coverage in parentheses): jevkit/client.py (22%), jevkit/web_server.py (0%),
jevkit/credentials.py (0%), src/adl/providers/laya_worker.py (19%, SandboxedLayaProcess),
src/adl/api/setup_server.py (72%), src/adl/api/setup_controller.py (74%),
src/adl/api/__init__.py (49%), src/adl/queries/typed.py (73%).

Absolute safety, honored throughout this file:
  - ZERO real network. jevkit/client.py's `transport` and `send` parameters are always a local
    fake/mock; SandboxedLayaProcess's `subprocess.Popen` is always patched to a fake process
    talking over real (but purely local) os.pipe() file descriptors -- never a real sandboxed
    child process, never laya_mlx/mlx (never imported at module scope; not installed here).
  - ZERO real macOS Keychain. Every SetupController/SetupServer test supplies an in-memory
    keychain stand-in; `security` is never exec'd.
  - Every server binds to 127.0.0.1 on port 0 (OS-assigned) and is always shut down + joined,
    even on failure, via addCleanup/try-finally.
  - All writes go through tempfile.TemporaryDirectory(); nothing touches
    ~/.local/state/qualixar-jev-decision-layer or any other real state.

House style: stdlib only (unittest, unittest.mock); ROOT/RUNTIME/sys.path.insert as in
tests/test_core_contracts.py and tests/test_setup_smoke.py; self.subTest for tables; every test
class carries a docstring saying why it exists.
"""

from __future__ import annotations

import dataclasses
import email.message
import hashlib
import http.client
import io
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch
from urllib.parse import urlencode


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "plugins" / "qualixar-jev-decision-layer" / "runtime"
sys.path.insert(0, str(RUNTIME))


# ============================================================================================
# Shared, file-local test infrastructure. Nothing here is product code.
# ============================================================================================

class _FakeKeychainStore:
    """In-memory stand-in for src.adl.api.keychain.MacKeychain -- never touches the real
    macOS Keychain. Separate small class from test_setup_smoke.py's _MemoryKeychain (that file
    is not imported here, per the "do not edit an existing test file" rule -- this is its own,
    file-local equivalent)."""

    def __init__(self):
        self._values: dict[str, str] = {}

    def get(self, provider: str) -> str:
        from src.adl.api.keychain import KeychainError

        try:
            return self._values[provider]
        except KeyError:
            raise KeychainError("KEYCHAIN_ITEM_MISSING") from None

    def put(self, provider: str, value: str) -> None:
        self._values[provider] = value

    def delete(self, provider: str) -> None:
        self._values.pop(provider, None)


def _http_request(port: int, method: str, path: str, *, values=None, cookie=None, extra_headers=None):
    """One-shot HTTP request against a loopback test server, mirroring the pattern already
    used by tests/test_setup_smoke.py (not imported; reproduced locally)."""
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    body = urlencode(values).encode() if values is not None else None
    headers = dict(extra_headers or {})
    if body is not None:
        headers.setdefault("Origin", f"http://127.0.0.1:{port}")
        headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
    if cookie:
        headers["Cookie"] = cookie
    connection.request(method, path, body=body, headers=headers)
    response = connection.getresponse()
    result = response.status, dict(response.getheaders()), response.read()
    connection.close()
    return result


def _raw_http_request(port: int, method: str, path: str, headers: dict, body: bytes = b"") -> tuple[int, bytes]:
    """Sends a hand-built HTTP/1.1 request over a real loopback TCP socket. Used only where
    http.client's own header normalization would make it impossible to send a deliberately
    malformed request (missing/garbled Content-Length, a bare Transfer-Encoding, etc) --
    needed to exercise setup_server.py's own defenses against exactly such requests."""
    lines = [f"{method} {path} HTTP/1.1", f"Host: 127.0.0.1:{port}"]
    for name, value in headers.items():
        lines.append(f"{name}: {value}")
    lines.append("Connection: close")
    head = ("\r\n".join(lines) + "\r\n\r\n").encode()
    with socket.create_connection(("127.0.0.1", port), timeout=3) as sock:
        sock.sendall(head + body)
        sock.settimeout(3)
        chunks = []
        try:
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
        except OSError:
            pass
    raw = b"".join(chunks)
    header_blob, _, remaining_body = raw.partition(b"\r\n\r\n")
    status_line = header_blob.split(b"\r\n", 1)[0]
    status = int(status_line.split(b" ")[1])
    return status, remaining_body


# ============================================================================================
# jevkit/credentials.py -- backward-compatible direct-TypeSafe credential helpers (0% baseline)
# ============================================================================================

class CredentialsBackwardCompatTests(unittest.TestCase):
    """jevkit/credentials.py is a two-function backward-compat shim over jevkit/providers.py.
    Before this file it had zero coverage: nothing had ever called credential_path() or
    get_credential() through this module."""

    def test_credential_path_matches_the_typesafe_provider_path_under_a_scratch_config_root(self):
        from jevkit.credentials import credential_path

        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"XDG_CONFIG_HOME": directory}):
                path = credential_path()
            self.assertEqual(path, Path(directory) / "qualixar-jev-decision-layer" / "typesafe-api-key")

    def test_get_credential_reads_the_typesafe_env_var_without_touching_disk(self):
        from jevkit.credentials import get_credential

        with patch.dict(os.environ, {"TYPESAFE_API_KEY": "unit-test-fake-typesafe-key"}):
            with patch("jevkit.providers.default_config_root", side_effect=AssertionError(
                    "must not touch the filesystem when the env var already satisfies the credential")):
                self.assertEqual(get_credential(), "unit-test-fake-typesafe-key")

    def test_get_credential_propagates_the_fixed_no_credential_error(self):
        from jevkit.credentials import get_credential
        from jevkit.security import SafeError

        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"XDG_CONFIG_HOME": directory, "TYPESAFE_API_KEY": ""}):
                with self.assertRaisesRegex(SafeError, "NO_CREDENTIAL"):
                    get_credential()


# ============================================================================================
# jevkit/web_server.py -- read-only loopback evidence viewer (0% baseline)
# ============================================================================================

def _build_web_root(root: Path, *, runs=(), symlink_runs_dir=False, symlink_index=False):
    """A minimal but real on-disk layout satisfying web_server.py's ROOT-relative reads:
    use_cases/catalog.json (for engine.catalog()) and web/{index.html,app.js,styles.css}."""
    (root / "use_cases").mkdir(parents=True, exist_ok=True)
    (root / "use_cases" / "catalog.json").write_text("[]")
    web = root / "web"
    web.mkdir(parents=True, exist_ok=True)
    real_index = web / "_real_index.html"
    real_index.write_text("<html>synthetic</html>")
    if symlink_index:
        (web / "index.html").symlink_to(real_index)
    else:
        (web / "index.html").write_text("<html>synthetic</html>")
    (web / "app.js").write_text("// synthetic")
    (web / "styles.css").write_text("/* synthetic */")
    runs_dir = root / "artifacts" / "runs"
    if symlink_runs_dir:
        real_runs = root / "artifacts" / "_real_runs"
        real_runs.mkdir(parents=True)
        (root / "artifacts").mkdir(exist_ok=True)
        runs_dir.symlink_to(real_runs)
    else:
        runs_dir.mkdir(parents=True, exist_ok=True)
        for name, record in runs:
            (runs_dir / name).write_text(json.dumps(record))
    return root


class WebServerRecordsTests(unittest.TestCase):
    """records() is the ONLY filter between whatever lands in artifacts/runs/*.json and a
    read-only viewer with no authentication. Before this file nothing called it: the
    schema_version/data_classification/mode allow-list, the screen() re-check, the symlinked
    runs-directory refusal, and silent skip-on-SafeError were all unverified."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_symlinked_runs_directory_yields_no_records_not_a_crash(self):
        from jevkit.web_server import records

        _build_web_root(self.root, symlink_runs_dir=True)
        self.assertEqual(records(self.root), [])

    def test_only_synthetic_fixture_or_live_schema_v1_records_are_returned(self):
        from jevkit.web_server import records

        good = {"schema_version": 1, "data_classification": "synthetic", "mode": "fixture", "note": "ok"}
        good_live = {"schema_version": 1, "data_classification": "synthetic", "mode": "live", "note": "ok"}
        wrong_classification = {**good, "data_classification": "user-approved"}
        wrong_mode = {**good, "mode": "custom"}
        wrong_schema = {**good, "schema_version": 2}
        not_a_dict = ["not", "a", "dict"]
        _build_web_root(self.root, runs=(
            ("a-good.json", good), ("b-good-live.json", good_live),
            ("c-bad-classification.json", wrong_classification),
            ("d-bad-mode.json", wrong_mode), ("e-bad-schema.json", wrong_schema),
            ("f-not-dict.json", not_a_dict),
        ))
        found = records(self.root)
        self.assertEqual(len(found), 2)
        self.assertTrue(all(r["data_classification"] == "synthetic" for r in found))

    def test_a_record_that_fails_screening_is_dropped_entirely_not_redacted_in_place(self):
        from jevkit.web_server import records

        leaky = {"schema_version": 1, "data_classification": "synthetic", "mode": "fixture",
                 "note": "contact leaker@example.com for the key"}
        clean = {"schema_version": 1, "data_classification": "synthetic", "mode": "fixture", "note": "safe"}
        _build_web_root(self.root, runs=(("a-leaky.json", leaky), ("b-clean.json", clean)))
        # records() only appends when screen() finds NOTHING (`if not findings: runs.append(clean)`)
        # -- a flagged record is dropped from the feed entirely, never partially redacted.
        found = records(self.root)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["note"], "safe")

    def test_malformed_json_file_is_skipped_not_fatal(self):
        from jevkit.web_server import records

        _build_web_root(self.root)
        (self.root / "artifacts" / "runs" / "broken.json").write_text("{not json")
        self.assertEqual(records(self.root), [])

    def test_at_most_300_most_recent_runs_are_considered(self):
        from jevkit.web_server import records

        good = {"schema_version": 1, "data_classification": "synthetic", "mode": "fixture"}
        _build_web_root(self.root, runs=[(f"run-{i:04d}.json", good) for i in range(305)])
        self.assertEqual(len(records(self.root)), 300)


class WebServerHttpTests(unittest.TestCase):
    """make_server()'s Handler is the entire HTTP-facing attack surface of the viewer: origin
    binding, the read-only route allow-list, and the symlink refusal on static files. Every
    request here goes through a real HTTPServer bound to 127.0.0.1:0 -- shut down and joined in
    tearDown even on assertion failure."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        _build_web_root(self.root)

    def _start(self):
        from jevkit.web_server import make_server

        # make_server hardcodes ThreadingHTTPServer(('127.0.0.1', port), Handler); port=0 asks
        # the OS for a free ephemeral port so the test never collides with a real listener.
        server = make_server(port=0, root=self.root)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, timeout=2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def test_root_and_named_static_files_are_served_with_correct_content_type(self):
        server = self._start()
        port = server.server_port
        for path, expected_type, expected_body in (
            ("/", "text/html", b"<html>synthetic</html>"),
            ("/index.html", "text/html", b"<html>synthetic</html>"),
            ("/app.js", "application/javascript", b"// synthetic"),
            ("/styles.css", "text/css", b"/* synthetic */"),
        ):
            with self.subTest(path=path):
                status, headers, body = _http_request(port, "GET", path)
                self.assertEqual(status, 200)
                self.assertIn(expected_type, headers["Content-Type"])
                self.assertEqual(body, expected_body)

    def test_head_request_matches_get_routing_with_no_body(self):
        server = self._start()
        status, headers, body = _http_request(server.server_port, "HEAD", "/app.js")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")

    def test_unknown_path_is_404(self):
        server = self._start()
        status, _headers, body = _http_request(server.server_port, "GET", "/does-not-exist")
        self.assertEqual(status, 404)
        self.assertIn(b"Not found", body)

    def test_symlinked_static_file_is_refused_as_not_found(self):
        symlinked_root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, symlinked_root, ignore_errors=True)
        _build_web_root(symlinked_root, symlink_index=True)
        self.root = symlinked_root
        server = self._start()
        status, _headers, _body = _http_request(server.server_port, "GET", "/index.html")
        self.assertEqual(status, 404)

    def test_every_mutating_verb_is_rejected_as_read_only(self):
        server = self._start()
        port = server.server_port
        for method in ("POST", "PUT", "DELETE", "PATCH"):
            with self.subTest(method=method):
                status, _headers, body = _http_request(port, method, "/api/data")
                self.assertEqual(status, 405)
                self.assertIn(b"Read-only viewer", body)

    def test_mismatched_host_header_is_rejected_as_cross_origin(self):
        server = self._start()
        status, _headers, body = _http_request(
            server.server_port, "GET", "/api/data",
            extra_headers={"Host": "evil.example.invalid:9999"})
        self.assertEqual(status, 403)
        self.assertIn(b"Origin or Host rejected", body)

    def test_mismatched_origin_header_with_a_correct_host_is_also_rejected(self):
        server = self._start()
        port = server.server_port
        status, _headers, body = _http_request(
            port, "GET", "/api/data",
            extra_headers={"Host": f"127.0.0.1:{port}", "Origin": "http://evil.example.invalid"})
        self.assertEqual(status, 403)
        self.assertIn(b"Origin or Host rejected", body)

    def test_api_data_reports_catalog_and_runs_with_the_custom_data_hidden_flag(self):
        good = {"schema_version": 1, "data_classification": "synthetic", "mode": "fixture"}
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        _build_web_root(root, runs=(("run.json", good),))
        self.root = root
        server = self._start()
        status, headers, body = _http_request(server.server_port, "GET", "/api/data")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        payload = json.loads(body)
        self.assertTrue(payload["read_only"])
        self.assertTrue(payload["custom_data_hidden"])
        self.assertEqual(payload["catalog"], [])
        self.assertEqual(len(payload["runs"]), 1)


class WebServerServeTests(unittest.TestCase):
    """serve() is the CLI/process entrypoint: it validates the port before ever binding, then
    guarantees server_close() runs even if serve_forever() raises. Neither branch was covered
    before this file."""

    def test_out_of_range_port_is_rejected_before_any_socket_is_opened(self):
        from jevkit.security import SafeError
        from jevkit.web_server import serve

        for bad_port in (0, 1023, 65536, -1):
            with self.subTest(bad_port=bad_port):
                with patch("jevkit.web_server.make_server", side_effect=AssertionError(
                        "must not construct a server for an invalid port")):
                    with self.assertRaisesRegex(SafeError, "INVALID_PORT"):
                        serve(port=bad_port)

    def test_valid_port_always_closes_the_server_even_after_serve_forever_returns(self):
        from jevkit.web_server import serve

        closed = []
        fake_server = SimpleNamespace(serve_forever=lambda: None, server_close=lambda: closed.append(True))
        with patch("jevkit.web_server.make_server", return_value=fake_server) as make_server_mock:
            serve(port=8765)
        make_server_mock.assert_called_once_with(8765)
        self.assertEqual(closed, [True])

    def test_server_close_still_runs_when_serve_forever_raises(self):
        from jevkit.web_server import serve

        closed = []

        def boom():
            raise RuntimeError("synthetic interrupt")

        fake_server = SimpleNamespace(serve_forever=boom, server_close=lambda: closed.append(True))
        with patch("jevkit.web_server.make_server", return_value=fake_server):
            with self.assertRaises(RuntimeError):
                serve(port=8765)
        self.assertEqual(closed, [True])


# ============================================================================================
# jevkit/client.py -- dependency-free Jev HTTP adapter (22% baseline)
# ============================================================================================

class _FakeHttpResponse:
    """Stands in for the object `with send(request, timeout=20) as response:` binds -- a real
    urllib response is never opened; no real network call is ever made by this file."""

    def __init__(self, body: bytes):
        self._body = body

    def read(self, size: int = -1) -> bytes:
        return self._body if size is None or size < 0 else self._body[:size]

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def _http_error(code: int, *, retry_after: str | None = None) -> urllib.error.HTTPError:
    headers = email.message.Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return urllib.error.HTTPError(
        url="https://provider.test.invalid/v1/decide", code=code, msg="synthetic",
        hdrs=headers, fp=io.BytesIO(b""))


class ClientRetryDelayTests(unittest.TestCase):
    """retry_delay() decides how long evaluate() sleeps between retryable failures -- a pure
    function, but previously never called by any test (baseline missing lines 27-36)."""

    def test_no_header_falls_back_to_bounded_exponential_backoff(self):
        from jevkit.client import retry_delay

        self.assertEqual(retry_delay(None, 0), 0.5)
        self.assertEqual(retry_delay(None, 1), 1.0)
        self.assertEqual(retry_delay(None, 5), 4.0)  # capped at 4.0

    def test_numeric_header_is_clamped_to_zero_and_twenty(self):
        from jevkit.client import retry_delay

        self.assertEqual(retry_delay("10", 0), 10.0)
        self.assertEqual(retry_delay("-5", 0), 0.0)
        self.assertEqual(retry_delay("100", 0), 20.0)

    def test_http_date_header_is_parsed_into_a_clamped_delay(self):
        from jevkit.client import retry_delay

        future = time.time() + 5
        header = email.utils.formatdate(future, usegmt=True)
        delay = retry_delay(header, 0)
        self.assertGreater(delay, 0.0)
        self.assertLessEqual(delay, 20.0)

    def test_unparseable_header_falls_back_to_the_default_backoff(self):
        from jevkit.client import retry_delay

        self.assertEqual(retry_delay("not-a-number-or-a-date", 2), retry_delay(None, 2))


class ClientNoRedirectTests(unittest.TestCase):
    """NoRedirect must silently swallow every redirect the provider ever sends -- an evaluate()
    call must never follow a 3xx to an attacker-controlled or unexpected host."""

    def test_redirect_request_always_returns_none(self):
        from jevkit.client import NoRedirect

        self.assertIsNone(NoRedirect().redirect_request(None, None, None, None, None))


class ClientEvaluateTests(unittest.TestCase):
    """evaluate() is the ENTIRE outbound/inbound boundary for a live Jev call: credential
    lookup, outbound secret screening, byte budgets, grant reservation, retry/backoff, response
    validation, and inbound secret screening. Baseline coverage: 0% of this function (lines
    55-150). Every HTTP interaction here is a local fake; `transport`/`send` never opens a
    socket, and CallBudget's sqlite ledger lives under a TemporaryDirectory."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        from jevkit.providers import ProviderProfile

        self.profile = ProviderProfile(
            provider_id="unit-test-provider", display_name="Unit Test Provider",
            endpoint="https://provider.test.invalid/v1/decide", model="unit-test-model",
            env_var="UNIT_TEST_JEV_API_KEY", key_filename="unit-test-jev-api-key")
        self.questions = {"decision": {"type": "noul", "instructions": "Is this synthetic?"}}
        self.env_patch = patch.dict(os.environ, {self.profile.env_var: "unit-test-fake-credential-value"})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def _grant(self, *, calls=10, minutes=10, profile=None, workspace_id=None, revision=None):
        from jevkit.authorization import CallBudget

        profile = profile or self.profile
        budget = CallBudget(self.root, provider_id=profile.provider_id,
                            provider_profile_sha256=profile.profile_sha256,
                            workspace_id=workspace_id, revision=revision)
        budget.grant(calls, minutes)
        return budget

    def _evaluate(self, transport, *, state="a synthetic, unremarkable evaluation state", **kwargs):
        from jevkit.client import evaluate

        return evaluate(self.root, state, self.questions, False, provider=self.profile,
                        transport=transport, sleep=lambda _s: None, **kwargs)

    def test_state_of_an_unsupported_type_is_rejected_before_any_credential_lookup(self):
        from jevkit.security import SafeError

        with patch("jevkit.client.get_provider_credential", side_effect=AssertionError(
                "must not look up a credential before validating state shape")):
            with self.assertRaisesRegex(SafeError, "INVALID_STATE"):
                self._evaluate(transport=Mock(), state=object())

    def test_a_secret_looking_state_is_blocked_outbound_before_any_transport_call(self):
        from jevkit.security import SafeError

        self._grant()
        with self.assertRaisesRegex(SafeError, "OUTBOUND_DATA_BLOCKED"):
            self._evaluate(transport=Mock(side_effect=AssertionError("must not call transport")),
                           state="contact leaker@example.com about this ticket")

    def test_an_oversized_payload_is_rejected_before_any_transport_call(self):
        from jevkit.security import SafeError

        self._grant()
        with self.assertRaisesRegex(SafeError, "REQUEST_BYTE_BUDGET_EXCEEDED"):
            self._evaluate(transport=Mock(side_effect=AssertionError("must not call transport")),
                           state="z" * 40_000)

    def test_successful_first_attempt_returns_a_normalized_answer_with_transport_metadata(self):
        raw = {"model": self.profile.model, "answers": {"decision": {"type": "noul", "noul": 0.42}},
               "usage": {"input_tokens": 5, "output_tokens": 2}}
        self._grant(workspace_id="ws-1", revision="r" * 64)
        transport = Mock(return_value=_FakeHttpResponse(json.dumps(raw).encode()))
        result = self._evaluate(transport=transport, workspace_id="ws-1", revision="r" * 64,
                                case_id="case-1", data_classification="public",
                                request_id="req-1", request_sha256="s" * 64)
        self.assertEqual(result["answers"]["decision"], {"type": "noul", "noul": 0.42})
        self.assertEqual(result["_transport"]["attempts"], 1)
        self.assertIn("observed_latency_ms", result["_transport"])
        self.assertTrue(result["_grant_id"])
        self.assertEqual(result["_provider_id"], self.profile.provider_id)
        self.assertEqual(result["_provider_profile_sha256"], self.profile.profile_sha256)
        transport.assert_called_once()

    def test_oversized_response_body_is_rejected(self):
        from jevkit.security import SafeError

        self._grant()
        transport = Mock(return_value=_FakeHttpResponse(b"x" * 1_000_001))
        with self.assertRaisesRegex(SafeError, "API_RESPONSE_TOO_LARGE"):
            self._evaluate(transport=transport)

    def test_non_json_response_body_is_rejected(self):
        from jevkit.security import SafeError

        self._grant()
        transport = Mock(return_value=_FakeHttpResponse(b"not json at all"))
        with self.assertRaisesRegex(SafeError, "API_NON_JSON_RESPONSE"):
            self._evaluate(transport=transport)

    def test_a_provider_model_field_that_itself_looks_like_a_secret_is_blocked_inbound(self):
        """The only string in a normalized result NOT re-derived from the locally-supplied
        `questions` dict (already outbound-screened) is the provider's own `model` field. This
        proves the inbound screen() call is a real, independently-reachable defense, not dead
        code shadowed by the outbound check."""
        from jevkit.providers import ProviderProfile
        from jevkit.security import SafeError

        leaky_profile = ProviderProfile(
            provider_id="unit-test-provider", display_name="Unit Test Provider",
            endpoint="https://provider.test.invalid/v1/decide", model="leaked-contact@example.com",
            env_var="UNIT_TEST_JEV_API_KEY", key_filename="unit-test-jev-api-key")
        self._grant(profile=leaky_profile)
        raw = {"model": leaky_profile.model, "answers": {"decision": {"type": "noul", "noul": 0.1}},
               "usage": {"input_tokens": 1, "output_tokens": 1}}
        transport = Mock(return_value=_FakeHttpResponse(json.dumps(raw).encode()))
        from jevkit.client import evaluate

        with self.assertRaisesRegex(SafeError, "INBOUND_DATA_BLOCKED"):
            evaluate(self.root, "state", self.questions, False, provider=leaky_profile,
                    transport=transport, sleep=lambda _s: None)

    def test_retryable_status_below_the_backoff_ceiling_sleeps_then_succeeds_on_retry(self):
        self._grant()
        raw = {"model": self.profile.model, "answers": {"decision": {"type": "noul", "noul": 0.9}},
               "usage": {"input_tokens": 1, "output_tokens": 1}}
        slept = []
        transport = Mock(side_effect=[_http_error(503, retry_after="1"), _FakeHttpResponse(json.dumps(raw).encode())])
        from jevkit.client import evaluate

        result = evaluate(self.root, "state", self.questions, False, provider=self.profile,
                          transport=transport, sleep=slept.append)
        self.assertEqual(result["_transport"]["attempts"], 2)
        self.assertEqual(slept, [1.0])

    def test_retryable_status_at_or_above_the_backoff_ceiling_refuses_immediately(self):
        from jevkit.security import SafeError

        self._grant()
        transport = Mock(side_effect=_http_error(503, retry_after="60"))
        with self.assertRaisesRegex(SafeError, "API_BACKOFF_REQUIRED"):
            self._evaluate(transport=transport)
        transport.assert_called_once()

    def test_non_retryable_http_status_is_refused_without_any_sleep(self):
        from jevkit.security import SafeError

        self._grant()
        slept = []
        transport = Mock(side_effect=_http_error(400))
        from jevkit.client import evaluate

        with self.assertRaisesRegex(SafeError, "API_HTTP_400"):
            evaluate(self.root, "state", self.questions, False, provider=self.profile,
                    transport=transport, sleep=slept.append)
        self.assertEqual(slept, [])

    def test_retryable_status_still_refuses_once_the_attempt_budget_is_exhausted(self):
        """Covers the attempt==2 branch of the SAME retryable-status code path: `attempt < 2`
        is now False, so the loop falls through to the identical API_HTTP_ raise as a
        non-retryable status, rather than reaching the trailing API_ATTEMPTS_EXHAUSTED line.
        See KnownDefectTests for why that trailing line is unreachable."""
        from jevkit.security import SafeError

        self._grant()
        transport = Mock(side_effect=[_http_error(503, retry_after="0"), _http_error(503, retry_after="0"),
                                      _http_error(503, retry_after="0")])
        with self.assertRaisesRegex(SafeError, "API_HTTP_503"):
            self._evaluate(transport=transport)
        self.assertEqual(transport.call_count, 3)

    def test_ambiguous_transport_failure_is_refused_without_any_retry(self):
        from jevkit.security import SafeError

        self._grant()
        for error in (OSError("synthetic transport failure"), urllib.error.URLError("synthetic")):
            with self.subTest(error=type(error).__name__):
                transport = Mock(side_effect=error)
                with self.assertRaisesRegex(SafeError, "API_TRANSPORT_FAILURE"):
                    self._evaluate(transport=transport)
                transport.assert_called_once()

    def test_no_live_grant_is_refused_before_any_transport_call(self):
        from jevkit.security import SafeError

        # Deliberately skip self._grant(): the ledger has no row at all.
        with self.assertRaisesRegex(SafeError, "LIVE_NOT_AUTHORIZED"):
            self._evaluate(transport=Mock(side_effect=AssertionError("must not call transport")))


class KnownDefectTests(unittest.TestCase):
    """Pre-existing defects found while writing coverage for jevkit/client.py. Not patched, per
    instructions: reported here with @unittest.expectedFailure and left for a human/product
    decision."""

    @unittest.expectedFailure
    def test_defect_api_attempts_exhausted_is_unreachable_dead_code(self):
        """DEFECT (jevkit/client.py:150): `raise SafeError("API_ATTEMPTS_EXHAUSTED")` after the
        `for attempt in range(3):` loop can never execute. Every branch inside the loop body
        either `return`s (success) or `raise`s (every HTTPError sub-case, and the bare
        URLError/TimeoutError/OSError case) on every one of the 3 iterations -- including the
        final iteration, where `attempt < 2` is False and the retryable-status branch falls
        through to `raise SafeError("API_HTTP_" + str(status) + ...)`  instead of ever reaching
        the loop's own trailing statement. Current: the line is present but 0% reachable by any
        input, so it can never actually report API_ATTEMPTS_EXHAUSTED to a caller. Correct: either
        remove the line (it documents intent that the code no longer needs), or restructure so
        some genuine "ran out of attempts with no definitive error" case exists and reaches it.
        Concrete failing input: none exists -- that absence IS the defect. This test documents
        the claim by asserting the loop cannot fall through; it is expected to fail (i.e. the
        assertion that the line is reachable fails), flagging the defect without bending a real
        test to accept the dead branch as correct behavior.
        """
        import inspect

        from jevkit import client

        source = inspect.getsource(client.evaluate)
        # The assertion an honest reader would want to be false: that the loop can complete
        # its 3rd iteration and fall through to the trailing raise. It cannot -- so this
        # xfails, pinning the defect until someone deliberately changes the control flow.
        self.assertTrue(
            any("break" in line for line in source.splitlines()),
            "no `break` exists in evaluate(); every branch must return/raise, so the trailing "
            "`raise SafeError('API_ATTEMPTS_EXHAUSTED')` is unreachable dead code (client.py:150)")


# ============================================================================================
# src/adl/providers/laya_worker.py -- SandboxedLayaProcess (19% baseline)
# ============================================================================================

def _build_laya_artifact(root: Path, *, weight_content: bytes = b"synthetic-mlx-weight-bytes"):
    """A real, on-disk artifact + manifest + config that passes SandboxedLayaProcess.__init__'s
    entire verification chain, for a fictitious repository (never a real PRODUCTION_PINS
    value, never touching laya_mlx/mlx)."""
    from src.adl.providers.laya_worker import PreparedArtifact

    # A fresh subdirectory per call: this fixture is called more than once against the same
    # `root` (e.g. once per subTest iteration), and a fixed "model"/"manifest.json" name would
    # collide with a previous call's files on the second invocation.
    base = Path(tempfile.mkdtemp(dir=root))
    model_dir = base / "model"
    model_dir.mkdir()
    weight_path = model_dir / "model.safetensors"
    weight_path.write_bytes(weight_content)
    weight_path.chmod(0o600)
    checksum = hashlib.sha256(weight_content).hexdigest()
    manifest = {"schema_version": 1, "repository": "test/laya", "revision": "a" * 40,
                "files": {"model.safetensors": checksum}}
    manifest_path = base / "manifest.json"
    manifest_path.write_text(json.dumps(manifest))
    manifest_path.chmod(0o600)
    artifact = PreparedArtifact(profile_id="test/laya", checkpoint_revision="a" * 40,
                               checkpoint_sha256=checksum, model_path=weight_path)
    config = {"model_dir": str(model_dir), "weight_sha256": checksum,
              "artifact_manifest": str(manifest_path), "repository": "test/laya", "revision": "a" * 40}
    return artifact, config, model_dir, manifest_path, weight_path


class _FakeSandboxWorkerProcess:
    """Stands in for the real subprocess.Popen handle SandboxedLayaProcess._spawn() would
    return. Talks over real os.pipe() file descriptors (SandboxedLayaProcess._exchange() does
    raw os.read/os.write/selectors on file descriptors, so a BytesIO stand-in cannot work) --
    but there is never a real child process on the other end: replies are pre-seeded into the
    pipe buffer before the code under test ever reads them."""

    def __init__(self, reply_bytes: bytes = b""):
        in_read, in_write = os.pipe()
        out_read, out_write = os.pipe()
        if reply_bytes:
            os.write(out_write, reply_bytes)
        self.stdin = os.fdopen(in_write, "wb", buffering=0)
        self.stdout = os.fdopen(out_read, "rb", buffering=0)
        self._in_read_fd = in_read
        self._out_write_fd = out_write
        self.pid = 999_999_991
        self._alive = True

    def poll(self):
        return None if self._alive else 0

    def wait(self, timeout=None):
        self._alive = False
        return 0

    def close_extra_fds(self):
        """Closes every descriptor this fake owns: the two pipe ends product code never
        touches directly (_in_read_fd, _out_write_fd), AND stdin/stdout themselves. Product
        code (_stop_locked) closes stdin/stdout on a normal teardown path, but several tests
        construct one of these and then deliberately never let SandboxedLayaProcess's own
        teardown run (a raised exception before `_process` is assigned, or a directly-called
        `_exchange`) -- relying on CPython's refcounting to eventually close those file
        objects works, but leaves the exact moment non-deterministic. Closing everything here,
        idempotently, means every test that builds one of these can register this single
        method with addCleanup and never leak a descriptor regardless of which code path ran.
        """
        for closer in (self.stdin.close, self.stdout.close):
            try:
                closer()
            except OSError:
                pass
        for fd in (self._in_read_fd, self._out_write_fd):
            try:
                os.close(fd)
            except OSError:
                pass


class LayaWorkerModuleHelperTests(unittest.TestCase):
    """Module-level helpers (_approved_interpreter, _sha256, _verify_artifact,
    PersistentLayaWorker) underlie every safety check SandboxedLayaProcess makes. Baseline:
    19% of the whole module; these free functions were the least-covered part."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_persistent_laya_worker_is_permanently_disabled(self):
        from src.adl.providers.laya_worker import PersistentLayaWorker, WorkerError

        with self.assertRaisesRegex(WorkerError, "IN_PROCESS_WORKER_DISABLED"):
            PersistentLayaWorker()
        with self.assertRaisesRegex(WorkerError, "IN_PROCESS_WORKER_DISABLED"):
            PersistentLayaWorker("ignored", kwarg="also ignored")

    def test_approved_interpreter_accepts_the_running_interpreter_itself(self):
        from src.adl.providers import laya_worker

        base = str(Path(sys.executable).resolve())
        with patch.object(laya_worker.sys, "executable", sys.executable), \
             patch.object(laya_worker.sys, "_base_executable", base, create=True):
            self.assertTrue(laya_worker._approved_interpreter(Path(sys.executable)))

    def test_approved_interpreter_rejects_an_unrelated_path(self):
        from src.adl.providers.laya_worker import _approved_interpreter

        self.assertFalse(_approved_interpreter(self.root / "not-a-real-interpreter"))

    def test_sha256_refuses_a_missing_or_symlinked_or_hardlinked_file(self):
        from jevkit.security import SafeError
        from src.adl.providers.laya_worker import WorkerError, _sha256

        with self.assertRaisesRegex(WorkerError, "ARTIFACT_NOT_READY"):
            _sha256(self.root / "does-not-exist")

        real = self.root / "real.bin"
        real.write_bytes(b"x")
        link = self.root / "link.bin"
        link.symlink_to(real)
        with self.assertRaises((WorkerError, SafeError)):
            _sha256(link)

        hardlinked = self.root / "hardlinked.bin"
        os.link(real, hardlinked)
        with self.assertRaisesRegex(WorkerError, "ARTIFACT_NOT_READY"):
            _sha256(hardlinked)

    def test_verify_artifact_rejects_every_malformed_shape(self):
        from src.adl.providers.laya_worker import PreparedArtifact, WorkerError, _verify_artifact

        artifact, config, model_dir, manifest_path, weight_path = _build_laya_artifact(self.root)
        with self.subTest("not_a_prepared_artifact"):
            with self.assertRaisesRegex(WorkerError, "ARTIFACT_NOT_READY"):
                _verify_artifact("not-an-artifact")
        with self.subTest("blank_profile_id"):
            with self.assertRaisesRegex(WorkerError, "ARTIFACT_NOT_READY"):
                _verify_artifact(PreparedArtifact("", artifact.checkpoint_revision,
                                                  artifact.checkpoint_sha256, artifact.model_path))
        with self.subTest("bad_revision_shape"):
            with self.assertRaisesRegex(WorkerError, "ARTIFACT_NOT_READY"):
                _verify_artifact(PreparedArtifact("p", "short", artifact.checkpoint_sha256, artifact.model_path))
        with self.subTest("bad_sha256_shape"):
            with self.assertRaisesRegex(WorkerError, "ARTIFACT_NOT_READY"):
                _verify_artifact(PreparedArtifact("p", artifact.checkpoint_revision, "short", artifact.model_path))
        with self.subTest("world_writable_file"):
            weight_path.chmod(0o666)
            with self.assertRaisesRegex(WorkerError, "ARTIFACT_NOT_READY"):
                _verify_artifact(artifact)
            weight_path.chmod(0o600)
        with self.subTest("digest_mismatch"):
            wrong = PreparedArtifact(artifact.profile_id, artifact.checkpoint_revision, "f" * 64, artifact.model_path)
            with self.assertRaisesRegex(WorkerError, "ARTIFACT_NOT_READY"):
                _verify_artifact(wrong)
        with self.subTest("valid_artifact_passes"):
            _verify_artifact(artifact)  # must not raise


class LayaWorkerConstructionTests(unittest.TestCase):
    """SandboxedLayaProcess.__init__ is the entire artifact/config verification gate before a
    single byte of untrusted config is trusted. Baseline: essentially none of this was
    covered. require_native_sandbox and _approved_interpreter are patched at the seams the
    module itself exposes for exactly this purpose -- never a real sandbox-exec invocation."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.python_path = self.root / "python-stub"
        self.python_path.write_text("#!/bin/sh\nexit 0\n")
        self.python_path.chmod(0o700)
        from src.adl.providers import laya_worker

        self.laya_worker = laya_worker
        self._patches = [
            patch.object(laya_worker, "_approved_interpreter", return_value=True),
            patch.object(laya_worker.SandboxedLayaProcess, "require_native_sandbox", Mock()),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    def _construct(self, **overrides):
        artifact, config, model_dir, manifest_path, weight_path = _build_laya_artifact(self.root)
        config = {**config, **overrides.pop("config_overrides", {})}
        process = self.laya_worker.SandboxedLayaProcess(
            overrides.pop("artifact", artifact), config, overrides.pop("python", self.python_path), **overrides)
        self.addCleanup(process.close)
        return process, artifact, config, model_dir, manifest_path

    def test_valid_construction_succeeds_and_can_be_used_as_a_context_manager(self):
        process, *_ = self._construct()
        with process as same:
            self.assertIs(same, process)
        self.assertIsNone(process._process)

    def test_max_pending_bounds_are_enforced(self):
        for bad in (0, 257, True, "5"):
            with self.subTest(bad=bad):
                with self.assertRaisesRegex(ValueError, "INVALID_QUEUE_BOUND"):
                    self._construct(max_pending=bad)

    def test_non_dict_config_is_rejected(self):
        from src.adl.providers.laya_worker import WorkerError

        artifact, _config, _model_dir, _manifest, _weight = _build_laya_artifact(self.root)
        with self.assertRaisesRegex(WorkerError, "ARTIFACT_CONFIG_MISMATCH"):
            self.laya_worker.SandboxedLayaProcess(artifact, ["not", "a", "dict"], self.python_path)

    def test_config_model_dir_or_hash_mismatch_with_the_artifact_is_rejected(self):
        with self.assertRaisesRegex(self.laya_worker.WorkerError, "ARTIFACT_CONFIG_MISMATCH"):
            self._construct(config_overrides={"weight_sha256": "f" * 64})

    def test_manifest_world_readable_or_oversized_or_missing_is_rejected(self):
        process_ok, artifact, config, model_dir, manifest_path = self._construct()
        with self.subTest("world_readable_manifest"):
            manifest_path.chmod(0o644)
            with self.assertRaisesRegex(self.laya_worker.WorkerError, "ARTIFACT_CONFIG_MISMATCH"):
                self.laya_worker.SandboxedLayaProcess(artifact, config, self.python_path)
            manifest_path.chmod(0o600)
        with self.subTest("missing_manifest"):
            with self.assertRaisesRegex(self.laya_worker.WorkerError, "ARTIFACT_CONFIG_MISMATCH"):
                self.laya_worker.SandboxedLayaProcess(
                    artifact, {**config, "artifact_manifest": str(self.root / "missing.json")}, self.python_path)

    def test_manifest_content_mismatch_with_config_or_artifact_is_rejected(self):
        _process_ok, artifact, config, model_dir, manifest_path = self._construct()
        bad_manifest = json.loads(manifest_path.read_text())
        bad_manifest["revision"] = "b" * 40
        bad_path = self.root / "bad-manifest.json"
        bad_path.write_text(json.dumps(bad_manifest))
        bad_path.chmod(0o600)
        with self.assertRaisesRegex(self.laya_worker.WorkerError, "ARTIFACT_CONFIG_MISMATCH"):
            self.laya_worker.SandboxedLayaProcess(artifact, {**config, "artifact_manifest": str(bad_path)},
                                                  self.python_path)

    def test_unapproved_or_missing_python_interpreter_is_rejected(self):
        with patch.object(self.laya_worker, "_approved_interpreter", return_value=False):
            with self.assertRaisesRegex(self.laya_worker.WorkerError, "MLX_PYTHON_UNAPPROVED"):
                self._construct()
        with self.assertRaisesRegex(self.laya_worker.WorkerError, "MLX_PYTHON_MISSING"):
            self._construct(python=self.root / "no-such-python")


class LayaWorkerCommandAndProfileTests(unittest.TestCase):
    """command()/sandbox_profile()/child_environment() build the exact sandbox-exec invocation
    and its child environment. These are pure string/list builders -- exercised directly,
    without ever invoking /usr/bin/sandbox-exec."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.python_path = self.root / "python-stub"
        self.python_path.write_text("#!/bin/sh\n")
        self.python_path.chmod(0o700)
        from src.adl.providers import laya_worker

        self.laya_worker = laya_worker

    def test_command_shape_with_the_running_interpreter_approved(self):
        with patch.object(self.laya_worker, "_approved_interpreter", return_value=True):
            command = self.laya_worker.SandboxedLayaProcess.command(
                self.python_path, model_dir=self.root, artifact_manifest=self.root / "m.json",
                repository_root=RUNTIME, temp_root=self.root)
        self.assertEqual(command[0], str(self.laya_worker.SandboxedLayaProcess.SANDBOX_BINARY))
        self.assertEqual(command[1], "-p")
        self.assertIn("(deny default)", command[2])
        self.assertEqual(command[3:], [str(self.python_path), "-u", "-m", "jev_auto.mlx_worker"])

    def test_sandbox_profile_denies_default_and_network_and_allows_the_temp_root(self):
        with patch.object(self.laya_worker, "_approved_interpreter", return_value=True):
            profile = self.laya_worker.SandboxedLayaProcess.sandbox_profile(
                python=self.python_path, model_dir=self.root, artifact_manifest=self.root / "m.json",
                repository_root=RUNTIME, temp_root=self.root)
        self.assertIn("(deny default)", profile)
        self.assertIn("(deny network*)", profile)
        self.assertIn(json.dumps(str(self.root.resolve())), profile)

    def test_sandbox_profile_refuses_an_unapproved_python_interpreter(self):
        with patch.object(self.laya_worker, "_approved_interpreter", return_value=False):
            with self.assertRaisesRegex(self.laya_worker.WorkerError, "PYTHON_RUNTIME_UNTRUSTED"):
                self.laya_worker.SandboxedLayaProcess.sandbox_profile(python=self.python_path)

    def test_sandbox_profile_refuses_whitelisting_the_filesystem_root(self):
        with self.assertRaisesRegex(self.laya_worker.WorkerError, "PYTHON_RUNTIME_UNTRUSTED"):
            self.laya_worker.SandboxedLayaProcess.sandbox_profile(model_dir=Path("/"))

    def test_sandbox_profile_honors_a_pyvenv_cfg_pointing_at_an_approved_system_root(self):
        real_python = Path("/opt/homebrew/bin/python3")
        if not real_python.is_file():
            self.skipTest("no /opt/homebrew/bin/python3 on this host to pin the venv test to")
        venv_python = self.root / "bin" / "python"
        venv_python.parent.mkdir(parents=True)
        venv_python.write_text("#!/bin/sh\n")
        venv_python.chmod(0o700)
        (self.root / "pyvenv.cfg").write_text(f"home = /opt/homebrew/bin\nexecutable = {real_python}\n")
        with patch.object(self.laya_worker, "_approved_interpreter", return_value=True):
            profile = self.laya_worker.SandboxedLayaProcess.sandbox_profile(python=venv_python)
        self.assertIn("(allow process-exec", profile)

    def test_child_environment_is_offline_and_deterministic(self):
        env = self.laya_worker.SandboxedLayaProcess.child_environment(RUNTIME)
        self.assertEqual(env["HF_HUB_OFFLINE"], "1")
        self.assertEqual(env["TRANSFORMERS_OFFLINE"], "1")
        self.assertEqual(env["PYTHONPATH"], str(RUNTIME.resolve()))
        self.assertNotIn("HOME", env)

        with_temp = self.laya_worker.SandboxedLayaProcess.child_environment(RUNTIME, self.root)
        self.assertEqual(with_temp["HOME"], str(self.root))
        self.assertEqual(with_temp["TMPDIR"], str(self.root))

        with patch.dict(os.environ, {"LANG": "en_US.UTF-8"}):
            with_lang = self.laya_worker.SandboxedLayaProcess.child_environment(RUNTIME)
        self.assertEqual(with_lang["LANG"], "en_US.UTF-8")


class LayaWorkerKillProcessGroupTests(unittest.TestCase):
    """kill_process_group() is what stands between a hung/misbehaving sandboxed child and a
    leaked process forever. os.killpg is always mocked here -- this suite never sends a real
    signal to a real process group."""

    def setUp(self):
        from src.adl.providers import laya_worker

        self.laya_worker = laya_worker

    def test_an_already_dead_process_is_left_alone(self):
        fake = SimpleNamespace(pid=1, poll=lambda: 0, wait=Mock())
        with patch.object(self.laya_worker.os, "killpg") as killpg:
            self.laya_worker.SandboxedLayaProcess.kill_process_group(fake)
        killpg.assert_not_called()
        fake.wait.assert_not_called()

    def test_a_live_process_gets_sigterm_and_a_clean_wait(self):
        fake = SimpleNamespace(pid=42, poll=lambda: None, wait=Mock(return_value=0))
        with patch.object(self.laya_worker.os, "killpg") as killpg:
            self.laya_worker.SandboxedLayaProcess.kill_process_group(fake)
        killpg.assert_called_once_with(42, signal.SIGTERM)
        fake.wait.assert_called_once_with(timeout=0.5)

    def test_a_process_that_ignores_sigterm_is_escalated_to_sigkill(self):
        fake = SimpleNamespace(pid=42, poll=lambda: None,
                               wait=Mock(side_effect=[subprocess.TimeoutExpired("x", 0.5), 0]))
        with patch.object(self.laya_worker.os, "killpg") as killpg:
            self.laya_worker.SandboxedLayaProcess.kill_process_group(fake)
        self.assertEqual(killpg.call_args_list, [unittest.mock.call(42, signal.SIGTERM),
                                                 unittest.mock.call(42, signal.SIGKILL)])
        self.assertEqual(fake.wait.call_count, 2)

    def test_a_process_lookup_error_from_killpg_is_swallowed_both_times(self):
        fake = SimpleNamespace(pid=42, poll=lambda: None,
                               wait=Mock(side_effect=[subprocess.TimeoutExpired("x", 0.5), 0]))
        with patch.object(self.laya_worker.os, "killpg", side_effect=ProcessLookupError):
            self.laya_worker.SandboxedLayaProcess.kill_process_group(fake)  # must not raise


class LayaWorkerLifecycleTests(unittest.TestCase):
    """warmup()/predict()/_exchange()/close() drive the actual JSON-over-pipes protocol.
    subprocess.Popen is always patched to _FakeSandboxWorkerProcess, which talks over real
    os.pipe() descriptors with pre-seeded replies -- there is never a real child process, and
    laya_mlx/mlx are never imported."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.python_path = self.root / "python-stub"
        self.python_path.write_text("#!/bin/sh\n")
        self.python_path.chmod(0o700)
        from src.adl.providers import laya_worker

        self.laya_worker = laya_worker
        for p in (patch.object(laya_worker, "_approved_interpreter", return_value=True),
                  patch.object(laya_worker.SandboxedLayaProcess, "require_native_sandbox", Mock())):
            p.start()
            self.addCleanup(p.stop)
        self._fake_processes: list[_FakeSandboxWorkerProcess] = []

    def _process(self, **kwargs):
        artifact, config, *_ = _build_laya_artifact(self.root)
        process = self.laya_worker.SandboxedLayaProcess(artifact, config, self.python_path, **kwargs)
        self.addCleanup(process.close)
        return process

    def _popen_returning(self, reply_bytes: bytes):
        fake = _FakeSandboxWorkerProcess(reply_bytes)
        self._fake_processes.append(fake)
        self.addCleanup(fake.close_extra_fds)
        return patch.object(self.laya_worker.subprocess, "Popen", return_value=fake)

    @staticmethod
    def _reply(*objs) -> bytes:
        return b"".join(json.dumps(o).encode() + b"\n" for o in objs)

    def test_warmup_then_predict_reuses_the_already_loaded_worker(self):
        load_reply = {"ok": True, "result": {"ready": True, "default_device": "cpu", "device_source": "unit-test"}}
        predict_reply = {"ok": True, "result": {"answers": {"decision": {"type": "noul", "noul": 0.5}}}}
        process = self._process()
        with self._popen_returning(self._reply(load_reply, predict_reply)):
            warm = process.warmup(timeout=5)
            self.assertTrue(warm["ready"])
            self.assertEqual(warm["default_device"], "cpu")
            result = process.predict("a synthetic state", {"decision": {"type": "noul", "instructions": "x"}},
                                     timeout=5)
        self.assertEqual(result["output"], {"answers": {"decision": {"type": "noul", "noul": 0.5}}})
        self.assertFalse(result["telemetry"]["cold"])
        self.assertEqual(result["telemetry"]["resident_calls"], 1)

    def test_predict_without_a_prior_warmup_performs_its_own_cold_load(self):
        load_reply = {"ok": True, "result": {"ready": True, "default_device": "gpu", "device_source": "unit-test"}}
        predict_reply = {"ok": True, "result": {"answers": {}}}
        process = self._process()
        with self._popen_returning(self._reply(load_reply, predict_reply)):
            result = process.predict("state", {}, timeout=5)
        self.assertTrue(result["telemetry"]["cold"])
        self.assertIsNotNone(result["telemetry"]["cold_load_ms"])

    def test_load_reply_missing_ready_true_is_refused(self):
        process = self._process()
        with self._popen_returning(self._reply({"ok": True, "result": {"ready": False}})):
            with self.assertRaisesRegex(self.laya_worker.WorkerError, "MLX_LOAD_NOT_READY"):
                process.warmup(timeout=5)

    def test_load_reply_with_unknown_device_or_missing_source_is_refused(self):
        for bad_result in ({"ready": True, "default_device": "quantum", "device_source": "x"},
                           {"ready": True, "default_device": "cpu", "device_source": ""}):
            with self.subTest(bad_result=bad_result):
                process = self._process()
                with self._popen_returning(self._reply({"ok": True, "result": bad_result})):
                    with self.assertRaisesRegex(self.laya_worker.WorkerError, "DEVICE_TELEMETRY_REQUIRED"):
                        process.warmup(timeout=5)

    def test_worker_busy_when_the_admission_semaphore_is_already_exhausted(self):
        process = self._process(max_pending=1)
        process._admission.acquire()  # steal the one and only slot
        with self.assertRaisesRegex(self.laya_worker.WorkerBusy, "WORKER_QUEUE_FULL"):
            process.predict("state", {}, timeout=5)

    def test_invalid_deadline_values_are_rejected_for_warmup_and_predict(self):
        process = self._process()
        for bad_timeout in (0, -1, 121, True):
            with self.subTest(method="warmup", bad_timeout=bad_timeout):
                with self.assertRaisesRegex(ValueError, "INVALID_DEADLINE"):
                    process.warmup(timeout=bad_timeout)
            with self.subTest(method="predict", bad_timeout=bad_timeout):
                with self.assertRaisesRegex(ValueError, "INVALID_DEADLINE"):
                    process.predict("state", {}, timeout=bad_timeout)

    def test_read_side_deadline_is_enforced_when_nothing_is_ever_written_back(self):
        process = self._process()
        with self._popen_returning(b""):  # nobody ever replies
            with self.assertRaisesRegex(self.laya_worker.WorkerError, "WORKER_DEADLINE_EXCEEDED"):
                process.warmup(timeout=0.2)

    def test_write_side_deadline_is_enforced_when_the_pipe_is_already_full(self):
        process = self._process()
        fake = _FakeSandboxWorkerProcess(b"")
        self._fake_processes.append(fake)
        self.addCleanup(fake.close_extra_fds)
        os.set_blocking(fake.stdin.fileno(), False)
        filled = False
        try:
            # 256 x 64KiB = 16 MiB: comfortably past every default OS pipe buffer size (64KiB
            # on Linux/macOS) with a wide margin, so this does not depend on the exact default
            # and is not the kind of test that silently skips on an unusual runner.
            for _ in range(256):
                os.write(fake.stdin.fileno(), b"x" * 65536)
        except BlockingIOError:
            filled = True
        if not filled:
            self.skipTest("could not fill the pipe buffer on this platform")
        with patch.object(self.laya_worker.subprocess, "Popen", return_value=fake):
            with self.assertRaisesRegex(self.laya_worker.WorkerError, "WORKER_DEADLINE_EXCEEDED"):
                process.warmup(timeout=0.2)

    def test_dead_process_before_exchange_is_reported_as_worker_stopped(self):
        process = self._process()
        fake = _FakeSandboxWorkerProcess(b"")
        self._fake_processes.append(fake)
        self.addCleanup(fake.close_extra_fds)
        with patch.object(self.laya_worker.subprocess, "Popen", return_value=fake):
            process._process = fake
            fake._alive = False
            with self.assertRaisesRegex(self.laya_worker.WorkerError, "MLX_WORKER_STOPPED"):
                process._exchange({"kind": "predict"}, time.monotonic() + 5)

    def test_recoverable_would_truncate_failure_keeps_the_worker_alive(self):
        load_reply = {"ok": True, "result": {"ready": True, "default_device": "cpu", "device_source": "x"}}
        process = self._process()
        with self._popen_returning(self._reply(load_reply)):
            process.warmup(timeout=5)
            live_process_handle = process._process
            with patch.object(process, "_exchange", side_effect=self.laya_worker.WorkerError(
                    "MLX_STATE_WOULD_TRUNCATE")):
                with self.assertRaisesRegex(self.laya_worker.WorkerError, "MLX_STATE_WOULD_TRUNCATE"):
                    process.predict("state", {}, timeout=5)
        self.assertIs(process._process, live_process_handle, "a recoverable refusal must not tear down the worker")

    def test_non_recoverable_failure_discards_the_worker(self):
        load_reply = {"ok": True, "result": {"ready": True, "default_device": "cpu", "device_source": "x"}}
        process = self._process()
        with self._popen_returning(self._reply(load_reply)):
            process.warmup(timeout=5)
            with patch.object(process, "_exchange", side_effect=self.laya_worker.WorkerError("MLX_WORKER_STOPPED")):
                with self.assertRaisesRegex(self.laya_worker.WorkerError, "MLX_WORKER_STOPPED"):
                    process.predict("state", {}, timeout=5)
        self.assertIsNone(process._process, "a non-recoverable failure must discard the dead worker")

    def test_spawn_launch_failure_is_normalized(self):
        process = self._process()
        with patch.object(self.laya_worker.subprocess, "Popen", side_effect=OSError("synthetic launch failure")):
            with self.assertRaisesRegex(self.laya_worker.WorkerError, "OFFLINE_SANDBOX_LAUNCH_FAILED"):
                process._spawn()

    def test_close_tears_down_the_process_and_removes_the_scratch_directory(self):
        process = self._process()
        temp_root = process._temp_root
        self.assertTrue(temp_root.is_dir())
        with self._popen_returning(self._reply({"ok": True, "result": {"ready": True, "default_device": "cpu",
                                                                        "device_source": "x"}})):
            process.warmup(timeout=5)
        process.close()
        self.assertIsNone(process._process)
        self.assertFalse(temp_root.exists())
        process.close()  # idempotent: must not raise on a second close


class LayaWorkerAdditionalCoverageTests(unittest.TestCase):
    """A second pass closing the highest-value remaining gaps after the first round of
    laya_worker.py tests (which landed at ~90%, too close to the floor for comfort): the
    _closing-during-operation races in warmup()/predict(), _load_locked()'s own
    manifest-changed-after-construction check, _stop_locked()'s stdin-close error swallow,
    require_native_sandbox()'s REAL (unpatched) body, an untrusted-venv sandbox_profile()
    branch, and a few _exchange() protocol-framing edge cases."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.python_path = self.root / "python-stub"
        self.python_path.write_text("#!/bin/sh\n")
        self.python_path.chmod(0o700)
        from src.adl.providers import laya_worker

        self.laya_worker = laya_worker
        self._interpreter_patch = patch.object(laya_worker, "_approved_interpreter", return_value=True)
        self._sandbox_patch = patch.object(laya_worker.SandboxedLayaProcess, "require_native_sandbox", Mock())
        self._interpreter_patch.start()
        self._sandbox_patch.start()
        self.addCleanup(self._interpreter_patch.stop)
        self.addCleanup(self._sandbox_patch.stop)

    def _process(self, **kwargs):
        artifact, config, *_ = _build_laya_artifact(self.root)
        process = self.laya_worker.SandboxedLayaProcess(artifact, config, self.python_path, **kwargs)
        self.addCleanup(process.close)
        return process, artifact, config

    def test_approved_interpreter_installed_path_that_does_not_exist_is_false_not_a_crash(self):
        # This test wants the REAL _approved_interpreter, not setUp's always-True patch --
        # pause it, then restart before teardown's addCleanup(self._interpreter_patch.stop)
        # runs (stopping an already-stopped patcher raises RuntimeError).
        self._interpreter_patch.stop()
        try:
            with patch("src.adl.providers.laya_worker.home_root", return_value=self.root / "no-such-home"):
                missing = self.root / "no-such-home" / "mlx-env" / "bin" / "python"
                self.assertFalse(self.laya_worker._approved_interpreter(missing))
        finally:
            self._interpreter_patch.start()

    def test_verify_artifact_reports_a_genuinely_missing_model_path_as_not_ready(self):
        from src.adl.providers.laya_worker import PreparedArtifact, WorkerError, _verify_artifact

        artifact = PreparedArtifact("p", "a" * 40, "b" * 64, self.root / "does-not-exist.safetensors")
        with self.assertRaisesRegex(WorkerError, "ARTIFACT_NOT_READY"):
            _verify_artifact(artifact)

    def test_require_native_sandbox_real_body_covers_both_branches(self):
        """Every OTHER test in this file patches require_native_sandbox() away entirely (it
        would otherwise probe the real host); this is the one place its actual body runs, so
        setUp's own class-wide patch is paused for the duration of this test only."""
        self._sandbox_patch.stop()
        try:
            cls = self.laya_worker.SandboxedLayaProcess
            real_binary = self.root / "fake-sandbox-exec"
            real_binary.write_text("#!/bin/sh\n")
            with patch.object(self.laya_worker.platform, "system", return_value="Darwin"), \
                 patch.object(cls, "SANDBOX_BINARY", real_binary):
                cls.require_native_sandbox()  # Darwin + a present binary: must not raise
            with patch.object(self.laya_worker.platform, "system", return_value="Linux"), \
                 patch.object(cls, "SANDBOX_BINARY", real_binary):
                with self.assertRaisesRegex(self.laya_worker.WorkerError, "OFFLINE_SANDBOX_UNAVAILABLE"):
                    cls.require_native_sandbox()
            with patch.object(self.laya_worker.platform, "system", return_value="Darwin"), \
                 patch.object(cls, "SANDBOX_BINARY", self.root / "does-not-exist-binary"):
                with self.assertRaisesRegex(self.laya_worker.WorkerError, "OFFLINE_SANDBOX_UNAVAILABLE"):
                    cls.require_native_sandbox()
        finally:
            self._sandbox_patch.start()

    def test_sandbox_profile_refuses_a_venv_pointing_outside_approved_system_roots(self):
        venv_python = self.root / "venv" / "bin" / "python"
        venv_python.parent.mkdir(parents=True)
        venv_python.write_text("#!/bin/sh\n")
        venv_python.chmod(0o700)
        not_approved = self.root / "not-approved-python"
        not_approved.write_text("#!/bin/sh\n")  # must exist: resolve(strict=True) requires it
        (self.root / "venv" / "pyvenv.cfg").write_text(f"executable = {not_approved}\n")
        with self.assertRaisesRegex(self.laya_worker.WorkerError, "PYTHON_RUNTIME_UNTRUSTED"):
            self.laya_worker.SandboxedLayaProcess.sandbox_profile(python=venv_python)

    def test_stop_locked_swallows_an_oserror_closing_stdin(self):
        process, _artifact, _config = self._process()
        fake = SimpleNamespace(
            stdin=SimpleNamespace(close=lambda: (_ for _ in ()).throw(OSError("simulated"))),
            stdout=SimpleNamespace(close=lambda: None))
        process._process = fake
        with patch.object(self.laya_worker.SandboxedLayaProcess, "kill_process_group", staticmethod(lambda p: None)):
            process._stop_locked()  # must not raise despite stdin.close() raising OSError
        self.assertIsNone(process._process)

    def test_manifest_changed_after_construction_is_caught_before_ever_spawning(self):
        process, _artifact, config = self._process()
        manifest_path = Path(config["artifact_manifest"])
        manifest_path.write_bytes(manifest_path.read_bytes() + b"\n")
        manifest_path.chmod(0o600)
        with patch.object(self.laya_worker.subprocess, "Popen", side_effect=AssertionError(
                "must not spawn once the manifest digest has already changed")):
            with self.assertRaisesRegex(self.laya_worker.WorkerError, "ARTIFACT_CONFIG_CHANGED"):
                process.warmup(timeout=5)

    def test_closing_flag_set_during_spawn_is_reported_once_the_process_exists(self):
        process, _artifact, _config = self._process()
        spawned: list = []

        def spawn_then_close():
            process._closing.set()
            fake = _FakeSandboxWorkerProcess(b"")
            spawned.append(fake)
            self.addCleanup(fake.close_extra_fds)
            return fake

        with patch.object(process, "_spawn", side_effect=spawn_then_close):
            with self.assertRaisesRegex(self.laya_worker.WorkerError, "WORKER_CLOSED"):
                process.warmup(timeout=5)

    def test_warmup_and_predict_refuse_immediately_once_closing_is_already_set(self):
        process, _artifact, _config = self._process()
        process._closing.set()
        with self.assertRaisesRegex(self.laya_worker.WorkerError, "WORKER_CLOSED"):
            process.warmup(timeout=5)
        with self.assertRaisesRegex(self.laya_worker.WorkerError, "WORKER_CLOSED"):
            process.predict("s", {}, timeout=5)

    def test_warmup_and_predict_report_deadline_exceeded_when_the_lock_is_already_held(self):
        process, _artifact, _config = self._process()
        process._lock.acquire()
        try:
            with self.assertRaisesRegex(self.laya_worker.WorkerError, "WORKER_DEADLINE_EXCEEDED"):
                process.warmup(timeout=0.1)
            with self.assertRaisesRegex(self.laya_worker.WorkerError, "WORKER_DEADLINE_EXCEEDED"):
                process.predict("s", {}, timeout=0.1)
        finally:
            process._lock.release()

    def test_closing_right_after_the_lock_is_reported_as_closed_for_warmup_and_predict(self):
        """Also covers the SAME code's re-check inside each method's own `except Exception`
        handler: the WORKER_CLOSED raised here is itself caught by that handler, which checks
        _closing again and re-raises the identical code -- one scenario, two guarded lines.

        A real threading.Lock's `acquire` attribute is read-only (cannot be unittest.mock
        patched directly), so this swaps in a thin acquire/release wrapper around the real
        lock instead."""
        process, _artifact, _config = self._process()
        real_lock = process._lock

        class _ClosingLock:
            def acquire(self, *a, **kw):
                result = real_lock.acquire(*a, **kw)
                process._closing.set()
                return result

            def release(self):
                return real_lock.release()

            def __enter__(self):
                self.acquire()
                return self

            def __exit__(self, *_exc):
                self.release()
                return False

        process._lock = _ClosingLock()
        with self.assertRaisesRegex(self.laya_worker.WorkerError, "WORKER_CLOSED"):
            process.warmup(timeout=5)
        process._closing.clear()
        with self.assertRaisesRegex(self.laya_worker.WorkerError, "WORKER_CLOSED"):
            process.predict("s", {}, timeout=5)

    def test_closing_during_a_failing_load_reports_closed_not_the_original_error(self):
        process, _artifact, _config = self._process()

        def failing_load(_deadline):
            process._closing.set()
            raise self.laya_worker.WorkerError("MLX_LOAD_NOT_READY")

        with patch.object(process, "_load_locked", side_effect=failing_load):
            with self.assertRaisesRegex(self.laya_worker.WorkerError, "WORKER_CLOSED"):
                process.warmup(timeout=5)
        process._closing.clear()
        with patch.object(process, "_load_locked", side_effect=failing_load):
            with self.assertRaisesRegex(self.laya_worker.WorkerError, "WORKER_CLOSED"):
                process.predict("s", {}, timeout=5)

    def test_exchange_protocol_framing_edge_cases(self):
        with self.subTest("oversized_request_is_refused_before_any_write"):
            process, _artifact, _config = self._process()
            fake = _FakeSandboxWorkerProcess(b"")  # alive enough for the size check
            self.addCleanup(fake.close_extra_fds)
            process._process = fake
            with self.assertRaisesRegex(self.laya_worker.WorkerError, "MLX_REQUEST_SIZE"):
                process._exchange({"kind": "x", "pad": "y" * 200_000}, time.monotonic() + 5)

        for name, reply in (
            ("non_json_reply_line", b"not json at all\n"),
            ("ok_true_but_result_not_a_dict", json.dumps({"ok": True, "result": "not-a-dict"}).encode() + b"\n"),
            ("error_code_not_matching_the_fixed_shape_falls_back",
             json.dumps({"ok": False, "error": "lowercase-not-allowed"}).encode() + b"\n"),
        ):
            with self.subTest(name=name):
                # A fresh process (and fresh artifact/manifest) per case: reusing one process
                # would leave `_process` already set from a prior case, so _load_locked()
                # would short-circuit as "already loaded" and never reach the new fake reply.
                process, _artifact, _config = self._process()
                fake_process = _FakeSandboxWorkerProcess(reply)
                self.addCleanup(fake_process.close_extra_fds)
                with patch.object(self.laya_worker.subprocess, "Popen", return_value=fake_process):
                    with self.assertRaisesRegex(self.laya_worker.WorkerError, "MLX_PROTOCOL_INVALID"):
                        process.warmup(timeout=5)


# ============================================================================================
# src/adl/api/__init__.py -- source-only local management contract (49% baseline)
# ============================================================================================

class ApiPackageInitTests(unittest.TestCase):
    """SetupSession and the two request()/request_ui() facades are pure, dependency-free
    functions with zero I/O -- baseline gaps were simply "never called," not "hard to test."""

    def test_setup_session_create_validates_port_and_lifetime(self):
        from src.adl.api import SetupSession

        for bad_port in (0, 70000, True, -1):
            with self.subTest(bad_port=bad_port):
                with self.assertRaisesRegex(ValueError, "INVALID_SETUP_PORT"):
                    SetupSession.create(port=bad_port)
        for bad_lifetime in (0, 3601, -1):
            with self.subTest(bad_lifetime=bad_lifetime):
                with self.assertRaisesRegex(ValueError, "INVALID_SETUP_LIFETIME"):
                    SetupSession.create(port=54321, lifetime_seconds=bad_lifetime)
        session = SetupSession.create(port=54321, lifetime_seconds=10)
        self.assertEqual(session.port, 54321)
        self.assertGreater(session.expires_at, time.monotonic())

    def test_setup_session_matches_requires_both_session_and_csrf_and_is_not_expired(self):
        from src.adl.api import SetupSession

        session = SetupSession.create(port=1, lifetime_seconds=1)
        self.assertTrue(session.matches(session=session.session, csrf=session.csrf))
        self.assertFalse(session.matches(session="wrong", csrf=session.csrf))
        self.assertFalse(session.matches(session=session.session, csrf="wrong"))
        self.assertFalse(session.matches(session=None, csrf=session.csrf))
        with patch("src.adl.api.time.monotonic", return_value=session.expires_at + 1):
            self.assertFalse(session.matches(session=session.session, csrf=session.csrf))

    def test_canonical_path_accepts_only_a_clean_v1_path(self):
        from src.adl.api import _canonical_path

        self.assertTrue(_canonical_path("/v1/status"))
        for bad in (123, "status", "/v1/status/", "/v1//status", "/v1/a%2fb", "/v1/a\\b",
                   "/v1/a?b", "/v1/a#b", "/v1/a;b"):
            with self.subTest(bad=bad):
                self.assertFalse(_canonical_path(bad))

    def test_status_reports_local_runtime_with_no_credential_exposure(self):
        from src.adl.api import _status

        self.assertEqual(_status(), {"status": 200, "runtime": "local", "credentials": "not_exposed",
                                     "browser_storage": []})

    def test_request_enforces_origin_host_csrf_session_and_route(self):
        from src.adl.api import ALLOWED_ORIGIN, request

        with self.subTest("non_canonical_path"):
            self.assertEqual(request("GET", "not-canonical", origin=ALLOWED_ORIGIN, csrf="c", session="s"),
                             {"status": 403, "code": "NON_CANONICAL_PATH"})
        with self.subTest("bad_origin"):
            self.assertEqual(request("GET", "/v1/status", origin="http://evil.example", csrf="c", session="s"),
                             {"status": 403, "code": "CROSS_ORIGIN_REJECTED"})
        with self.subTest("bad_host"):
            self.assertEqual(request("GET", "/v1/status", origin=ALLOWED_ORIGIN, host="evil", csrf="c", session="s"),
                             {"status": 403, "code": "CROSS_ORIGIN_REJECTED"})
        with self.subTest("blank_csrf"):
            self.assertEqual(request("GET", "/v1/status", origin=ALLOWED_ORIGIN, csrf="", session="s"),
                             {"status": 403, "code": "CROSS_ORIGIN_REJECTED"})
        with self.subTest("forbidden_path"):
            self.assertEqual(request("GET", "/v1/consents", origin=ALLOWED_ORIGIN, csrf="c", session="s"),
                             {"status": 403, "code": "MCP_CANNOT_CREATE_CONSENT"})
        with self.subTest("non_get_method"):
            self.assertEqual(request("POST", "/v1/status", origin=ALLOWED_ORIGIN, csrf="c", session="s"),
                             {"status": 403, "code": "MCP_CANNOT_CREATE_CONSENT"})
        with self.subTest("missing_session"):
            self.assertEqual(request("GET", "/v1/status", origin=ALLOWED_ORIGIN, csrf="c", session=None),
                             {"status": 401, "code": "SESSION_REQUIRED"})
        with self.subTest("unknown_route"):
            self.assertEqual(request("GET", "/v1/unknown", origin=ALLOWED_ORIGIN, csrf="c", session="s"),
                             {"status": 404, "code": "ROUTE_NOT_FOUND"})
        with self.subTest("status_route"):
            self.assertEqual(request("GET", "/v1/status", origin=ALLOWED_ORIGIN, csrf="c", session="s")["status"], 200)

    def test_request_ui_defaults_match_request_when_no_authority_is_supplied(self):
        from src.adl.api import ALLOWED_ORIGIN, request_ui

        self.assertEqual(request_ui("GET", "not-canonical", origin=ALLOWED_ORIGIN, csrf="c", session="s"),
                         {"status": 403, "code": "NON_CANONICAL_PATH"})
        self.assertEqual(request_ui("GET", "/v1/status", origin="http://evil", csrf="c", session="s"),
                         {"status": 403, "code": "CROSS_ORIGIN_REJECTED"})
        self.assertEqual(request_ui("GET", "/v1/status", origin=ALLOWED_ORIGIN, csrf="c", session=None),
                         {"status": 401, "code": "SESSION_REQUIRED"})
        self.assertEqual(request_ui("GET", "/v1/status", origin=ALLOWED_ORIGIN, csrf="c", session="s"),
                         {"status": 403, "code": "SESSION_AUTHORITY_REQUIRED"})

    def test_request_ui_with_an_authority_requires_a_genuine_session_match(self):
        from src.adl.api import SetupSession, request_ui

        authority = SetupSession.create(port=54321, lifetime_seconds=10)
        expected_origin = f"http://127.0.0.1:{authority.port}"
        with self.subTest("wrong_origin_for_this_authoritys_port"):
            self.assertEqual(request_ui("GET", "/v1/status", origin="http://127.0.0.1", csrf="c",
                                        session="s", authority=authority),
                             {"status": 403, "code": "CROSS_ORIGIN_REJECTED"})
        with self.subTest("session_present_but_authority_rejects_it"):
            self.assertEqual(request_ui("GET", "/v1/status", origin=expected_origin,
                                        host=f"127.0.0.1:{authority.port}", csrf="wrong-csrf",
                                        session="wrong-session", authority=authority),
                             {"status": 403, "code": "SESSION_INVALID"})
        with self.subTest("fully_valid_status_request"):
            result = request_ui("GET", "/v1/status", origin=expected_origin, host=f"127.0.0.1:{authority.port}",
                                csrf=authority.csrf, session=authority.session, authority=authority)
            self.assertEqual(result["status"], 200)
        with self.subTest("valid_session_but_unknown_route"):
            result = request_ui("GET", "/v1/unknown", origin=expected_origin, host=f"127.0.0.1:{authority.port}",
                                csrf=authority.csrf, session=authority.session, authority=authority)
            self.assertEqual(result, {"status": 404, "code": "ROUTE_NOT_FOUND"})
        with self.subTest("valid_session_but_non_get_method"):
            result = request_ui("POST", "/v1/status", origin=expected_origin, host=f"127.0.0.1:{authority.port}",
                                csrf=authority.csrf, session=authority.session, authority=authority)
            self.assertEqual(result, {"status": 404, "code": "ROUTE_NOT_FOUND"})


# ============================================================================================
# src/adl/api/setup_server.py -- private one-session loopback wizard (72% baseline)
# ============================================================================================

class _StubSetupController:
    """A minimal, purpose-built duck-typed stand-in for SetupController -- used ONLY to reach
    a getattr(...) fallback branch in setup_server.py's do_GET that the REAL SetupController
    can never take (it always defines local_ready as a bound method)."""

    def __init__(self, workspace, *, policy_exists=False, load_existing=None, local_attestor=None):
        self.workspace = workspace
        self._policy_exists = policy_exists
        self._load_existing = load_existing or (lambda: {})
        self.local_attestor = local_attestor

    def policy_exists(self):
        return self._policy_exists

    def load_existing(self):
        return self._load_existing()


def _serve_setup(test_case: unittest.TestCase, controller, *, host_inventory=lambda: []):
    from src.adl.api.setup_server import SetupServer

    server = SetupServer(("127.0.0.1", 0), controller, host_inventory=host_inventory)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    test_case.addCleanup(thread.join, timeout=2)
    test_case.addCleanup(server.server_close)
    test_case.addCleanup(server.shutdown)
    return server


def _get_setup_page(port: int) -> tuple[str, str, bytes]:
    status, headers, body = _http_request(port, "GET", "/setup")
    assert status == 200, body
    cookie = headers["Set-Cookie"].split(";", 1)[0]
    csrf = re.search(rb"name=['\"]csrf['\"] value=['\"]([^'\"]+)", body).group(1).decode()
    return cookie, csrf, body


class SetupServerConstructionTests(unittest.TestCase):
    """SetupServer.__init__ and its background session-expiry watcher. Baseline: the
    loopback-only guard (line 65) and the watcher's own shutdown call (lines 79-80) were both
    unreached."""

    def test_a_non_loopback_bind_address_is_refused(self):
        from src.adl.api.setup_controller import SetupController
        from src.adl.api.setup_server import SetupServer

        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "project"
            workspace.mkdir()
            controller = SetupController(workspace, keychain=_FakeKeychainStore(),
                                         bridge=lambda *_: None, start=lambda *_: None)
            with self.assertRaisesRegex(ValueError, "SETUP_LOOPBACK_ONLY"):
                SetupServer(("0.0.0.0", 0), controller, host_inventory=lambda: [])

    def test_an_already_expired_session_makes_the_watcher_shut_the_server_down(self):
        from src.adl.api import SetupSession
        from src.adl.api.setup_controller import SetupController
        from src.adl.api.setup_server import SetupServer

        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "project"
            workspace.mkdir()
            controller = SetupController(workspace, keychain=_FakeKeychainStore(),
                                         bridge=lambda *_: None, start=lambda *_: None)
            expired = SetupSession(port=1, session="s", csrf="c", expires_at=time.monotonic() - 1)
            with patch.object(SetupSession, "create", return_value=expired):
                server = SetupServer(("127.0.0.1", 0), controller, host_inventory=lambda: [])
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            thread.join(timeout=2)
            try:
                self.assertFalse(thread.is_alive(),
                                 "an already-expired session must make serve_forever return promptly")
            finally:
                server.server_close()


class SetupServerRateLimitAndRoutingTests(unittest.TestCase):
    """The fixed 100-request cap and the GET route table (/setup, /status, everything else)
    are shared, security-relevant plumbing -- baseline missed the cap entirely (128-129) and
    two of the three GET routes' edge cases (165-166, 168-169)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workspace = Path(self.tmp.name) / "project"
        self.workspace.mkdir()

    def _controller(self):
        from src.adl.api.setup_controller import SetupController

        return SetupController(self.workspace, keychain=_FakeKeychainStore(),
                               bridge=lambda *_: None, start=lambda *_: None)

    def test_the_101st_request_is_rejected_with_a_fixed_rate_limit_code(self):
        server = _serve_setup(self, self._controller())
        server.request_count = 100
        status, _headers, body = _http_request(server.server_port, "GET", "/setup")
        self.assertEqual(status, 429)
        self.assertIn(b"SETUP_REQUEST_LIMIT", body)

    def test_a_path_that_is_neither_setup_nor_status_is_404(self):
        server = _serve_setup(self, self._controller())
        status, _headers, body = _http_request(server.server_port, "GET", "/nonexistent")
        self.assertEqual(status, 404)
        self.assertIn(b"ROUTE_NOT_FOUND", body)

    def test_an_expired_session_makes_get_setup_report_410_when_the_watcher_is_disabled(self):
        """Isolates do_GET's OWN session-expiry check (line 168-169) from the background
        watcher thread (which would otherwise race to shut the whole server down first): the
        watcher is disabled by pre-setting its stop event before serve_forever ever starts it."""
        server_container = []
        from src.adl.api.setup_server import SetupServer

        controller = self._controller()
        server = SetupServer(("127.0.0.1", 0), controller, host_inventory=lambda: [])
        server._watch_stop.set()  # disable the expiry watcher for this test only
        server.setup_session = dataclasses.replace(server.setup_session, expires_at=time.monotonic() - 1)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, timeout=2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        status, _headers, body = _http_request(server.server_port, "GET", "/setup")
        self.assertEqual(status, 410)
        self.assertIn(b"SETUP_SESSION_EXPIRED", body)
        del server_container


class SetupServerHostAndSessionTests(unittest.TestCase):
    """Origin/Host binding and the /status route's own session-cookie check -- the entire
    trust boundary between "the local wizard tab" and any other local process."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workspace = Path(self.tmp.name) / "project"
        self.workspace.mkdir()
        from src.adl.api.setup_controller import SetupController

        self.controller = SetupController(self.workspace, keychain=_FakeKeychainStore(),
                                          bridge=lambda *_: None, start=lambda *_: None)

    def test_mismatched_host_header_is_rejected_on_get(self):
        server = _serve_setup(self, self.controller)
        status, _headers, body = _http_request(
            server.server_port, "GET", "/setup", extra_headers={"Host": "evil.example:1"})
        self.assertEqual(status, 403)
        self.assertIn(b"CROSS_ORIGIN_REJECTED", body)

    def test_status_without_a_cookie_or_with_a_wrong_cookie_is_403(self):
        server = _serve_setup(self, self.controller)
        status, _headers, body = _http_request(server.server_port, "GET", "/status")
        self.assertEqual(status, 403)
        self.assertIn(b"SESSION_INVALID", body)
        status, _headers, body = _http_request(
            server.server_port, "GET", "/status", cookie="adl_setup=totally-wrong-session-value")
        self.assertEqual(status, 403)
        self.assertIn(b"SESSION_INVALID", body)

    def test_status_reports_not_enrolled_when_load_existing_raises(self):
        controller = _StubSetupController(self.workspace, policy_exists=False,
                                          load_existing=lambda: (_ for _ in ()).throw(RuntimeError("no policy")))
        server = _serve_setup(self, controller)
        cookie, _csrf, _page = _get_setup_page(server.server_port)
        status, _headers, body = _http_request(server.server_port, "GET", "/status", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertIn(b"Not enrolled", body)

    def test_status_reports_enrolled_policy_fields_without_reflecting_a_credential(self):
        existing = {"provider": "typesafe", "decision_mode": "jev-public", "data_classification": "public",
                    "max_calls_per_day": 20, "max_bytes_per_day": 20000, "auto_prepare_jev": False,
                    "local_laya_enabled": False}
        controller = _StubSetupController(self.workspace, policy_exists=True, load_existing=lambda: existing)
        server = _serve_setup(self, controller)
        cookie, _csrf, _page = _get_setup_page(server.server_port)
        status, _headers, body = _http_request(server.server_port, "GET", "/status", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertIn(b"TypeSafe", body)
        self.assertIn(b"20 attempts/day", body)


class SetupServerLocalReadyDuckTypeTests(unittest.TestCase):
    """do_GET checks `getattr(controller, 'local_ready', None)` before falling back to
    `local_attestor is not None`. The real SetupController always defines local_ready, so this
    fallback (lines 177-178) can only be reached with a duck-typed stand-in."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workspace = Path(self.tmp.name) / "project"
        self.workspace.mkdir()

    def test_local_attestor_present_without_a_local_ready_method_enables_local_options(self):
        controller = _StubSetupController(self.workspace, local_attestor=lambda cfg: cfg)
        self.assertFalse(hasattr(controller, "local_ready"))
        server = _serve_setup(self, controller)
        _cookie, _csrf, page = _get_setup_page(server.server_port)
        self.assertNotIn(b"value='hybrid' disabled", page)

    def test_no_local_attestor_and_no_local_ready_disables_local_options(self):
        controller = _StubSetupController(self.workspace, local_attestor=None)
        server = _serve_setup(self, controller)
        _cookie, _csrf, page = _get_setup_page(server.server_port)
        self.assertIn(b"value='hybrid' disabled", page)


class SetupServerHostInventoryFailureTests(unittest.TestCase):
    """A third-party host_inventory() callback failing must degrade to an empty host list, not
    break the setup page (lines 212-213)."""

    def test_host_inventory_exception_yields_an_empty_but_still_200_page(self):
        from src.adl.api.setup_controller import SetupController

        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "project"
            workspace.mkdir()
            controller = SetupController(workspace, keychain=_FakeKeychainStore(),
                                         bridge=lambda *_: None, start=lambda *_: None)
            server = _serve_setup(self, controller,
                                  host_inventory=lambda: (_ for _ in ()).throw(RuntimeError("boom")))
            status, _headers, body = _http_request(server.server_port, "GET", "/setup")
        self.assertEqual(status, 200)
        self.assertIn(b"<ul></ul>", body)


class SetupServerFormBoundaryTests(unittest.TestCase):
    """_form()'s own defenses: cross-origin POSTs, non-form content types, missing/garbled/
    oversized Content-Length, malformed bodies, unexpected keys, and cookie/session mismatch.
    Baseline: 252-253, 255-256, 259-261, 263-264, 269-271, 273-274, 277-278 were all unreached."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workspace = Path(self.tmp.name) / "project"
        self.workspace.mkdir()
        from src.adl.api.setup_controller import SetupController

        self.controller = SetupController(self.workspace, keychain=_FakeKeychainStore(),
                                          bridge=lambda *_: None, start=lambda *_: None)
        self.server = _serve_setup(self, self.controller)
        self.cookie, self.csrf, _page = _get_setup_page(self.server.server_port)

    def _fields(self, **overrides):
        return {"csrf": self.csrf, "provider": "typesafe", "mode": "jev-public",
                "days": "1", "daily_calls": "2", "daily_bytes": "2000", **overrides}

    def test_missing_origin_header_is_cross_origin_rejected(self):
        port = self.server.server_port
        status, body = _raw_http_request(
            port, "POST", "/preview",
            {"Host": f"127.0.0.1:{port}", "Content-Type": "application/x-www-form-urlencoded",
             "Content-Length": "0", "Cookie": self.cookie})
        self.assertEqual(status, 403)
        self.assertIn(b"CROSS_ORIGIN_REJECTED", body)

    def test_non_form_content_type_is_rejected(self):
        port = self.server.server_port
        body_bytes = json.dumps(self._fields()).encode()
        status, body = _raw_http_request(
            port, "POST", "/preview",
            {"Host": f"127.0.0.1:{port}", "Origin": f"http://127.0.0.1:{port}",
             "Content-Type": "application/json", "Content-Length": str(len(body_bytes)),
             "Cookie": self.cookie}, body_bytes)
        self.assertEqual(status, 415)
        self.assertIn(b"SETUP_FORM_REQUIRED", body)

    def test_a_transfer_encoding_header_is_rejected_even_with_a_correct_content_type(self):
        port = self.server.server_port
        status, body = _raw_http_request(
            port, "POST", "/preview",
            {"Host": f"127.0.0.1:{port}", "Origin": f"http://127.0.0.1:{port}",
             "Content-Type": "application/x-www-form-urlencoded", "Transfer-Encoding": "chunked",
             "Cookie": self.cookie}, b"0\r\n\r\n")
        self.assertEqual(status, 415)

    def test_missing_or_non_numeric_content_length_is_rejected(self):
        port = self.server.server_port
        for length_header in (None, "not-a-number"):
            with self.subTest(length_header=length_header):
                headers = {"Host": f"127.0.0.1:{port}", "Origin": f"http://127.0.0.1:{port}",
                          "Content-Type": "application/x-www-form-urlencoded", "Cookie": self.cookie}
                if length_header is not None:
                    headers["Content-Length"] = length_header
                status, body = _raw_http_request(port, "POST", "/preview", headers, b"")
                self.assertEqual(status, 411)
                self.assertIn(b"SETUP_LENGTH_REQUIRED", body)

    def test_zero_or_oversized_content_length_is_rejected(self):
        port = self.server.server_port
        for length in (0, 20_000):
            with self.subTest(length=length):
                headers = {"Host": f"127.0.0.1:{port}", "Origin": f"http://127.0.0.1:{port}",
                          "Content-Type": "application/x-www-form-urlencoded",
                          "Content-Length": str(length), "Cookie": self.cookie}
                status, body = _raw_http_request(port, "POST", "/preview", headers, b"")
                self.assertEqual(status, 413)
                self.assertIn(b"SETUP_BODY_TOO_LARGE", body)

    def test_malformed_url_encoded_body_is_rejected(self):
        port = self.server.server_port
        # No '=' at all: parse_qs(..., strict_parsing=True) cannot split this into a key/value
        # pair and raises ValueError -- unlike an invalid percent-escape, which urllib.parse
        # silently leaves as literal text rather than rejecting.
        malformed = b"a-field-with-no-equals-sign-at-all"
        headers = {"Host": f"127.0.0.1:{port}", "Origin": f"http://127.0.0.1:{port}",
                  "Content-Type": "application/x-www-form-urlencoded",
                  "Content-Length": str(len(malformed)), "Cookie": self.cookie}
        status, body = _raw_http_request(port, "POST", "/preview", headers, malformed)
        self.assertEqual(status, 400)
        self.assertIn(b"SETUP_FORM_INVALID", body)

    def test_unexpected_form_key_is_rejected(self):
        status, _headers, body = _http_request(
            self.server.server_port, "POST", "/preview",
            values={**self._fields(), "totally_unexpected_key": "x"}, cookie=self.cookie)
        self.assertEqual(status, 400)
        self.assertIn(b"SETUP_FORM_INVALID", body)

    def test_missing_or_mismatched_cookie_is_session_invalid(self):
        status, _headers, body = _http_request(
            self.server.server_port, "POST", "/preview", values=self._fields(), cookie=None)
        self.assertEqual(status, 403)
        self.assertIn(b"SESSION_INVALID", body)
        status, _headers, body = _http_request(
            self.server.server_port, "POST", "/preview", values=self._fields(),
            cookie="adl_setup=not-the-real-session")
        self.assertEqual(status, 403)
        self.assertIn(b"SESSION_INVALID", body)


class SetupServerChoiceAndRouteTests(unittest.TestCase):
    """_choice()'s mode/legacy-field conflict handling and do_POST's own route table --
    baseline: 293, 309-310, 316-317, 322-323 were unreached."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workspace = Path(self.tmp.name) / "project"
        self.workspace.mkdir()
        from src.adl.api.setup_controller import SetupController

        self.controller = SetupController(self.workspace, keychain=_FakeKeychainStore(),
                                          bridge=lambda *_: None, start=lambda *_: None)
        self.server = _serve_setup(self, self.controller)
        self.cookie, self.csrf, _page = _get_setup_page(self.server.server_port)

    def _post(self, path, **fields):
        return _http_request(self.server.server_port, "POST", path,
                             values={"csrf": self.csrf, **fields}, cookie=self.cookie)

    def test_supplying_both_mode_and_the_legacy_classification_field_is_rejected(self):
        status, _headers, body = self._post(
            "/preview", provider="typesafe", mode="jev-public", classification="public",
            days="1", daily_calls="2", daily_bytes="2000")
        self.assertEqual(status, 400)
        self.assertIn(b"SETUP_SCOPE_INVALID", body)

    def test_an_unrecognized_mode_value_is_rejected(self):
        status, _headers, body = self._post(
            "/preview", provider="typesafe", mode="not-a-real-mode",
            days="1", daily_calls="2", daily_bytes="2000")
        self.assertEqual(status, 400)
        self.assertIn(b"SETUP_SCOPE_INVALID", body)

    def test_post_to_an_unknown_path_is_404(self):
        status, _headers, body = self._post("/does-not-exist")
        self.assertEqual(status, 404)
        self.assertIn(b"ROUTE_NOT_FOUND", body)

    def test_preview_carrying_a_credential_or_confirm_field_is_rejected(self):
        for extra_field in ("credential", "confirm"):
            with self.subTest(extra_field=extra_field):
                status, _headers, body = self._post(
                    "/preview", provider="typesafe", mode="jev-public", days="1",
                    daily_calls="2", daily_bytes="2000", **{extra_field: "yes"})
                self.assertEqual(status, 400)
                self.assertIn(b"SETUP_FORM_INVALID", body)

    def test_post_request_count_over_the_limit_is_also_rate_limited(self):
        """Covers do_POST's OWN call site for _limited() (a second, separately-executed line
        from the one GET already exercises)."""
        self.server.request_count = 100
        status, _headers, body = self._post("/preview", provider="typesafe", mode="jev-public",
                                            days="1", daily_calls="2", daily_bytes="2000")
        self.assertEqual(status, 429)
        self.assertIn(b"SETUP_REQUEST_LIMIT", body)


class SetupServerPreviewAndApplyFlowTests(unittest.TestCase):
    """The full preview -> apply lifecycle, including the local_laya hidden field on the
    legacy (non-mode) form shape, double-apply rejection, and the SetupError-to-status-code
    mapping. Baseline: 346, 348-349, 388-389, 406-414 were unreached."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workspace = Path(self.tmp.name) / "project"
        self.workspace.mkdir()

    def _controller(self, **kwargs):
        from src.adl.api.setup_controller import SetupController

        return SetupController(self.workspace, keychain=_FakeKeychainStore(),
                               bridge=lambda *_: None, start=lambda *_: None, **kwargs)

    def test_legacy_form_shape_with_local_laya_on_surfaces_a_hidden_field_on_preview(self):
        model = {"repository": "aac6fef/laya-mlx", "revision": "a" * 40, "weight_sha256": "b" * 64,
                 "model_dir": "/synthetic/model", "artifact_manifest": "/synthetic/manifest"}
        controller = self._controller(local_config=lambda: model, local_attestor=lambda cfg: dict(cfg))
        server = _serve_setup(self, controller)
        cookie, csrf, _page = _get_setup_page(server.server_port)
        status, _headers, body = _http_request(
            server.server_port, "POST", "/preview",
            values={"csrf": csrf, "provider": "typesafe", "classification": "internal-minimized",
                   "days": "1", "daily_calls": "2", "daily_bytes": "2000", "local_laya": "on"},
            cookie=cookie)
        self.assertEqual(status, 200)
        self.assertIn(b"<input type='hidden' name='local_laya' value='on'>", body)

    def test_applying_twice_is_rejected_the_second_time(self):
        controller = self._controller()
        server = _serve_setup(self, controller)
        # A successful /apply schedules server.shutdown() on a background thread (see
        # setup_server.py's do_POST). Neutralize it so the listening socket survives long
        # enough for this test's OWN second request to deterministically reach the
        # `self.server.applied` guard, instead of racing a real shutdown.
        server.shutdown = lambda: None
        cookie, csrf, _page = _get_setup_page(server.server_port)
        fields = {"csrf": csrf, "provider": "typesafe", "mode": "jev-public", "days": "1",
                  "daily_calls": "2", "daily_bytes": "2000", "generic": "on"}
        status, _headers, review = _http_request(
            server.server_port, "POST", "/preview", values=fields, cookie=cookie)
        self.assertEqual(status, 200)
        nonce = re.search(rb"name=['\"]review_nonce['\"] value=['\"]([^'\"]+)", review).group(1).decode()
        apply_fields = {**fields, "review_nonce": nonce, "confirm": "yes", "credential": "unit-test-key-value"}
        status, _headers, _body = _http_request(
            server.server_port, "POST", "/apply", values=apply_fields, cookie=cookie)
        self.assertEqual(status, 200)
        status, _headers, body = _http_request(
            server.server_port, "POST", "/apply", values=apply_fields, cookie=cookie)
        self.assertEqual(status, 409)
        self.assertIn(b"SETUP_ALREADY_APPLIED", body)

    def test_existing_policy_review_required_maps_to_409(self):
        """preview() and apply() share the identical `except (AutoError, OSError): raise
        SetupError('EXISTING_POLICY_REVIEW_REQUIRED')` guard around load_existing(), so a
        corrupt existing policy is refused at the very first /preview call -- there is no
        well-formed request that reaches /apply in this state."""
        from jev_auto.common import AutoError

        controller = self._controller(policy_exists=lambda: True,
                                      load_existing=lambda: (_ for _ in ()).throw(AutoError("CORRUPT_POLICY")))
        server = _serve_setup(self, controller)
        cookie, csrf, _page = _get_setup_page(server.server_port)
        fields = {"csrf": csrf, "provider": "typesafe", "mode": "jev-public", "days": "1",
                  "daily_calls": "2", "daily_bytes": "2000", "generic": "on"}
        status, _headers, body = _http_request(
            server.server_port, "POST", "/preview", values=fields, cookie=cookie)
        self.assertEqual(status, 409)
        self.assertIn(b"EXISTING_POLICY_REVIEW_REQUIRED", body)

    def test_an_unexpected_exception_during_apply_is_a_fixed_500(self):
        controller = self._controller()
        server = _serve_setup(self, controller)
        cookie, csrf, _page = _get_setup_page(server.server_port)
        fields = {"csrf": csrf, "provider": "typesafe", "mode": "jev-public", "days": "1",
                  "daily_calls": "2", "daily_bytes": "2000", "generic": "on"}
        status, _headers, review = _http_request(
            server.server_port, "POST", "/preview", values=fields, cookie=cookie)
        self.assertEqual(status, 200)
        nonce = re.search(rb"name=['\"]review_nonce['\"] value=['\"]([^'\"]+)", review).group(1).decode()
        apply_fields = {**fields, "review_nonce": nonce, "confirm": "yes", "credential": "unit-test-key-value"}
        with patch.object(type(controller), "apply", side_effect=RuntimeError("/Users/secret/path leaked")):
            status, _headers, body = _http_request(
                server.server_port, "POST", "/apply", values=apply_fields, cookie=cookie)
        self.assertEqual(status, 500)
        self.assertIn(b"SETUP_INTERNAL_FAILURE", body)
        self.assertNotIn(b"secret", body)


class SetupServerCliEntrypointTests(unittest.TestCase):
    """build_controller() and main() -- the process entrypoint used by jev_auto to launch the
    wizard. main() never opens a real browser (ADL_SETUP_NO_BROWSER=1) and never binds past
    this test (SetupServer is patched to a fake that returns immediately)."""

    def test_build_controller_wires_the_real_local_attestor(self):
        from src.adl.api.local_attestor import attest_local_config
        from src.adl.api.setup_server import build_controller

        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "project"
            workspace.mkdir()
            controller = build_controller(workspace)
        self.assertIs(controller.local_attestor, attest_local_config)

    def test_main_builds_a_server_for_the_cli_supplied_workspace_and_never_opens_a_browser(self):
        from src.adl.api import setup_server

        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "project"
            workspace.mkdir()
            # `with X as server:` looks up __enter__/__exit__ on the TYPE, not the instance,
            # so a SimpleNamespace with those as instance attributes does not work here --
            # MagicMock pre-wires its magic methods (including context-manager support).
            fake_server = MagicMock()
            fake_server.__enter__.return_value = fake_server
            fake_server.__exit__.return_value = False
            fake_server.server_port = 54321
            with patch.object(setup_server, "SetupServer", return_value=fake_server) as server_cls, \
                 patch.object(setup_server, "webbrowser") as fake_browser, \
                 patch.dict(os.environ, {"ADL_SETUP_NO_BROWSER": "1"}), \
                 patch.object(sys, "argv", ["adl-setup", "--workspace", str(workspace)]):
                setup_server.main()
            server_cls.assert_called_once()
            fake_browser.open.assert_not_called()


# ============================================================================================
# src/adl/api/setup_controller.py -- first-run consent controller (74% baseline)
# ============================================================================================

class SetupControllerReadLocalConfigTests(unittest.TestCase):
    """_read_local_config() tries the current install path, then the legacy path, and returns
    None rather than raising if neither is a valid private record. Baseline: 89-92 unreached."""

    def test_current_path_missing_falls_back_to_the_legacy_path(self):
        from src.adl.api.setup_controller import SetupController

        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            legacy = home / ".local" / "state" / "qualixar-jev-auto" / "mlx-installation.json"
            legacy.parent.mkdir(parents=True, mode=0o700)
            legacy.write_text('{"repository": "aac6fef/laya-mlx", "revision": "' + "a" * 40 + '"}')
            legacy.chmod(0o600)
            with patch.object(Path, "home", return_value=home), \
                 patch.dict(os.environ, {"XDG_STATE_HOME": str(home / "new-state")}):
                found = SetupController._read_local_config()
        self.assertEqual(found["repository"], "aac6fef/laya-mlx")

    def test_neither_path_present_returns_none_not_an_exception(self):
        from src.adl.api.setup_controller import SetupController

        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            with patch.object(Path, "home", return_value=home), \
                 patch.dict(os.environ, {"XDG_STATE_HOME": str(home / "new-state")}):
                self.assertIsNone(SetupController._read_local_config())

    def test_a_present_but_unsafe_legacy_record_returns_none(self):
        from src.adl.api.setup_controller import SetupController

        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            legacy = home / ".local" / "state" / "qualixar-jev-auto" / "mlx-installation.json"
            legacy.parent.mkdir(parents=True, mode=0o700)
            legacy.write_text("{}")
            legacy.chmod(0o644)  # world-readable: read_private() must refuse this
            with patch.object(Path, "home", return_value=home), \
                 patch.dict(os.environ, {"XDG_STATE_HOME": str(home / "new-state")}):
                self.assertIsNone(SetupController._read_local_config())


class SetupControllerAttestedLocalModelTests(unittest.TestCase):
    """_attested_local_model() is the single choke point every local-Laya branch depends on.
    Baseline: the "config missing repository/revision" short-circuit (148-149) was unreached."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workspace = Path(self.tmp.name) / "project"
        self.workspace.mkdir()

    def _controller(self, **kwargs):
        from src.adl.api.setup_controller import SetupController

        return SetupController(self.workspace, keychain=_FakeKeychainStore(),
                               bridge=lambda *_: None, start=lambda *_: None, **kwargs)

    def test_no_attestor_at_all_is_none(self):
        controller = self._controller(local_attestor=None)
        self.assertIsNone(controller._attested_local_model())
        self.assertFalse(controller.local_ready())

    def test_local_config_missing_repository_or_revision_short_circuits_to_none(self):
        for bad_config in ({}, {"repository": "x"}, {"revision": "y"}, "not-a-dict"):
            with self.subTest(bad_config=bad_config):
                controller = self._controller(local_config=lambda: bad_config,
                                              local_attestor=lambda cfg: (_ for _ in ()).throw(
                                                  AssertionError("attestor must not run without repo+revision")))
                self.assertIsNone(controller._attested_local_model())

    def test_attestor_exception_is_swallowed_to_none(self):
        model = {"repository": "aac6fef/laya-mlx", "revision": "a" * 40}
        controller = self._controller(local_config=lambda: model,
                                      local_attestor=lambda cfg: (_ for _ in ()).throw(RuntimeError("boom")))
        self.assertIsNone(controller._attested_local_model())

    def test_attestor_returning_a_changed_identity_is_none(self):
        model = {"repository": "aac6fef/laya-mlx", "revision": "a" * 40}
        controller = self._controller(local_config=lambda: model,
                                      local_attestor=lambda cfg: {**cfg, "revision": "b" * 40})
        self.assertIsNone(controller._attested_local_model())


class SetupControllerPreviewTests(unittest.TestCase):
    """preview() is the ONLY code path allowed to read an existing policy before a user has
    confirmed anything. Baseline: 162-163 (corrupt existing policy), 166 (pending, not ready,
    existing is ignored), 172 (local model not attested) were unreached."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workspace = Path(self.tmp.name) / "project"
        self.workspace.mkdir()

    def _controller(self, **kwargs):
        from src.adl.api.setup_controller import SetupController

        return SetupController(self.workspace, keychain=_FakeKeychainStore(),
                               bridge=lambda *_: None, start=lambda *_: None, **kwargs)

    def _choice(self, **overrides):
        from src.adl.api.setup_controller import SetupChoice

        base = dict(provider="typesafe", data_classification="public", days=1, daily_calls=2,
                    daily_bytes=2000, generic_query_enabled=True)
        return SetupChoice(**{**base, **overrides})

    def test_a_corrupt_existing_policy_is_reviewed_as_a_fixed_error(self):
        from jev_auto.common import AutoError
        from src.adl.api.setup_controller import SetupError

        # preview()'s except clause only catches (AutoError, OSError) -- a bare RuntimeError
        # would (correctly) propagate uncaught instead of being folded into a SetupError.
        controller = self._controller(policy_exists=lambda: True,
                                      load_existing=lambda: (_ for _ in ()).throw(AutoError("CORRUPT_POLICY")))
        with self.assertRaisesRegex(SetupError, "EXISTING_POLICY_REVIEW_REQUIRED"):
            controller.preview(self._choice())

    def test_an_existing_but_still_pending_policy_is_not_treated_as_an_upgrade(self):
        existing = {"provider": "typesafe", "setup_origin": "local_wizard", "setup_state": "pending"}
        controller = self._controller(policy_exists=lambda: True, load_existing=lambda: existing)
        review = controller.preview(self._choice())
        self.assertFalse(review["upgrade"])
        self.assertIsNone(review["current_data_classification"])

    def test_local_route_without_an_attested_model_is_refused(self):
        from src.adl.api.setup_controller import SetupError

        controller = self._controller(local_attestor=None)
        with self.assertRaisesRegex(SetupError, "LOCAL_MODEL_NOT_ATTESTED"):
            controller.preview(self._choice(provider="laya-mlx", data_classification="restricted"))


class SetupControllerKeychainErrorMappingTests(unittest.TestCase):
    """_keychain_error() must only ever surface the fixed, screened set of codes -- an
    unrecognized KeychainError message must be flattened to a generic code, never echoed.
    Baseline: 240-242 unreached."""

    def test_unrecognized_keychain_error_code_is_flattened(self):
        from src.adl.api.keychain import KeychainError
        from src.adl.api.setup_controller import SetupController

        result = SetupController._keychain_error(KeychainError("SOME_BRAND_NEW_CODE_NOBODY_MAPPED"))
        self.assertEqual(str(result), "KEYCHAIN_OPERATION_FAILED")

    def test_every_recognized_keychain_error_code_passes_through_unchanged(self):
        from src.adl.api.keychain import KeychainError
        from src.adl.api.setup_controller import SetupController

        for code in ("KEYCHAIN_WRITE_FAILED", "KEYCHAIN_READ_FAILED", "KEYCHAIN_ITEM_MISSING",
                    "KEYCHAIN_CREDENTIAL_INVALID", "KEYCHAIN_UNAVAILABLE"):
            with self.subTest(code=code):
                self.assertEqual(str(SetupController._keychain_error(KeychainError(code))), code)


class SetupControllerApplyUpgradeTests(unittest.TestCase):
    """_apply_upgrade() is only reachable through apply() on an already-"ready" policy, but is
    exercised directly here to pin its own guard order. Baseline: 196, 200-201, 213-220
    unreached."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workspace = Path(self.tmp.name) / "project"
        self.workspace.mkdir()
        from src.adl.api.setup_controller import SetupController

        self.keychain = _FakeKeychainStore()
        self.keychain.put("typesafe", "unit-test-existing-credential")
        from jev_auto.settings import make_policy

        # A hand-rolled minimal dict is not enough here: _apply_upgrade() runs the existing
        # dict through the SAME validate_policy() that requires every DEFAULTS field (request
        # byte budgets, retention, etc) -- only make_policy() produces something that survives
        # that check, matching how a "ready" policy would actually look on disk.
        self.existing = make_policy(self.workspace, "typesafe", days=1, data_classification="public",
                                    generic_query_enabled=True, setup_origin="local_wizard",
                                    setup_state="ready", routes={})
        # policy_exists/load_existing must reflect self.existing so that preview() actually
        # recognizes it as a reviewable "ready" policy and sets self._previewed_existing --
        # otherwise EVERY _apply_upgrade() call below hits its own first guard
        # (SETUP_REVIEW_REQUIRED) regardless of what is under test.
        self.controller = SetupController(self.workspace, keychain=self.keychain,
                                          bridge=lambda *_: None, start=lambda *_: None,
                                          policy_exists=lambda: True,
                                          load_existing=lambda: self.existing)

    def _choice(self, **overrides):
        from src.adl.api.setup_controller import SetupChoice

        base = dict(provider="typesafe", data_classification="internal-minimized", days=2,
                    daily_calls=20, daily_bytes=20000, generic_query_enabled=True)
        return SetupChoice(**{**base, **overrides})

    def test_calling_apply_upgrade_without_a_prior_preview_is_refused(self):
        from src.adl.api.setup_controller import SetupError

        with self.assertRaisesRegex(SetupError, "SETUP_REVIEW_REQUIRED"):
            self.controller._apply_upgrade(self._choice(), self.existing, None)

    def test_supplying_a_credential_on_an_upgrade_is_also_refused(self):
        from src.adl.api.setup_controller import SetupError

        self.controller.preview(self._choice())
        with self.assertRaisesRegex(SetupError, "SETUP_REVIEW_REQUIRED"):
            self.controller._apply_upgrade(self._choice(), self.existing, "a-credential")

    def test_a_keychain_error_during_upgrade_verification_is_mapped(self):
        from src.adl.api.keychain import KeychainError
        from src.adl.api.setup_controller import SetupError

        self.controller.preview(self._choice())
        with patch.object(self.keychain, "get", side_effect=KeychainError("KEYCHAIN_READ_FAILED")):
            with self.assertRaisesRegex(SetupError, "KEYCHAIN_READ_FAILED"):
                self.controller._apply_upgrade(self._choice(), self.existing, None)

    def test_enabling_local_laya_on_upgrade_without_an_attested_model_is_refused(self):
        from src.adl.api.setup_controller import SetupError

        choice = self._choice(local_laya_enabled=True)
        self.controller.local_attestor = None
        # Bypass preview()'s own earlier LOCAL_MODEL_NOT_ATTESTED raise so _apply_upgrade's
        # OWN (second) check is what is actually under test.
        self.controller._previewed_existing = self.existing
        from src.adl.api.setup_controller import SetupError as _SetupError

        with self.assertRaisesRegex(_SetupError, "LOCAL_MODEL_NOT_ATTESTED"):
            self.controller._apply_upgrade(choice, self.existing, None)

    def test_a_local_model_that_changed_since_the_reviewed_preview_is_refused(self):
        from src.adl.api.setup_controller import SetupError

        model_v1 = {"repository": "aac6fef/laya-mlx", "revision": "a" * 40, "weight_sha256": "b" * 64,
                   "model_dir": "/m", "artifact_manifest": "/a"}
        model_v2 = {**model_v1, "revision": "c" * 40}
        calls = {"n": 0}

        def local_config():
            calls["n"] += 1
            return model_v1 if calls["n"] == 1 else model_v2

        self.controller.local_config = local_config
        self.controller.local_attestor = lambda cfg: dict(cfg)
        choice = self._choice(local_laya_enabled=True)
        self.controller.preview(choice)
        with self.assertRaisesRegex(SetupError, "LOCAL_MODEL_CHANGED_SINCE_REVIEW"):
            self.controller._apply_upgrade(choice, self.existing, None)

    def test_a_successful_upgrade_updates_routes_and_returns_pending_host_trust(self):
        # The real `upgrade` (replace_reviewed_policy) verifies self._previewed_existing
        # against the actual on-disk policy.json; self.existing is an in-memory fixture that
        # was never written there, so `upgrade` is stubbed to isolate _apply_upgrade's OWN
        # payload-shape and status-code logic (already covered end-to-end, disk included, by
        # tests/test_setup_smoke.py's controller.apply()-based upgrade tests).
        self.controller.upgrade = lambda *a: None
        self.controller.preview(self._choice())
        result = self.controller._apply_upgrade(self._choice(), self.existing, None)
        self.assertEqual(result["status"], "UPDATED_PENDING_HOST_TRUST")


class SetupControllerFinishTests(unittest.TestCase):
    """_finish() is the last step before a policy is considered live -- it must map a keychain
    failure to a fixed code and must never leave the caller thinking a partially-applied
    bridge/start/complete sequence succeeded. Baseline: 246-249, 254-255 unreached."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workspace = Path(self.tmp.name) / "project"
        self.workspace.mkdir()
        from src.adl.api.setup_controller import SetupController

        self.keychain = _FakeKeychainStore()
        self.controller = SetupController(self.workspace, keychain=self.keychain,
                                          bridge=lambda *_: None, start=lambda *_: None)

    def test_missing_keychain_credential_at_finish_time_is_mapped(self):
        from src.adl.api.setup_controller import SetupError

        with self.assertRaisesRegex(SetupError, "KEYCHAIN_ITEM_MISSING"):
            self.controller._finish({"provider": "typesafe"})

    def test_a_bridge_or_start_failure_after_a_verified_credential_is_partial_policy_saved(self):
        from src.adl.api.setup_controller import SetupError

        self.keychain.put("typesafe", "unit-test-credential")
        self.controller.bridge = lambda *_: (_ for _ in ()).throw(RuntimeError("bridge down"))
        with self.assertRaisesRegex(SetupError, "SETUP_PARTIAL_POLICY_SAVED"):
            self.controller._finish({"provider": "typesafe"}, keychain_verified=True)


class SetupControllerApplyTests(unittest.TestCase):
    """apply() end to end: confirmation, existing-policy recovery states, the local-route
    consent/attestation chain, make_policy failures, and the keychain credential-replacement
    flow. Baseline: 268, 274-275, 278-284, 289, 292, 295, 297, 319-320, 332, 337-341, 344-345,
    349-350 unreached."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workspace = Path(self.tmp.name) / "project"
        self.workspace.mkdir()

    def _controller(self, **kwargs):
        from src.adl.api.setup_controller import SetupController

        keychain = kwargs.pop("keychain", None) or _FakeKeychainStore()
        return SetupController(self.workspace, keychain=keychain, bridge=lambda *_: None,
                               start=lambda *_: None, **kwargs), keychain

    def _choice(self, **overrides):
        from src.adl.api.setup_controller import SetupChoice

        base = dict(provider="typesafe", data_classification="public", days=1, daily_calls=2,
                    daily_bytes=2000, generic_query_enabled=True)
        return SetupChoice(**{**base, **overrides})

    def test_unconfirmed_apply_is_refused_before_anything_else(self):
        from src.adl.api.setup_controller import SetupError

        controller, _keychain = self._controller()
        with self.assertRaisesRegex(SetupError, "USER_CONFIRMATION_REQUIRED"):
            controller.apply(self._choice(), credential="x", confirmed=False)

    def test_a_corrupt_existing_policy_at_apply_time_is_refused(self):
        from jev_auto.common import AutoError
        from src.adl.api.setup_controller import SetupError

        controller, _keychain = self._controller(
            policy_exists=lambda: True, load_existing=lambda: (_ for _ in ()).throw(AutoError("CORRUPT_POLICY")))
        with self.assertRaisesRegex(SetupError, "EXISTING_POLICY_REVIEW_REQUIRED"):
            controller.apply(self._choice(), credential="unit-test-key-value", confirmed=True)

    def test_existing_policy_from_a_foreign_setup_origin_is_refused(self):
        from src.adl.api.setup_controller import SetupError

        existing = {"provider": "typesafe", "setup_origin": "some-other-tool", "setup_state": "pending"}
        controller, _keychain = self._controller(policy_exists=lambda: True, load_existing=lambda: existing)
        with self.assertRaisesRegex(SetupError, "EXISTING_POLICY_REVIEW_REQUIRED"):
            controller.apply(self._choice(), credential="unit-test-key-value", confirmed=True)

    def test_pending_policy_with_a_changed_choice_digest_is_a_recovery_conflict(self):
        from src.adl.api.setup_controller import SetupError

        existing = {"provider": "typesafe", "setup_origin": "local_wizard", "setup_state": "pending",
                   "setup_choice_digest": "not-the-real-digest"}
        controller, _keychain = self._controller(policy_exists=lambda: True, load_existing=lambda: existing)
        with self.assertRaisesRegex(SetupError, "SETUP_RECOVERY_CONFLICT"):
            controller.apply(self._choice(), credential=None, confirmed=True)

    def test_pending_policy_with_a_matching_digest_but_a_new_credential_requires_review(self):
        from src.adl.api.setup_controller import SetupChoice, SetupController, SetupError

        choice = self._choice()
        digest = SetupController(self.workspace)._choice_digest(choice)
        existing = {"provider": "typesafe", "setup_origin": "local_wizard", "setup_state": "pending",
                   "setup_choice_digest": digest}
        controller, _keychain = self._controller(policy_exists=lambda: True, load_existing=lambda: existing)
        with self.assertRaisesRegex(SetupError, "SETUP_RECOVERY_KEY_REVIEW_REQUIRED"):
            controller.apply(choice, credential="a-new-credential", confirmed=True)

    def test_pending_policy_with_a_matching_digest_and_no_credential_just_finishes(self):
        from src.adl.api.setup_controller import SetupController

        choice = self._choice()
        digest = SetupController(self.workspace)._choice_digest(choice)
        existing = {"provider": "typesafe", "setup_origin": "local_wizard", "setup_state": "pending",
                   "setup_choice_digest": digest}
        keychain = _FakeKeychainStore()
        keychain.put("typesafe", "unit-test-credential")
        controller, _keychain = self._controller(
            policy_exists=lambda: True, load_existing=lambda: existing, keychain=keychain)
        # The real `complete` (transition_setup_policy_ready) expects `existing` to already be
        # the exact bytes on disk at state_dir/policy.json; this test's `existing` is a
        # hand-built in-memory fixture, so `complete` is stubbed out -- apply()'s OWN
        # "pending, matching digest" branch (not the disk-persistence machinery) is under test.
        controller.complete = lambda *_: None
        result = controller.apply(choice, credential=None, confirmed=True)
        self.assertEqual(result["status"], "ENROLLED_PENDING_HOST_TRUST")

    def test_a_credential_supplied_for_the_laya_only_local_route_is_refused(self):
        from src.adl.api.setup_controller import SetupError

        controller, _keychain = self._controller()
        with self.assertRaisesRegex(SetupError, "LOCAL_ROUTE_DOES_NOT_USE_CLOUD_KEY"):
            controller.apply(self._choice(provider="laya-mlx", data_classification="restricted"),
                            credential="should-not-be-here", confirmed=True)

    def test_local_config_missing_repository_or_revision_is_local_model_not_prepared(self):
        from src.adl.api.setup_controller import SetupError

        controller, _keychain = self._controller(local_config=lambda: {})
        with self.assertRaisesRegex(SetupError, "LOCAL_MODEL_NOT_PREPARED"):
            controller.apply(self._choice(provider="laya-mlx", data_classification="restricted"),
                            credential=None, confirmed=True)

    def test_local_config_present_but_attestation_fails_is_local_model_not_attested(self):
        from src.adl.api.setup_controller import SetupError

        model = {"repository": "aac6fef/laya-mlx", "revision": "a" * 40}
        controller, _keychain = self._controller(
            local_config=lambda: model, local_attestor=lambda cfg: (_ for _ in ()).throw(RuntimeError("boom")))
        with self.assertRaisesRegex(SetupError, "LOCAL_MODEL_NOT_ATTESTED"):
            controller.apply(self._choice(provider="laya-mlx", data_classification="restricted"),
                            credential=None, confirmed=True)

    def test_local_model_changed_between_preview_and_apply_is_refused(self):
        from src.adl.api.setup_controller import SetupError

        model_v1 = {"repository": "aac6fef/laya-mlx", "revision": "a" * 40, "weight_sha256": "b" * 64,
                   "model_dir": "/m", "artifact_manifest": "/a"}
        model_v2 = {**model_v1, "revision": "c" * 40}
        calls = {"n": 0}

        def local_config():
            calls["n"] += 1
            return model_v1 if calls["n"] <= 2 else model_v2

        controller, _keychain = self._controller(local_config=local_config,
                                                 local_attestor=lambda cfg: dict(cfg))
        choice = self._choice(provider="laya-mlx", data_classification="restricted")
        controller.preview(choice)
        with self.assertRaisesRegex(SetupError, "LOCAL_MODEL_CHANGED_SINCE_REVIEW"):
            controller.apply(choice, credential=None, confirmed=True)

    def test_make_policy_failure_is_a_fixed_scope_invalid_error(self):
        from src.adl.api import setup_controller
        from src.adl.api.setup_controller import SetupError

        controller, keychain = self._controller()
        keychain.put("typesafe", "unit-test-credential")
        with patch.object(setup_controller, "make_policy", side_effect=ValueError("synthetic")):
            with self.assertRaisesRegex(SetupError, "SETUP_SCOPE_INVALID"):
                controller.apply(self._choice(), credential=None, confirmed=True)

    def test_a_purely_hosted_policy_never_carries_an_mlx_field(self):
        controller, keychain = self._controller()
        keychain.put("typesafe", "unit-test-credential")
        persisted = {}
        controller.persist = lambda _workspace, policy: persisted.update(policy)
        # persist() is faked (captures the policy instead of writing it), so the real
        # `complete` (which reads back the just-persisted file) has nothing to read; only
        # apply()'s OWN policy-shape logic is under test here.
        controller.complete = lambda *_: None
        controller.apply(self._choice(), credential=None, confirmed=True)
        self.assertNotIn("mlx", persisted)

    def test_keychain_replacement_flow_new_differing_and_confirmed_replacement(self):
        controller, keychain = self._controller()
        controller.persist = lambda *_: None
        controller.complete = lambda *_: None
        controller.apply(self._choice(), credential="first-credential-value", confirmed=True)
        self.assertEqual(keychain.get("typesafe"), "first-credential-value")

        from src.adl.api.setup_controller import SetupError

        with self.assertRaisesRegex(SetupError, "KEYCHAIN_REPLACEMENT_REQUIRES_CONFIRMATION"):
            controller.apply(self._choice(), credential="second-different-value", confirmed=True)

        controller.apply(self._choice(), credential="second-different-value", confirmed=True,
                        replace_existing_credential=True)
        self.assertEqual(keychain.get("typesafe"), "second-different-value")

    def test_no_credential_supplied_reads_the_existing_one_and_maps_a_missing_key(self):
        from src.adl.api.setup_controller import SetupError

        controller, _keychain = self._controller()  # empty keychain: nothing stored yet
        controller.persist = lambda *_: None
        with self.assertRaisesRegex(SetupError, "KEYCHAIN_ITEM_MISSING"):
            controller.apply(self._choice(), credential=None, confirmed=True)

    def test_persist_failure_reports_partial_keychain_stored_when_a_new_key_was_written(self):
        from src.adl.api.setup_controller import SetupError

        controller, keychain = self._controller()
        controller.persist = lambda *_: (_ for _ in ()).throw(OSError("disk full"))
        with self.assertRaisesRegex(SetupError, "SETUP_PARTIAL_KEYCHAIN_STORED"):
            controller.apply(self._choice(), credential="brand-new-value", confirmed=True)
        self.assertEqual(keychain.get("typesafe"), "brand-new-value")

    def test_persist_failure_reports_plain_write_failed_when_no_new_key_was_written(self):
        from src.adl.api.setup_controller import SetupError

        controller, keychain = self._controller()
        keychain.put("typesafe", "already-there")
        controller.persist = lambda *_: (_ for _ in ()).throw(OSError("disk full"))
        with self.assertRaisesRegex(SetupError, "SETUP_POLICY_WRITE_FAILED"):
            controller.apply(self._choice(), credential=None, confirmed=True)


# ============================================================================================
# src/adl/queries/typed.py -- bounded typed query compiler (73% baseline)
# ============================================================================================

class QueriesTypedBoundedShapeTests(unittest.TestCase):
    """_bounded_shape() is the FIRST gate on any state/questions payload -- depth, node count,
    key length, and string length. Baseline: 68, 71, 74, 77-79 unreached."""

    def test_excessive_nesting_depth_is_rejected(self):
        from src.adl.queries.typed import QueryError, _bounded_shape

        nested: Any = "leaf"
        for _ in range(40):
            nested = {"n": nested}
        with self.assertRaisesRegex(QueryError, "REQUEST_DEPTH_OR_SIZE"):
            _bounded_shape(nested)

    def test_excessive_node_count_is_rejected(self):
        from src.adl.queries.typed import QueryError, _bounded_shape

        huge = {str(i): i for i in range(20_001)}
        with self.assertRaisesRegex(QueryError, "REQUEST_DEPTH_OR_SIZE"):
            _bounded_shape(huge)

    def test_a_dict_key_that_is_not_a_short_string_is_rejected(self):
        from src.adl.queries.typed import QueryError, _bounded_shape

        with self.subTest("non_string_key"):
            with self.assertRaisesRegex(QueryError, "REQUEST_SHAPE_INVALID"):
                _bounded_shape({1: "x"})
        with self.subTest("overlong_key"):
            with self.assertRaisesRegex(QueryError, "REQUEST_SHAPE_INVALID"):
                _bounded_shape({"k" * 129: "x"})

    def test_an_overlong_string_leaf_is_rejected(self):
        from src.adl.queries.typed import QueryError, _bounded_shape

        with self.assertRaisesRegex(QueryError, "REQUEST_TOO_LARGE"):
            _bounded_shape("x" * 8001)

    def test_an_unsupported_leaf_type_is_rejected(self):
        from src.adl.queries.typed import QueryError, _bounded_shape

        with self.assertRaisesRegex(QueryError, "REQUEST_SHAPE_INVALID"):
            _bounded_shape({1, 2, 3})  # a set has no JSON-safe shape

    def test_well_formed_shapes_of_every_supported_kind_pass(self):
        from src.adl.queries.typed import _bounded_shape

        _bounded_shape({"a": [1, 2.5, True, None, "ok", {"nested": "value"}]})  # must not raise


class QueriesTypedValidateQuestionsTests(unittest.TestCase):
    """_validate_questions() is stricter than jevkit.contract's version: bounded question_id
    regex, a closed key set per question, and a tighter overall byte budget. Baseline: 84, 87,
    89, 93, 100, 105, 110, 112 unreached."""

    def test_bad_question_id_is_rejected(self):
        from src.adl.queries.typed import QueryError, _validate_questions

        with self.assertRaisesRegex(QueryError, "QUESTION_INVALID"):
            _validate_questions({"1bad-start": {"type": "noul", "instructions": "x"}})

    def test_question_with_an_unexpected_extra_key_is_rejected(self):
        from src.adl.queries.typed import QueryError, _validate_questions

        with self.assertRaisesRegex(QueryError, "QUESTION_INVALID"):
            _validate_questions({"q": {"type": "noul", "instructions": "x", "unexpected": 1}})

    def test_unknown_type_or_blank_instructions_is_rejected(self):
        from src.adl.queries.typed import QueryError, _validate_questions

        with self.subTest("unknown_type"):
            with self.assertRaisesRegex(QueryError, "QUESTION_INVALID"):
                _validate_questions({"q": {"type": "essay", "instructions": "x"}})
        with self.subTest("blank_instructions"):
            with self.assertRaisesRegex(QueryError, "QUESTION_INVALID"):
                _validate_questions({"q": {"type": "noul", "instructions": "   "}})

    def test_choice_criteria_shape_is_bounded(self):
        from src.adl.queries.typed import QueryError, _validate_questions

        with self.subTest("not_a_dict"):
            with self.assertRaisesRegex(QueryError, "QUESTION_INVALID"):
                _validate_questions({"q": {"type": "choice", "instructions": "x", "criteria": ["a", "b"]}})
        with self.subTest("too_few"):
            with self.assertRaisesRegex(QueryError, "QUESTION_INVALID"):
                _validate_questions({"q": {"type": "choice", "instructions": "x", "criteria": {"a": "A"}}})
        with self.subTest("blank_label"):
            with self.assertRaisesRegex(QueryError, "QUESTION_INVALID"):
                _validate_questions({"q": {"type": "choice", "instructions": "x",
                                          "criteria": {"": "A", "b": "B"}}})

    def test_score_criteria_shape_is_bounded(self):
        from src.adl.queries.typed import QueryError, _validate_questions

        with self.subTest("not_a_list"):
            with self.assertRaisesRegex(QueryError, "QUESTION_INVALID"):
                _validate_questions({"q": {"type": "score", "instructions": "x", "criteria": {"a": "A"}}})
        with self.subTest("too_many"):
            with self.assertRaisesRegex(QueryError, "QUESTION_INVALID"):
                _validate_questions({"q": {"type": "score", "instructions": "x",
                                          "criteria": [str(i) for i in range(11)]}})

    def test_noul_criteria_shape_is_bounded_when_present(self):
        from src.adl.queries.typed import QueryError, _validate_questions

        with self.subTest("bad_key"):
            with self.assertRaisesRegex(QueryError, "QUESTION_INVALID"):
                _validate_questions({"q": {"type": "noul", "instructions": "x",
                                          "criteria": {"maybe": "sometimes"}}})
        with self.subTest("valid_criteria"):
            _validate_questions({"q": {"type": "noul", "instructions": "x",
                                       "criteria": {"true": "yes", "false": "no"}}})  # must not raise

    def test_oversized_question_payload_exceeds_the_byte_budget(self):
        from src.adl.queries.typed import QueryError, _validate_questions

        big_criteria = {f"option-{i:03d}": "x" * 80 for i in range(200)}
        with self.assertRaisesRegex(QueryError, "QUESTION_INVALID"):
            _validate_questions({"q": {"type": "choice", "instructions": "Pick one", "criteria": big_criteria}})


class QueriesTypedPrepareQueryWithPolicyTests(unittest.TestCase):
    """_prepare_query_with_policy() is the single choke point between a caller's raw
    state/questions and either a hosted or local compiled query. Baseline: 125, 127, 129, 136,
    147, 149, 154-155, 163, 174, 182-183, 193, 199 unreached."""

    def _questions(self):
        return {"decision": {"type": "noul", "instructions": "Is this synthetic?"}}

    def test_automatic_is_always_forbidden_at_this_layer(self):
        from src.adl.queries.typed import QueryError, _prepare_query_with_policy

        with self.assertRaisesRegex(QueryError, "GENERIC_AUTO_FORBIDDEN"):
            _prepare_query_with_policy("s", self._questions(), provider="typesafe",
                                       policy={"provider": "typesafe", "generic_query_enabled": True},
                                       data_classification="public", automatic=True)

    def test_policy_not_a_dict_or_generic_disabled_is_rejected(self):
        from src.adl.queries.typed import QueryError, _prepare_query_with_policy

        for policy in ("not-a-dict", {"provider": "typesafe", "generic_query_enabled": False}):
            with self.subTest(policy=policy):
                with self.assertRaisesRegex(QueryError, "GENERIC_QUERY_NOT_ENROLLED"):
                    _prepare_query_with_policy("s", self._questions(), provider="typesafe",
                                               policy=policy, data_classification="public")

    def test_non_dict_routes_is_policy_invalid(self):
        from src.adl.queries.typed import QueryError, _prepare_query_with_policy

        policy = {"provider": "typesafe", "generic_query_enabled": True, "routes": "not-a-dict"}
        with self.assertRaisesRegex(QueryError, "POLICY_INVALID"):
            _prepare_query_with_policy("s", self._questions(), provider="typesafe", policy=policy,
                                       data_classification="public")

    def test_switching_provider_away_from_the_enrolled_generic_route_needs_consent(self):
        from src.adl.queries.typed import QueryError, _prepare_query_with_policy

        policy = {"provider": "typesafe", "generic_query_enabled": True, "routes": {}}
        with self.assertRaisesRegex(QueryError, "PROCESSOR_SWITCH_NEEDS_CONSENT"):
            _prepare_query_with_policy("s", self._questions(), provider="openrouter", policy=policy,
                                       data_classification="public")

    def test_unsupported_provider_name_is_rejected(self):
        from src.adl.queries.typed import QueryError, _prepare_query_with_policy

        policy = {"provider": "not-a-real-provider", "generic_query_enabled": True, "routes": {}}
        with self.assertRaisesRegex(QueryError, "PROCESSOR_SWITCH_NEEDS_CONSENT"):
            _prepare_query_with_policy("s", self._questions(), provider="not-a-real-provider",
                                       policy=policy, data_classification="public")

    def test_invalid_data_classification_value_is_rejected(self):
        from src.adl.queries.typed import QueryError, _prepare_query_with_policy

        policy = {"provider": "typesafe", "generic_query_enabled": True, "routes": {},
                 "data_classification": "public"}
        with self.assertRaisesRegex(QueryError, "DATA_CLASSIFICATION_INVALID"):
            _prepare_query_with_policy("s", self._questions(), provider="typesafe", policy=policy,
                                       data_classification="not-a-real-classification")

    def test_restricted_hosted_query_requires_jev_maximum_enrollment(self):
        from src.adl.queries.typed import QueryError, _prepare_query_with_policy

        policy = {"provider": "typesafe", "generic_query_enabled": True, "routes": {},
                 "data_classification": "restricted", "decision_mode": "hybrid"}
        with self.assertRaisesRegex(QueryError, "REMOTE_RESTRICTED_DATA"):
            _prepare_query_with_policy("s", self._questions(), provider="typesafe", policy=policy,
                                       data_classification="restricted")

    def test_requesting_a_classification_the_workspace_never_enrolled_is_rejected(self):
        from src.adl.queries.typed import QueryError, _prepare_query_with_policy

        policy = {"provider": "typesafe", "generic_query_enabled": True, "routes": {},
                 "data_classification": "public"}
        with self.assertRaisesRegex(QueryError, "DATA_CLASSIFICATION_NOT_ENROLLED"):
            _prepare_query_with_policy("s", self._questions(), provider="typesafe", policy=policy,
                                       data_classification="internal-minimized")

    def test_state_of_an_unsupported_type_is_rejected(self):
        from src.adl.queries.typed import QueryError, _prepare_query_with_policy

        policy = {"provider": "typesafe", "generic_query_enabled": True, "routes": {},
                 "data_classification": "public"}
        with self.assertRaisesRegex(QueryError, "STATE_INVALID"):
            _prepare_query_with_policy(12345, self._questions(), provider="typesafe", policy=policy,
                                       data_classification="public")

    def test_a_secret_looking_state_is_blocked(self):
        from src.adl.queries.typed import QueryError, _prepare_query_with_policy

        policy = {"provider": "typesafe", "generic_query_enabled": True, "routes": {},
                 "data_classification": "public"}
        with self.assertRaisesRegex(QueryError, "INPUT_DATA_BLOCKED"):
            _prepare_query_with_policy("contact leaker@example.com now", self._questions(),
                                       provider="typesafe", policy=policy, data_classification="public")

    def test_non_finite_state_values_are_rejected_at_the_final_encoding_step(self):
        """_bounded_shape() permits any float instance (including NaN/Infinity -- it only
        checks `isinstance(current, (int, float, bool))`), so a non-finite value survives all
        the way to the final `_canonical(...)` payload build, which DOES reject it
        (json.dumps(..., allow_nan=False)). This is the only reachable path to _canonical()'s
        own except-clause (lines 57-58) from this module: every other call site validates or
        re-derives its strings from data that already passed a stricter, purely-string check."""
        from src.adl.queries.typed import QueryError, _prepare_query_with_policy

        policy = {"provider": "typesafe", "generic_query_enabled": True, "routes": {},
                 "data_classification": "public"}
        with self.assertRaisesRegex(QueryError, "REQUEST_INVALID"):
            _prepare_query_with_policy({"value": float("nan")}, self._questions(), provider="typesafe",
                                       policy=policy, data_classification="public")

    def test_laya_route_requires_a_dict_shaped_mlx_policy_field(self):
        from src.adl.queries.typed import QueryError, _prepare_query_with_policy

        policy = {"provider": "typesafe", "generic_query_enabled": True,
                 "routes": {"generic": "laya-mlx"}, "local_laya_enabled": True,
                 "data_classification": "public", "mlx": "not-a-dict"}
        with self.assertRaisesRegex(QueryError, "LOCAL_MODEL_NOT_ATTESTED"):
            _prepare_query_with_policy("s", self._questions(), provider="laya-mlx", policy=policy,
                                       data_classification="public")

    def test_laya_route_with_a_real_verified_artifact_compiles_successfully(self):
        from src.adl.queries.typed import _prepare_query_with_policy

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model_dir = root / "model"
            model_dir.mkdir()
            weight = model_dir / "model.safetensors"
            weight_bytes = b"synthetic local model placeholder"
            weight.write_bytes(weight_bytes)
            weight.chmod(0o600)
            digest = hashlib.sha256(weight_bytes).hexdigest()
            manifest_path = root / "manifest.json"
            manifest_path.write_text(json.dumps({
                "repository": "aac6fef/laya-mlx", "revision": "a" * 40,
                "files": {"model.safetensors": digest}}))
            manifest_path.chmod(0o600)
            mlx = {"repository": "aac6fef/laya-mlx", "revision": "a" * 40, "weight_sha256": digest,
                  "model_dir": str(model_dir), "artifact_manifest": str(manifest_path)}
            policy = {"provider": "typesafe", "generic_query_enabled": True,
                     "routes": {"generic": "laya-mlx"}, "local_laya_enabled": True,
                     "data_classification": "public", "mlx": mlx}
            compiled = _prepare_query_with_policy("s", self._questions(), provider="laya-mlx",
                                                  policy=policy, data_classification="public")
        self.assertEqual(compiled.provider, "laya-mlx")
        self.assertEqual(compiled.expected_model, "laya-mlx@" + "a" * 40)
        self.assertTrue(compiled.local_preflight_required)

    def test_laya_route_with_a_tampered_weight_file_is_refused(self):
        from src.adl.queries.typed import QueryError, _prepare_query_with_policy

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model_dir = root / "model"
            model_dir.mkdir()
            weight = model_dir / "model.safetensors"
            weight.write_bytes(b"original content")
            weight.chmod(0o600)
            digest = hashlib.sha256(b"original content").hexdigest()
            manifest_path = root / "manifest.json"
            manifest_path.write_text(json.dumps({
                "repository": "aac6fef/laya-mlx", "revision": "a" * 40,
                "files": {"model.safetensors": digest}}))
            manifest_path.chmod(0o600)
            weight.write_bytes(b"tampered content, different from the manifest digest")
            mlx = {"repository": "aac6fef/laya-mlx", "revision": "a" * 40, "weight_sha256": digest,
                  "model_dir": str(model_dir), "artifact_manifest": str(manifest_path)}
            policy = {"provider": "typesafe", "generic_query_enabled": True,
                     "routes": {"generic": "laya-mlx"}, "local_laya_enabled": True,
                     "data_classification": "public", "mlx": mlx}
            with self.assertRaisesRegex(QueryError, "LOCAL_MODEL_NOT_ATTESTED"):
                _prepare_query_with_policy("s", self._questions(), provider="laya-mlx", policy=policy,
                                           data_classification="public")

    def test_payload_over_the_max_request_bytes_is_rejected(self):
        from src.adl.queries.typed import QueryError, _prepare_query_with_policy

        policy = {"provider": "typesafe", "generic_query_enabled": True, "routes": {},
                 "data_classification": "public"}
        with self.assertRaisesRegex(QueryError, "REQUEST_TOO_LARGE"):
            _prepare_query_with_policy("x" * 47_000, self._questions(), provider="typesafe",
                                       policy=policy, data_classification="public")


class QueriesTypedPrepareQueryPublicWrapperTests(unittest.TestCase):
    """prepare_query() is the public entrypoint: it loads the BROKER-persisted policy itself
    (a caller-supplied policy dict is only ever accepted by the private function above).
    Baseline: 225-236 -- the entire function -- unreached."""

    def test_no_enrolled_workspace_is_a_fixed_not_enrolled_error(self):
        from src.adl.queries.typed import QueryError, prepare_query

        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "project"
            workspace.mkdir()
            with patch.dict(os.environ, {"XDG_STATE_HOME": str(Path(directory) / "state")}):
                with self.assertRaisesRegex(QueryError, "GENERIC_QUERY_NOT_ENROLLED"):
                    prepare_query(workspace, "s", {"decision": {"type": "noul", "instructions": "x"}},
                                 provider="typesafe", data_classification="public")

    def test_an_enrolled_workspace_compiles_and_stamps_workspace_and_policy_fields(self):
        from jev_auto.common import digest as auto_digest
        from jev_auto.settings import make_policy, save_policy
        from src.adl.queries.typed import prepare_query

        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "project"
            workspace.mkdir()
            with patch.dict(os.environ, {"XDG_STATE_HOME": str(Path(directory) / "state")}):
                policy = make_policy(workspace, "typesafe", days=1, data_classification="public",
                                     generic_query_enabled=True)
                save_policy(workspace, policy)
                compiled = prepare_query(workspace, "a synthetic, unremarkable state",
                                         {"decision": {"type": "noul", "instructions": "x"}},
                                         provider="typesafe", data_classification="public")
        self.assertEqual(compiled.workspace_id, policy["workspace_id"])
        self.assertEqual(compiled.policy_sha256, auto_digest(policy))
        self.assertEqual(compiled.policy_expires_at, policy["expires_at"])


if __name__ == "__main__":
    unittest.main()
