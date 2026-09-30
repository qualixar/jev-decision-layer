"""No local program can freeze the setup wizard or use up its request limit.

An idle connection (a browser's speculative preconnect, or any local process)
must not stop the real browser from loading the page, and requests without a
session must not count toward the limit that protects the consent form.
"""

from __future__ import annotations

import http.client
import os
import re
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "plugins" / "qualixar-jev-decision-layer" / "runtime"
sys.path.insert(0, str(RUNTIME))


class _MemoryKeychain:
    def available(self):
        return True

    def get(self, provider):
        return "synthetic-key"

    def put(self, provider, value):
        pass

    def delete(self, provider):
        pass


def _request(port, method, path, *, body=None, headers=None, timeout=3):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


class SetupWizardAvailabilityTests(unittest.TestCase):
    def setUp(self):
        from src.adl.api.setup_controller import SetupController
        from src.adl.api.setup_server import SetupServer

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(os.path.realpath(self.tmp.name))
        workspace = root / "project"
        workspace.mkdir()
        environment = patch.dict(os.environ, {"HOME": str(root), "XDG_STATE_HOME": str(root / "state"),
                                              "XDG_CONFIG_HOME": str(root / "config")})
        environment.start()
        self.addCleanup(environment.stop)
        controller = SetupController(workspace, keychain=_MemoryKeychain(),
                                     bridge=lambda *_: None, start=lambda *_: None)
        self.server = SetupServer(("127.0.0.1", 0), controller, host_inventory=lambda: [],
                                  claude_policy=lambda: None)
        self.port = self.server.server_port
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.origin = f"http://127.0.0.1:{self.port}"

    def _open_session(self):
        status, headers, body = _request(self.port, "GET", self.server.launch_path)
        self.assertEqual(status, 200)
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        csrf = re.search(rb"name='csrf' value=\"([^\"]+)\"", body).group(1).decode()
        return cookie, csrf

    def _preview(self, cookie, csrf):
        form = urlencode({"csrf": csrf, "mode": "jev-public", "provider": "typesafe", "days": "1",
                          "daily_calls": "5", "daily_bytes": "5000"})
        return _request(self.port, "POST", "/preview", body=form,
                        headers={"Cookie": cookie, "Origin": self.origin,
                                 "Content-Type": "application/x-www-form-urlencoded"})

    def test_an_idle_connection_does_not_freeze_the_page(self):
        idle = [socket.create_connection(("127.0.0.1", self.port)) for _ in range(3)]
        self.addCleanup(lambda: [sock.close() for sock in idle])
        started = time.monotonic()
        status, _headers, _body = _request(self.port, "GET", self.server.launch_path, timeout=3)
        self.assertEqual(status, 200)
        self.assertLess(time.monotonic() - started, 2.0)

    def test_a_stalled_connection_is_closed_by_the_server(self):
        stalled = socket.create_connection(("127.0.0.1", self.port))
        self.addCleanup(stalled.close)
        stalled.sendall(b"GET /setup HTTP/1.1\r\n")  # never finishes its headers
        stalled.settimeout(12)
        started = time.monotonic()
        self.assertEqual(stalled.recv(1024), b"")  # the server hangs up
        self.assertLess(time.monotonic() - started, 11)

    def test_many_requests_without_a_session_do_not_lock_the_wizard(self):
        for _ in range(101):
            self.assertEqual(_request(self.port, "GET", "/setup")[0], 403)
        for _ in range(101):
            status, _headers, _body = _request(
                self.port, "POST", "/preview", body="csrf=x",
                headers={"Origin": self.origin, "Content-Type": "application/x-www-form-urlencoded"})
            self.assertEqual(status, 403)
        cookie, csrf = self._open_session()
        self.assertEqual(self._preview(cookie, csrf)[0], 200)

    def test_the_limit_still_holds_for_posts_that_carry_the_session(self):
        cookie, csrf = self._open_session()
        for _ in range(100):
            self.assertEqual(self._preview(cookie, csrf)[0], 200)
        status, _headers, body = self._preview(cookie, csrf)
        self.assertEqual(status, 429)
        self.assertIn(b"SETUP_REQUEST_LIMIT", body)
        # Reloading the page with the session is not a consent attempt.
        self.assertEqual(_request(self.port, "GET", "/setup", headers={"Cookie": cookie})[0], 200)

    def test_concurrent_connections_are_bounded(self):
        from src.adl.api.setup_server import SetupServer

        self.assertTrue(SetupServer.daemon_threads)
        held = [socket.create_connection(("127.0.0.1", self.port)) for _ in range(SetupServer.max_connections + 4)]
        self.addCleanup(lambda: [sock.close() for sock in held])
        time.sleep(0.3)
        # Connections beyond the bound are closed at once instead of queuing a thread.
        closed = 0
        for sock in held[SetupServer.max_connections:]:
            sock.settimeout(2)
            try:
                closed += sock.recv(1) == b""
            except (ConnectionResetError, socket.timeout):
                closed += 1
        self.assertGreaterEqual(closed, 4)
        for sock in held:
            sock.close()
        time.sleep(0.3)
        self.assertEqual(_request(self.port, "GET", self.server.launch_path)[0], 200)


class ConnectionSlotTests(unittest.TestCase):
    def test_a_connection_whose_thread_cannot_start_gives_its_slot_back(self):
        import socketserver
        from src.adl.api.setup_controller import SetupController
        from src.adl.api.setup_server import SetupServer

        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "project"
            workspace.mkdir()
            controller = SetupController(workspace, keychain=_MemoryKeychain(),
                                         bridge=lambda *_: None, start=lambda *_: None)
            server = SetupServer(("127.0.0.1", 0), controller, host_inventory=lambda: [],
                                 claude_policy=lambda: None)
            try:
                with patch.object(socketserver.ThreadingMixIn, "process_request",
                                  side_effect=RuntimeError("can't start new thread")):
                    for _ in range(SetupServer.max_connections + 1):
                        with self.assertRaises(RuntimeError):
                            server.process_request(object(), ("127.0.0.1", 1))
                # Every slot came back: none leaked on the failure path.
                for _ in range(SetupServer.max_connections):
                    self.assertTrue(server._connection_slots.acquire(blocking=False))
            finally:
                server.server_close()


if __name__ == "__main__":
    unittest.main()
