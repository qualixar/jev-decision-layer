"""The setup wizard and the recipe workbench open only from their one-time launch link.

A local program that finds the port must not be able to fetch the page, the
session cookie or the CSRF value. Only the browser that the launcher opened
gets them, and the link works once.
"""

from __future__ import annotations

import http.client
import io
import json
import os
import re
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import urlencode


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "plugins" / "qualixar-jev-decision-layer" / "runtime"
sys.path.insert(0, str(RUNTIME))


class _MemoryKeychain:
    def __init__(self):
        self.keys = {"typesafe": "synthetic-key-stored-earlier"}

    def available(self):
        return True

    def get(self, provider):
        from src.adl.api.keychain import KeychainError

        if provider not in self.keys:
            raise KeychainError("KEYCHAIN_ITEM_MISSING")
        return self.keys[provider]

    def put(self, provider, value):
        self.keys[provider] = value

    def delete(self, provider):
        self.keys.pop(provider, None)


def _request(port, method, path, *, body=None, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


class _SetupWizardCase(unittest.TestCase):
    def setUp(self):
        from src.adl.api.setup_controller import SetupController
        from src.adl.api.setup_server import SetupServer

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(os.path.realpath(self.tmp.name))
        self.workspace = root / "project"
        self.workspace.mkdir()
        environment = patch.dict(os.environ, {"HOME": str(root), "XDG_STATE_HOME": str(root / "state"),
                                              "XDG_CONFIG_HOME": str(root / "config")})
        environment.start()
        self.addCleanup(environment.stop)
        controller = SetupController(self.workspace, keychain=_MemoryKeychain(),
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

    def assertNoSession(self, status, headers, body):
        self.assertEqual(status, 403)
        self.assertNotIn("Set-Cookie", headers)
        self.assertNotIn(b"name='csrf'", body)
        self.assertNotIn(self.server.setup_session.csrf.encode(), body)
        self.assertNotIn(self.server.setup_session.session.encode(), body)


class SetupLaunchTokenTests(_SetupWizardCase):
    def test_the_launch_link_carries_a_256_bit_one_time_token(self):
        url = self.server.launch_url
        self.assertTrue(url.startswith(f"http://127.0.0.1:{self.port}/setup?t="))
        token = url.split("?t=", 1)[1]
        self.assertRegex(token, r"^[A-Za-z0-9_-]{43,}$")
        self.assertEqual(self.server.launch_path, url[len(self.origin):])

    def test_a_client_without_the_token_gets_no_cookie_and_no_csrf(self):
        self.assertNoSession(*_request(self.port, "GET", "/setup"))

    def test_a_wrong_or_malformed_token_gets_no_cookie_and_no_csrf(self):
        token = self.server.launch_path.split("?t=", 1)[1]
        for path in ("/setup?t=" + "A" * len(token), "/setup?t=", "/setup?t=" + token + "&t=" + token,
                     "/setup?x=" + token, "/setup?t=" + token[:-1]):
            with self.subTest(path=path[:20]):
                self.assertNoSession(*_request(self.port, "GET", path))

    def test_a_non_ascii_or_non_text_token_is_refused_without_error(self):
        self.assertFalse(self.server.redeem_launch_token("té"))
        self.assertFalse(self.server.redeem_launch_token(None))
        self.assertNoSession(*_request(self.port, "GET", "/setup?t=%C3%A9"))
        # The real token still works afterwards: failed attempts do not burn it.
        self.assertEqual(_request(self.port, "GET", self.server.launch_path)[0], 200)

    def test_the_first_get_exchanges_the_token_and_a_replay_fails(self):
        status, headers, body = _request(self.port, "GET", self.server.launch_path)
        self.assertEqual(status, 200)
        self.assertIn("HttpOnly; SameSite=Strict", headers["Set-Cookie"])
        self.assertIn(b"name='csrf'", body)
        # Another local client replaying the same link after the browser used it.
        self.assertNoSession(*_request(self.port, "GET", self.server.launch_path))

    def test_the_browser_keeps_working_with_its_cookie_after_the_exchange(self):
        _status, headers, _body = _request(self.port, "GET", self.server.launch_path)
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        status, _headers, body = _request(self.port, "GET", "/setup", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertIn(b"name='csrf'", body)
        status, _headers, _body = _request(self.port, "GET", self.server.launch_path, headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        status, _headers, _body = _request(self.port, "GET", "/status", headers={"Cookie": cookie})
        self.assertEqual(status, 200)

    def test_a_wrong_cookie_does_not_reopen_the_page(self):
        _request(self.port, "GET", self.server.launch_path)
        self.assertNoSession(*_request(self.port, "GET", "/setup", headers={"Cookie": "adl_setup=forged"}))

    def test_a_post_marked_cross_site_by_the_browser_is_refused(self):
        _status, headers, body = _request(self.port, "GET", self.server.launch_path)
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        csrf = re.search(rb"name='csrf' value=\"([^\"]+)\"", body).group(1).decode()
        form = urlencode({"csrf": csrf, "mode": "jev-public", "provider": "typesafe", "days": "1",
                          "daily_calls": "5", "daily_bytes": "5000"})
        base = {"Cookie": cookie, "Origin": self.origin, "Content-Type": "application/x-www-form-urlencoded"}
        for site in ("cross-site", "same-site", "none"):
            with self.subTest(site=site):
                status, _headers, body = _request(self.port, "POST", "/preview", body=form,
                                                  headers={**base, "Sec-Fetch-Site": site})
                self.assertEqual(status, 403)
                self.assertIn(b"CROSS_ORIGIN_REJECTED", body)
        status, _headers, _body = _request(self.port, "POST", "/preview", body=form,
                                           headers={**base, "Sec-Fetch-Site": "same-origin"})
        self.assertEqual(status, 200)
        # A client that sends no fetch metadata still passes the other checks.
        status, _headers, _body = _request(self.port, "POST", "/preview", body=form, headers=base)
        self.assertEqual(status, 200)


class SetupLaunchAnnouncementTests(unittest.TestCase):
    """main() hands the tokenized link to the browser opener only."""

    def _run_main(self, *, tty, opened=True):
        from src.adl.api import setup_server

        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "project"
            workspace.mkdir()
            server = Mock()
            server.__enter__ = Mock(return_value=server)
            server.__exit__ = Mock(return_value=False)
            server.launch_url = "http://127.0.0.1:54321/setup?t=" + "T" * 43
            output = io.StringIO()
            output.isatty = lambda: tty
            with patch.object(setup_server, "SetupServer", return_value=server), \
                    patch.object(setup_server, "build_controller", return_value=Mock()), \
                    patch.object(setup_server.webbrowser, "open", return_value=opened) as browser, \
                    patch.dict(os.environ, {}, clear=False), \
                    patch.object(sys, "stdout", output), \
                    patch.object(sys, "argv", ["open-setup", "--workspace", str(workspace)]):
                os.environ.pop("ADL_SETUP_NO_BROWSER", None)
                try:
                    setup_server.main()
                    code = 0
                except SystemExit as error:
                    code = error.code
        return server, browser, output.getvalue(), code

    def test_a_pipe_never_receives_the_link_or_the_token(self):
        server, browser, printed, code = self._run_main(tty=False)
        browser.assert_called_once_with(server.launch_url)
        self.assertNotIn("http://", printed)
        self.assertNotIn("T" * 43, printed)
        self.assertIn("Qualixar setup opened in your browser.", printed)
        self.assertEqual(code, 0)
        server.serve_forever.assert_called_once()

    def test_a_terminal_shows_the_link_so_a_person_can_copy_it(self):
        server, browser, printed, _code = self._run_main(tty=True)
        browser.assert_called_once_with(server.launch_url)
        self.assertIn(server.launch_url, printed)
        server.serve_forever.assert_called_once()

    def test_a_pipe_with_no_browser_stops_instead_of_waiting_unreachable(self):
        server, _browser, printed, code = self._run_main(tty=False, opened=False)
        self.assertNotIn("http://", printed)
        self.assertIn("could not open a browser", printed)
        self.assertNotEqual(code, 0)
        server.serve_forever.assert_not_called()

    def test_a_failing_browser_opener_counts_as_no_browser(self):
        from src.adl.api import setup_server

        with patch.object(setup_server.webbrowser, "open", side_effect=RuntimeError("no display")), \
                patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ADL_SETUP_NO_BROWSER", None)
            self.assertFalse(setup_server._open_browser("http://127.0.0.1:1/setup?t=x"))

    def test_an_output_that_cannot_say_whether_it_is_a_terminal_is_treated_as_a_pipe(self):
        from src.adl.api import setup_server

        class Closed(io.StringIO):
            def isatty(self):
                raise ValueError("I/O operation on closed file")

        output = Closed()
        with patch.object(sys, "stdout", output):
            self.assertTrue(setup_server._announce("http://127.0.0.1:1/setup?t=" + "T" * 43, True))
        self.assertNotIn("T" * 43, output.getvalue())


class SetupToolResultTests(unittest.TestCase):
    """The MCP jev_setup result carries no link and no token."""

    class _Process:
        """A real pipe holding the launcher's one status line; no child process."""

        def __init__(self, line):
            read_fd, write_fd = os.pipe()
            os.write(write_fd, line)
            os.close(write_fd)
            self.stdout = os.fdopen(read_fd, "rb")
            self.pid = 999_999

        def poll(self):
            return None

        def wait(self, timeout=None):
            return 0

    def _open(self, line):
        from jev_auto import mcp

        with tempfile.TemporaryDirectory() as directory:
            process = self._Process(line)
            with patch.object(mcp, "workspace", side_effect=lambda value: Path(value)), \
                    patch.object(mcp.subprocess, "Popen", return_value=process), \
                    patch.object(mcp.os, "killpg"):
                return mcp._open_setup(os.path.realpath(directory))

    def test_the_result_has_no_url_and_no_token(self):
        result = self._open(b"Qualixar setup opened in your browser. Enter keys only in the local browser, never in chat.\n")
        self.assertEqual(result["status"], "SETUP_WIZARD_OPEN")
        self.assertNotIn("url", result)
        encoded = json.dumps(result)
        self.assertNotIn("http", encoded)
        self.assertNotIn("127.0.0.1", encoded)

    def test_a_launcher_line_that_carries_a_link_is_never_passed_through(self):
        from jev_auto.common import AutoError

        leaked = b"Qualixar setup: http://127.0.0.1:54321/setup?t=" + b"T" * 43 + b"\n"
        with self.assertRaises(AutoError) as caught:
            self._open(leaked)
        self.assertEqual(str(caught.exception), "SETUP_START_FAILED")
        self.assertNotIn("T" * 43, str(caught.exception))

    def test_no_browser_is_a_distinct_error(self):
        from jev_auto.common import AutoError

        with self.assertRaises(AutoError) as caught:
            self._open(b"Qualixar setup could not open a browser. Run open-setup in a terminal to get a private link.\n")
        self.assertEqual(str(caught.exception), "SETUP_BROWSER_UNAVAILABLE")

    def test_dispatch_of_jev_setup_returns_the_launcher_result_without_a_link(self):
        from jev_auto import mcp

        legacy = Mock()
        legacy.tools.return_value = []
        with tempfile.TemporaryDirectory() as directory:
            result = mcp.dispatch("jev_setup", {"workspace_path": os.path.realpath(directory)}, legacy,
                                  setup_launcher=lambda _path: {"status": "SETUP_WIZARD_OPEN"})
        self.assertEqual(result, {"status": "SETUP_WIZARD_OPEN"})


class WorkbenchLaunchTokenTests(unittest.TestCase):
    def setUp(self):
        from jev_auto.recipe_workbench import RecipeWorkbench
        from src.adl.api.recipe_workbench_server import WorkbenchServer

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        service = RecipeWorkbench(Path(self.tmp.name), engine_factory=Mock())
        self.server = WorkbenchServer(("127.0.0.1", 0), service)
        self.port = self.server.server_port
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def assertNoSession(self, status, headers, body):
        self.assertEqual(status, 403)
        self.assertNotIn("Set-Cookie", headers)
        self.assertNotIn(self.server.session.csrf.encode(), body)
        self.assertNotIn(self.server.session.token.encode(), body)

    def test_the_launch_link_carries_a_256_bit_one_time_token(self):
        self.assertTrue(self.server.launch_url.startswith(f"http://127.0.0.1:{self.port}/?t="))
        self.assertRegex(self.server.launch_url.split("?t=", 1)[1], r"^[A-Za-z0-9_-]{43,}$")

    def test_a_client_without_the_token_gets_no_cookie_and_no_csrf(self):
        self.assertNoSession(*_request(self.port, "GET", "/", headers={"Host": f"127.0.0.1:{self.port}"}))

    def test_the_first_get_exchanges_the_token_and_a_replay_fails(self):
        host = {"Host": f"127.0.0.1:{self.port}"}
        status, headers, body = _request(self.port, "GET", self.server.launch_path, headers=host)
        self.assertEqual(status, 200)
        self.assertIn(self.server.session.csrf.encode(), body)
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        self.assertNoSession(*_request(self.port, "GET", self.server.launch_path, headers=host))
        status, _headers, _body = _request(self.port, "GET", "/", headers={**host, "Cookie": cookie})
        self.assertEqual(status, 200)
        status, _headers, _body = _request(self.port, "GET", "/api/catalog", headers={**host, "Cookie": cookie})
        self.assertEqual(status, 200)

    def test_a_malformed_or_non_ascii_link_is_refused_without_burning_the_token(self):
        host = {"Host": f"127.0.0.1:{self.port}"}
        token = self.server.launch_path.split("?t=", 1)[1]
        self.assertFalse(self.server.redeem_launch_token("té"))
        for path in ("/?t=" + token + "&t=" + token, "/?t=%C3%A9", "/?x=" + token, "/?"):
            with self.subTest(path=path[:12]):
                self.assertNoSession(*_request(self.port, "GET", path, headers=host))
        self.assertEqual(_request(self.port, "GET", self.server.launch_path, headers=host)[0], 200)

    def test_requests_without_the_session_do_not_use_up_the_request_limit(self):
        host = {"Host": f"127.0.0.1:{self.port}"}
        for _ in range(self.server.request_limit + 5):
            status, _headers, _body = _request(self.port, "GET", "/api/catalog", headers=host)
            self.assertEqual(status, 403)
        status, _headers, _body = _request(self.port, "GET", self.server.launch_path, headers=host)
        self.assertEqual(status, 200)

    def test_the_workbench_command_prints_the_link_only_to_a_terminal(self):
        from jev_auto import cli

        for tty in (False, True):
            with self.subTest(tty=tty), tempfile.TemporaryDirectory() as directory:
                server = Mock()
                server.launch_url = "http://127.0.0.1:54321/?t=" + "W" * 43
                output = io.StringIO()
                output.isatty = lambda tty=tty: tty
                with patch("src.adl.api.recipe_workbench_server.WorkbenchServer", return_value=server), \
                        patch("jev_auto.recipe_workbench.RecipeWorkbench"), \
                        patch("webbrowser.open", return_value=True) as browser, \
                        patch.object(sys, "stdout", output):
                    self.assertEqual(cli.main(["workbench", "--workspace", os.path.realpath(directory)]), 0)
                browser.assert_called_once_with(server.launch_url)
                if tty:
                    self.assertIn(server.launch_url, output.getvalue())
                else:
                    self.assertNotIn("W" * 43, output.getvalue())
                    self.assertNotIn("http://", output.getvalue())

    def test_the_workbench_command_treats_an_unknown_output_as_a_pipe(self):
        from jev_auto import cli

        class Closed(io.StringIO):
            armed = False

            def isatty(self):
                # argparse asks too; fail only once the workbench is launching.
                if self.armed:
                    raise ValueError("I/O operation on closed file")
                return False

        def open_browser(_url):
            output.armed = True
            return True

        with tempfile.TemporaryDirectory() as directory:
            server = Mock()
            server.launch_url = "http://127.0.0.1:54321/?t=" + "W" * 43
            output = Closed()
            with patch("src.adl.api.recipe_workbench_server.WorkbenchServer", return_value=server), \
                    patch("jev_auto.recipe_workbench.RecipeWorkbench"), \
                    patch("webbrowser.open", side_effect=open_browser), \
                    patch.object(sys, "stdout", output):
                self.assertEqual(cli.main(["workbench", "--workspace", os.path.realpath(directory)]), 0)
        self.assertNotIn("W" * 43, output.getvalue())
        self.assertIn("Recipe workbench opened in your browser.", output.getvalue())


if __name__ == "__main__":
    unittest.main()
