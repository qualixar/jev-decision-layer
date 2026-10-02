"""The setup link never sits on a command line, and expiry lets a save finish.

On Linux the browser opener is a separate process whose arguments other
local accounts can read, so the one-time link is handed over in a private
page instead. When the ten-minute session runs out, a save already in
progress completes before the page closes.
"""

from __future__ import annotations

import os
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "plugins" / "qualixar-jev-decision-layer" / "runtime"
sys.path.insert(0, str(RUNTIME))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.adl.api import setup_server  # noqa: E402
from test_setup_wizard_availability import _MemoryKeychain  # noqa: E402

URL = "http://127.0.0.1:50000/setup?t=abcDEF123-_one-time"


class PrivateLinkTests(unittest.TestCase):
    def open(self, platform):
        opened = []
        with patch.object(setup_server.sys, "platform", platform), \
             patch.dict(os.environ, {"ADL_SETUP_NO_BROWSER": ""}), \
             patch.object(setup_server.webbrowser, "open", side_effect=lambda target: opened.append(target) or True):
            self.assertTrue(setup_server.open_private_link(URL))
        return opened[0]

    def test_on_linux_the_link_goes_through_a_private_page(self):
        target = self.open("linux")
        self.assertTrue(target.startswith("file://"))
        self.assertNotIn("t=abc", target)
        page = Path(target[len("file://"):])
        self.addCleanup(lambda: page.exists() and page.unlink())
        self.assertEqual(stat.S_IMODE(page.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(page.parent.stat().st_mode), 0o700)
        self.assertIn(URL, page.read_text())

    def test_the_page_cleanup_never_keeps_the_command_running(self):
        timers, at_exit = [], []

        class Recorder:
            def __init__(self, delay, function, args=(), kwargs=None):
                self.delay, self.daemon = delay, False
                timers.append(self)

            def start(self):
                self.started_as_daemon = self.daemon

        with patch.object(setup_server.threading, "Timer", Recorder), \
             patch.object(setup_server.atexit, "register", lambda *args, **kwargs: at_exit.append((args, kwargs))):
            page = Path(setup_server._private_forwarding_page(URL)[len("file://"):])
        self.addCleanup(lambda: page.exists() and page.unlink())
        self.assertTrue(timers[0].started_as_daemon)
        # A command that finishes first removes the page on its way out.
        (function, folder), kwargs = at_exit[0]
        self.assertEqual((function, folder, kwargs), (setup_server.shutil.rmtree, page.parent, {"ignore_errors": True}))

    def test_on_a_mac_the_link_is_handed_to_the_browser_directly(self):
        self.assertEqual(self.open("darwin"), URL)


class ExpiryTests(unittest.TestCase):
    def test_a_save_in_progress_finishes_before_the_session_closes(self):
        from src.adl.api.setup_controller import SetupController
        with tempfile.TemporaryDirectory() as folder:
            workspace = Path(os.path.realpath(folder)) / "project"
            workspace.mkdir()
            controller = SetupController(workspace, keychain=_MemoryKeychain(),
                                         bridge=lambda *_: None, start=lambda *_: None)
            server = setup_server.SetupServer(("127.0.0.1", 0), controller, host_inventory=lambda: [],
                                              claude_policy=lambda: None)
            self.addCleanup(server.server_close)
            server.state_lock.acquire()  # a save in progress
            server.setup_session = server.setup_session.__class__(**{**server.setup_session.__dict__,
                                                                     "expires_at": time.monotonic() - 1})
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            time.sleep(0.5)
            self.assertTrue(thread.is_alive(), "the session closed under a running save")
            server.state_lock.release()
            thread.join(3)
            self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
