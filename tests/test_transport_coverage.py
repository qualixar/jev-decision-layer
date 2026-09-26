"""Transport-boundary coverage: jev_auto/ipc.py's Unix socket client,
jev_auto/mcp.py's bounded MCP facade, and jevkit/mcp_server.py's legacy MCP
server. All three talk to something outside the process (a broker over a
Unix socket, or an MCP client over stdio) and were far below the 90%
line-coverage bar for exactly that kind of surface.

No test here spawns the real broker (jev_auto/server.py's Engine): each
either controls its own socket/thread directly, or patches at a named
module boundary (ensure/request/subprocess.Popen/time.sleep). Zero network,
zero writes outside TemporaryDirectory, zero touches of ~/.local/state.
"""
from __future__ import annotations

import fcntl
import io
import json
import os
import selectors
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "plugins" / "qualixar-jev-decision-layer" / "runtime"
sys.path.insert(0, str(RUNTIME))


# --------------------------------------------------------------------------
# Shared test-only helpers (not product code; kept minimal and local).
# --------------------------------------------------------------------------

class _ScriptedSocket:
    """A recv()-only stand-in with no real networking, so wire-framing bugs
    in read_message() are caught without a live broker on the other end.
    Mirrors real socket.recv() semantics: never returns more than requested,
    and returns b'' once the scripted payload is exhausted (EOF)."""

    def __init__(self, payload: bytes, max_chunk: int | None = None):
        self._buffer = payload
        self._max_chunk = max_chunk

    def recv(self, bufsize):
        limit = bufsize if self._max_chunk is None else min(bufsize, self._max_chunk)
        chunk, self._buffer = self._buffer[:limit], self._buffer[limit:]
        return chunk


def _serve_one_reply(sock, reply_obj):
    """Run in a background thread: accept exactly one connection on `sock`,
    decode the request with the REAL wire framing, and send back `reply_obj`.
    Stands in for jev_auto/server.py's Handler without spawning it or the
    Engine it wraps."""
    from jev_auto.common import canonical
    from jev_auto.ipc import read_message

    conn, _addr = sock.accept()
    try:
        read_message(conn)
        conn.sendall(canonical(reply_obj) + b"\n")
    finally:
        conn.close()


def _fake_legacy(extra_tools=()):
    """A minimal stand-in for jevkit.mcp_server's duck-typed (tools, call)
    interface, shaped only as far as jev_auto/mcp.py's definitions()/
    dispatch() actually touch it. This keeps the dispatch()-focused tests
    below about dispatch()'s own branching, not jevkit.mcp_server's
    separately-tested RuntimeContext/workspace-binding plumbing -- that
    cross-module contract is pinned by test_hermes_parity.py::HostParity
    and test_core_contracts.py instead.
    """
    evaluate_schema = {
        "type": "object",
        "properties": {
            "case_id": {"type": "string"},
            "workspace_path": {"type": "string", "minLength": 1, "maxLength": 4096},
            "state": {"type": "object"},
            "request_id": {"type": "string"},
            "data_classification": {"type": "string", "enum": ["public", "internal-minimized"]},
        },
        "required": ["case_id"],
        "additionalProperties": False,
        "allOf": [{"if": {"required": ["state"]},
                   "then": {"required": ["request_id", "data_classification"]}}],
    }
    evaluate_tool = {"name": "jev_evaluate", "description": "legacy evaluate",
                     "inputSchema": evaluate_schema}
    calls = []

    def _call(name, args, scope):
        calls.append({"name": name, "args": args, "scope": scope})
        return {"legacy": True}

    legacy = SimpleNamespace(tools=lambda scope: [evaluate_tool, *extra_tools], call=_call)
    return legacy, calls


# --------------------------------------------------------------------------
# jev_auto/ipc.py
# --------------------------------------------------------------------------

class AddressComputationTests(unittest.TestCase):
    """Computing the broker socket path must be deterministic, collision-free
    across workspaces, and must refuse to build a path Unix cannot bind to
    rather than silently truncating it -- a silent truncation could make two
    different workspaces share one socket and answer each other's requests.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _workspace(self, name):
        path = Path(self._tmp.name) / name
        path.mkdir()
        return path

    def test_same_workspace_and_state_base_always_produce_the_same_socket_path(self):
        from jev_auto import ipc

        workspace = self._workspace("repeatable")
        base = Path(self._tmp.name) / "state"
        first = ipc.address(str(workspace), base)
        second = ipc.address(str(workspace), base)
        self.assertEqual(first, second)
        self.assertTrue(first.name.endswith(".sock"))

    def test_different_workspaces_never_share_a_socket_path(self):
        from jev_auto import ipc

        base = Path(self._tmp.name) / "state"
        one = ipc.address(str(self._workspace("a")), base)
        two = ipc.address(str(self._workspace("b")), base)
        self.assertNotEqual(one, two)

    def test_address_refuses_to_build_a_path_over_the_unix_socket_limit(self):
        from jev_auto import ipc
        from jev_auto.common import AutoError

        long_root = Path(self._tmp.name) / ("x" * 200)
        with patch.object(ipc, "private_dir", return_value=long_root):
            with self.assertRaises(AutoError) as cm:
                ipc.address(str(self._workspace("c")))
        self.assertEqual(str(cm.exception), "SOCKET_PATH_TOO_LONG")


class ReadMessageFramingTests(unittest.TestCase):
    """The broker and every client share one newline-delimited, size-bounded
    frame. A bug here either hangs a connection forever waiting for a
    newline that will never arrive, or lets one message hold unbounded
    memory before anyone notices.
    """

    def test_a_single_recv_delivering_the_whole_line_decodes_cleanly(self):
        from jev_auto.ipc import read_message

        sock = _ScriptedSocket(b'{"ok":true}\n')
        self.assertEqual(read_message(sock), {"ok": True})

    def test_a_message_split_across_many_small_recv_calls_still_assembles(self):
        from jev_auto.ipc import read_message

        sock = _ScriptedSocket(b'{"ok":true}\n', max_chunk=4)
        self.assertEqual(read_message(sock), {"ok": True})

    def test_trailing_whitespace_only_after_the_newline_is_tolerated(self):
        from jev_auto.ipc import read_message

        sock = _ScriptedSocket(b'{"ok":true}\n   ')
        self.assertEqual(read_message(sock), {"ok": True})

    def test_connection_closed_before_any_newline_is_eof_not_a_hang(self):
        from jev_auto.common import AutoError
        from jev_auto.ipc import read_message

        sock = _ScriptedSocket(b"")
        with self.assertRaises(AutoError) as cm:
            read_message(sock)
        self.assertEqual(str(cm.exception), "IPC_EOF")

    def test_oversized_payload_with_no_newline_is_rejected_not_buffered_forever(self):
        """MUTATION 1 target: the raise on the size cap in read_message()."""
        from jev_auto.common import AutoError
        from jev_auto.ipc import read_message

        sock = _ScriptedSocket(b"x" * 600_000)
        with self.assertRaises(AutoError) as cm:
            read_message(sock)
        self.assertEqual(str(cm.exception), "IPC_MESSAGE_SIZE")

    def test_trailing_non_whitespace_after_the_first_newline_is_rejected(self):
        from jev_auto.common import AutoError
        from jev_auto.ipc import read_message

        sock = _ScriptedSocket(b'{"a":1}\nEXTRA')
        with self.assertRaises(AutoError) as cm:
            read_message(sock)
        self.assertEqual(str(cm.exception), "IPC_ONE_REQUEST_PER_CONNECTION")


class RequestSafetyTests(unittest.TestCase):
    """request() must refuse to talk to anything that is not a same-user,
    owner-only-mode Unix socket: on a shared machine this same-UID socket
    check is the ENTIRE trust boundary between "advisory local tool" and
    "arbitrary local peer can answer for the broker." Every case here builds
    a real (but inert or fully test-controlled) socket file -- never the
    real jev_auto/server.py broker.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workspace = Path(self._tmp.name) / "workspace"
        self.workspace.mkdir()
        self.base = Path(self._tmp.name) / "state"

    def _address(self):
        from jev_auto import ipc
        return ipc.address(str(self.workspace), self.base)

    def test_no_socket_file_at_all_is_broker_unavailable(self):
        from jev_auto import ipc
        from jev_auto.common import AutoError

        with self.assertRaises(AutoError) as cm:
            ipc.request(str(self.workspace), {"op": "health"}, self.base, timeout=1)
        self.assertEqual(str(cm.exception), "BROKER_UNAVAILABLE")

    def test_a_bound_but_unlistened_socket_is_connection_refused_broker_unavailable(self):
        from jev_auto import ipc
        from jev_auto.common import AutoError

        addr = self._address()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(str(addr))
        os.chmod(addr, 0o600)
        sock.close()  # bound, never listen()ed: the inode exists, nobody accepts
        self.addCleanup(lambda: addr.unlink(missing_ok=True))

        with self.assertRaises(AutoError) as cm:
            ipc.request(str(self.workspace), {"op": "health"}, self.base, timeout=1)
        self.assertEqual(str(cm.exception), "BROKER_UNAVAILABLE")

    def test_a_regular_file_where_the_socket_should_be_is_rejected_as_unsafe(self):
        """MUTATION 2 target: the `not stat.S_ISSOCK(...)` half of the guard
        in request(). Mode is pinned to 0600 so the permission-bits half of
        the same guard cannot also explain the raise."""
        from jev_auto import ipc
        from jev_auto.common import AutoError

        addr = self._address()
        addr.write_text("not a socket")
        os.chmod(addr, 0o600)
        self.addCleanup(lambda: addr.unlink(missing_ok=True))

        with self.assertRaises(AutoError) as cm:
            ipc.request(str(self.workspace), {"op": "health"}, self.base, timeout=1)
        self.assertEqual(str(cm.exception), "UNSAFE_SOCKET")

    def test_a_group_or_world_readable_socket_is_rejected_as_unsafe(self):
        from jev_auto import ipc
        from jev_auto.common import AutoError

        addr = self._address()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(str(addr))
        os.chmod(addr, 0o644)  # world-readable: exactly what the mode mask rejects
        self.addCleanup(sock.close)
        self.addCleanup(lambda: addr.unlink(missing_ok=True))

        with self.assertRaises(AutoError) as cm:
            ipc.request(str(self.workspace), {"op": "health"}, self.base, timeout=1)
        self.assertEqual(str(cm.exception), "UNSAFE_SOCKET")

    def test_an_oversized_request_is_rejected_before_ever_connecting(self):
        from jev_auto import ipc
        from jev_auto.common import AutoError

        addr = self._address()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(str(addr))
        os.chmod(addr, 0o600)
        sock.listen(1)
        self.addCleanup(sock.close)
        self.addCleanup(lambda: addr.unlink(missing_ok=True))

        with self.assertRaises(AutoError) as cm:
            ipc.request(str(self.workspace), {"op": "health", "text": "x" * 600_000},
                       self.base, timeout=1)
        self.assertEqual(str(cm.exception), "IPC_MESSAGE_SIZE")

    def test_full_round_trip_through_a_controlled_fake_broker(self):
        from jev_auto import ipc

        addr = self._address()
        server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server_sock.bind(str(addr))
        os.chmod(addr, 0o600)
        server_sock.listen(1)
        self.addCleanup(server_sock.close)
        self.addCleanup(lambda: addr.unlink(missing_ok=True))
        thread = threading.Thread(target=_serve_one_reply,
                                  args=(server_sock, {"ok": True, "result": {"pong": True}}),
                                  daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)

        result = ipc.request(str(self.workspace), {"op": "health"}, self.base, timeout=5)
        self.assertEqual(result, {"pong": True})

    def test_broker_reported_application_error_is_surfaced_as_autoerror(self):
        from jev_auto import ipc
        from jev_auto.common import AutoError

        addr = self._address()
        server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server_sock.bind(str(addr))
        os.chmod(addr, 0o600)
        server_sock.listen(1)
        self.addCleanup(server_sock.close)
        self.addCleanup(lambda: addr.unlink(missing_ok=True))
        thread = threading.Thread(target=_serve_one_reply,
                                  args=(server_sock, {"ok": False, "error": "SYNTHETIC_BROKER_ERROR"}),
                                  daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)

        with self.assertRaises(AutoError) as cm:
            ipc.request(str(self.workspace), {"op": "health"}, self.base, timeout=5)
        self.assertEqual(str(cm.exception), "SYNTHETIC_BROKER_ERROR")

    def test_a_broker_that_accepts_but_never_answers_times_out_as_broker_unavailable(self):
        from jev_auto import ipc
        from jev_auto.common import AutoError

        addr = self._address()
        server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server_sock.bind(str(addr))
        os.chmod(addr, 0o600)
        server_sock.listen(1)
        self.addCleanup(server_sock.close)
        self.addCleanup(lambda: addr.unlink(missing_ok=True))
        accepted = threading.Event()

        def _stall():
            conn, _addr = server_sock.accept()
            accepted.set()
            time.sleep(1.0)
            conn.close()

        thread = threading.Thread(target=_stall, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)

        with self.assertRaises(AutoError) as cm:
            ipc.request(str(self.workspace), {"op": "health"}, self.base, timeout=0.1)
        self.assertEqual(str(cm.exception), "BROKER_UNAVAILABLE")
        self.assertTrue(accepted.wait(timeout=5))


class EnsureBrokerLifecycleTests(unittest.TestCase):
    """ensure() is the state machine between "the plugin looks alive" and
    "every advisory tool call blocks or spawns a duplicate broker." None of
    these tests spawn the real jev_auto/server.py process: subprocess.Popen
    is always mocked, so what's under test is ensure()'s own orchestration
    (health-check, lock, retry, give up) -- not the broker it starts.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workspace = Path(self._tmp.name) / "workspace"
        self.workspace.mkdir()
        self.base = Path(self._tmp.name) / "state"

    def test_an_already_healthy_broker_on_the_matching_version_is_a_noop(self):
        from jev_auto import ipc

        with patch.object(ipc, "load_policy", return_value=None) as mock_load, \
             patch.object(ipc, "request", return_value={"version": "1.0.0"}) as mock_request, \
             patch.object(ipc.subprocess, "Popen") as mock_popen:
            self.assertIsNone(ipc.ensure(str(self.workspace), self.base))
        mock_load.assert_called_once_with(str(self.workspace), self.base)
        mock_popen.assert_not_called()
        self.assertEqual(mock_request.call_count, 1)

    def test_a_version_mismatch_is_a_hard_stop_that_never_spawns_a_broker(self):
        from jev_auto import ipc
        from jev_auto.common import AutoError

        with patch.object(ipc, "load_policy", return_value=None), \
             patch.object(ipc, "request", return_value={"version": "0.9.9"}) as mock_request, \
             patch.object(ipc.subprocess, "Popen") as mock_popen:
            with self.assertRaises(AutoError) as cm:
                ipc.ensure(str(self.workspace), self.base)
        self.assertEqual(str(cm.exception), "BROKER_VERSION_MISMATCH")
        mock_popen.assert_not_called()
        self.assertEqual(mock_request.call_count, 1)

    def test_a_symlinked_start_lock_is_rejected_before_it_is_ever_opened(self):
        from jev_auto import ipc
        from jev_auto.common import AutoError

        with patch.object(ipc, "load_policy", return_value=None), \
             patch.object(ipc, "request", side_effect=AutoError("BROKER_UNAVAILABLE")):
            root = ipc.private_dir(ipc.state_dir(str(self.workspace), self.base))
            target = root / "elsewhere"
            target.write_text("x")
            (root / "start.lock").symlink_to(target)
            with self.assertRaises(AutoError) as cm:
                ipc.ensure(str(self.workspace), self.base)
        self.assertEqual(str(cm.exception), "UNSAFE_START_LOCK")

    def test_spawns_the_broker_once_and_returns_once_it_becomes_healthy(self):
        from jev_auto import ipc
        from jev_auto.common import AutoError

        responses = [AutoError("BROKER_UNAVAILABLE"), AutoError("BROKER_UNAVAILABLE"),
                    AutoError("BROKER_UNAVAILABLE"), {"version": "1.0.0"}]

        def _request(*_args, **_kwargs):
            item = responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        with patch.object(ipc, "load_policy", return_value=None), \
             patch.object(ipc, "request", side_effect=_request) as mock_request, \
             patch.object(ipc.subprocess, "Popen") as mock_popen, \
             patch.object(ipc.time, "sleep", return_value=None):
            self.assertIsNone(ipc.ensure(str(self.workspace), self.base))

        # subprocess.Popen is also reached transitively: jev_auto.common.workspace()
        # shells out to `git rev-parse` (via subprocess.run, itself Popen-based) both
        # for state_dir()'s workspace_id() and again for the --workspace argv value
        # below. Patching the single shared subprocess.Popen catches those too, so
        # assert on the specific broker-spawning call rather than "called once".
        broker_calls = [c for c in mock_popen.call_args_list if "jev_auto.server" in c.args[0]]
        self.assertEqual(len(broker_calls), 1)
        call = broker_calls[0]
        argv = call.args[0]
        self.assertEqual(argv[0], sys.executable)
        self.assertEqual(argv[1:3], ["-m", "jev_auto.server"])
        self.assertIn("--workspace", argv)
        self.assertIn("--state-base", argv)
        self.assertIn(str(self.base), argv)
        self.assertEqual(call.kwargs["stdin"], subprocess.DEVNULL)
        self.assertTrue(call.kwargs["start_new_session"])
        self.assertEqual(mock_request.call_count, 4)

    def test_gives_up_after_exhausting_poll_attempts_but_still_releases_the_lock(self):
        from jev_auto import ipc
        from jev_auto.common import AutoError

        with patch.object(ipc, "load_policy", return_value=None), \
             patch.object(ipc, "request", side_effect=AutoError("BROKER_UNAVAILABLE")) as mock_request, \
             patch.object(ipc.subprocess, "Popen") as mock_popen, \
             patch.object(ipc.time, "sleep", return_value=None):
            with self.assertRaises(AutoError) as cm:
                ipc.ensure(str(self.workspace), self.base)
        self.assertEqual(str(cm.exception), "BROKER_START_FAILED")
        broker_calls = [c for c in mock_popen.call_args_list if "jev_auto.server" in c.args[0]]
        self.assertEqual(len(broker_calls), 1)
        self.assertEqual(mock_request.call_count, 32)  # 1 initial + 1 post-lock + 30 polls

        # The lock must be released even on the give-up path: a fresh,
        # non-blocking exclusive flock on the very same file must succeed
        # immediately, or a real second caller would hang behind a leaked lock.
        lock_path = ipc.private_dir(ipc.state_dir(str(self.workspace), self.base)) / "start.lock"
        fd = os.open(lock_path, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


# --------------------------------------------------------------------------
# jev_auto/mcp.py
# --------------------------------------------------------------------------

class McpAutoDispatchEnsureBranchTests(unittest.TestCase):
    """dispatch() special-cases exactly one broker failure -- not enrolled
    yet -- and only for jev_evaluate. Every other broker failure, and every
    other tool name, must propagate so a real outage is never mistaken for
    a workspace that simply has not run setup.
    """

    def test_not_enrolled_evaluate_falls_back_to_the_legacy_tool(self):
        from jev_auto import mcp
        from jev_auto.common import AutoError

        legacy, calls = _fake_legacy()
        with patch.object(mcp, "ensure", side_effect=AutoError("WORKSPACE_NOT_ENROLLED")) as mock_ensure:
            result = mcp.dispatch("jev_evaluate",
                                  {"workspace_path": "/synthetic", "case_id": "x", "state": {}},
                                  legacy)
        self.assertEqual(result, {"legacy": True})
        self.assertEqual(calls, [{"name": "jev_evaluate",
                                  "args": {"workspace_path": "/synthetic", "case_id": "x", "state": {}},
                                  "scope": "global-hybrid"}])
        mock_ensure.assert_called_once_with("/synthetic")

    def test_not_enrolled_is_only_special_cased_for_jev_evaluate(self):
        """MUTATION 3 target: drop `name=='jev_evaluate' and ` from the
        guard and an unenrolled workspace silently answers every bounded
        tool with a legacy call instead of surfacing the real state."""
        from jev_auto import mcp
        from jev_auto.common import AutoError

        legacy, calls = _fake_legacy()
        with patch.object(mcp, "ensure", side_effect=AutoError("WORKSPACE_NOT_ENROLLED")):
            with self.assertRaises(AutoError) as cm:
                mcp.dispatch("jev_route",
                            {"workspace_path": "/synthetic", "kind": "task", "task": "t",
                             "candidates": [{"id": "a", "description": "A"},
                                            {"id": "b", "description": "B"}],
                             "data_classification": "public"},
                            legacy)
        self.assertEqual(str(cm.exception), "WORKSPACE_NOT_ENROLLED")
        self.assertEqual(calls, [])

    def test_a_broker_failure_that_is_not_workspace_not_enrolled_is_never_swallowed(self):
        from jev_auto import mcp
        from jev_auto.common import AutoError

        legacy, calls = _fake_legacy()
        with patch.object(mcp, "ensure", side_effect=AutoError("BROKER_START_FAILED")):
            with self.assertRaises(AutoError) as cm:
                mcp.dispatch("jev_evaluate",
                            {"workspace_path": "/synthetic", "case_id": "x", "state": {}},
                            legacy)
        self.assertEqual(str(cm.exception), "BROKER_START_FAILED")
        self.assertEqual(calls, [])


class McpAutoDispatchOpCoverageTests(unittest.TestCase):
    """Each bounded op is a hand-written request dict literal in dispatch();
    a typo in a key name would silently ask the broker the wrong question
    while still returning *a* response. Supplying an explicit caller=
    bypasses ensure()/request() entirely, so these tests check dispatch()'s
    own wiring in isolation (the broker-lifecycle side belongs to
    EnsureBrokerLifecycleTests and RequestSafetyTests).
    """

    def test_each_bounded_op_builds_the_request_its_schema_promises(self):
        from jev_auto import mcp

        cases = (
            ("jev_recipe_try",
             {"workspace_path": "/w", "recipe_id": "qualixar.brief-fit", "input": {"a": 1},
              "data_classification": "public"},
             {"op": "recipe_try", "recipe_id": "qualixar.brief-fit", "input": {"a": 1},
              "data_classification": "public"}),
            ("jev_verify",
             {"workspace_path": "/w", "source_text": "hello", "extraction": {"a": 1},
              "data_classification": "public"},
             {"op": "verify", "source_text": "hello", "extraction": {"a": 1},
              "threshold": 0.70, "data_classification": "public"}),
            ("jev_rerank",
             {"workspace_path": "/w", "query": "q", "memories": [{"id": 1}],
              "data_classification": "public"},
             {"op": "rerank", "query": "q", "memories": [{"id": 1}], "data_classification": "public"}),
            ("jev_review_diff",
             {"workspace_path": "/w", "goal": "review this", "diff": "--- a\n+++ b\n",
              "data_classification": "public"},
             {"op": "review_diff", "goal": "review this", "diff": "--- a\n+++ b\n",
              "data_classification": "public"}),
            ("jev_evaluate",
             {"workspace_path": "/w", "case_id": "x", "state": {}},
             {"op": "evaluate", "case_id": "x", "state": {}}),
        )
        for name, args, expected_request in cases:
            with self.subTest(name=name):
                legacy, _calls = _fake_legacy()  # fresh per case: no shared mutable state across subTests
                seen = []
                result = mcp.dispatch(name, args, legacy,
                                      caller=lambda request: seen.append(request) or {"status": "STUB"})
                self.assertEqual(result, {"status": "STUB"})
                self.assertEqual(seen, [expected_request])

    def test_recipe_catalog_and_selftest_are_dispatched_fully_offline(self):
        """Both tools are documented as reachable with no workspace, no
        enrollment and no provider call -- confirm dispatch() itself never
        calls ensure()/request() to serve them."""
        from jev_auto import mcp

        legacy, _calls = _fake_legacy()
        with patch.object(mcp, "ensure", side_effect=AssertionError("ensure() must not run for offline tools")), \
             patch.object(mcp, "request", side_effect=AssertionError("request() must not run for offline tools")):
            catalog = mcp.dispatch("jev_recipe_catalog", {}, legacy)
            self.assertEqual(len(catalog["recipes"]), 36)

            report = mcp.dispatch("jev_recipe_selftest", {}, legacy)
            self.assertEqual(report["mode"], "fixture")
            self.assertIn("cases", report)

            outcome = mcp.dispatch("jev_recipe_selftest",
                                   {"recipe_id": "qualixar.brief-fit", "variant": "nominal"}, legacy)
            self.assertEqual(outcome["recipe_id"], "qualixar.brief-fit")

            with self.assertRaises(mcp.AutoError) as cm:
                mcp.dispatch("jev_recipe_selftest", {"variant": "nominal"}, legacy)
        self.assertEqual(str(cm.exception), "MCP_ARGUMENTS")

    def test_a_legacy_only_tool_name_falls_through_to_the_legacy_call(self):
        from jev_auto import mcp

        legacy, calls = _fake_legacy(extra_tools=[{
            "name": "jev_health", "description": "legacy health",
            "inputSchema": {"type": "object", "properties": {}, "required": [],
                            "additionalProperties": False},
        }])
        result = mcp.dispatch("jev_health", {}, legacy)
        self.assertEqual(result, {"legacy": True})
        self.assertEqual(calls, [{"name": "jev_health", "args": {}, "scope": "global-hybrid"}])


class _FakeSetupProcess:
    """Stands in for the subprocess.Popen handle _open_setup() reads its
    single status line from -- a real pipe, no real child process."""

    def __init__(self, line: bytes = b""):
        read_fd, write_fd = os.pipe()
        if line:
            os.write(write_fd, line)
        os.close(write_fd)
        self.stdout = os.fdopen(read_fd, "rb")
        self.pid = 999999

    def poll(self):
        return None

    def wait(self, timeout=None):
        return 0


class McpSetupWizardTests(unittest.TestCase):
    """jev_setup's whole security model is "launch only the packaged
    loopback wizard, never a caller-supplied command, and never hand back
    a URL that isn't 127.0.0.1" -- _extract_setup_url and _open_setup are
    where that is actually enforced. Reproducible from this file alone
    (test_core_contracts.py also covers this pair, but a per-module
    coverage floor gated on this file must not depend on that).
    """

    GOOD_LINE = b"Qualixar setup: http://127.0.0.1:54321/setup. Enter keys only in the local browser, never in chat.\n"
    BAD_LINE = b"Qualixar setup: http://example.com/setup. Enter keys only in the local browser.\n"

    def test_extract_setup_url_accepts_only_the_packaged_loopback_line(self):
        from jev_auto.mcp import _extract_setup_url

        self.assertEqual(_extract_setup_url(self.GOOD_LINE), "http://127.0.0.1:54321/setup")
        with self.assertRaises(ValueError):
            _extract_setup_url(self.BAD_LINE)

    def test_open_setup_reports_a_fixed_error_when_the_launcher_cannot_start(self):
        from jev_auto.common import AutoError
        from jev_auto.mcp import _open_setup

        with tempfile.TemporaryDirectory() as directory:
            with patch("jev_auto.mcp.subprocess.Popen", side_effect=OSError("synthetic")):
                with self.assertRaisesRegex(AutoError, "SETUP_START_FAILED"):
                    _open_setup(directory)

    def test_open_setup_reports_launcher_missing_when_the_packaged_script_is_absent(self):
        from jev_auto.common import AutoError
        from jev_auto.mcp import _open_setup

        with tempfile.TemporaryDirectory() as directory:
            with patch.object(Path, "is_file", return_value=False):
                with self.assertRaisesRegex(AutoError, "SETUP_LAUNCHER_MISSING"):
                    _open_setup(directory)

    def test_open_setup_times_out_if_the_launcher_never_becomes_readable(self):
        """The internal SETUP_START_TIMEOUT is caught by the same broad
        except as every other failure mode here and re-raised as the same
        fixed SETUP_START_FAILED -- callers never learn *why* the wizard
        did not start, only that it did not."""
        from jev_auto.common import AutoError
        from jev_auto.mcp import _open_setup

        process = _FakeSetupProcess()  # nothing ever written: stdout has no data
        with tempfile.TemporaryDirectory() as directory:
            with patch("jev_auto.mcp.workspace", side_effect=lambda value: Path(value)), \
                 patch("jev_auto.mcp.subprocess.Popen", return_value=process), \
                 patch.object(selectors.DefaultSelector, "select", return_value=[]), \
                 patch("jev_auto.mcp.os.killpg") as killed:
                with self.assertRaisesRegex(AutoError, "SETUP_START_FAILED"):
                    _open_setup(directory)
        killed.assert_called_once_with(process.pid, signal.SIGKILL)
        process.stdout.close()

    def test_open_setup_rejects_a_suspiciously_long_response_line(self):
        from jev_auto.common import AutoError
        from jev_auto.mcp import _open_setup

        process = _FakeSetupProcess(b"x" * 600)  # no newline within the first 512 bytes
        with tempfile.TemporaryDirectory() as directory:
            with patch("jev_auto.mcp.workspace", side_effect=lambda value: Path(value)), \
                 patch("jev_auto.mcp.subprocess.Popen", return_value=process), \
                 patch("jev_auto.mcp.os.killpg") as killed:
                with self.assertRaisesRegex(AutoError, "SETUP_START_FAILED"):
                    _open_setup(directory)
        killed.assert_called_once_with(process.pid, signal.SIGKILL)

    def test_open_setup_returns_the_loopback_url_on_a_well_formed_response(self):
        from jev_auto.mcp import _open_setup

        process = _FakeSetupProcess(self.GOOD_LINE)
        with tempfile.TemporaryDirectory() as directory:
            with patch("jev_auto.mcp.workspace", side_effect=lambda value: Path(value)), \
                 patch("jev_auto.mcp.subprocess.Popen", return_value=process):
                result = _open_setup(directory)
        self.assertEqual(result, {"status": "SETUP_WIZARD_OPEN", "url": "http://127.0.0.1:54321/setup",
                                  "expires_in_seconds": 600, "credential_entry": "PRIVATE_BROWSER_ONLY"})

    def test_open_setup_kills_the_process_and_fails_when_the_response_url_is_not_loopback(self):
        from jev_auto.common import AutoError
        from jev_auto.mcp import _open_setup

        process = _FakeSetupProcess(self.BAD_LINE)
        with tempfile.TemporaryDirectory() as directory:
            with patch("jev_auto.mcp.workspace", side_effect=lambda value: Path(value)), \
                 patch("jev_auto.mcp.subprocess.Popen", return_value=process), \
                 patch("jev_auto.mcp.os.killpg") as killed:
                with self.assertRaisesRegex(AutoError, "SETUP_START_FAILED"):
                    _open_setup(directory)
        killed.assert_called_once_with(process.pid, signal.SIGKILL)


class AutoMcpServeLoopTests(unittest.TestCase):
    """jev_auto/mcp.py's serve() is the process boundary for the bounded
    tool surface: same defensive contract as the legacy loop (isolate one
    bad line, never leak an internal exception, never die), but framed over
    sys.stdin.buffer.readline() instead of a text-line iterator, so it earns
    its own pass even though the shape rhymes with jevkit's server.
    """

    @staticmethod
    def _lines(*objs):
        return ("\n".join(json.dumps(o) for o in objs) + "\n").encode()

    def _run(self, payload: bytes):
        from jev_auto import mcp

        buffer = io.StringIO()
        with patch("sys.stdin", SimpleNamespace(buffer=io.BytesIO(payload))), redirect_stdout(buffer):
            mcp.serve()
        return [json.loads(line) for line in buffer.getvalue().splitlines()]

    def test_an_oversized_line_without_a_trailing_newline_is_rejected(self):
        replies = self._run(b"x" * 512_001)
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0]["error"]["message"], "MCP_MESSAGE_SIZE")

    def test_invalid_json_and_wrong_shaped_requests_are_isolated_per_line(self):
        payload = b"{not json\n" + json.dumps({"jsonrpc": "1.0", "id": 1}).encode() + b"\n"
        replies = self._run(payload)
        self.assertEqual([r["error"]["message"] for r in replies], ["INVALID_JSON", "MCP_REQUEST"])

    def test_notification_without_id_is_silently_skipped_not_a_hang(self):
        payload = self._lines({"jsonrpc": "2.0", "method": "notifications/initialized"},
                              {"jsonrpc": "2.0", "id": 1, "method": "ping"})
        replies = self._run(payload)
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0]["id"], 1)

    def test_non_dict_params_is_rejected(self):
        payload = self._lines({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": "nope"})
        replies = self._run(payload)
        self.assertEqual(replies[0]["error"]["message"], "MCP_PARAMS")

    def test_full_session_initialize_ping_list_call_and_unknown_method(self):
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "id": 2, "method": "ping"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
             "params": {"name": "jev_recipe_selftest", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
             "params": {"name": "jev_does_not_exist", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 6, "method": "surprise/method"},
        ]
        replies = {r["id"]: r for r in self._run(self._lines(*requests))}
        self.assertIn("result", replies[1])
        self.assertEqual(replies[2]["result"], {})
        self.assertIn("tools", replies[3]["result"])
        self.assertFalse(replies[4]["result"]["isError"])
        self.assertTrue(replies[5]["result"]["isError"])
        self.assertEqual(replies[5]["result"]["content"][0]["text"], "MCP_TOOL_NAME")
        self.assertEqual(replies[6]["error"]["code"], -32601)

    def test_a_non_autoerror_exception_inside_dispatch_never_leaks_past_a_fixed_code(self):
        from jev_auto import mcp

        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "jev_recipe_selftest", "arguments": {}}},
        ]
        with patch.object(mcp, "dispatch", side_effect=RuntimeError("/Users/example/secret")):
            replies = {r["id"]: r for r in self._run(self._lines(*requests))}
        self.assertTrue(replies[2]["result"]["isError"])
        self.assertEqual(replies[2]["result"]["content"][0]["text"], "JEV_TOOL_UNAVAILABLE")

    def test_an_exception_outside_tools_call_is_caught_by_the_outer_handler_too(self):
        """tools/call has its own inner catch-all (previous test); ping,
        initialize and tools/list do not, and must still fall back to the
        loop's OWN generic handler instead of crashing the process."""
        from jev_auto import mcp

        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "ping"},
        ]
        with patch.object(mcp, "definitions", side_effect=RuntimeError("boom")):
            replies = {r["id"]: r for r in self._run(self._lines(*requests))}
        self.assertEqual(replies[2]["error"], {"code": -32603, "message": "Internal error"})
        # the loop must recover and keep serving the next request
        self.assertEqual(replies[3]["result"], {})


# --------------------------------------------------------------------------
# jevkit/mcp_server.py
# --------------------------------------------------------------------------

class LegacyMcpValidateArgumentsTests(unittest.TestCase):
    """Every raise in _validate_arguments guards a distinct malformed-input
    shape between an MCP client and a workspace-authority decision. The only
    reason to exercise each one directly is to catch a validator that
    quietly stopped validating -- an accidental `pass`, an inverted
    comparison, a branch nobody can reach anymore.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_root = Path(self._tmp.name) / "state"

    def _ctx(self, scope):
        from jevkit import mcp_server
        return mcp_server.context(root=RUNTIME, scope=scope, state_root=self.state_root)

    def test_missing_required_key_is_rejected(self):
        from jevkit import mcp_server
        from jevkit.security import SafeError

        ctx = self._ctx(mcp_server.GLOBAL_OFFLINE)
        with self.assertRaisesRegex(SafeError, "INVALID_TOOL_ARGUMENTS"):
            mcp_server.call("jev_describe", {}, ctx=ctx)

    def test_unknown_extra_key_is_rejected(self):
        from jevkit import mcp_server
        from jevkit.security import SafeError

        ctx = self._ctx(mcp_server.GLOBAL_OFFLINE)
        with self.assertRaisesRegex(SafeError, "INVALID_TOOL_ARGUMENTS"):
            mcp_server.call("jev_health", {"unexpected": True}, ctx=ctx)

    def test_allof_conditional_requirement_fires_only_once_its_trigger_key_is_present(self):
        from jevkit import mcp_server
        from jevkit.security import SafeError

        ctx = self._ctx(mcp_server.GLOBAL_HYBRID)
        case_id = ctx.catalog()[0]["id"]
        with self.assertRaisesRegex(SafeError, "INVALID_TOOL_ARGUMENTS"):
            # 'state' is present, which must require request_id AND
            # data_classification -- neither is supplied here.
            mcp_server.call("jev_evaluate", {"case_id": case_id, "workspace_path": "/w",
                                             "state": {}}, ctx=ctx)

    def test_object_typed_property_rejects_a_non_dict_value(self):
        from jevkit import mcp_server
        from jevkit.security import SafeError

        ctx = self._ctx(mcp_server.GLOBAL_HYBRID)
        case_id = ctx.catalog()[0]["id"]
        with self.assertRaisesRegex(SafeError, "INVALID_TOOL_ARGUMENTS"):
            mcp_server.call("jev_evaluate", {"case_id": case_id, "workspace_path": "/w",
                                             "state": "not-a-dict", "request_id": "r1",
                                             "data_classification": "public"}, ctx=ctx)

    def test_string_typed_property_rejects_a_non_string_value(self):
        from jevkit import mcp_server
        from jevkit.security import SafeError

        ctx = self._ctx(mcp_server.GLOBAL_OFFLINE)
        with self.assertRaisesRegex(SafeError, "INVALID_TOOL_ARGUMENTS"):
            mcp_server.call("jev_describe", {"case_id": 12345}, ctx=ctx)

    def test_string_length_bounds_are_enforced(self):
        from jevkit import mcp_server
        from jevkit.security import SafeError

        ctx = self._ctx(mcp_server.GLOBAL_OFFLINE)
        with self.assertRaisesRegex(SafeError, "INVALID_TOOL_ARGUMENTS"):
            mcp_server.call("jev_policy_check", {"intent": ""}, ctx=ctx)  # below minLength=1

    def test_enum_violation_is_rejected(self):
        """MUTATION 4 target: `value not in definition['enum']`.

        Deliberately uses jev_describe, whose schema has exactly one
        property (case_id) -- not jev_run_fixture's case_id+variant pair.
        case_id is itself enum-constrained, so an out-of-enum *variant*
        alongside a valid case_id would let the same mutated line fire a
        false positive on the valid case_id first (dict iteration order),
        raising the right-shaped error for the wrong reason and making the
        test pass even under the mutation. A single-property schema removes
        that collateral path entirely.
        """
        from jevkit import mcp_server
        from jevkit.security import SafeError

        ctx = self._ctx(mcp_server.GLOBAL_OFFLINE)
        with self.assertRaisesRegex(SafeError, "INVALID_TOOL_ARGUMENTS"):
            mcp_server.call("jev_describe", {"case_id": "not-a-real-case-xyz"}, ctx=ctx)

    def test_unknown_tool_name_that_slipped_past_the_known_table_is_rejected(self):
        """If tools() ever grows a name call()'s dispatch forgets to handle,
        this must fail loudly with UNKNOWN_TOOL -- the same "a new tool
        forces a decision" guarantee test_hermes_parity.py's HostParity
        pins for the Hermes allow-list, applied here to call()'s own
        dispatch fallback."""
        from jevkit import mcp_server
        from jevkit.security import SafeError

        ctx = self._ctx(mcp_server.GLOBAL_OFFLINE)
        phantom = {"name": "jev_phantom", "description": "x",
                   "inputSchema": {"type": "object", "properties": {}, "required": [],
                                    "additionalProperties": False}}
        real_tools = mcp_server.tools
        with patch.object(mcp_server, "tools",
                          side_effect=lambda **kwargs: real_tools(**kwargs) + [phantom]):
            with self.assertRaisesRegex(SafeError, "UNKNOWN_TOOL"):
                mcp_server.call("jev_phantom", {}, ctx=ctx)

    def test_catalog_describe_and_run_fixture_dispatch_through_call(self):
        from jevkit import mcp_server

        ctx = self._ctx(mcp_server.GLOBAL_OFFLINE)
        catalog = mcp_server.call("jev_catalog", {}, ctx=ctx)
        self.assertIsInstance(catalog, list)
        self.assertTrue(catalog)
        case_id = catalog[0]["id"]
        spec = mcp_server.call("jev_describe", {"case_id": case_id}, ctx=ctx)
        self.assertIsInstance(spec, dict)
        fixture = mcp_server.call("jev_run_fixture", {"case_id": case_id}, ctx=ctx)
        self.assertEqual(fixture["case_id"], case_id)
        self.assertIn("fixture_contract_passed", fixture)

    def test_evaluate_dispatch_forwards_case_state_and_workspace_root_to_the_context(self):
        """Lines 99-103 are pure glue between validated args and
        RuntimeContext.evaluate(); patch evaluate() itself so this test
        checks the glue, not the separately-owned grant/workspace-binding
        logic inside RuntimeContext.evaluate."""
        from jevkit import mcp_server
        from jevkit.runtime import RuntimeContext

        ctx = self._ctx(mcp_server.GLOBAL_HYBRID)
        case_id = ctx.catalog()[0]["id"]
        seen = []

        def fake_evaluate(self_, case_id_, state=None, *, request_id=None,
                          data_classification=None, workspace_root=None):
            seen.append((case_id_, state, request_id, data_classification, workspace_root))
            return {"stub": True}

        with patch.object(RuntimeContext, "evaluate", fake_evaluate):
            result = mcp_server.call(
                "jev_evaluate",
                {"case_id": case_id, "workspace_path": "/synthetic/ws",
                 "state": {"a": 1}, "request_id": "r1", "data_classification": "public"},
                ctx=ctx)
        self.assertEqual(result, {"stub": True})
        self.assertEqual(seen, [(case_id, {"a": 1}, "r1", "public", Path("/synthetic/ws"))])


class LegacyMcpServeLoopTests(unittest.TestCase):
    """serve() is a synchronous stdio loop: one malformed or hostile line
    must degrade to a clean JSON-RPC error on THAT line only. A regression
    here either crashes the whole server process or leaks internal detail
    to whatever is speaking MCP to it.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_root = Path(self._tmp.name) / "state"

    @staticmethod
    def _lines(*objs_or_text):
        parts = [item if isinstance(item, str) else json.dumps(item) for item in objs_or_text]
        return "\n".join(parts) + "\n"

    def _run(self, payload: str):
        from jevkit import mcp_server

        buffer = io.StringIO()
        with patch("sys.stdin", io.StringIO(payload)), redirect_stdout(buffer):
            mcp_server.serve(root=RUNTIME, state_root=self.state_root)
        return [json.loads(line) for line in buffer.getvalue().splitlines()]

    def test_oversized_invalid_json_and_invalid_request_are_isolated_per_line(self):
        payload = self._lines("x" * 512_001, "{not json", json.dumps({"jsonrpc": "1.0", "id": 9}))
        replies = self._run(payload)
        self.assertEqual([r["error"]["code"] for r in replies], [-32700, -32700, -32600])
        self.assertEqual(replies[0]["error"]["message"], "Message too large")
        self.assertEqual(replies[1]["error"]["message"], "Invalid JSON")
        self.assertEqual(replies[2]["error"]["message"], "Invalid request")

    def test_notification_without_id_produces_no_reply_but_the_loop_continues(self):
        payload = self._lines({"jsonrpc": "2.0", "method": "notifications/initialized"},
                              {"jsonrpc": "2.0", "id": 1, "method": "ping"})
        replies = self._run(payload)
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0]["id"], 1)

    def test_method_before_initialize_is_rejected(self):
        payload = self._lines({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        replies = self._run(payload)
        self.assertEqual(replies[0]["error"]["message"], "INITIALIZE_REQUIRED")

    def test_full_session_ping_list_call_and_unknown_method(self):
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "id": 2, "method": "ping"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
             "params": {"name": "jev_health", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
             "params": {"name": "jev_describe", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 6, "method": "surprise/method"},
        ]
        replies = {r["id"]: r for r in self._run(self._lines(*requests))}
        self.assertIn("result", replies[1])
        self.assertEqual(replies[2]["result"], {})
        self.assertIn("tools", replies[3]["result"])
        self.assertFalse(replies[4]["result"]["isError"])
        self.assertTrue(replies[5]["result"]["isError"])
        self.assertIn("INVALID_TOOL_ARGUMENTS", replies[5]["result"]["content"][0]["text"])
        self.assertEqual(replies[6]["error"]["code"], -32601)

    def test_an_unexpected_internal_exception_is_reported_generically_not_leaked(self):
        """MUTATION 5 target: the outer `except Exception:` must answer with
        the fixed 'Internal error; sensitive details suppressed' string,
        never str(e) -- a raw exception string can carry a local path."""
        from jevkit import mcp_server

        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "ping"},
        ]
        with patch.object(mcp_server, "tools", side_effect=RuntimeError("/Users/example/secret-path leaked")):
            replies = {r["id"]: r for r in self._run(self._lines(*requests))}
        self.assertEqual(replies[2]["error"]["message"], "Internal error; sensitive details suppressed")
        self.assertNotIn("secret-path", json.dumps(replies[2]))
        self.assertEqual(replies[3]["result"], {})


if __name__ == "__main__":
    unittest.main()
