"""Coverage-floor tests for jev_auto/ipc.py and jev_auto/server.py.

Focus: the Windows-only branches (address()'s named-pipe path, request()'s
named-pipe forwarding, ensure()'s Windows process-creation flags), a couple
of narrow POSIX error-handling guards that the existing suite never
triggers, and server.py's live serve() request loop.

Windows-only branches are exercised on macOS/Linux by patching *the target
module's own* `os` (and, for one ipc.py test, `subprocess`) name binding to
a small proxy whose `.name` reads "nt" (or which adds the two Windows-only
process-creation-flag constants) while delegating everything else to the
real stdlib module. This never touches the interpreter-wide `os.name`
(pathlib would start building WindowsPath objects and explode), and the
real POSIX filesystem/sockets underneath still do the actual work, so
assertions are made on real files, real sockets, and real return values -
not merely "the line executed".

No test here touches the real ~/.local/state directory: every call that
would reach home_root()/state_dir() passes an explicit `base` argument
pointing inside a tempfile.TemporaryDirectory().
"""
from __future__ import annotations

import importlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

RUNTIME = Path(__file__).resolve().parents[1] / "plugins" / "qualixar-jev-decision-layer" / "runtime"
sys.path.insert(0, str(RUNTIME))
# macOS's per-user TMPDIR (/var/folders/.../T) is long enough on its own to
# blow the ~104-byte Unix socket path limit once a hash and leaf directory
# are appended, which is exactly the platform quirk ipc.py:20 special-cases
# for real. Tests that exercise the *Linux* (non-darwin) address() path
# need a short root to avoid tripping SOCKET_PATH_TOO_LONG for a reason
# that has nothing to do with what they are testing.
SHORT_TMP = "/private/tmp" if sys.platform == "darwin" else "/tmp"

import jev_auto.ipc as ipc  # noqa: E402
import jev_auto.server as server  # noqa: E402
from jev_auto.common import AutoError  # noqa: E402
from jev_auto.settings import make_policy, save_policy  # noqa: E402


class _FakeNtOs:
    """See test_coverage_store.py's docstring for the rationale: delegates
    everything except `.name` (== "nt") to the real `os` module, and is
    patched only onto a target module's own `os` binding."""

    name = "nt"

    def __getattr__(self, attr):
        return getattr(os, attr)


FAKE_NT_OS = _FakeNtOs()


class _FakeNtSubprocess:
    """`subprocess.CREATE_NEW_PROCESS_GROUP`/`DETACHED_PROCESS` are only
    defined by the real stdlib `subprocess` module on actual Windows,
    because CPython guards them with `if _mswindows:`. ipc.py's
    Windows-only spawn branch references both, so faking `os.name` alone
    is not enough - this proxy also supplies those two constants (with
    their real documented values) while delegating everything else
    (DEVNULL, and - via an explicit override below - Popen) to the real
    module."""

    CREATE_NEW_PROCESS_GROUP = 0x00000200
    DETACHED_PROCESS = 0x00000008

    def __getattr__(self, attr):
        return getattr(subprocess, attr)


# ==========================================================================
# jev_auto/ipc.py
# ==========================================================================

class AddressWindowsPipeTests(unittest.TestCase):
    """address()'s `if os.name == 'nt':` branch (ipc.py:12-17)."""

    def test_windows_address_builds_a_named_pipe_path_from_the_mocked_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "project"
            workspace.mkdir()
            other_workspace = root / "other-project"
            other_workspace.mkdir()
            base = root / "state"
            with patch("jev_auto.transports.windows_pipe.current_identity",
                        return_value=("S-1-5-21-user", "S-1-5-5-0-logon")):
                with patch.object(ipc, "os", FAKE_NT_OS):
                    endpoint = ipc.address(workspace, base)
                    # Deterministic: the same workspace/base/identity must
                    # yield the same pipe name on a second call...
                    endpoint_again = ipc.address(workspace, base)
                    # ...but a different workspace must not collide with it.
                    other_endpoint = ipc.address(other_workspace, base)
        self.assertIsInstance(endpoint, str)
        self.assertTrue(endpoint.startswith(r"\\.\pipe\qualixar-jev-"))
        self.assertTrue(endpoint.endswith("-v1"))
        self.assertEqual(endpoint, endpoint_again)
        self.assertNotEqual(endpoint, other_endpoint)


class LinuxRuntimeDirFallbackTests(unittest.TestCase):
    """The `except OSError: pass` at ipc.py:31-32, inside the Linux
    XDG_RUNTIME_DIR candidate-validation block."""

    def test_an_unreachable_xdg_runtime_dir_is_caught_and_ignored(self):
        with tempfile.TemporaryDirectory(dir=SHORT_TMP) as directory:
            root = Path(directory)
            workspace = root / "project"
            workspace.mkdir()
            missing_runtime = root / "does-not-exist-anywhere"
            with patch.object(ipc.sys, "platform", "linux"), \
                 patch.object(ipc.tempfile, "gettempdir", return_value=str(root)), \
                 patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(missing_runtime)}):
                endpoint = ipc.address(workspace, root / "state")
            # candidate.lstat() on a path that was never created raises
            # FileNotFoundError (an OSError); address() must swallow it and
            # fall through to the default per-uid root under our own temp
            # dir, never adopt (or crash on) the missing directory.
            self.assertFalse(str(endpoint).startswith(str(missing_runtime)))
            self.assertTrue(str(endpoint).startswith(str(root)))


class AuthenticateUnixPeerDarwinTests(unittest.TestCase):
    """The darwin branch's own failure guard (ipc.py:53-54): a `getpeereid`
    that returns nonzero (native failure) must fail closed, distinctly from
    the "function missing" and "UID mismatch" guards already covered
    elsewhere."""

    def test_a_nonzero_getpeereid_return_fails_closed(self):
        class FakeSock:
            def fileno(self):
                return 7

        fake_libc = MagicMock()
        fake_libc.getpeereid.return_value = -1  # nonzero == native failure
        with patch.object(ipc.sys, "platform", "darwin"), \
             patch.object(ipc.ctypes, "CDLL", return_value=fake_libc):
            with self.assertRaisesRegex(AutoError, "IPC_PEER_UNVERIFIED"):
                ipc.authenticate_unix_peer(FakeSock())


class IpcWorkspaceReraiseTests(unittest.TestCase):
    """`_ipc_workspace` (ipc.py:71-78): only WORKSPACE_NOT_ENROLLED is
    swallowed in favour of the raw path; any other AutoError from
    governing_workspace must propagate unchanged."""

    def test_reraises_autoerrors_other_than_not_enrolled(self):
        def raise_other(path, base=None):
            raise AutoError("SOME_OTHER_POLICY_FAILURE")

        with patch.object(ipc, "governing_workspace", side_effect=raise_other):
            with self.assertRaisesRegex(AutoError, "SOME_OTHER_POLICY_FAILURE"):
                ipc._ipc_workspace("/some/workspace/path")

    def test_falls_back_to_the_raw_workspace_only_for_not_enrolled(self):
        def raise_not_enrolled(path, base=None):
            raise AutoError("WORKSPACE_NOT_ENROLLED")

        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            with patch.object(ipc, "governing_workspace", side_effect=raise_not_enrolled):
                result = ipc._ipc_workspace(workspace)
        self.assertEqual(result, workspace.resolve())


class RequestWindowsBranchTests(unittest.TestCase):
    """request()'s `if os.name == 'nt':` branch (ipc.py:84-95): the health
    precheck, the version check, and unpack()'s success/error paths."""

    def _workspace(self, stack):
        root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        workspace = root / "project"
        workspace.mkdir()
        return workspace, root / "state"

    def test_prechecks_health_then_forwards_the_real_op_and_keeps_its_timeout(self):
        calls = []

        def fake_pipe_request(endpoint, payload, timeout=16):
            calls.append((endpoint, dict(payload), timeout))
            if payload.get("op") == "health":
                return {"ok": True, "result": {"version": "1.0.0"}}
            return {"ok": True, "result": {"echo": payload}}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "project"
            workspace.mkdir()
            base = root / "state"
            with patch("jev_auto.transports.windows_pipe.request", side_effect=fake_pipe_request), \
                 patch.object(ipc, "address", return_value=r"\\.\pipe\test-endpoint"), \
                 patch.object(ipc, "os", FAKE_NT_OS):
                result = ipc.request(workspace, {"op": "probe"}, base, timeout=9)

        self.assertEqual(result, {"echo": {"op": "probe"}})
        self.assertEqual([c[1]["op"] for c in calls], ["health", "probe"])
        self.assertEqual(calls[0][2], 1, "the health precheck caps at min(timeout, 1)")
        self.assertEqual(calls[1][2], 9, "the real call keeps the caller's own timeout")
        self.assertTrue(all(c[0] == r"\\.\pipe\test-endpoint" for c in calls),
                         "both calls must forward address()'s own endpoint, not a hardcoded one")

    def test_health_op_itself_skips_the_redundant_precheck(self):
        calls = []

        def fake_pipe_request(endpoint, payload, timeout=16):
            calls.append(dict(payload))
            return {"ok": True, "result": {"version": "1.0.0"}}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "project"
            workspace.mkdir()
            base = root / "state"
            with patch("jev_auto.transports.windows_pipe.request", side_effect=fake_pipe_request), \
                 patch.object(ipc, "address", return_value=r"\\.\pipe\test-endpoint"), \
                 patch.object(ipc, "os", FAKE_NT_OS):
                result = ipc.request(workspace, {"op": "health"}, base, timeout=5)

        self.assertEqual(result, {"version": "1.0.0"})
        self.assertEqual(len(calls), 1, "op=health must not trigger a second precheck call")

    def test_detects_a_broker_version_mismatch_on_the_precheck(self):
        def fake_pipe_request(endpoint, payload, timeout=16):
            return {"ok": True, "result": {"version": "0.0.1"}}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "project"
            workspace.mkdir()
            base = root / "state"
            with patch("jev_auto.transports.windows_pipe.request", side_effect=fake_pipe_request), \
                 patch.object(ipc, "address", return_value=r"\\.\pipe\test-endpoint"), \
                 patch.object(ipc, "os", FAKE_NT_OS):
                with self.assertRaisesRegex(AutoError, "BROKER_VERSION_MISMATCH"):
                    ipc.request(workspace, {"op": "probe"}, base)

    def test_unpack_surfaces_the_remote_error_code_for_a_failed_envelope(self):
        def fake_pipe_request(endpoint, payload, timeout=16):
            if payload.get("op") == "health":
                return {"ok": True, "result": {"version": "1.0.0"}}
            return {"ok": False, "error": "SOMETHING_BROKE_REMOTELY"}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "project"
            workspace.mkdir()
            base = root / "state"
            with patch("jev_auto.transports.windows_pipe.request", side_effect=fake_pipe_request), \
                 patch.object(ipc, "address", return_value=r"\\.\pipe\test-endpoint"), \
                 patch.object(ipc, "os", FAKE_NT_OS):
                with self.assertRaisesRegex(AutoError, "SOMETHING_BROKE_REMOTELY"):
                    ipc.request(workspace, {"op": "probe"}, base)


class EnsureWindowsSpawnFlagsTests(unittest.TestCase):
    """ensure()'s `if os.name == 'nt':` spawn-flags branch (ipc.py:134-135)."""

    def test_spawns_with_windows_process_creation_flags_not_start_new_session(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "project"
            workspace.mkdir()
            base = root / "state"
            save_policy(workspace, make_policy(workspace, "typesafe", days=3), base=base)

            call_count = {"n": 0}

            def fake_request(path, obj, base=None, timeout=16):
                call_count["n"] += 1
                if call_count["n"] <= 2:
                    raise AutoError("BROKER_UNAVAILABLE")
                return {"version": "1.0.0"}

            popen_calls = []

            def fake_popen(args, **kwargs):
                popen_calls.append(kwargs)
                return MagicMock()

            fake_subprocess = _FakeNtSubprocess()
            fake_subprocess.Popen = fake_popen

            with patch.object(ipc, "request", side_effect=fake_request), \
                 patch.object(ipc, "subprocess", fake_subprocess), \
                 patch.object(ipc, "os", FAKE_NT_OS):
                ipc.ensure(workspace, base)

        self.assertEqual(len(popen_calls), 1)
        self.assertIn("creationflags", popen_calls[0])
        self.assertEqual(popen_calls[0]["creationflags"],
                         _FakeNtSubprocess.CREATE_NEW_PROCESS_GROUP | _FakeNtSubprocess.DETACHED_PROCESS)
        self.assertNotIn("start_new_session", popen_calls[0])


# ==========================================================================
# jev_auto/server.py
# ==========================================================================

class ServeRequestLoopTests(unittest.TestCase):
    """serve()'s own accept loop (server.py:69-70): every existing test in
    this suite uses idle_seconds=0, so the loop body (`server.handle_request()`)
    itself has never actually run. `last_activity` is captured fresh inside
    Server.__init__ (the last thing that happens before the while loop), so
    the only thing racing the idle deadline is the loop itself, not Engine
    construction beforehand; idle_seconds=0.6 against Server.timeout=.25
    gives a generous 2-3 iteration margin over that so this cannot flake on
    a loaded CI box, while still keeping the test well under a second."""

    def test_the_accept_loop_calls_handle_request_at_least_once(self):
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
                with patch.object(server.Server, "handle_request",
                                   side_effect=server.Server.handle_request, autospec=True) as spy:
                    result = server.serve(workspace, base, idle_seconds=0.6)

        self.assertIsNone(result)
        self.assertFalse(fake_addr.exists(), "the socket must still be unlinked on the way out")
        self.assertGreaterEqual(spy.call_count, 1, "the accept loop body must have run at least once")


class WindowsServeStaysClosedTests(unittest.TestCase):
    """The Windows broker is deliberately closed since 1.0.8.

    server.py refuses os.name == 'nt' before it touches state, the broker lock
    or the named-pipe transport ("scope 1.0.8 support to verified macOS
    runtime"). The dispatch to transports.windows_pipe further down is dormant
    until that contract is revalidated, which is why those lines stay
    uncovered. This test pins the closure: it fails if the refusal is removed
    without the Windows path being deliberately reopened.
    """

    def test_serve_refuses_windows_before_touching_state_or_the_pipe_transport(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "project"
            workspace.mkdir()
            base = root / "state-base"
            with patch("jev_auto.transports.windows_pipe.serve",
                       side_effect=AssertionError("the pipe transport must stay closed")) as pipe, \
                 patch.object(server, "private_dir",
                              side_effect=AssertionError("no state may be created")) as state, \
                 patch.object(server, "os", FAKE_NT_OS), \
                 self.assertRaisesRegex(AutoError, "^WINDOWS_UNSUPPORTED_IN_1_0_8$"):
                server.serve(workspace, base, idle_seconds=1)
            pipe.assert_not_called()
            state.assert_not_called()
            self.assertFalse(base.exists())


class WindowsPlaceholderUnixStreamServerTests(unittest.TestCase):
    """server.py:26-30: on a real Windows host, `socketserver` has no
    `UnixStreamServer`, so server.py defines a tiny import-only placeholder
    class instead so the module can still be imported. Exercised here by
    temporarily hiding `socketserver.UnixStreamServer` and reloading
    server.py; both the attribute and the module are restored in `finally`
    regardless of outcome, and this is the only test in the suite that
    touches import machinery, so the blast radius is contained to this one
    test method."""

    def test_the_placeholder_class_is_defined_when_the_stdlib_lacks_the_real_one(self):
        import socketserver as socketserver_module

        self.assertTrue(hasattr(socketserver_module, "UnixStreamServer"),
                         "this test's premise requires the real attribute to exist first")
        original = socketserver_module.UnixStreamServer
        delattr(socketserver_module, "UnixStreamServer")
        try:
            reloaded = importlib.reload(server)
            placeholder = reloaded._UnixStreamServer
            self.assertIsNot(placeholder, original)
            self.assertIn("placeholder", (placeholder.__doc__ or "").lower())
        finally:
            socketserver_module.UnixStreamServer = original
            importlib.reload(server)
            self.assertIs(server._UnixStreamServer, original,
                          "the module must be left exactly as every other test expects it")


if __name__ == "__main__":
    unittest.main()
