"""Hermes exposes the bounded, explicit Codex context-tool contracts."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "qualixar-jev-decision-layer"
RUNTIME = PLUGIN / "runtime"
sys.path.insert(0, str(RUNTIME))


def load_plugin():
    spec = importlib.util.spec_from_file_location("qualixar_jev_hermes_parity", PLUGIN / "__init__.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class HermesParityTests(unittest.TestCase):
    def test_plugin_registers_context_tools_with_the_codex_mcp_contract(self):
        from jev_auto.mcp import definitions

        class FakeContext:
            def __init__(self):
                self.tools = []

            def register_tool(self, **kwargs):
                self.tools.append(kwargs)

            def register_skill(self, *_args):
                pass

            def register_hook(self, *_args):
                pass

        context = FakeContext()
        load_plugin().register(context)
        hermes = {tool["name"]: tool["schema"] for tool in context.tools}
        codex = {tool["name"]: tool for tool in definitions(SimpleNamespace(tools=lambda **_kwargs: []))}

        for name in ("jev_prepare", "jev_reduce", "jev_recall"):
            with self.subTest(name=name):
                self.assertIn(name, hermes)
                actual = hermes[name]["parameters"]
                expected = codex[name]["inputSchema"]
                self.assertEqual(actual["type"], expected["type"])
                self.assertEqual(actual["required"], expected["required"])
                self.assertEqual(actual["additionalProperties"], expected["additionalProperties"])
                self.assertEqual(set(actual["properties"]), set(expected["properties"]))
                for property_name, expected_property in expected["properties"].items():
                    with self.subTest(property_name=property_name):
                        self.assertEqual(actual["properties"][property_name]["type"], expected_property["type"])
        self.assertLess(hermes["jev_reduce"]["parameters"]["properties"]["text"]["maxLength"],
                        codex["jev_reduce"]["inputSchema"]["properties"]["text"]["maxLength"])

    def test_plugin_manifest_advertises_all_context_tools(self):
        manifest = (PLUGIN / "plugin.yaml").read_text()
        for name in ("jev_prepare", "jev_reduce", "jev_recall"):
            with self.subTest(name=name):
                self.assertIn(f"  - {name}", manifest)

    def test_native_bridge_dispatches_all_context_tools(self):
        from jev_auto.hermes_tool import handle

        cases = (
            ("jev_prepare", {"workspace_path": "/synthetic", "goal": "Prepare focused context"}),
            ("jev_reduce", {"workspace_path": "/synthetic", "goal": "Reduce safely", "text": "Synthetic text"}),
            ("jev_recall", {"workspace_path": "/synthetic", "receipt_id": "a" * 64}),
        )
        seen = []

        def dispatcher(name, arguments):
            seen.append((name, arguments))
            return {"status": "ADVISORY", "receipt_id": "a" * 64}

        for name, arguments in cases:
            with self.subTest(name=name):
                self.assertEqual(handle({"name": name, "arguments": arguments}, dispatcher=dispatcher)["status"], "ADVISORY")
        self.assertEqual([name for name, _arguments in seen], [name for name, _arguments in cases])

    def test_native_bridge_keeps_untrusted_context_tool_input_out_of_the_dispatcher(self):
        from jev_auto.hermes_tool import handle

        seen = []

        def dispatcher(name, arguments):
            seen.append((name, arguments))
            return {"unexpected": True}

        invalid = (
            ("jev_prepare", {"workspace_path": "relative/path", "goal": "Prepare focused context"}),
            ("jev_reduce", {"workspace_path": "/synthetic", "goal": "Reduce safely", "text": 42}),
            ("jev_recall", {"workspace_path": "/synthetic", "receipt_id": "../../receipt"}),
            ("jev_recall", {"workspace_path": "/synthetic", "receipt_id": "a" * 64, "start": 2, "end": 1}),
        )
        for name, arguments in invalid:
            with self.subTest(name=name):
                self.assertEqual(handle({"name": name, "arguments": arguments}, dispatcher=dispatcher),
                                 {"error": "HERMES_TOOL_ARGUMENTS"})
        self.assertEqual(seen, [])

    def test_bridge_retains_serialized_argument_and_result_caps(self):
        from jev_auto.hermes_tool import handle

        self.assertEqual(
            handle({"name": "jev_route", "arguments": {"task": "x" * 33_000}},
                   dispatcher=lambda *_args: {"unexpected": True}),
            {"error": "HERMES_TOOL_TOO_LARGE"},
        )
        self.assertEqual(
            handle({"name": "jev_recall", "arguments": {
                "workspace_path": "/synthetic", "receipt_id": "a" * 64,
            }}, dispatcher=lambda *_args: {"text": "x" * 5_000}),
            {"error": "HERMES_TOOL_RESULT_INVALID"},
        )

    def test_usable_ascii_reduce_request_passes_bridge_validation(self):
        from jev_auto.hermes_tool import handle

        arguments = {
            "workspace_path": "/synthetic",
            "goal": "Reduce the synthetic output",
            "text": "x" * 2_100,
        }
        self.assertEqual(handle({"name": "jev_reduce", "arguments": arguments},
                                dispatcher=lambda *_args: {"status": "ADVISORY"}), {"status": "ADVISORY"})

    def test_reduce_schema_discloses_encoding_dependent_envelope_cap(self):
        schema = {tool["name"]: tool["schema"] for tool in self._registered_tools()}["jev_reduce"]["parameters"]
        text = schema["properties"]["text"]
        self.assertEqual(text["maxLength"], 20_000)
        self.assertIn("32 KiB", text["description"])
        self.assertIn("non-ASCII", text["description"])

    def test_escaped_reduce_request_over_the_canonical_cap_is_rejected(self):
        from jev_auto.hermes_tool import handle

        arguments = {
            "workspace_path": "/synthetic",
            "goal": "Reduce the synthetic output",
            "text": '"' * 20_000,
        }
        self.assertEqual(handle({"name": "jev_reduce", "arguments": arguments},
                                dispatcher=lambda *_args: {"unexpected": True}),
                         {"error": "HERMES_TOOL_TOO_LARGE"})

    def test_parent_bridge_rejects_oversized_nested_payload_before_spawning_child(self):
        """A hostile tool payload must not allocate a child or block on its stdin."""
        plugin = load_plugin()
        payload = {"name": "jev_route", "arguments": {"nested": ["x" * 4_096] * 16}}

        with patch.object(plugin.subprocess, "Popen") as popen:
            self.assertEqual(plugin._run_child(payload, launcher=PLUGIN / "missing-launcher"), {})

        popen.assert_not_called()

    def test_parent_bridge_rejects_escaped_payload_before_spawning_child(self):
        """The cap applies to canonical JSON bytes, not only apparent string length."""
        plugin = load_plugin()
        payload = {"name": "jev_route", "arguments": {"text": '"' * 20_000}}

        with patch.object(plugin.subprocess, "Popen") as popen:
            self.assertEqual(plugin._run_child(payload, launcher=PLUGIN / "missing-launcher"), {})

        popen.assert_not_called()

    def test_parent_bridge_retries_a_temporarily_nonwritable_stdin_before_deadline(self):
        """EAGAIN from the sidecar pipe is bounded and does not become a host hang."""
        plugin = load_plugin()
        original_write = plugin.os.write
        attempts = 0

        def temporarily_blocked(fd, data):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise BlockingIOError()
            return original_write(fd, data)

        with patch.object(plugin.os, "write", side_effect=temporarily_blocked):
            result = plugin._run_child(
                {"name": "jev_recipe_catalog", "arguments": {}},
                launcher=PLUGIN / "scripts" / "launch-hermes-tool",
            )

        self.assertGreaterEqual(attempts, 2)
        self.assertIsInstance(result, dict)

    @staticmethod
    def _registered_tools():
        class FakeContext:
            def __init__(self):
                self.tools = []

            def register_tool(self, **kwargs):
                self.tools.append(kwargs)

            def register_skill(self, *_args):
                pass

            def register_hook(self, *_args):
                pass

        context = FakeContext()
        load_plugin().register(context)
        return context.tools


if __name__ == "__main__":
    unittest.main()
