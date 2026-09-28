"""Hermes exposes the shared bounded baseline and declared optional tools."""

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
    def test_plugin_registers_common_baseline_with_exact_mcp_schemas(self):
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

        baseline = {"jev_recipe_catalog", "jev_recipe_selftest", "jev_setup",
                    "jev_route", "jev_recipe_try", "jev_recall"}
        self.assertTrue(baseline <= set(hermes))
        for name in baseline:
            with self.subTest(name=name):
                self.assertEqual(hermes[name]["parameters"], codex[name]["inputSchema"])

    def test_plugin_manifest_advertises_all_context_tools(self):
        manifest = (PLUGIN / "plugin.yaml").read_text()
        expected = {tool["name"] for tool in self._registered_tools()}
        advertised = set()
        inside = False
        for line in manifest.splitlines():
            if line == "provides_tools:":
                inside = True
            elif inside and line.startswith("provides_hooks:"):
                break
            elif inside and line.strip().startswith("- "):
                advertised.add(line.strip()[2:])
        self.assertEqual(advertised, expected)
        for name in ("jev_recipe_catalog", "jev_recipe_selftest", "jev_setup",
                     "jev_route", "jev_recipe_try", "jev_recall"):
            with self.subTest(name=name):
                self.assertIn(f"  - {name}", manifest)

    def test_native_bridge_dispatches_route_recipe_try_and_recall(self):
        from jev_auto.hermes_tool import handle

        cases = (
            ("jev_recall", {"workspace_path": "/synthetic", "receipt_id": "a" * 64}),
            ("jev_route", {"workspace_path": "/synthetic", "kind": "task", "task": "Choose a task",
                           "candidates": [{"id": "a"}, {"id": "b"}],
                           "data_classification": "public"}),
            ("jev_recipe_try", {"workspace_path": "/synthetic", "recipe_id": "invoice-triage",
                                "input": {"invoice": "synthetic"}, "data_classification": "public"}),
        )
        seen = []

        def dispatcher(name, arguments):
            seen.append((name, arguments))
            return {"status": "ADVISORY", "receipt_id": "a" * 64}

        for name, arguments in cases:
            with self.subTest(name=name):
                self.assertEqual(handle({"name": name, "arguments": arguments}, dispatcher=dispatcher)["status"], "ADVISORY")
        self.assertEqual([name for name, _arguments in seen], [name for name, _arguments in cases])

    def test_offline_recipe_selftest_runs_without_enrollment_or_provider_calls(self):
        from jev_auto import mcp
        from jev_auto.hermes_tool import handle

        with patch.object(mcp, "ensure", side_effect=AssertionError("self-test must not enroll")), \
             patch.object(mcp, "request", side_effect=AssertionError("self-test must not call a provider")):
            result = handle({"name": "jev_recipe_selftest", "arguments": {}})
        self.assertIsInstance(result, dict)
        self.assertNotIn("error", result)
        self.assertTrue(result)

    def test_registration_bridge_and_manifest_have_no_unregistered_tools(self):
        from jev_auto.hermes_tool import ALLOWED

        registered = {tool["name"] for tool in self._registered_tools()}
        manifest = (PLUGIN / "plugin.yaml").read_text()
        advertised = {line.strip()[2:] for line in manifest.splitlines()
                      if line.startswith("  - ") and line.strip()[2:] != "pre_llm_call"}
        self.assertEqual(registered, ALLOWED)
        self.assertEqual(registered, advertised)
        self.assertNotIn("jev_typed_decide", registered)

    def test_route_and_recipe_try_are_advisory_and_return_receipt(self):
        from jev_auto.hermes_tool import handle

        receipt = "b" * 64
        calls = []

        def dispatcher(name, arguments):
            calls.append((name, arguments))
            return {"status": "ADVISORY", "receipt_id": receipt, "execution_authorized": False}

        route = {"workspace_path": "/synthetic", "kind": "tool", "task": "Find a review tool",
                 "candidates": [{"id": "read"}, {"id": "search"}],
                 "data_classification": "public"}
        recipe = {"workspace_path": "/synthetic", "recipe_id": "invoice-triage",
                  "input": {"invoice": "synthetic"}, "data_classification": "public"}
        expected = {"status": "ADVISORY", "receipt_id": receipt, "execution_authorized": False}
        for name, args in (("jev_route", route), ("jev_recipe_try", recipe)):
            with self.subTest(name=name):
                self.assertEqual(handle({"name": name, "arguments": args}, dispatcher=dispatcher), expected)
        self.assertEqual([name for name, _ in calls], ["jev_route", "jev_recipe_try"])
        descriptions = {tool["name"]: tool["schema"]["description"] for tool in self._registered_tools()}
        self.assertIn("Does not execute it", descriptions["jev_route"])
        self.assertIn("advisory only", descriptions["jev_recipe_try"])

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


class HostParity(unittest.TestCase):
    """Hermes has an explicit common baseline and exact exposure accounting."""

    BASELINE = {"jev_recipe_catalog", "jev_recipe_selftest", "jev_setup", "jev_route",
                "jev_recipe_try", "jev_recall"}

    def test_all_baseline_tools_exist_in_the_shared_server_and_hermes(self):
        from jev_auto.mcp import definitions
        from jevkit import mcp_server

        served = {tool["name"] for tool in definitions(mcp_server)}
        registered = {tool["name"] for tool in HermesParityTests._registered_tools()}
        self.assertTrue(self.BASELINE <= served)
        self.assertTrue(self.BASELINE <= registered)

    def test_registered_tools_exactly_match_bridge_allow_list_and_manifest(self):
        from jev_auto.hermes_tool import ALLOWED

        registered = {tool["name"] for tool in HermesParityTests._registered_tools()}
        manifest = (PLUGIN / "plugin.yaml").read_text()
        advertised = set()
        inside = False
        for line in manifest.splitlines():
            if line == "provides_tools:":
                inside = True
            elif inside and line.startswith("provides_hooks:"):
                break
            elif inside and line.strip().startswith("- "):
                advertised.add(line.strip()[2:])
        self.assertEqual(registered, ALLOWED)
        self.assertEqual(registered, advertised)
        self.assertNotIn("jev_typed_decide", registered)
