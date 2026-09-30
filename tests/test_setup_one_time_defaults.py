"""One approval should last: wizard and CLI defaults for a folder-level grant.

A parent folder (not a Git repository) is almost always chosen to cover the
projects under it, so the wizard pre-ticks child coverage there. The review
screen and its separate confirmation checkbox are unchanged, so nothing is
saved without the person approving it. Permission lasts 365 days by default.
"""

from __future__ import annotations

import http.client
import io
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "plugins" / "qualixar-jev-decision-layer" / "runtime"
sys.path.insert(0, str(RUNTIME))

from src.adl.api import setup_server  # noqa: E402
from src.adl.api.setup_controller import SetupController  # noqa: E402
from src.adl.api.setup_server import SetupServer  # noqa: E402

_COVER_TICKED = "name='cover_descendants' type='checkbox' value='yes' checked>"
_COVER_UNTICKED = "name='cover_descendants' type='checkbox' value='yes'>"


def _page(project: Path, **controller_kwargs) -> str:
    controller = SetupController(project, local_config=lambda: None, **controller_kwargs)
    server = SetupServer(("127.0.0.1", 0), controller, host_inventory=lambda: [])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        connection.request("GET", "/setup")
        response = connection.getresponse()
        body = response.read().decode("utf-8")
        connection.close()
        assert response.status == 200, response.status
        return body
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class WizardDefaultTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_a_parent_folder_pre_ticks_child_coverage(self):
        folder = self.root / "Documents"
        folder.mkdir()
        body = _page(folder)
        self.assertIn(_COVER_TICKED, body)

    def test_a_git_repository_does_not_pre_tick_child_coverage(self):
        repo = self.root / "project"
        repo.mkdir()
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True, capture_output=True)
        body = _page(repo)
        self.assertIn(_COVER_UNTICKED, body)

    def test_a_bare_repository_does_not_pre_tick_child_coverage(self):
        bare = self.root / "store.git"
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True, capture_output=True)
        self.assertFalse(setup_server._suggest_coverage(bare, {}, False))

    def test_any_ambiguous_git_answer_leaves_coverage_unticked(self):
        folder = self.root / "Documents"
        folder.mkdir()
        self.assertTrue(setup_server._suggest_coverage(folder, {}, False))
        dubious = subprocess.CompletedProcess([], 128, b"", b"fatal: detected dubious ownership in repository")
        with patch.object(setup_server.subprocess, "run", return_value=dubious):
            self.assertFalse(setup_server._suggest_coverage(folder, {}, False))
        with patch.object(setup_server.subprocess, "run", side_effect=OSError("no git")):
            self.assertFalse(setup_server._suggest_coverage(folder, {}, False))
        self.assertFalse(setup_server._suggest_coverage(self.root / "missing", {}, False))

    def test_an_unreadable_repository_is_not_mistaken_for_a_plain_folder(self):
        import os

        repo = self.root / "locked"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
        (repo / "sub").mkdir()
        os.chmod(repo / ".git", 0)
        self.addCleanup(os.chmod, repo / ".git", 0o755)
        self.assertFalse(setup_server._suggest_coverage(repo, {}, False))
        self.assertFalse(setup_server._suggest_coverage(repo / "sub", {}, False))

    def test_expiry_outside_the_policy_range_renders_as_unknown(self):
        for value in (0, -1, 10**13, float("nan"), True, "soon", None):
            with self.subTest(value=value):
                self.assertEqual(setup_server._expiry(value), "unknown")
        self.assertRegex(setup_server._expiry(1_790_000_000), r"^\d{4}-\d{2}-\d{2}$")

    def test_the_saved_page_names_no_single_host(self):
        source = Path(setup_server.__file__).read_text()
        self.assertNotIn("Review Codex hook trust in Codex", source)

    def test_a_root_that_cannot_hold_coverage_is_never_pre_ticked(self):
        folder = self.root / "Documents"
        folder.mkdir()
        with patch.object(setup_server, "descendant_root_allowed", return_value=False):
            body = _page(folder)
        self.assertIn(_COVER_UNTICKED, body)

    def test_an_existing_policy_keeps_its_own_coverage_choice(self):
        folder = self.root / "Documents"
        folder.mkdir()
        without = _page(folder, policy_exists=lambda: True,
                        load_existing=lambda: {"provider": "typesafe", "covers_descendants": False})
        self.assertIn(_COVER_UNTICKED, without)
        legacy = _page(folder, policy_exists=lambda: True, load_existing=lambda: {"provider": "typesafe"})
        self.assertIn(_COVER_UNTICKED, legacy)
        covered = _page(folder, policy_exists=lambda: True,
                        load_existing=lambda: {"provider": "typesafe", "covers_descendants": True})
        self.assertIn(_COVER_TICKED, covered)

    def test_permission_defaults_to_the_one_year_maximum(self):
        folder = self.root / "Documents"
        folder.mkdir()
        body = _page(folder)
        self.assertIn("name='days' type='number' min='1' max='365' value='365' required", body)


class _Server:
    """A running wizard with a session cookie, for multi-step UI checks."""

    def __init__(self, controller, hosts=()):
        self.server = SetupServer(("127.0.0.1", 0), controller, host_inventory=lambda: list(hosts))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.cookie = None

    def request(self, method, path, values=None):
        from urllib.parse import urlencode

        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        body = urlencode(values).encode() if values is not None else None
        headers = {"Origin": f"http://127.0.0.1:{self.server.server_port}",
                   "Content-Type": "application/x-www-form-urlencoded"} if body else {}
        if self.cookie:
            headers["Cookie"] = self.cookie
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        page = response.read().decode("utf-8")
        cookie = response.getheader("Set-Cookie")
        if cookie and self.cookie is None:
            self.cookie = cookie.split(";", 1)[0]
        connection.close()
        return response.status, page

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class WizardCopyTests(unittest.TestCase):
    """The wizard must say what one approval does: it covers every harness."""

    def setUp(self):
        import os

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        env = patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "state")})
        env.start()
        self.addCleanup(env.stop)
        self.folder = self.root / "Documents"
        self.folder.mkdir()

    def test_the_form_says_one_approval_covers_every_harness_and_names_no_single_host(self):
        hosts = [{"label": "Codex", "detected": True}, {"label": "VS Code", "detected": False}]
        wizard = _Server(SetupController(self.folder, local_config=lambda: None), hosts)
        try:
            _status, page = wizard.request("GET", "/setup")
        finally:
            wizard.close()
        self.assertIn("One approval covers every harness", page)
        self.assertIn("You do not approve again per harness", page)
        self.assertIn("every folder and project under this one", page)
        self.assertNotIn("Codex hook trust", page)
        self.assertNotIn("Auto disabled", page)
        self.assertIn("Uses this grant once the Jev plugin is installed there", page)
        self.assertEqual(page.count("not yet verified"), 2)

    def test_the_review_screen_repeats_the_scope_before_saving(self):
        import re

        wizard = _Server(SetupController(self.folder, local_config=lambda: None))
        try:
            _status, page = wizard.request("GET", "/setup")
            csrf = re.search(r"name='csrf' value=\"([^\"]+)\"", page).group(1)
            fields = {"csrf": csrf, "provider": "typesafe", "mode": "jev-public", "days": "365",
                      "daily_calls": "100", "daily_bytes": "2000000", "generic": "on",
                      "auto_prepare": "off", "cover_descendants": "yes"}
            status, review = wizard.request("POST", "/preview", fields)
        finally:
            wizard.close()
        self.assertEqual(status, 200)
        self.assertIn("in every harness", review)
        self.assertIn("confirm_descendants", review)
        self.assertIn("for 365 days", review)
        self.assertNotIn("Codex hook trust", review)

    def test_the_status_page_shows_coverage_expiry_and_harness_scope(self):
        from jev_auto.settings import DESCENDANT_COVERAGE_APPROVED, make_policy, save_policy

        policy = make_policy(self.folder, "typesafe", days=365, generic_query_enabled=True,
                             covers_descendants=True, descendant_approval=DESCENDANT_COVERAGE_APPROVED,
                             setup_origin="local_wizard", setup_state="ready")
        save_policy(self.folder, policy)
        wizard = _Server(SetupController(self.folder, local_config=lambda: None))
        try:
            wizard.request("GET", "/setup")
            status, page = wizard.request("GET", "/status")
        finally:
            wizard.close()
        self.assertEqual(status, 200)
        self.assertIn("Covers child folders and projects: <strong>Yes</strong>", page)
        self.assertIn("Expires:", page)
        self.assertIn("Generic typed queries: <strong>Enabled</strong>", page)
        self.assertIn("One approval covers every harness", page)


class CliDefaultTests(unittest.TestCase):
    def test_cli_enrollment_defaults_to_the_one_year_maximum(self):
        from jev_auto import cli

        with tempfile.TemporaryDirectory() as directory:
            out = io.StringIO()
            with patch.object(sys.stdin, "isatty", return_value=True), \
                 patch("builtins.input", return_value="NO"), redirect_stdout(out):
                code = cli.main(["enroll", "--workspace", directory, "--provider", "typesafe"])
        self.assertEqual(code, 2)
        self.assertIn("Days: 365", out.getvalue())


if __name__ == "__main__":
    unittest.main()
