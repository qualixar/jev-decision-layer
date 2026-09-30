"""Jev + Laya: a restricted decision stays on this Mac, everything else goes to Jev.

A live run of every tool under a hybrid grant showed route, recipes and review
keeping restricted requests on Laya while verify and rerank refused them as
REMOTE_RESTRICTED_DATA. Every generic decision tool now applies one rule.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_core_engine_coverage import FakeProviders, _EngineTestCase, _write_attested_mlx_fixture  # noqa: E402

from jev_auto.common import AutoError  # noqa: E402

REQUESTS = {
    "verify": {"op": "verify", "source_text": "The meeting is on Tuesday.", "extraction": {"day": "Tuesday"},
               "threshold": 0.7},
    "rerank": {"op": "rerank", "query": "When is the meeting?", "memories": [{"content": "It is on Tuesday."}]},
    "route": {"op": "route", "kind": "task", "task": "Rename a variable",
              "candidates": [{"id": "small", "description": "Fast model"}, {"id": "large", "description": "Strong model"}]},
    "review_diff": {"op": "review_diff", "goal": "Find risk", "diff": "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a = 1\n+a = 0\n"},
}


class _Providers(FakeProviders):
    """Reports the attested Laya identity for local calls and the default for hosted ones."""

    def __init__(self, revision):
        super().__init__()
        self.revision = revision

    def evaluate(self, p, state, questions):
        self._model = f"laya-mlx@{self.revision}" if p["provider"] == "laya-mlx" else None
        return super().evaluate(p, state, questions)


class HybridRoutingTests(_EngineTestCase):
    def enroll_hybrid(self, local=True):
        model_dir, manifest_path, revision, weight_sha256 = _write_attested_mlx_fixture(self._tmp.name)
        self.revision = revision
        mlx = {"repository": "aac6fef/laya-mlx", "revision": revision, "weight_sha256": weight_sha256,
               "model_dir": str(model_dir), "artifact_manifest": str(manifest_path)}
        self.enroll("typesafe", generic_query_enabled=True, local_laya_enabled=local, mlx=mlx,
                    data_classification="internal-minimized")

    def run_op(self, name, classification):
        fake = _Providers(self.revision)
        engine = self.build_engine(fake)
        result = engine.dispatch({**REQUESTS[name], "data_classification": classification})
        return result, fake.calls[-1].policy["provider"]

    def test_a_restricted_decision_stays_on_laya_for_every_generic_tool(self):
        self.enroll_hybrid()
        for name in REQUESTS:
            with self.subTest(tool=name):
                result, provider = self.run_op(name, "restricted")
                self.assertEqual(provider, "laya-mlx")
                self.assertEqual(result["provider"], "laya-mlx")

    def test_other_decisions_go_to_jev(self):
        self.enroll_hybrid()
        for name in REQUESTS:
            with self.subTest(tool=name):
                _result, provider = self.run_op(name, "internal-minimized")
                self.assertEqual(provider, "typesafe")

    def test_without_laya_enabled_a_restricted_decision_is_still_refused(self):
        self.enroll_hybrid(local=False)
        for name in REQUESTS:
            with self.subTest(tool=name), self.assertRaises(AutoError) as caught:
                self.run_op(name, "restricted")
            self.assertEqual(str(caught.exception), "REMOTE_RESTRICTED_DATA")


if __name__ == "__main__":
    unittest.main()
