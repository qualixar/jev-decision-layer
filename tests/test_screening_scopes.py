"""Cross-platform screening and enrolled data-scope behavior for live decisions."""

from __future__ import annotations

import os
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "plugins" / "qualixar-jev-decision-layer" / "runtime"
sys.path.insert(0, str(RUNTIME))

from jev_auto.common import AutoError, require_clean, screen as auto_screen  # noqa: E402
from jev_auto.engine import Engine  # noqa: E402
from jev_auto.settings import make_policy, save_policy_new  # noqa: E402
from jevkit.security import screen as legacy_screen  # noqa: E402
from src.adl.queries.typed import QueryError, _prepare_query_with_policy  # noqa: E402


QUESTIONS = {"decision": {"type": "noul", "instructions": "Should this work item be reviewed?"}}
CONTACT_AND_PATHS = (
    "Please email alex@example.com about this work item.",
    "Inspect /Users/alex/project/main.py for the change.",
    r"Inspect C:\Users\alex\project\main.py for the change.",
    r"Inspect \\server\share\Users\alex\main.py for the change.",
)
SECRETS = (
    "api_key=synthetic-secret-value-12345",
    "Bearer syntheticBearerToken123456789",
    "sk-proj-SyntheticKey123456789012345",
    "-----BEGIN PRIVATE KEY-----\nsynthetic-private-material\n-----END PRIVATE KEY-----",
    {"metadata": {"password": "synthetic-password-value"}},
)


class ScreeningScopeTests(unittest.TestCase):
    """Ordinary context needs reviewed scope; credential patterns always block."""

    def test_both_screens_detect_posix_windows_and_unc_home_paths_by_default(self):
        for value in CONTACT_AND_PATHS:
            with self.subTest(value=value):
                self.assertTrue(auto_screen(value))
                self.assertTrue(legacy_screen(value)[1])
                self.assertEqual(auto_screen(value, allow_context=True), [])
                self.assertEqual(legacy_screen(value, allow_context=True)[1], [])

    def test_true_secrets_still_block_with_context_allowance(self):
        for value in SECRETS:
            with self.subTest(value=value):
                self.assertTrue(auto_screen(value, allow_context=True))
                self.assertTrue(legacy_screen(value, allow_context=True)[1])
                with self.assertRaisesRegex(AutoError, "SENSITIVE_PAYLOAD_NOT_SENT"):
                    require_clean(value, allow_context=True)

    def test_public_query_rejects_contact_and_paths_before_compilation(self):
        public = {"provider": "typesafe", "generic_query_enabled": True,
                  "data_classification": "public"}
        for value in CONTACT_AND_PATHS:
            with self.subTest(value=value), self.assertRaisesRegex(QueryError, "INPUT_DATA_BLOCKED"):
                _prepare_query_with_policy(value, QUESTIONS, provider="typesafe",
                                           policy=public, data_classification="public")

    def test_enrolled_internal_scope_accepts_contact_and_paths(self):
        internal = {"provider": "typesafe", "generic_query_enabled": True,
                    "data_classification": "internal-minimized"}
        for value in CONTACT_AND_PATHS:
            with self.subTest(value=value):
                compiled = _prepare_query_with_policy(
                    value, QUESTIONS, provider="typesafe", policy=internal,
                    data_classification="internal-minimized")
                self.assertEqual(json.loads(compiled.payload_json)["state"], value)
        with self.assertRaisesRegex(QueryError, "DATA_CLASSIFICATION_NOT_ENROLLED"):
            _prepare_query_with_policy(CONTACT_AND_PATHS[0], QUESTIONS,
                                       provider="typesafe", policy={**internal, "data_classification": "public"},
                                       data_classification="internal-minimized")

    def test_enrolled_restricted_scope_accepts_context_but_not_secrets(self):
        maximum = {"provider": "typesafe", "generic_query_enabled": True,
                   "data_classification": "restricted", "decision_mode": "jev-maximum"}
        compiled = _prepare_query_with_policy(CONTACT_AND_PATHS[2], QUESTIONS,
                                              provider="typesafe", policy=maximum,
                                              data_classification="restricted")
        self.assertEqual(json.loads(compiled.payload_json)["state"], CONTACT_AND_PATHS[2])
        for value in SECRETS:
            with self.subTest(value=value), self.assertRaisesRegex(QueryError, "INPUT_DATA_BLOCKED"):
                _prepare_query_with_policy(value, QUESTIONS, provider="typesafe",
                                           policy=maximum, data_classification="restricted")

    def test_live_engine_calls_provider_for_permitted_context_and_zero_times_for_secrets(self):
        class FakeProvider:
            def __init__(self):
                self.calls = []

            def evaluate(self, policy, state, questions):
                self.calls.append((policy, state, questions))
                return {"model": "jev-1.13.0", "answers": {"decision": {"type": "noul", "noul": 0.1}},
                        "usage": {"input_tokens": 3, "output_tokens": 3}}

        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "project"
            project.mkdir()
            with patch.dict(os.environ, {"XDG_STATE_HOME": str(Path(directory) / "state-home")}):
                policy = make_policy(project, "typesafe", generic_query_enabled=True,
                                     data_classification="internal-minimized")
                save_policy_new(project, policy)
                provider = FakeProvider()
                engine = Engine(project, provider=provider)
                for value in CONTACT_AND_PATHS:
                    response = engine.evaluate_typed(value, QUESTIONS, "typesafe", "internal-minimized")
                    self.assertEqual(response["status"], "ADVISORY")
                self.assertEqual(len(provider.calls), len(CONTACT_AND_PATHS))
                self.assertTrue(all(call[0]["data_classification"] == "internal-minimized"
                                    for call in provider.calls))
                with self.assertRaisesRegex(AutoError, "INPUT_DATA_BLOCKED"):
                    engine.evaluate_typed(CONTACT_AND_PATHS[0], QUESTIONS, "typesafe", "public")
                self.assertEqual(len(provider.calls), len(CONTACT_AND_PATHS))
                for value in SECRETS:
                    with self.subTest(value=value), self.assertRaisesRegex(AutoError, "INPUT_DATA_BLOCKED"):
                        engine.evaluate_typed(value, QUESTIONS, "typesafe", "internal-minimized")
                self.assertEqual(len(provider.calls), len(CONTACT_AND_PATHS))


if __name__ == "__main__":
    unittest.main()
