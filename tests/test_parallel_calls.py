"""Parallel decisions run in parallel, and a timeout never reads as "unavailable".

The policy lock used to be held exclusively through provider transport, so
every call in a workspace waited for the one before it. Parallel subagents
then timed out and were told the broker was unavailable, although their calls
still ran and were charged, which invites a retry and a second charge.

Decisions now share the lock; a policy change (revoke, setup) still takes it
exclusively, so it waits for calls in flight and blocks new ones. A client
that times out after sending gets BROKER_TIMEOUT. A busy broker reads the
request before refusing it, so the refusal arrives, and the client waits and
retries a refused request, which never started.
"""

from __future__ import annotations

import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_core_engine_coverage import FakeProviders, _EngineTestCase  # noqa: E402

from jev_auto import ipc  # noqa: E402
from jev_auto.common import AutoError, canonical  # noqa: E402
from jev_auto.server import Server  # noqa: E402
from jev_auto.settings import revoke  # noqa: E402

QUESTIONS = {"pick": {"type": "choice", "instructions": "pick one", "criteria": {"a": "opt a", "b": "opt b"}}}


class _Slow(FakeProviders):
    def __init__(self, seconds):
        super().__init__()
        self.seconds = seconds
        self.active = 0
        self.peak = 0
        self.guard = threading.Lock()
        self.finished = []

    def evaluate(self, p, state, questions):
        with self.guard:
            self.active += 1
            self.peak = max(self.peak, self.active)
        time.sleep(self.seconds)
        with self.guard:
            self.active -= 1
            self.finished.append(time.monotonic())
        return super().evaluate(p, state, questions)


class SharedLockTests(_EngineTestCase):
    def test_decisions_in_one_workspace_run_at_the_same_time(self):
        self.enroll(case_ids=["c1"])
        provider = _Slow(0.4)
        engine = self.build_engine(provider)
        errors = []

        def ask(n):
            try:
                engine.judge("c1", {"n": n}, QUESTIONS)
            except Exception as error:  # noqa: BLE001
                errors.append(error)

        started = time.monotonic()
        threads = [threading.Thread(target=ask, args=(n,)) for n in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertGreaterEqual(provider.peak, 2)
        self.assertLess(time.monotonic() - started, 4 * 0.4)

    def test_a_revoke_waits_for_the_call_in_flight_and_blocks_the_next(self):
        self.enroll(case_ids=["c1"])
        provider = _Slow(0.5)
        engine = self.build_engine(provider)
        outcome = {}

        def ask():
            outcome["result"] = engine.judge("c1", {"n": 1}, QUESTIONS)

        thread = threading.Thread(target=ask)
        thread.start()
        while provider.active == 0:
            time.sleep(0.01)
        revoke(self.project)
        revoked_at = time.monotonic()
        thread.join()
        self.assertIn("receipt_id", outcome["result"])
        self.assertGreaterEqual(revoked_at, provider.finished[0])
        with self.assertRaises(AutoError):
            engine.judge("c1", {"n": 2}, QUESTIONS)


class _SocketCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir="/private/tmp" if sys.platform == "darwin" else "/tmp")
        self.addCleanup(self._tmp.cleanup)
        self.folder = Path(self._tmp.name)
        self.addr = self.folder / "b.sock"
        patcher = patch.object(ipc, "address", return_value=self.addr)
        patcher.start()
        self.addCleanup(patcher.stop)
        ws = patch.object(ipc, "_ipc_workspace", side_effect=lambda path, base=None: Path(path))
        ws.start()
        self.addCleanup(ws.stop)


class ClientErrorTests(_SocketCase):
    def test_no_broker_is_unavailable(self):
        with self.assertRaises(AutoError) as caught:
            ipc.request(self.folder, {"op": "health"}, timeout=0.3)
        self.assertEqual(str(caught.exception), "BROKER_UNAVAILABLE")

    def test_a_request_that_was_sent_and_never_answered_is_a_timeout(self):
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(self.addr))
        os.chmod(self.addr, 0o600)
        server.listen(1)
        self.addCleanup(server.close)
        held = []
        threading.Thread(target=lambda: held.append(server.accept()), daemon=True).start()
        with self.assertRaises(AutoError) as caught:
            ipc.request(self.folder, {"op": "typed_query"}, timeout=0.4)
        self.assertEqual(str(caught.exception), "BROKER_TIMEOUT")


class _Engine:
    def __init__(self, seconds):
        self.seconds = seconds
        self.calls = []

    def dispatch(self, request):
        self.calls.append(request)
        time.sleep(self.seconds)
        return {"done": request.get("n")}


class BusyBrokerTests(_SocketCase):
    def start(self, slots, seconds):
        engine = _Engine(seconds)
        server = Server(self.addr, engine)
        server.slots = threading.BoundedSemaphore(slots)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return engine

    def test_a_busy_refusal_reaches_the_client_as_busy(self):
        self.start(slots=1, seconds=1.5)
        first = threading.Thread(target=lambda: ipc.request(self.folder, {"op": "x", "n": 1}, timeout=5), daemon=True)
        first.start()
        time.sleep(0.2)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(2)
            client.connect(str(self.addr))
            client.sendall(canonical({"op": "x", "n": 2, "pad": "p" * 200_000}) + b"\n")
            reply = client.recv(4096)
        self.assertIn(b"BROKER_BUSY", reply)

    def test_the_client_waits_and_retries_a_request_the_busy_broker_never_started(self):
        engine = self.start(slots=1, seconds=0.6)
        first = threading.Thread(target=lambda: ipc.request(self.folder, {"op": "x", "n": 1}, timeout=5), daemon=True)
        first.start()
        time.sleep(0.1)
        self.assertEqual(ipc.request(self.folder, {"op": "x", "n": 2}, timeout=5), {"done": 2})
        first.join()
        self.assertEqual(sorted(call["n"] for call in engine.calls), [1, 2])


class DefaultTimeoutTests(unittest.TestCase):
    def test_the_default_wait_covers_a_retried_decision(self):
        with patch.object(ipc, "load_policy", return_value={"timeout_seconds": 12}):
            self.assertGreaterEqual(ipc.call_timeout(Path("/x")), 2 * 12 + 10)
        with patch.object(ipc, "load_policy", side_effect=AutoError("WORKSPACE_NOT_ENROLLED")):
            self.assertEqual(ipc.call_timeout(Path("/x")), 16)
        with patch.object(ipc, "load_policy", return_value={"timeout_seconds": 30}):
            self.assertLessEqual(ipc.call_timeout(Path("/x")), 75)


if __name__ == "__main__":
    unittest.main()
