"""Regression tests for the quarantined legacy Policy Mode surface."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "plugins" / "qualixar-jev-decision-layer" / "runtime"
sys.path.insert(0, str(RUNTIME))


class LegacyPolicyIdentityTests(unittest.TestCase):
    def test_legacy_policy_reports_current_plugin_identity_and_advisory_modes(self):
        import jevkit
        from jevkit.policy_mode import policy_status

        status = policy_status(config_root=Path("/nonexistent-policy-root"))

        shipped = json.loads((ROOT / "plugins/qualixar-jev-decision-layer/plugin.json").read_text())["version"]
        self.assertEqual(jevkit.__version__, shipped)
        # Not the product version: the legacy policy document's own schema
        # version, which is a contract with policy files already on disk.
        self.assertEqual(status["version"], "1.0.0")
        self.assertTrue(status["deprecated"])
        self.assertEqual(status["modes"], ["off", "assist"])
        self.assertFalse(status["enforcement_active"])

    def test_historical_enforce_configuration_is_advisory_only(self):
        from jevkit.policy_mode import classify_intent, policy_mode

        with patch.dict(os.environ, {"QUALIXAR_JEV_POLICY_MODE": "enforce"}):
            self.assertEqual(policy_mode(), "assist")
            decision = classify_intent("Please review this patch")

        self.assertEqual(decision["status"], "SUGGEST")

    def test_only_advisory_modes_can_be_persisted(self):
        from jevkit.policy_mode import policy_mode, write_policy_mode

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "config"
            path = write_policy_mode("assist", config_root=root)
            self.assertEqual(path.read_text(), "assist\n")
            self.assertEqual(policy_mode(root), "assist")
            path.write_text("off\n")
            self.assertEqual(write_policy_mode("assist", root, overwrite=False).read_text(), "off\n")
            with self.assertRaisesRegex(ValueError, "INVALID_POLICY_MODE"):
                write_policy_mode("enforce", root)

    def test_classifier_and_compatibility_hook_remain_advisory(self):
        from jevkit.policy_mode import classify_intent, handle_hook_event

        self.assertEqual(classify_intent("", mode="assist")["reason_code"], "EMPTY_INTENT")
        self.assertEqual(classify_intent("Bearer abcdefgh", mode="assist")["status"], "BLOCK")
        self.assertEqual(classify_intent("Please review this patch", mode="off")["reason_code"], "POLICY_OFF")
        self.assertEqual(classify_intent("ordinary deterministic work", mode="assist")["status"], "SKIP")

        with tempfile.TemporaryDirectory() as directory:
            result = handle_hook_event(
                {"hook_event_name": "UserPromptSubmit", "prompt": "Please review this patch"},
                data_root=Path(directory),
                mode="enforce",
            )

        self.assertEqual(result["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")
        self.assertNotIn("permissionDecision", result["hookSpecificOutput"])

    def test_reachable_legacy_mcp_tools_expose_only_the_quarantined_surface(self):
        from jevkit.mcp_server import call, tools

        with tempfile.TemporaryDirectory() as directory:
            status = call("jev_policy_status", {}, root=RUNTIME, state_root=Path(directory))
            decision = call(
                "jev_policy_check",
                {"intent": "Please review this patch"},
                root=RUNTIME,
                state_root=Path(directory),
            )
            policy_tool = next(item for item in tools(root=RUNTIME, state_root=Path(directory))
                               if item["name"] == "jev_policy_check")

        self.assertEqual(status["version"], "1.0.0")
        self.assertTrue(status["deprecated"])
        self.assertEqual(decision["status"], "SUGGEST")
        self.assertNotIn("REQUIRE", policy_tool["description"])
        self.assertIn("never denies a host tool", policy_tool["description"])

    def test_historical_pending_ledger_cannot_deny_a_governed_tool(self):
        from jevkit.policy_mode import handle_hook_event

        turn_id = "legacy-pending-turn"
        with tempfile.TemporaryDirectory() as directory:
            data_root = Path(directory)
            policy_root = data_root / "policy"
            policy_root.mkdir()
            digest = hashlib.sha256(turn_id.encode("utf-8")).hexdigest()
            (policy_root / f"{digest}.json").write_text('{"status":"pending"}')

            result = handle_hook_event(
                {
                    "hook_event_name": "PreToolUse",
                    "turn_id": turn_id,
                    "tool_name": "Bash",
                },
                data_root=data_root,
            )

        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
