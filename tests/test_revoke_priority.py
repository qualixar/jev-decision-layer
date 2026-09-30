"""A revoke is never starved by decisions, and a refused folder gets no queued call.

Decisions share the policy lock, and the lock gave a waiting revoke no
priority: with two overlapping callers, `jev revoke` waited as long as the
load lasted while new hosted calls kept starting. A queued policy change now
stops new decisions at a gate, and only the calls already in flight finish.
A refused child folder waits the same way, and every request says which
folder it is for, so one queued before the refusal is refused at the
provider step.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_core_engine_coverage import FakeProviders, _EngineTestCase  # noqa: E402

from jev_auto.common import AutoError  # noqa: E402
from jev_auto.settings import DESCENDANT_COVERAGE_APPROVED, revoke  # noqa: E402

QUESTIONS = {"pick": {"type": "choice", "instructions": "pick one", "criteria": {"a": "opt a", "b": "opt b"}}}
CALL = 0.4


class _Timed(FakeProviders):
    def __init__(self):
        super().__init__()
        self.starts = []
        self.guard = threading.Lock()

    def evaluate(self, p, state, questions):
        with self.guard:
            self.starts.append(time.monotonic())
        time.sleep(CALL)
        return super().evaluate(p, state, questions)


class RevokePriorityTests(_EngineTestCase):
    def test_a_revoke_under_overlapping_load_returns_promptly_and_stops_new_calls(self):
        self.enroll(case_ids=["c1"])
        provider = _Timed()
        engine = self.build_engine(provider)
        stop = threading.Event()
        counter = iter(range(10_000))

        def worker(offset):
            time.sleep(offset)
            while not stop.is_set():
                try:
                    engine.judge("c1", {"n": next(counter)}, QUESTIONS)
                except AutoError:
                    return

        workers = [threading.Thread(target=worker, args=(offset,)) for offset in (0.0, CALL / 2)]
        for thread in workers:
            thread.start()
        time.sleep(1.2)
        asked = time.monotonic()
        revoke(self.project)
        returned = time.monotonic()
        stop.set()
        for thread in workers:
            thread.join(10)
        self.assertLess(returned - asked, 2 * CALL + 0.5, "revoke waited for more than the calls in flight")
        self.assertEqual([start for start in provider.starts if start > returned], [])


class ChildRefusalTests(_EngineTestCase):
    def setUp(self):
        super().setUp()
        self.child = self.project / "client" / "app"
        self.child.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(self.child)], check=True)
        self.enroll(generic_query_enabled=True, covers_descendants=True,
                    descendant_approval=DESCENDANT_COVERAGE_APPROVED)

    def route(self, engine, requested):
        return engine.dispatch({"op": "route", "kind": "task", "task": "Rename a variable",
                                "candidates": [{"id": "small", "description": "Fast"},
                                               {"id": "large", "description": "Careful"}],
                                "data_classification": "public", "requested_path": str(requested)})

    def test_a_request_for_a_folder_refused_after_it_was_sent_is_refused(self):
        engine = self.build_engine()
        self.route(engine, self.child)
        revoke(self.project / "client")
        with self.assertRaises(AutoError) as caught:
            self.route(engine, self.child)
        self.assertEqual(str(caught.exception), "AUTO_DISABLED_OR_EXPIRED")
        self.assertIn("receipt_id", self.route(engine, self.project))

    def test_a_child_refusal_waits_for_the_call_in_flight(self):
        provider = _Timed()
        engine = self.build_engine(provider)
        thread = threading.Thread(target=lambda: self.route(engine, self.child))
        thread.start()
        while not provider.starts:
            time.sleep(0.01)
        revoke(self.project / "client")
        refused_at = time.monotonic()
        thread.join()
        self.assertGreaterEqual(refused_at, provider.starts[0] + CALL - 0.05)


if __name__ == "__main__":
    unittest.main()
