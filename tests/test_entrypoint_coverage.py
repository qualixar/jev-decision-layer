"""Entry-point and process-boundary coverage: jev_auto/cli.py (the `scripts/jev`
command surface), the legacy `jev.py` CLI, `auto_entry.py`, the resident broker
in jev_auto/server.py, the Claude Code hook in jev_auto/claude_hook.py, and the
honesty invariants in jev_auto/measure.py.

These are the files a host actually spawns or execs. Before this file they were
at 0-8% line coverage: nobody drove `main()` with real argument lists, nobody
exercised the broker's Handler/Server classes, nobody called the Claude Code
hook's stdin/stdout wiring.

Safety posture (verified empirically while writing this file, not assumed):
  * No network. No macOS Keychain access (enroll tests always pass an explicit
    --provider so jevkit.providers.resolve_provider's real Keychain-adjacent
    path is never reached; the one test that exercises that call path mocks it).
  * No write outside a TemporaryDirectory, with one narrow, deliberate
    exception shared by every other coverage file in this suite: jev_auto's own
    IPC layer always roots its Unix-socket directory at a fixed per-UID path
    under /private/tmp (see ipc.py:address) regardless of --state-root/--workspace.
    Tests that reach `ensure`/`request` either mock them (jev_auto/cli.py tests)
    or patch jev_auto.server.address to point inside a TemporaryDirectory
    (server.py tests), so no *socket file* is ever created outside our own
    temp directories - only the harmless, pre-existing per-UID marker
    directory that every IPC-adjacent test in this repo already creates.
  * Never touches the real ~/.local/state/qualixar-jev-decision-layer or
    ~/.config/qualixar-jev-decision-layer: every test that reaches state_dir()/
    home_root() patches XDG_STATE_HOME and/or XDG_CONFIG_HOME to a
    TemporaryDirectory first, or (for jev.py) passes an explicit --state-root.
  * Never touches ~/Library/Application Support/Claude/claude_desktop_config.json:
    jev_auto.host_mcp.plan/install are mocked for every 'claude-desktop' case.
  * No real broker subprocess is ever spawned: jev_auto.cli.ensure/request are
    mocked wherever main() would otherwise reach them; jev_auto/server.py's own
    serve() is driven directly (in-process) with idle_seconds=0 or with the
    advisory lock pre-held, never through `ensure()`'s Popen path.
"""

from __future__ import annotations

import fcntl
import io
import json
import os
import runpy
import socket
import sys
import tempfile
import threading
import unittest
import warnings
from contextlib import ExitStack, contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "plugins" / "qualixar-jev-decision-layer" / "runtime"
sys.path.insert(0, str(RUNTIME))


# --------------------------------------------------------------------------
# Shared test-only helpers (not product code).
# --------------------------------------------------------------------------

@contextmanager
def _isolated_xdg_home():
    """Point every XDG-derived path (state + config) at a throwaway root.

    Every jev_auto/cli.py command that reaches state_dir()/home_root()/
    bridge_record() must run under this, or it would read or write the real
    per-user directories the product uses in production.
    """
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        with patch.dict(os.environ, {
            "XDG_STATE_HOME": str(root / "state"),
            "XDG_CONFIG_HOME": str(root / "config"),
        }):
            yield root


def _run_cli(argv, *, ensure=None, request=None, isatty=None, input_value=None):
    """Invoke jev_auto.cli.main(argv), patching only the seams given.

    Returns (returncode, stdout_text, stderr_text). `ensure`/`request` patch
    the *cli module's* names (it imports them with `from .ipc import ...`,
    so the broker/socket layer is never touched unless a test wants that).
    """
    import jev_auto.cli as cli

    out, err = io.StringIO(), io.StringIO()
    with ExitStack() as stack:
        if ensure is not None:
            stack.enter_context(patch.object(cli, "ensure", ensure))
        if request is not None:
            stack.enter_context(patch.object(cli, "request", request))
        if isatty is not None:
            stack.enter_context(patch("sys.stdin.isatty", return_value=isatty))
        if input_value is not None:
            stack.enter_context(patch("builtins.input", return_value=input_value))
        stack.enter_context(redirect_stdout(out))
        stack.enter_context(redirect_stderr(err))
        rc = cli.main(argv)
    return rc, out.getvalue(), err.getvalue()


def _make_handler(sock, engine, stopping=None):
    """Build a jev_auto.server.Handler without going through socketserver's
    connection-accept machinery, so `.handle()` can be driven directly against
    a plain socket.socketpair() end."""
    import jev_auto.server as server

    handler = server.Handler.__new__(server.Handler)
    handler.request = sock
    handler.server = SimpleNamespace(engine=engine, stopping=stopping or threading.Event(),
                                      last_activity=0)
    return handler


class EntrypointCoverageMeasurementNote(unittest.TestCase):
    """Not a real test: documents how this file's coverage must be measured.

    Every module here (cli.py, jev.py, server.py, claude_hook.py, measure.py,
    auto_entry.py) is exercised ONLY in this file. Measure with:
        COVERAGE_FILE=/tmp/cov-entry python3 -m coverage run \
            --source=plugins/qualixar-jev-decision-layer/runtime \
            -m pytest tests/test_entrypoint_coverage.py -q
        COVERAGE_FILE=/tmp/cov-entry python3 -m coverage report
    A full-suite number (running the whole tests/ directory) also exercises
    these modules indirectly through shared imports and is NOT the number to
    gate on for this file's contribution.
    """

    def test_documentation_only(self):
        self.assertTrue(True)


# ==========================================================================
# jev_auto/cli.py - the `scripts/jev` command surface
# ==========================================================================

class CliBridgeRecordTests(unittest.TestCase):
    """`bridge_record`/`_bridge_lock` back the browser bridge's loadConfig().

    They live outside main()'s dispatch and write to a *global* (not
    workspace-scoped) config location, so they get their own direct tests
    rather than only being exercised incidentally through `enroll`.
    """

    def test_bridge_record_creates_then_updates_the_workspace_entry(self):
        import jev_auto.cli as cli

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "project"
            workspace.mkdir()
            with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(root / "config")}):
                cli.bridge_record(workspace, {"browser_origins": ["https://example.com"],
                                               "browser_max_steps": 7})
                bridge_file = root / "config" / "qualixar-jev-decision-layer" / "auto-bridge.json"
                first = json.loads(bridge_file.read_text())
                self.assertEqual(first["schema_version"], 1)
                self.assertEqual(len(first["workspaces"]), 1)
                entry = next(iter(first["workspaces"].values()))
                self.assertEqual(entry["maxSteps"], 7)

                # A second call for the same workspace updates in place, not append.
                cli.bridge_record(workspace, {"browser_origins": ["https://example.org"],
                                               "browser_max_steps": 3})
                second = json.loads(bridge_file.read_text())
                self.assertEqual(len(second["workspaces"]), 1)
                self.assertEqual(next(iter(second["workspaces"].values()))["maxSteps"], 3)

    def test_bridge_record_rejects_a_corrupted_schema_rather_than_overwrite_it(self):
        import jev_auto.cli as cli
        from jev_auto.common import AutoError

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "project"
            workspace.mkdir()
            with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(root / "config")}):
                bridge_dir = root / "config" / "qualixar-jev-decision-layer"
                bridge_dir.mkdir(parents=True, mode=0o700)
                bridge_file = bridge_dir / "auto-bridge.json"
                bridge_file.write_text('{"schema_version": 2, "workspaces": {}}')
                os.chmod(bridge_file, 0o600)
                with self.assertRaisesRegex(AutoError, "BRIDGE_SCHEMA"):
                    cli.bridge_record(workspace, {"browser_origins": [], "browser_max_steps": 1})


class CliHostRegisterTests(unittest.TestCase):
    """`host-register` writes into someone else's config file, so this suite
    is careful never to reach the two real hosts (antigravity, claude-desktop):
    vscode is exercised for real inside a TemporaryDirectory workspace;
    claude-desktop always mocks jev_auto.host_mcp.plan/install so the real
    ~/Library/Application Support/Claude/claude_desktop_config.json is never
    read or written by this test file.
    """

    def test_vscode_target_without_a_workspace_is_a_clean_error(self):
        rc, out, err = _run_cli(["host-register", "--host", "vscode"])
        self.assertEqual(rc, 2)
        self.assertEqual(out, "")
        self.assertIn("HOST_MCP_WORKSPACE_REQUIRED", err)

    def test_vscode_plan_then_write_then_idempotent_replan(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "project"
            workspace.mkdir()

            rc, out, err = _run_cli(["host-register", "--host", "vscode", "--workspace", str(workspace)])
            self.assertEqual(rc, 0)
            plan = json.loads(out)
            self.assertEqual(plan["action"], "create")
            self.assertFalse(plan["written"])
            self.assertIn("Nothing written", err)
            self.assertFalse((workspace / ".vscode" / "mcp.json").exists())

            rc, out, _err = _run_cli(["host-register", "--host", "vscode",
                                       "--workspace", str(workspace), "--write"])
            self.assertEqual(rc, 0)
            installed = json.loads(out)
            self.assertTrue(installed["written"])
            config = json.loads((workspace / ".vscode" / "mcp.json").read_text())
            self.assertIn("qualixar-jev", config["servers"])

            rc, out, _err = _run_cli(["host-register", "--host", "vscode", "--workspace", str(workspace)])
            self.assertEqual(json.loads(out)["action"], "unchanged")

    def test_claude_desktop_write_never_touches_the_real_config_and_warns_to_quit_the_app(self):
        import jev_auto.host_mcp as host_mcp

        with patch.object(host_mcp, "install",
                           return_value={"host": "claude-desktop", "action": "create", "written": True}) as installed:
            rc, out, err = _run_cli(["host-register", "--host", "claude-desktop", "--write"])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["written"], True)
        self.assertIn("Quit the Claude desktop app", err)
        installed.assert_called_once_with("claude-desktop", None)

    def test_claude_desktop_plan_without_write_never_touches_the_real_config(self):
        import jev_auto.host_mcp as host_mcp

        with patch.object(host_mcp, "plan",
                           return_value={"host": "claude-desktop", "action": "create", "written": False}) as planned:
            rc, out, err = _run_cli(["host-register", "--host", "claude-desktop"])
        self.assertEqual(rc, 0)
        self.assertIn("Nothing written", err)
        self.assertNotIn("Quit the Claude desktop app", err, "the quit-app warning is --write only")
        planned.assert_called_once_with("claude-desktop", None)


class CliVscodeCommandTests(unittest.TestCase):
    """The standalone `vscode` command (distinct from `host-register --host
    vscode`): same underlying adapter, different CLI entry point in main()."""

    def test_plan_then_write(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "project"
            workspace.mkdir()

            rc, out, err = _run_cli(["vscode", "--workspace", str(workspace)])
            self.assertEqual(rc, 0)
            self.assertEqual(json.loads(out)["action"], "create")
            self.assertIn("Nothing written", err)

            rc, out, _err = _run_cli(["vscode", "--workspace", str(workspace), "--write"])
            self.assertEqual(rc, 0)
            self.assertTrue(json.loads(out)["written"])
            self.assertTrue((workspace / ".vscode" / "mcp.json").exists())


class CliSelftestCommandTests(unittest.TestCase):
    """`selftest` replays shipped fixtures through the local gate offline -
    genuinely safe to run for real, no mocking required."""

    def test_full_suite_passes_offline(self):
        rc, out, _err = _run_cli(["selftest"])
        result = json.loads(out)
        self.assertEqual(rc, 0)
        self.assertTrue(result["all_passed"])
        self.assertEqual(result["recipes"], 36)

    def test_single_recipe_and_variant(self):
        rc, out, _err = _run_cli(["selftest", "--recipe", "qualixar.brief-fit", "--variant", "nominal"])
        self.assertEqual(rc, 0)
        self.assertTrue(json.loads(out)["matched"])

    def test_unknown_recipe_is_a_clean_error_not_a_crash(self):
        rc, _out, err = _run_cli(["selftest", "--recipe", "not-a-real-recipe"])
        self.assertEqual(rc, 2)
        self.assertIn("RECIPE_NOT_FOUND", err)


class CliStatusCommandTests(unittest.TestCase):
    """`status` never calls ensure()/request() - it only reads the local
    policy file, so these run with no broker mocking at all."""

    def test_unenrolled_workspace_reports_false(self):
        with _isolated_xdg_home():
            with tempfile.TemporaryDirectory() as directory:
                workspace = Path(directory) / "project"
                workspace.mkdir()
                rc, out, _err = _run_cli(["status", "--workspace", str(workspace)])
        self.assertEqual(rc, 0)
        self.assertEqual(out, '{"enrolled":false}\n')

    def test_enrolled_workspace_reports_provider_and_expiry(self):
        from jev_auto.settings import make_policy, save_policy

        with _isolated_xdg_home():
            with tempfile.TemporaryDirectory() as directory:
                workspace = Path(directory) / "project"
                workspace.mkdir()
                save_policy(workspace, make_policy(workspace, "typesafe", days=5))
                rc, out, _err = _run_cli(["status", "--workspace", str(workspace)])
        self.assertEqual(rc, 0)
        body = json.loads(out)
        self.assertEqual(body, {"enrolled": True, "provider": "typesafe", "routes": {},
                                 "expires_at": body["expires_at"]})


class CliEnrollCommandTests(unittest.TestCase):
    """`enroll` is the only command that requires a real TTY and writes both
    a workspace policy and a global bridge record. `ensure()` is always
    mocked here so no broker subprocess is ever spawned; the provider is
    always given explicitly except in the one test that verifies the
    'existing' branch, which mocks jevkit.providers.resolve_provider so the
    real Keychain-adjacent credential probe is never reached.
    """

    def test_requires_a_real_terminal(self):
        with _isolated_xdg_home():
            with tempfile.TemporaryDirectory() as directory:
                workspace = Path(directory) / "project"
                workspace.mkdir()
                rc, out, err = _run_cli(["enroll", "--workspace", str(workspace),
                                          "--provider", "typesafe"], isatty=False)
        self.assertEqual(rc, 2)
        self.assertEqual(out, "")
        self.assertIn("PRIVATE_TERMINAL_SETUP_REQUIRED", err)

    def test_declining_the_prompt_cancels_setup_and_writes_nothing(self):
        with _isolated_xdg_home() as root:
            with tempfile.TemporaryDirectory() as directory:
                workspace = Path(directory) / "project"
                workspace.mkdir()
                rc, out, err = _run_cli(["enroll", "--workspace", str(workspace),
                                          "--provider", "typesafe"],
                                         isatty=True, input_value="no, thanks")
            self.assertEqual(rc, 2)
            self.assertIn("SETUP_CANCELLED", err)
            self.assertIn("One-time workspace enrollment", out)
            self.assertFalse((root / "state").exists(), "declining must not create any state")

    def test_happy_path_writes_a_policy_and_a_bridge_record(self):
        from jev_auto.settings import load_policy

        with _isolated_xdg_home() as root:
            with tempfile.TemporaryDirectory() as directory:
                workspace = Path(directory) / "project"
                workspace.mkdir()
                rc, out, err = _run_cli(
                    ["enroll", "--workspace", str(workspace), "--provider", "typesafe",
                     "--days", "3", "--generic-query"],
                    isatty=True, input_value="ENABLE", ensure=lambda path: None)
                self.assertEqual(rc, 0)
                self.assertEqual(err, "")
                self.assertIn("Jev Decision Layer enabled", out)
                policy = load_policy(workspace)
            self.assertEqual(policy["provider"], "typesafe")
            self.assertTrue(policy["generic_query_enabled"])
            bridge_file = root / "config" / "qualixar-jev-decision-layer" / "auto-bridge.json"
            self.assertTrue(bridge_file.exists())

    def test_existing_provider_choice_delegates_to_resolve_provider_without_touching_it_for_real(self):
        import jevkit.providers as jevkit_providers

        with _isolated_xdg_home():
            with tempfile.TemporaryDirectory() as directory:
                workspace = Path(directory) / "project"
                workspace.mkdir()
                with patch.object(jevkit_providers, "resolve_provider",
                                   return_value=SimpleNamespace(provider_id="openrouter")) as resolved:
                    rc, out, _err = _run_cli(
                        ["enroll", "--workspace", str(workspace)],  # --provider defaults to "existing"
                        isatty=True, input_value="ENABLE", ensure=lambda path: None)
        self.assertEqual(rc, 0)
        self.assertIn("Provider: openrouter", out)
        resolved.assert_called_once()


class CliRouteLocalCommandTests(unittest.TestCase):
    """`route-local` requires enrollment plus an attested local-model record
    (mlx-installation.json under home_root()) before it will warm up a local
    route. `ensure`/`request` are mocked so the warmup never reaches a real
    broker."""

    def test_requires_enrollment(self):
        with _isolated_xdg_home():
            with tempfile.TemporaryDirectory() as directory:
                workspace = Path(directory) / "project"
                workspace.mkdir()
                rc, _out, err = _run_cli(["route-local", "--workspace", str(workspace),
                                           "--recipe", "sieve"], isatty=True)
        self.assertEqual(rc, 2)
        self.assertIn("WORKSPACE_NOT_ENROLLED", err)

    def test_requires_an_attested_local_model_record(self):
        from jev_auto.settings import make_policy, save_policy

        with _isolated_xdg_home():
            with tempfile.TemporaryDirectory() as directory:
                workspace = Path(directory) / "project"
                workspace.mkdir()
                save_policy(workspace, make_policy(workspace, "typesafe", days=3))
                # No mlx-installation.json written under home_root(): the read
                # raises a bare FileNotFoundError, which main()'s outer
                # `except Exception` masks as a generic failure.
                rc, _out, err = _run_cli(["route-local", "--workspace", str(workspace),
                                           "--recipe", "sieve"], isatty=True)
        self.assertEqual(rc, 2)
        self.assertIn("SETUP_OR_RUNTIME_FAILURE", err)

    def _enrolled_with_attested_mlx(self, workspace):
        """Save a valid policy and drop an attested mlx-installation.json
        under the *currently active* home_root() (i.e. wherever the calling
        test's `_isolated_xdg_home()` context currently points XDG_STATE_HOME)."""
        from jev_auto.common import home_root
        from jev_auto.settings import make_policy, save_policy

        save_policy(workspace, make_policy(workspace, "typesafe", days=3))
        mlx_file = home_root() / "mlx-installation.json"
        mlx_file.parent.mkdir(parents=True, exist_ok=True)
        mlx_file.write_text(json.dumps({"repository": "aac6fef/laya-mlx", "revision": "a" * 40}))
        os.chmod(mlx_file, 0o600)

    def test_requires_at_least_one_recipe(self):
        with _isolated_xdg_home():
            with tempfile.TemporaryDirectory() as directory:
                workspace = Path(directory) / "project"
                workspace.mkdir()
                self._enrolled_with_attested_mlx(workspace)
                rc, _out, err = _run_cli(["route-local", "--workspace", str(workspace)], isatty=True)
        self.assertEqual(rc, 2)
        self.assertIn("SELECT_LOCAL_RECIPE", err)

    def test_declining_cancels_without_updating_routes(self):
        from jev_auto.settings import load_policy

        with _isolated_xdg_home():
            with tempfile.TemporaryDirectory() as directory:
                workspace = Path(directory) / "project"
                workspace.mkdir()
                self._enrolled_with_attested_mlx(workspace)
                rc, _out, err = _run_cli(["route-local", "--workspace", str(workspace),
                                           "--recipe", "sieve"], isatty=True, input_value="nope")
                self.assertEqual(rc, 2)
                self.assertIn("SETUP_CANCELLED", err)
                self.assertEqual(load_policy(workspace).get("routes", {}), {})

    def test_happy_path_warms_up_the_selected_local_routes(self):
        from jev_auto.settings import load_policy

        seen = []

        def fake_request(path, obj, timeout=16):
            seen.append((dict(obj), timeout))
            return {"ready": True}

        with _isolated_xdg_home():
            with tempfile.TemporaryDirectory() as directory:
                workspace = Path(directory) / "project"
                workspace.mkdir()
                self._enrolled_with_attested_mlx(workspace)
                rc, out, err = _run_cli(
                    ["route-local", "--workspace", str(workspace), "--recipe", "sieve", "--recipe", "probe"],
                    isatty=True, input_value="LOCAL", ensure=lambda path: None, request=fake_request)
                self.assertEqual(rc, 0)
                self.assertEqual(err, "")
                self.assertIn("Local routes: sieve,probe", out)
                self.assertEqual(load_policy(workspace)["routes"], {"sieve": "laya-mlx", "probe": "laya-mlx"})
        self.assertEqual(seen, [({"op": "warmup"}, 130)])


class CliBrokerCommandTests(unittest.TestCase):
    """The commands that talk to the resident broker: each is exercised with
    `ensure`/`request` mocked so no socket or subprocess is ever touched, and
    each test asserts the exact request payload main() builds."""

    def _enrolled_workspace(self, stack):
        root = stack.enter_context(_isolated_xdg_home())
        directory = stack.enter_context(tempfile.TemporaryDirectory())
        workspace = Path(directory) / "project"
        workspace.mkdir()
        return root, workspace

    def test_start_reports_broker_health(self):
        with ExitStack() as stack:
            _root, workspace = self._enrolled_workspace(stack)
            rc, out, _err = _run_cli(["start", "--workspace", str(workspace)],
                                      ensure=lambda path: None,
                                      request=lambda path, obj, timeout=16: {"op_seen": obj["op"]})
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out), {"op_seen": "health"})

    def test_warmup_uses_the_extended_timeout(self):
        seen = []
        with ExitStack() as stack:
            _root, workspace = self._enrolled_workspace(stack)
            rc, _out, _err = _run_cli(
                ["warmup", "--workspace", str(workspace)], ensure=lambda path: None,
                request=lambda path, obj, timeout=16: seen.append((obj, timeout)) or {})
        self.assertEqual(rc, 0)
        self.assertEqual(seen, [({"op": "warmup"}, 130)])

    def test_stats_forwards_the_stats_op(self):
        seen = []
        with ExitStack() as stack:
            _root, workspace = self._enrolled_workspace(stack)
            rc, _out, _err = _run_cli(
                ["stats", "--workspace", str(workspace)], ensure=lambda path: None,
                request=lambda path, obj, timeout=16: seen.append(obj) or {})
        self.assertEqual(rc, 0)
        self.assertEqual(seen, [{"op": "stats"}])

    def test_recall_forwards_receipt_window_with_defaults(self):
        seen = []
        with ExitStack() as stack:
            _root, workspace = self._enrolled_workspace(stack)
            rc, _out, _err = _run_cli(
                ["recall", "--workspace", str(workspace), "--receipt-id", "rid-123"],
                ensure=lambda path: None, request=lambda path, obj, timeout=16: seen.append(obj) or {})
        self.assertEqual(rc, 0)
        self.assertEqual(seen, [{"op": "recall", "receipt_id": "rid-123", "start": 1, "end": 120}])

    def test_recall_forwards_explicit_window(self):
        seen = []
        with ExitStack() as stack:
            _root, workspace = self._enrolled_workspace(stack)
            rc, _out, _err = _run_cli(
                ["recall", "--workspace", str(workspace), "--receipt-id", "rid-123",
                 "--start", "5", "--end", "9"],
                ensure=lambda path: None, request=lambda path, obj, timeout=16: seen.append(obj) or {})
        self.assertEqual(rc, 0)
        self.assertEqual(seen, [{"op": "recall", "receipt_id": "rid-123", "start": 5, "end": 9}])

    def test_probe_forwards_the_probe_op(self):
        seen = []
        with ExitStack() as stack:
            _root, workspace = self._enrolled_workspace(stack)
            rc, _out, _err = _run_cli(
                ["probe", "--workspace", str(workspace)], ensure=lambda path: None,
                request=lambda path, obj, timeout=16: seen.append(obj) or {})
        self.assertEqual(rc, 0)
        self.assertEqual(seen, [{"op": "probe"}])

    def test_bridge_config_only_calls_ensure_never_request(self):
        with ExitStack() as stack:
            _root, workspace = self._enrolled_workspace(stack)
            with patch("jev_auto.cli.request") as request_mock:
                rc, out, _err = _run_cli(["bridge-config", "--workspace", str(workspace)],
                                          ensure=lambda path: None)
        self.assertEqual(rc, 0)
        self.assertIn("Browser connection is registered", out)
        request_mock.assert_not_called()

    def test_stop_forwards_shutdown_without_calling_ensure_first(self):
        with ExitStack() as stack:
            _root, workspace = self._enrolled_workspace(stack)
            with patch("jev_auto.cli.ensure") as ensure_mock:
                rc, out, _err = _run_cli(["stop", "--workspace", str(workspace)],
                                          request=lambda path, obj: {"stopping": True})
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out), {"stopping": True})
        ensure_mock.assert_not_called()

    def test_revoke_disables_the_policy_and_best_effort_shuts_down_the_broker(self):
        """load_policy() itself refuses a disabled policy (validate_policy
        requires enabled=True), so the observable proof of revocation is the
        same one an external caller gets: `status` reports unenrolled."""
        from jev_auto.settings import make_policy, save_policy

        with ExitStack() as stack:
            _root, workspace = self._enrolled_workspace(stack)
            save_policy(workspace, make_policy(workspace, "typesafe", days=3))
            rc, out, err = _run_cli(["revoke", "--workspace", str(workspace)],
                                     request=lambda path, obj: {"stopping": True})
            self.assertEqual(rc, 0)
            self.assertEqual(err, "")
            self.assertIn("Further Auto requests disabled", out)
            _rc, status_out, _err = _run_cli(["status", "--workspace", str(workspace)])
            self.assertEqual(status_out, '{"enrolled":false}\n')

    def test_revoke_tolerates_a_broker_that_is_already_gone(self):
        """`request(shutdown)` raising AutoError during revoke is swallowed -
        the workspace is still revoked either way."""
        from jev_auto.common import AutoError
        from jev_auto.settings import make_policy, save_policy

        with ExitStack() as stack:
            _root, workspace = self._enrolled_workspace(stack)
            save_policy(workspace, make_policy(workspace, "typesafe", days=3))

            def raise_unavailable(path, obj):
                raise AutoError("BROKER_UNAVAILABLE")

            rc, out, _err = _run_cli(["revoke", "--workspace", str(workspace)], request=raise_unavailable)
            self.assertEqual(rc, 0)
            self.assertIn("Further Auto requests disabled", out)
            _rc, status_out, _err = _run_cli(["status", "--workspace", str(workspace)])
            self.assertEqual(status_out, '{"enrolled":false}\n')

    def test_revoke_on_an_unenrolled_workspace_hits_the_generic_failure_fallback(self):
        """settings.revoke() raises a bare FileNotFoundError (no policy.json
        exists yet), which is not an AutoError - this is the one branch that
        naturally reaches main()'s generic `except Exception` fallback."""
        with ExitStack() as stack:
            _root, workspace = self._enrolled_workspace(stack)
            rc, out, err = _run_cli(["revoke", "--workspace", str(workspace)])
        self.assertEqual(rc, 2)
        self.assertEqual(out, "")
        self.assertIn("SETUP_OR_RUNTIME_FAILURE", err)


class CliRouteCommandTests(unittest.TestCase):
    """`route` compiles a closed advisory choice and forwards it to the
    broker; its exit-code contract (0 / 2 / 3 / 4) is worth pinning exactly."""

    def _workspace(self, stack):
        stack.enter_context(_isolated_xdg_home())
        directory = stack.enter_context(tempfile.TemporaryDirectory())
        workspace = Path(directory) / "project"
        workspace.mkdir()
        return workspace

    def test_malformed_candidate_is_rejected_before_compile_route_ever_runs(self):
        with ExitStack() as stack:
            workspace = self._workspace(stack)
            rc, _out, err = _run_cli(["route", "--workspace", str(workspace), "--kind", "task",
                                       "--task", "Pick one", "--candidate", "missing-equals-sign",
                                       "--classification", "public"])
        self.assertEqual(rc, 2)
        self.assertIn("ROUTE_CANDIDATES_INVALID", err)

    def test_duplicate_candidate_ids_are_rejected_by_compile_route(self):
        with ExitStack() as stack:
            workspace = self._workspace(stack)
            rc, _out, err = _run_cli(["route", "--workspace", str(workspace), "--kind", "task",
                                       "--task", "Pick one", "--candidate", "a=First",
                                       "--candidate", "a=Second", "--classification", "public"])
        self.assertEqual(rc, 2)
        self.assertIn("ROUTE_CANDIDATES_INVALID", err)

    def test_success_and_abstain_unknown_status_codes(self):
        cases = (
            ({"status": "ADVISORY_UNCALIBRATED", "selected": "a"}, 0),
            ({"status": "ABSTAIN_UNKNOWN"}, 3),
        )
        for result, expected_rc in cases:
            with self.subTest(status=result["status"]):
                with ExitStack() as stack:
                    workspace = self._workspace(stack)
                    rc, out, _err = _run_cli(
                        ["route", "--workspace", str(workspace), "--kind", "task", "--task", "Pick one",
                         "--candidate", "a=First", "--candidate", "b=Second", "--classification", "public"],
                        ensure=lambda path: None, request=lambda path, obj: result)
                self.assertEqual(rc, expected_rc)
                self.assertEqual(json.loads(out), result)

    def test_error_code_prefix_selects_exit_four_others_get_exit_two(self):
        from jev_auto.common import AutoError

        cases = (
            ("PROVIDER_TIMEOUT", 4), ("DECISION_FAILED_OR_UNAVAILABLE", 4),
            ("MODEL_MISMATCH", 4), ("KEYCHAIN_LOCKED", 4), ("NO_CREDENTIAL", 4),
            ("BROKER_BUSY", 2), ("WORKSPACE_NOT_ENROLLED", 2),
        )
        for code, expected_rc in cases:
            with self.subTest(code=code):
                def raise_it(path, obj, _code=code):
                    raise AutoError(_code)

                with ExitStack() as stack:
                    workspace = self._workspace(stack)
                    rc, out, err = _run_cli(
                        ["route", "--workspace", str(workspace), "--kind", "task", "--task", "Pick one",
                         "--candidate", "a=First", "--candidate", "b=Second", "--classification", "public"],
                        ensure=lambda path: None, request=raise_it)
                self.assertEqual(rc, expected_rc)
                self.assertEqual(out, "")
                self.assertIn(code, err)

    def test_ensure_failure_is_also_caught_by_the_inner_handler(self):
        from jev_auto.common import AutoError

        with ExitStack() as stack:
            workspace = self._workspace(stack)
            rc, _out, err = _run_cli(
                ["route", "--workspace", str(workspace), "--kind", "task", "--task", "Pick one",
                 "--candidate", "a=First", "--candidate", "b=Second", "--classification", "public"],
                ensure=lambda path: (_ for _ in ()).throw(AutoError("WORKSPACE_NOT_ENROLLED")))
        self.assertEqual(rc, 2)
        self.assertIn("WORKSPACE_NOT_ENROLLED", err)


class CliMcpAndArgparseTests(unittest.TestCase):
    """The `mcp` command and main()'s own argument-parsing/exception-masking
    scaffolding, shared by every subcommand."""

    def test_mcp_command_delegates_to_mcp_serve(self):
        import jev_auto.mcp as mcp

        with patch.object(mcp, "serve") as serve_mock:
            rc, out, err = _run_cli(["mcp"])
        self.assertEqual(rc, 0)
        self.assertEqual((out, err), ("", ""))
        serve_mock.assert_called_once_with()

    def test_missing_subcommand_exits_two(self):
        import jev_auto.cli as cli

        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                cli.main([])
        self.assertEqual(caught.exception.code, 2)

    def test_unknown_subcommand_exits_two(self):
        import jev_auto.cli as cli

        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                cli.main(["not-a-real-command"])
        self.assertEqual(caught.exception.code, 2)

    def test_unexpected_exception_is_masked_never_leaking_internal_detail(self):
        with ExitStack() as stack:
            stack.enter_context(_isolated_xdg_home())
            directory = stack.enter_context(tempfile.TemporaryDirectory())
            workspace = Path(directory) / "project"
            workspace.mkdir()

            def boom(path):
                raise ValueError("some internal secret-ish detail")

            rc, out, err = _run_cli(["probe", "--workspace", str(workspace)], ensure=boom)
        self.assertEqual(rc, 2)
        self.assertEqual(out, "")
        self.assertIn("SETUP_OR_RUNTIME_FAILURE", err)
        self.assertNotIn("secret-ish", err)


# ==========================================================================
# jev.py - the legacy top-level CLI (jevkit surface)
# ==========================================================================

class JevLegacyCliTests(unittest.TestCase):
    """jev.py's own main() has no `argv=` parameter (it always reads
    sys.argv), so every call here patches sys.argv rather than passing a
    list. Every subcommand that would otherwise touch the real
    ~/.local/state directory is given an explicit --state-root pointing at a
    TemporaryDirectory instead of relying on environment overrides."""

    @staticmethod
    def _run(args):
        import jev

        old_argv = sys.argv
        sys.argv = ["jev"] + args
        out = io.StringIO()
        try:
            with redirect_stdout(out):
                rc = jev.main()
        finally:
            sys.argv = old_argv
        return rc, out.getvalue()

    def test_catalog_lists_twenty_legacy_cases(self):
        with tempfile.TemporaryDirectory() as directory:
            rc, out = self._run(["catalog", "--state-root", directory])
        self.assertEqual(rc, 0)
        self.assertEqual(len(json.loads(out)), 20)

    def test_describe_valid_case(self):
        with tempfile.TemporaryDirectory() as directory:
            rc, out = self._run(["describe", "01-skill-routing", "--state-root", directory])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["id"], "01-skill-routing")

    def test_describe_unknown_case_raises_directly_no_main_guard_here(self):
        """jev.py's main() has no internal try/except - SafeError propagates
        straight to the caller when main() is called as a function, exactly
        as it would to `if __name__=='__main__'` if that guard were absent."""
        from jevkit.security import SafeError

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(SafeError, "UNKNOWN_CASE"):
                self._run(["describe", "not-a-real-case", "--state-root", directory])

    def test_health_reports_offline_scope_without_a_provider_check(self):
        with tempfile.TemporaryDirectory() as directory:
            rc, out = self._run(["health", "--state-root", directory])
        self.assertEqual(rc, 0)
        body = json.loads(out)
        self.assertEqual(body["scope"], "global-offline")
        self.assertFalse(body["provider_checked"])

    def test_run_fixture_mode_default_and_explicit_variant(self):
        with tempfile.TemporaryDirectory() as directory:
            rc, out = self._run(["run", "01-skill-routing", "--state-root", directory])
            self.assertEqual(rc, 0)
            self.assertEqual(json.loads(out)["mode"], "fixture")
            rc, out = self._run(["run", "01-skill-routing", "--variant", "adversarial",
                                  "--state-root", directory])
            self.assertEqual(rc, 0)
            self.assertEqual(json.loads(out)["variant"], "adversarial")

    def test_run_live_mode_requires_project_live_scope(self):
        from jevkit.security import SafeError

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(SafeError, "LIVE_SCOPE_REQUIRED"):
                self._run(["run", "01-skill-routing", "--mode", "live", "--state-root", directory])

    def test_run_live_mode_in_hybrid_scope_requires_a_workspace(self):
        from jevkit.security import SafeError

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(SafeError, "WORKSPACE_ID_REQUIRED"):
                self._run(["run", "01-skill-routing", "--mode", "live",
                           "--scope", "global-hybrid", "--state-root", directory])

    def test_suite_fixture_mode_all_variants_by_default(self):
        with tempfile.TemporaryDirectory() as directory:
            rc, out = self._run(["suite", "--state-root", directory])
        body = json.loads(out)
        self.assertEqual(rc, 0)
        self.assertTrue(body["all_fixture_contracts_passed"])
        self.assertEqual(len(body["runs"]), 60)  # 20 cases x 3 variants

    def test_suite_fixture_mode_single_variant(self):
        with tempfile.TemporaryDirectory() as directory:
            rc, out = self._run(["suite", "--variant", "nominal", "--state-root", directory])
        body = json.loads(out)
        self.assertEqual(rc, 0)
        self.assertEqual(len(body["runs"]), 20)

    def test_mcp_subcommand_delegates_to_the_legacy_mcp_server(self):
        import jevkit.mcp_server as mcp_server

        with patch.object(mcp_server, "serve") as serve_mock:
            rc, out = self._run(["mcp"])
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")
        serve_mock.assert_called_once()

    def test_serve_subcommand_delegates_to_the_web_server_with_the_given_port(self):
        import jevkit.web_server as web_server

        with patch.object(web_server, "serve") as serve_mock:
            rc, out = self._run(["serve", "--port", "9999"])
        self.assertEqual(rc, 0)
        serve_mock.assert_called_once_with(9999)

    def test_dunder_main_guard_maps_success_and_safeerror_to_exit_codes(self):
        """Calling main() directly (as every other test here does) never
        reaches jev.py's `if __name__=='__main__':` try/except. This is the
        one test that actually re-executes the module as __main__, via
        runpy, to prove that guard maps a clean run to exit 0 and a SafeError
        to exit 2."""
        with tempfile.TemporaryDirectory() as directory:
            cases = (
                (["jev", "catalog", "--state-root", directory], 0),
                (["jev", "describe", "not-a-real-case", "--state-root", directory], 2),
            )
            for argv, expected_code in cases:
                with self.subTest(argv=argv):
                    old_argv = sys.argv
                    sys.argv = argv
                    try:
                        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                            with warnings.catch_warnings():
                                warnings.simplefilter("ignore", RuntimeWarning)
                                with self.assertRaises(SystemExit) as caught:
                                    runpy.run_module("jev", run_name="__main__", alter_sys=True)
                        self.assertEqual(caught.exception.code, expected_code)
                    finally:
                        sys.argv = old_argv


# ==========================================================================
# auto_entry.py - the tiny plugin-runtime entry point
# ==========================================================================

class AutoEntryTests(unittest.TestCase):
    """A 2-statement file: an import plus a __main__ guard. Both statements
    need their own test, since importing the module only ever covers the
    first one."""

    def test_main_is_jev_auto_cli_main(self):
        import auto_entry
        import jev_auto.cli as cli

        self.assertIs(auto_entry.main, cli.main)

    def test_dunder_main_guard_runs_cli_main_and_exits_with_its_return_code(self):
        old_argv = sys.argv
        sys.argv = ["jev-entry", "selftest"]  # full offline fixture suite: safe, deterministic
        try:
            with redirect_stdout(io.StringIO()):
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", RuntimeWarning)
                    with self.assertRaises(SystemExit) as caught:
                        runpy.run_module("auto_entry", run_name="__main__", alter_sys=True)
            self.assertEqual(caught.exception.code, 0)
        finally:
            sys.argv = old_argv


# ==========================================================================
# jev_auto/server.py - the resident broker process
# ==========================================================================

class ServerHandlerTests(unittest.TestCase):
    """Handler.handle() decodes one request, dispatches it, and must never
    let dispatch's exception, or a dead peer socket, escape - a broker that
    crashes on one bad request would take down every other in-flight one."""

    def test_normal_request_dispatches_and_returns_ok_result(self):
        a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            engine = SimpleNamespace(dispatch=lambda req: {"echo": req})
            b.sendall(b'{"op":"probe"}\n')
            _make_handler(a, engine).handle()
            response = json.loads(b.recv(65536).decode())
        finally:
            a.close(); b.close()
        self.assertEqual(response, {"ok": True, "result": {"echo": {"op": "probe"}}})

    def test_shutdown_op_sets_stopping_and_never_reaches_dispatch(self):
        a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        called = []
        stopping = threading.Event()
        try:
            engine = SimpleNamespace(dispatch=lambda req: called.append(req))
            b.sendall(b'{"op":"shutdown"}\n')
            _make_handler(a, engine, stopping).handle()
            response = json.loads(b.recv(65536).decode())
        finally:
            a.close(); b.close()
        self.assertEqual(response, {"ok": True, "result": {"stopping": True}})
        self.assertTrue(stopping.is_set())
        self.assertEqual(called, [])

    def test_autoerror_from_dispatch_is_reported_by_its_code(self):
        from jev_auto.common import AutoError

        a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)

        def raise_auto(_req):
            raise AutoError("RECIPE_NOT_ENROLLED")

        try:
            engine = SimpleNamespace(dispatch=raise_auto)
            b.sendall(b'{"op":"x"}\n')
            _make_handler(a, engine).handle()
            response = json.loads(b.recv(65536).decode())
        finally:
            a.close(); b.close()
        self.assertEqual(response, {"ok": False, "error": "RECIPE_NOT_ENROLLED"})

    def test_unexpected_exception_is_masked_never_leaking_internal_detail(self):
        a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)

        def raise_generic(_req):
            raise ValueError("leaky internal detail")

        try:
            engine = SimpleNamespace(dispatch=raise_generic)
            b.sendall(b'{"op":"x"}\n')
            _make_handler(a, engine).handle()
            raw = b.recv(65536)
            response = json.loads(raw.decode())
        finally:
            a.close(); b.close()
        self.assertEqual(response, {"ok": False, "error": "BROKER_INTERNAL_ERROR"})
        self.assertNotIn(b"leaky", raw)

    def test_peer_uid_check_branch_is_exercised_and_still_fails_closed(self):
        """SO_PEERCRED does not exist on this Darwin test host, so the `if
        hasattr(socket, 'SO_PEERCRED')` guard is normally always False here
        and that branch's body never runs in this environment. Forcing the
        attribute to exist (with an option number Darwin's getsockopt does
        not actually support) exercises that line deliberately: getsockopt
        raises OSError, which the handler's own `except Exception` still
        turns into the same safe, non-leaking response."""
        a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            engine = SimpleNamespace(dispatch=lambda req: {"should": "not-be-reached"})
            b.sendall(b'{"op":"x"}\n')
            with patch.object(socket, "SO_PEERCRED", 2, create=True):
                _make_handler(a, engine).handle()
            response = json.loads(b.recv(65536).decode())
        finally:
            a.close(); b.close()
        self.assertEqual(response, {"ok": False, "error": "BROKER_INTERNAL_ERROR"})

    def test_broken_pipe_on_sendall_is_swallowed_not_raised(self):
        a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        engine = SimpleNamespace(dispatch=lambda req: {"ok": "fine"})
        b.sendall(b'{"op":"x"}\n')
        b.close()  # peer gone before the handler can reply
        try:
            _make_handler(a, engine).handle()  # must not raise
        finally:
            a.close()


class ServerSlotLimitTests(unittest.TestCase):
    """Server bounds concurrent in-flight requests to 8 so a burst cannot
    exhaust threads; process_request must reject the ninth with BROKER_BUSY,
    and process_request_thread must always release its slot."""

    def test_process_request_rejects_when_all_eight_slots_are_taken(self):
        with tempfile.TemporaryDirectory() as directory:
            import jev_auto.server as server

            addr = Path(directory) / "s.sock"
            srv = server.Server(addr, SimpleNamespace(dispatch=lambda r: {}))
            try:
                srv.slots._value = 0  # whitebox: simulate all 8 slots in use

                class FakeRequest:
                    def __init__(self):
                        self.sent = None
                        self.closed = False

                    def sendall(self, data):
                        self.sent = data

                    def close(self):
                        self.closed = True

                fake = FakeRequest()
                srv.process_request(fake, ("peer",))
            finally:
                srv.server_close()
        self.assertEqual(json.loads(fake.sent.decode()), {"ok": False, "error": "BROKER_BUSY"})
        self.assertTrue(fake.closed)

    def test_process_request_delegates_to_the_real_handler_when_a_slot_is_free(self):
        """The mirror image of the BROKER_BUSY test above: with a slot
        available, process_request must take the *real* socketserver path
        (spawn a thread that builds a real Handler and calls .handle()), not
        just avoid sending BROKER_BUSY. socket.recv() on the peer end blocks
        until that background thread actually replies, so this needs no
        sleep/poll to synchronize."""
        import jev_auto.server as server

        with tempfile.TemporaryDirectory() as directory:
            addr = Path(directory) / "s.sock"
            srv = server.Server(addr, SimpleNamespace(dispatch=lambda req: {"echo": req}))
            a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                b.sendall(b'{"op":"probe"}\n')
                srv.process_request(a, ("peer",))
                response = json.loads(b.recv(65536).decode())
            finally:
                b.close()
                srv.server_close()
        self.assertEqual(response, {"ok": True, "result": {"echo": {"op": "probe"}}})

    def test_process_request_thread_always_releases_its_slot(self):
        import socketserver

        import jev_auto.server as server

        with tempfile.TemporaryDirectory() as directory:
            addr = Path(directory) / "s.sock"
            srv = server.Server(addr, SimpleNamespace(dispatch=lambda r: {}))
            try:
                srv.slots.acquire()
                self.assertEqual(srv.slots._value, 7)
                with patch.object(socketserver.ThreadingMixIn, "process_request_thread",
                                   lambda self, request, address: None):
                    srv.process_request_thread(object(), ("peer",))
                self.assertEqual(srv.slots._value, 8)
            finally:
                srv.server_close()

    def test_process_request_thread_releases_its_slot_even_if_the_base_handler_raises(self):
        import socketserver

        import jev_auto.server as server

        with tempfile.TemporaryDirectory() as directory:
            addr = Path(directory) / "s.sock"
            srv = server.Server(addr, SimpleNamespace(dispatch=lambda r: {}))
            try:
                srv.slots.acquire()

                def raise_inside(self, request, address):
                    raise RuntimeError("synthetic handler failure")

                with patch.object(socketserver.ThreadingMixIn, "process_request_thread", raise_inside):
                    with self.assertRaises(RuntimeError):
                        srv.process_request_thread(object(), ("peer",))
                self.assertEqual(srv.slots._value, 8, "the slot must be released even on failure")
            finally:
                srv.server_close()


class ServerServeLifecycleTests(unittest.TestCase):
    """serve()'s own setup/teardown: the advisory single-instance lock, the
    Engine construction that requires a real enrolled policy, and clean
    socket teardown. `jev_auto.server.address` is patched to a path inside
    our own TemporaryDirectory in every test that gets past the lock, so no
    socket file is ever created under the shared /private/tmp broker
    directory."""

    def test_returns_immediately_when_another_broker_already_holds_the_lock(self):
        import jev_auto.server as server
        from jev_auto.common import private_dir, state_dir

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "project"
            workspace.mkdir()
            base = root / "state-base"
            lock_path = private_dir(state_dir(workspace, base)) / "broker.lock"
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                with patch.object(server, "address", return_value=root / "unused.sock"):
                    result = server.serve(workspace, base, idle_seconds=0)
                self.assertIsNone(result)
                self.assertFalse((root / "unused.sock").exists())
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

    def test_raises_when_the_workspace_is_not_enrolled(self):
        import jev_auto.server as server
        from jev_auto.common import AutoError

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "project"
            workspace.mkdir()
            base = root / "state-base"
            with patch.object(server, "address", return_value=root / "unused.sock"):
                with self.assertRaisesRegex(AutoError, "WORKSPACE_NOT_ENROLLED"):
                    server.serve(workspace, base, idle_seconds=0)

    def test_builds_and_tears_down_the_engine_when_immediately_idle(self):
        import jev_auto.server as server
        from jev_auto.settings import make_policy, save_policy

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "project"
            workspace.mkdir()
            base = root / "state-base"
            sockets = root / "sockets"
            sockets.mkdir()
            fake_addr = sockets / "test.sock"
            save_policy(workspace, make_policy(workspace, "typesafe", days=1), base=base)

            with patch.object(server, "address", return_value=fake_addr):
                # idle_seconds=0 means "stopping OR already past idle" is true
                # before the loop's first iteration, so handle_request() is
                # never called and no connection is ever accepted.
                result = server.serve(workspace, base, idle_seconds=0)
            self.assertIsNone(result)
            self.assertFalse(fake_addr.exists(), "the socket must be unlinked on the way out")

    def test_a_stale_leftover_socket_file_is_unlinked_before_binding(self):
        """A crashed prior broker can leave its socket special file behind.
        serve() must clear it before binding a fresh one, not fail or bind
        alongside it."""
        import jev_auto.server as server
        from jev_auto.settings import make_policy, save_policy

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "project"
            workspace.mkdir()
            base = root / "state-base"
            sockets = root / "sockets"
            sockets.mkdir()
            fake_addr = sockets / "test.sock"
            fake_addr.write_text("stale leftover, not a real socket")
            save_policy(workspace, make_policy(workspace, "typesafe", days=1), base=base)

            with patch.object(server, "address", return_value=fake_addr):
                result = server.serve(workspace, base, idle_seconds=0)
            self.assertIsNone(result)
            self.assertFalse(fake_addr.exists())

    def test_a_symlinked_socket_path_is_refused_not_followed(self):
        import jev_auto.server as server
        from jev_auto.common import AutoError
        from jev_auto.settings import make_policy, save_policy

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "project"
            workspace.mkdir()
            base = root / "state-base"
            sockets = root / "sockets"
            sockets.mkdir()
            real_target = sockets / "elsewhere"
            real_target.write_text("x")
            fake_addr = sockets / "test.sock"
            fake_addr.symlink_to(real_target)
            save_policy(workspace, make_policy(workspace, "typesafe", days=1), base=base)

            with patch.object(server, "address", return_value=fake_addr):
                with self.assertRaisesRegex(AutoError, "UNSAFE_SOCKET"):
                    server.serve(workspace, base, idle_seconds=0)
            self.assertTrue(real_target.exists(), "must refuse, never delete through the symlink")


class ServerMainArgvTests(unittest.TestCase):
    """server.py's own argparse wiring: --workspace is required and untyped
    (a plain string), --state-base is optional and coerced to Path."""

    def test_parses_workspace_and_state_base_then_calls_serve(self):
        import jev_auto.server as server

        with patch.object(server, "serve") as serve_mock:
            old_argv = sys.argv
            sys.argv = ["jev-auto-server", "--workspace", "/some/workspace", "--state-base", "/some/base"]
            try:
                server.main()
            finally:
                sys.argv = old_argv
        serve_mock.assert_called_once_with("/some/workspace", Path("/some/base"))

    def test_state_base_defaults_to_none(self):
        import jev_auto.server as server

        with patch.object(server, "serve") as serve_mock:
            old_argv = sys.argv
            sys.argv = ["jev-auto-server", "--workspace", "/some/workspace"]
            try:
                server.main()
            finally:
                sys.argv = old_argv
        serve_mock.assert_called_once_with("/some/workspace", None)

    def test_missing_workspace_exits_two(self):
        import jev_auto.server as server

        old_argv = sys.argv
        sys.argv = ["jev-auto-server"]
        try:
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    server.main()
        finally:
            sys.argv = old_argv
        self.assertEqual(caught.exception.code, 2)

    def test_dunder_main_guard_invokes_main(self):
        """Runs server.py fresh as __main__ via runpy. The advisory lock is
        pre-acquired first so the real serve() inside takes its fastest,
        side-effect-free path (return None) rather than trying to bind a
        socket under the shared /private/tmp broker directory."""
        import jev_auto.server as server
        from jev_auto.common import private_dir, state_dir

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "project"
            workspace.mkdir()
            base = root / "state-base"
            lock_path = private_dir(state_dir(workspace, base)) / "broker.lock"
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            old_argv = sys.argv
            sys.argv = ["jev-auto-server", "--workspace", str(workspace), "--state-base", str(base)]
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", RuntimeWarning)
                    namespace = runpy.run_module("jev_auto.server", run_name="__main__", alter_sys=True)
                self.assertIn("main", namespace)
            finally:
                sys.argv = old_argv
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)


# ==========================================================================
# jev_auto/claude_hook.py - the Claude Code advisory hook
# ==========================================================================

class ClaudeHookHandleTests(unittest.TestCase):
    """handle() takes an explicit policy_loader precisely so tests can inject
    one; this is the same pattern already used for the sibling agy_hook
    module in test_core_contracts.py."""

    def test_non_dict_payload_is_silently_ignored(self):
        from jev_auto.claude_hook import handle

        self.assertEqual(handle([1, 2, 3]), "")
        self.assertEqual(handle("a string"), "")
        self.assertEqual(handle(None), "")

    def test_unhandled_event_names_are_silently_ignored(self):
        """An unhandled event name must short-circuit BEFORE workspace
        resolution or policy_loader - proven here with a loader that records
        whether it was ever called, not just by checking the return value
        (an empty return could otherwise also happen because "/x" does not
        exist, which would prove the wrong thing)."""
        from jev_auto.claude_hook import handle

        for name in ("Stop", "PostToolUse", "SomethingNew", None):
            with self.subTest(name=name):
                seen = []
                result = handle({"hook_event_name": name, "cwd": "/x"},
                                 policy_loader=lambda _p: seen.append(True) or {"enabled": True})
                self.assertEqual(result, "")
                self.assertEqual(seen, [], "policy_loader must not run for an unhandled event name")

    def test_missing_cwd_field_is_silently_ignored(self):
        """Same proof-of-non-invocation as above, for each way `cwd` can be
        absent or malformed: missing key, blank string, wrong type."""
        from jev_auto.claude_hook import handle

        for event in ({"hook_event_name": "SessionStart"},
                      {"hook_event_name": "SessionStart", "cwd": ""},
                      {"hook_event_name": "SessionStart", "cwd": 42}):
            with self.subTest(event=event):
                seen = []
                result = handle(event, policy_loader=lambda _p: seen.append(True) or {"enabled": True})
                self.assertEqual(result, "")
                self.assertEqual(seen, [], "policy_loader must not run without a valid cwd")

    def test_a_failing_policy_loader_is_treated_as_silence_never_raises(self):
        """`cwd` must resolve for real here: workspace() runs BEFORE
        policy_loader, so a nonexistent path would make this pass for the
        wrong reason (it would never even reach `blow_up`). A `seen` marker
        proves the loader was actually invoked."""
        from jev_auto.claude_hook import handle

        seen = []

        def blow_up(_path):
            seen.append(True)
            raise RuntimeError("disk on fire")

        with tempfile.TemporaryDirectory() as directory:
            result = handle({"hook_event_name": "SessionStart", "cwd": directory}, policy_loader=blow_up)
        self.assertEqual(result, "")
        self.assertEqual(seen, [True], "the failing loader must actually have been called")

    def test_unenrolled_workspace_is_silent_by_design_not_a_failure(self):
        from jev_auto.claude_hook import handle

        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(handle({"hook_event_name": "UserPromptSubmit", "cwd": directory},
                                     policy_loader=lambda _p: {"enabled": False}), "")
            self.assertEqual(handle({"hook_event_name": "UserPromptSubmit", "cwd": directory},
                                     policy_loader=lambda _p: {}), "")

    def test_enrolled_session_start_and_subagent_start_return_the_session_hint(self):
        """`cwd` must be a real, existing directory: handle() resolves it
        through common.workspace() BEFORE calling policy_loader, and that
        resolution raises (silently, fail-open) for a path that does not
        exist - a first draft of this test used the literal string "/x" and
        got "" back for that reason, not because the injected loader was
        ignored."""
        from jev_auto.claude_hook import _SESSION_HINT, handle

        with tempfile.TemporaryDirectory() as directory:
            for name in ("SessionStart", "SubagentStart"):
                with self.subTest(name=name):
                    result = handle({"hook_event_name": name, "cwd": directory},
                                     policy_loader=lambda _p: {"enabled": True})
                    self.assertEqual(result, _SESSION_HINT)

    def test_enrolled_user_prompt_submit_returns_the_prompt_hint(self):
        from jev_auto.claude_hook import _PROMPT_HINT, handle

        with tempfile.TemporaryDirectory() as directory:
            result = handle({"hook_event_name": "UserPromptSubmit", "cwd": directory},
                             policy_loader=lambda _p: {"enabled": True})
        self.assertEqual(result, _PROMPT_HINT)

    def test_enrolled_pretooluse_stays_silent_it_must_never_gain_authority(self):
        """HARD RULE from the module docstring: PreToolUse must never emit a
        permissionDecision or block anything, so this version stays silent
        even when enrolled."""
        from jev_auto.claude_hook import handle

        with tempfile.TemporaryDirectory() as directory:
            result = handle({"hook_event_name": "PreToolUse", "cwd": directory},
                             policy_loader=lambda _p: {"enabled": True})
        self.assertEqual(result, "")

    def test_workspace_path_is_resolved_through_the_shared_workspace_helper(self):
        """Only the documented `cwd` field is accepted, and it is passed
        through common.workspace() (not used verbatim) before reaching the
        policy loader."""
        from jev_auto.claude_hook import handle

        seen = []
        with tempfile.TemporaryDirectory() as directory:
            handle({"hook_event_name": "SessionStart", "cwd": directory},
                   policy_loader=lambda p: seen.append(p) or {"enabled": True})
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0], Path(directory).resolve())


class ClaudeHookMainEntryTests(unittest.TestCase):
    """main() itself: the stdin-byte-cap, JSON decode, and exit(0)-always
    contract, plus the one integration test that proves it wires through to
    a real enrolled workspace's real policy without any injected loader."""

    @staticmethod
    def _run_main(payload_bytes):
        import jev_auto.claude_hook as claude_hook

        out = io.StringIO()
        fake_stdin = SimpleNamespace(buffer=io.BytesIO(payload_bytes))
        code = None
        with patch.object(sys, "stdin", fake_stdin), patch.object(sys, "stdout", out):
            try:
                claude_hook.main()
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue()

    def test_always_exits_zero_and_prints_nothing_for_every_silent_shape(self):
        cases = {
            "empty payload": b"",
            "not a dict": b"[1,2,3]",
            "malformed json": b"{not json",
            "unhandled event": json.dumps({"hook_event_name": "Stop", "cwd": "/x"}).encode(),
            "no cwd field": json.dumps({"hook_event_name": "SessionStart"}).encode(),
            "oversized": b"x" * 70_000,
        }
        import jev_auto.claude_hook as claude_hook
        self.assertGreater(len(cases["oversized"]), claude_hook.MAX_INPUT_BYTES)
        for label, payload in cases.items():
            with self.subTest(label=label):
                code, out = self._run_main(payload)
                self.assertEqual(code, 0)
                self.assertEqual(out, "")

    def test_prints_the_session_hint_for_a_real_enrolled_workspace(self):
        from jev_auto.settings import make_policy, save_policy

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "project"
            workspace.mkdir()
            with patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state")}):
                save_policy(workspace, make_policy(workspace, "typesafe", days=1))
                code, out = self._run_main(
                    json.dumps({"hook_event_name": "SessionStart", "cwd": str(workspace)}).encode())
        self.assertEqual(code, 0)
        self.assertIn("Qualixar Jev Decision Layer is enrolled", out)

    def test_log_helper_writes_when_the_env_var_is_set_and_never_raises_otherwise(self):
        from jev_auto.claude_hook import _log

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "hook.log"
            with patch.dict(os.environ, {"JEV_CLAUDE_HOOK_LOG": str(target)}):
                _log("marker-one")
                _log("marker-two")
            self.assertEqual(target.read_text(), "marker-one\nmarker-two\n")

            with patch.dict(os.environ, {"JEV_CLAUDE_HOOK_LOG": str(Path(directory) / "no" / "such" / "dir.log")}):
                _log("must not raise")  # OSError on open() is swallowed

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("JEV_CLAUDE_HOOK_LOG", None)
            _log("no env var set: also must not raise")

    def test_dunder_main_guard_reads_stdin_and_always_exits_zero(self):
        """Calling main() directly (every other test above) never reaches
        the module's `if __name__ == "__main__":` line itself. Re-executes
        the module fresh as __main__ via runpy with an empty, harmless
        stdin payload to prove that line is wired to the real main()."""
        fake_stdin = SimpleNamespace(buffer=io.BytesIO(b""))
        out = io.StringIO()
        code = None
        with patch.object(sys, "stdin", fake_stdin), patch.object(sys, "stdout", out):
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                try:
                    runpy.run_module("jev_auto.claude_hook", run_name="__main__", alter_sys=True)
                except SystemExit as exc:
                    code = exc.code
        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue(), "")


# ==========================================================================
# jev_auto/measure.py - matched-run accounting and its honesty invariant
# ==========================================================================

class MeasureValidateRunTests(unittest.TestCase):
    """validate_run() is the input gate every other function in this module
    relies on; each rejection is a distinct, deliberately named AutoError."""

    def _base(self, **overrides):
        base = {"task_id": "t1", "task_fingerprint": "f1", "host": "codex", "model": "m",
                "effort": "medium", "accepted": True}
        return {**base, **overrides}

    def test_accepts_a_well_formed_run(self):
        from jev_auto.measure import validate_run

        run = self._base(input_tokens=100, cached_input_tokens=10, output_tokens=20,
                          elapsed_seconds=1.5, total_cost_usd=0.01)
        self.assertEqual(validate_run(run), run)

    def test_rejects_non_dict(self):
        from jev_auto.common import AutoError
        from jev_auto.measure import validate_run

        with self.assertRaisesRegex(AutoError, "RUN_OBJECT"):
            validate_run("not-a-dict")

    def test_rejects_missing_or_blank_identity_fields(self):
        from jev_auto.common import AutoError
        from jev_auto.measure import validate_run

        for field in ("task_id", "task_fingerprint", "host", "model", "effort"):
            with self.subTest(missing=field):
                run = self._base()
                del run[field]
                with self.assertRaisesRegex(AutoError, "RUN_IDENTITY"):
                    validate_run(run)
            with self.subTest(blank=field):
                with self.assertRaisesRegex(AutoError, "RUN_IDENTITY"):
                    validate_run(self._base(**{field: ""}))

    def test_rejects_a_non_boolean_accepted_flag(self):
        from jev_auto.common import AutoError
        from jev_auto.measure import validate_run

        with self.assertRaisesRegex(AutoError, "RUN_ACCEPTANCE"):
            validate_run(self._base(accepted="yes"))

    def test_rejects_non_integer_or_negative_or_boolean_token_counts(self):
        from jev_auto.common import AutoError
        from jev_auto.measure import validate_run

        for field in ("input_tokens", "cached_input_tokens", "output_tokens"):
            for bad in (1.5, -1, True):
                with self.subTest(field=field, bad=bad):
                    with self.assertRaisesRegex(AutoError, "RUN_TOKENS"):
                        validate_run(self._base(**{field: bad}))

    def test_cached_tokens_cannot_exceed_input_tokens(self):
        from jev_auto.common import AutoError
        from jev_auto.measure import validate_run

        with self.assertRaisesRegex(AutoError, "CACHE_SUBSET"):
            validate_run(self._base(input_tokens=10, cached_input_tokens=11, output_tokens=1))

    def test_rejects_out_of_range_or_non_finite_metrics(self):
        from jev_auto.common import AutoError
        from jev_auto.measure import validate_run

        for field in ("elapsed_seconds", "total_cost_usd"):
            for bad in (-1, float("nan"), float("inf")):
                with self.subTest(field=field, bad=bad):
                    with self.assertRaisesRegex(AutoError, "RUN_METRIC"):
                        validate_run(self._base(**{field: bad}))


class MeasureCompareHonestyTests(unittest.TestCase):
    """This is the module's whole reason for existing: `compare()` must
    never invent a number it cannot support with real data, and
    `subscription_quota_saving_percent` is a measurement the product has
    deliberately never taken."""

    def _run(self, **overrides):
        base = {"task_id": "t1", "task_fingerprint": "f1", "host": "codex", "model": "m",
                "effort": "medium", "accepted": True, "input_tokens": 1000,
                "cached_input_tokens": 200, "output_tokens": 100,
                "elapsed_seconds": 10.0, "total_cost_usd": 1.0}
        return {**base, **overrides}

    def test_never_invents_a_subscription_quota_number(self):
        from jev_auto.measure import compare

        # Even a run that supplies every other metric, and even a run that
        # tries to smuggle a value under that key, must still get None back:
        # the field is not read from either input, ever.
        a = self._run()
        b = self._run(input_tokens=400, cached_input_tokens=100, output_tokens=50,
                       elapsed_seconds=4.0, total_cost_usd=0.4,
                       subscription_quota_saving_percent=99)
        result = compare(a, b)
        self.assertIsNone(result["subscription_quota_saving_percent"])

    def test_computes_real_reductions_when_both_runs_are_accepted(self):
        from jev_auto.measure import compare

        a = self._run()
        b = self._run(input_tokens=400, cached_input_tokens=100, output_tokens=50,
                       elapsed_seconds=4.0, total_cost_usd=0.4)
        result = compare(a, b)
        self.assertTrue(result["accepted_both"])
        self.assertAlmostEqual(result["host_token_reduction_percent"], 59.0909090909, places=6)
        self.assertEqual(result["end_to_end_speedup"], 2.5)
        self.assertEqual(result["total_cost_reduction_percent"], 60.0)

    def test_reductions_are_none_when_either_run_was_not_accepted(self):
        from jev_auto.measure import compare

        a = self._run(accepted=False)
        b = self._run()
        result = compare(a, b)
        self.assertFalse(result["accepted_both"])
        self.assertIsNone(result["host_token_reduction_percent"])
        self.assertIsNone(result["elapsed_reduction_percent"])
        self.assertIsNone(result["end_to_end_speedup"])
        self.assertIsNone(result["total_cost_reduction_percent"])
        self.assertIsNone(result["subscription_quota_saving_percent"])

    def test_reductions_are_none_when_token_counts_are_simply_unknown(self):
        """No inference from anything else supplied - missing means None,
        never a guess."""
        from jev_auto.measure import compare

        a = self._run(input_tokens=None, cached_input_tokens=None)
        b = self._run(input_tokens=None, cached_input_tokens=None)
        result = compare(a, b)
        self.assertTrue(result["accepted_both"])
        self.assertIsNone(result["host_token_reduction_percent"])
        self.assertIsNone(result["host_tokens_baseline"])
        self.assertIsNone(result["host_tokens_treatment"])

    def test_rejects_comparing_runs_with_different_identity(self):
        from jev_auto.common import AutoError
        from jev_auto.measure import compare

        a = self._run()
        for field in ("task_id", "task_fingerprint", "host", "model", "effort"):
            with self.subTest(field=field):
                b = self._run(**{field: "different"})
                with self.assertRaisesRegex(AutoError, "UNMATCHED_RUNS"):
                    compare(a, b)

    def test_negative_reduction_is_preserved_not_clamped_to_zero(self):
        """A 'baseline' that used fewer tokens than the 'treatment' must show
        a negative percentage, not a floor of zero - reporting a regression
        as neutral would be exactly the kind of invented-good-news number
        this module exists to prevent."""
        from jev_auto.measure import compare

        a = self._run(input_tokens=100, cached_input_tokens=0, output_tokens=10)
        b = self._run(input_tokens=500, cached_input_tokens=0, output_tokens=50)
        result = compare(a, b)
        self.assertLess(result["host_token_reduction_percent"], 0)


class MeasureExtractCodexUsageTests(unittest.TestCase):
    """extract_codex_usage() turns a raw Codex transcript into a usage dict;
    its aggregation strategy must be explicit whenever more than one
    `turn.completed` record is present."""

    def test_requires_at_least_one_turn_completed_record(self):
        from jev_auto.common import AutoError
        from jev_auto.measure import extract_codex_usage

        with self.assertRaisesRegex(AutoError, "NO_HOST_USAGE"):
            extract_codex_usage([{"type": "other"}, {"type": "turn.completed"}])  # no "usage" key

    def test_multiple_records_without_a_declared_strategy_is_refused(self):
        from jev_auto.common import AutoError
        from jev_auto.measure import extract_codex_usage

        records = [
            {"type": "turn.completed", "usage": {"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 5}},
            {"type": "turn.completed", "usage": {"input_tokens": 20, "cached_input_tokens": 0, "output_tokens": 8}},
        ]
        with self.assertRaisesRegex(AutoError, "DECLARE_USAGE_AGGREGATION"):
            extract_codex_usage(records)

    def test_per_turn_sums_every_record_last_cumulative_keeps_only_the_last(self):
        from jev_auto.measure import extract_codex_usage

        records = [
            {"type": "turn.completed", "usage": {"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 5}},
            {"type": "turn.completed", "usage": {"input_tokens": 20, "cached_input_tokens": 0, "output_tokens": 8}},
        ]
        self.assertEqual(extract_codex_usage(records, multiple="per-turn"),
                         {"input_tokens": 30, "cached_input_tokens": 0, "output_tokens": 13})
        self.assertEqual(extract_codex_usage(records, multiple="last-cumulative"),
                         {"input_tokens": 20, "cached_input_tokens": 0, "output_tokens": 8})

    def test_a_single_missing_field_across_records_makes_the_whole_field_none(self):
        from jev_auto.measure import extract_codex_usage

        records = [
            {"type": "turn.completed", "usage": {"input_tokens": 10, "cached_input_tokens": None, "output_tokens": 5}},
        ]
        self.assertEqual(extract_codex_usage(records),
                         {"input_tokens": 10, "cached_input_tokens": None, "output_tokens": 5})

    def test_rejects_invalid_token_types_within_usage(self):
        from jev_auto.common import AutoError
        from jev_auto.measure import extract_codex_usage

        for bad in (True, -1, 1.5):
            with self.subTest(bad=bad):
                records = [{"type": "turn.completed",
                            "usage": {"input_tokens": bad, "cached_input_tokens": 0, "output_tokens": 1}}]
                with self.assertRaisesRegex(AutoError, "INVALID_HOST_USAGE"):
                    extract_codex_usage(records)

    def test_ignores_records_that_are_not_turn_completed_usage_dicts(self):
        from jev_auto.measure import extract_codex_usage

        records = [
            {"type": "other", "usage": {"input_tokens": 999, "cached_input_tokens": 0, "output_tokens": 999}},
            {"type": "turn.completed", "usage": {"input_tokens": 5, "cached_input_tokens": 0, "output_tokens": 2}},
            {"type": "turn.completed"},  # no usage key: not a candidate record at all
            "not-a-dict",
        ]
        self.assertEqual(extract_codex_usage(records),
                         {"input_tokens": 5, "cached_input_tokens": 0, "output_tokens": 2})


if __name__ == "__main__":
    unittest.main()
