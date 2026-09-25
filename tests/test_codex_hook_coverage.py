"""Codex PostToolUse coverage stays narrow, local, and additive."""

from __future__ import annotations

import copy
import json
import re
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "qualixar-jev-decision-layer"
RUNTIME = PLUGIN / "runtime"
HOOKS_JSON = PLUGIN / "hooks" / "hooks.json"
sys.path.insert(0, str(RUNTIME))


class CodexHookCoverageTests(unittest.TestCase):
    supported_tools = ("Bash", "mcp__filesystem__read_file")
    unsupported_tools = ("Read", "Grep", "WebSearch", "mcp__filesystem__write_file")

    @staticmethod
    def _post_tool_matcher() -> str:
        config = json.loads(HOOKS_JSON.read_text())
        groups = config["hooks"]["PostToolUse"]
        return next(group["matcher"] for group in groups if "matcher" in group)

    @staticmethod
    def _policy() -> dict[str, object]:
        return {
            "native_output_rewrite": True,
            "provider": "typesafe",
            "routes": {"sieve": "laya-mlx"},
            "timeout_seconds": 1,
        }

    def _handle(self, event: dict[str, object]):
        from jev_auto import hooks

        calls: list[dict[str, object]] = []

        def caller(_path: Path, request: dict[str, object]):
            calls.append(request)
            return {"changed": True, "receipt_id": "a" * 64}

        result = hooks.handle(
            event,
            base=Path("/private/tmp/jev-hook-coverage-state"),
            starter=lambda _path, _base: None,
            caller=caller,
        )
        return result, calls

    def test_matcher_and_handler_cover_only_documented_read_paths(self):
        from jev_auto.hooks import POST_TOOL_READ_ONLY_TOOLS

        matcher = self._post_tool_matcher()
        self.assertEqual(matcher, "^(Bash|mcp__filesystem__read_file)$")
        self.assertEqual(POST_TOOL_READ_ONLY_TOOLS, frozenset(self.supported_tools))
        for tool in self.supported_tools:
            with self.subTest(tool=tool):
                self.assertIsNotNone(re.fullmatch(matcher, tool))
        for tool in self.unsupported_tools:
            with self.subTest(tool=tool):
                self.assertIsNone(re.fullmatch(matcher, tool))

    def test_supported_bash_and_read_only_mcp_results_add_context_without_replacement(self):
        from unittest.mock import patch
        from jev_auto import hooks

        events = (
            {
                "hook_event_name": "PostToolUse", "cwd": "/private/tmp",
                "session_id": "synthetic-session", "tool_name": "Bash",
                "tool_input": {"command": "rg -n hook README.md"},
                "tool_response": {"output": "ordinary local output"},
            },
            {
                "hook_event_name": "PostToolUse", "cwd": "/private/tmp",
                "session_id": "synthetic-session", "tool_name": "mcp__filesystem__read_file",
                "tool_input": {"path": "README.md"},
                "tool_response": {"content": [{"type": "text", "text": "ordinary MCP text"}]},
            },
        )
        with patch.object(hooks, "workspace", side_effect=lambda path: Path(path)), \
             patch.object(hooks, "load_policy", side_effect=lambda *_args: self._policy()):
            for event in events:
                with self.subTest(tool=event["tool_name"]):
                    original = copy.deepcopy(event["tool_response"])
                    result, calls = self._handle(event)
                    self.assertEqual(calls[0]["op"], "sieve")
                    self.assertEqual(result["hookSpecificOutput"]["hookEventName"], "PostToolUse")
                    self.assertIn("Original tool output remains authoritative", result["hookSpecificOutput"]["additionalContext"])
                    self.assertEqual(event["tool_response"], original)
                    self.assertEqual(set(result), {"hookSpecificOutput"})
                    self.assertNotIn("suppressOutput", result["hookSpecificOutput"])
                    self.assertNotIn("decision", result["hookSpecificOutput"])

    def test_unmatched_hosted_legacy_and_write_paths_do_not_call_sieve(self):
        from unittest.mock import patch
        from jev_auto import hooks

        with patch.object(hooks, "workspace", side_effect=lambda path: Path(path)), \
             patch.object(hooks, "load_policy", side_effect=lambda *_args: self._policy()):
            for tool in self.unsupported_tools:
                with self.subTest(tool=tool):
                    result, calls = self._handle({
                        "hook_event_name": "PostToolUse", "cwd": "/private/tmp",
                        "session_id": "synthetic-session", "tool_name": tool,
                        "tool_input": {}, "tool_response": {"output": "ordinary output"},
                    })
                    self.assertIsNone(result)
                    self.assertEqual(calls, [])

    def test_ineligible_post_tool_events_do_not_start_local_broker(self):
        from unittest.mock import patch
        from jev_auto import hooks

        starts = []
        event = {"hook_event_name": "PostToolUse", "cwd": "/private/tmp",
                 "session_id": "synthetic-session", "tool_name": "Bash",
                 "tool_input": {}, "tool_response": {"output": "synthetic output"}}
        with patch.object(hooks, "workspace", side_effect=lambda path: Path(path)), \
             patch.object(hooks, "load_policy", return_value={**self._policy(), "routes": {}}):
            self.assertIsNone(hooks.handle(event, starter=lambda *_: starts.append(1)))
        with patch.object(hooks, "workspace", side_effect=lambda path: Path(path)), \
             patch.object(hooks, "load_policy", return_value=self._policy()):
            self.assertIsNone(hooks.handle({**event, "tool_name": "mcp__filesystem__write_file"},
                                            starter=lambda *_: starts.append(1)))
        self.assertEqual(starts, [])

    def test_mcp_text_parser_rejects_errors_non_text_and_oversized_output(self):
        from jev_auto.hooks import MAX_TOOL_TEXT_BYTES, plain_output

        self.assertIsNone(plain_output({"tool_response": {"isError": True, "content": []}}))
        self.assertIsNone(plain_output({"tool_response": {"content": []}}))
        self.assertIsNone(plain_output({"tool_response": {"content": [{"type": "resource", "text": "ignored"}]}}))
        self.assertIsNone(plain_output({"tool_response": {"content": [{"type": "text", "text": "x" * (MAX_TOOL_TEXT_BYTES + 1)}]}}))
        self.assertEqual(plain_output({"tool_response": {"content": [{"type": "text", "text": "safe"}]}}), "safe")


if __name__ == "__main__":
    unittest.main()
