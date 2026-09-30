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
                    "jev_route", "jev_recipe_try", "jev_recall", "jev_verify", "jev_rerank"}
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
                     "jev_route", "jev_recipe_try", "jev_recall", "jev_verify", "jev_rerank"):
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

    def test_native_bridge_dispatches_shared_verify_and_rerank_contracts(self):
        from jev_auto.hermes_tool import handle

        cases = (
            ("jev_verify", {"workspace_path": "/synthetic", "source_text": "Amount: 42",
                            "extraction": {"amount": 42}, "data_classification": "public"}),
            ("jev_rerank", {"workspace_path": "/synthetic", "query": "What is the amount?",
                            "memories": [{"text": "Amount: 42"}], "data_classification": "public"}),
        )
        seen = []

        def dispatcher(name, arguments):
            seen.append((name, arguments))
            return {"status": "ADVISORY", "execution_authorized": False}

        for name, arguments in cases:
            with self.subTest(name=name):
                self.assertEqual(handle({"name": name, "arguments": arguments}, dispatcher=dispatcher),
                                 {"status": "ADVISORY", "execution_authorized": False})
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

    def test_workspace_paths_are_canonicalized_for_each_native_platform(self):
        from jev_auto.hermes_tool import _canonical_workspace_path

        self.assertEqual(_canonical_workspace_path(r"C:\Users\person\repo\..\repo", windows=True),
                         r"C:\Users\person\repo")
        self.assertEqual(_canonical_workspace_path(r"C:/Users/person/repo", windows=True),
                         r"C:\Users\person\repo")
        for invalid in (r"C:relative\repo", r"\\server\share\repo", r"\\?\C:\repo",
                        r"\\.\PhysicalDrive0", "relative/repo", r"C:\x" + "\x00" + "repo"):
            with self.subTest(path=invalid):
                self.assertIsNone(_canonical_workspace_path(invalid, windows=True))
        self.assertEqual(_canonical_workspace_path("/tmp/work/../repo", windows=False), "/tmp/repo")
        self.assertIsNone(_canonical_workspace_path("relative/repo", windows=False))

    def test_windows_workspace_path_is_canonicalized_before_dispatch(self):
        from jev_auto import hermes_tool

        seen = []
        arguments = {"workspace_path": r"C:\Users\person\repo\..\repo", "goal": "Prepare focused context"}
        with patch.object(hermes_tool.sys, "platform", "win32"):
            result = hermes_tool.handle(
                {"name": "jev_prepare", "arguments": arguments},
                dispatcher=lambda _name, args: seen.append(args) or {"status": "ADVISORY"},
            )
        self.assertEqual(result, {"status": "ADVISORY"})
        self.assertEqual(seen[0]["workspace_path"], r"C:\Users\person\repo")
        self.assertEqual(arguments["workspace_path"], r"C:\Users\person\repo\..\repo")

    def test_bridge_retains_serialized_argument_and_result_caps(self):
        from jev_auto import hermes_tool
        from jev_auto.hermes_tool import handle
        plugin = load_plugin()

        self.assertEqual(hermes_tool.MAX_RESULT_BYTES, plugin.MAX_TOOL_RESULT_BYTES)
        # A valid advisory carrying several ranked passage summaries can exceed
        # 4 KiB; the still-bounded bridge must not turn it into a false error.
        large_valid_result = {"summary": "x" * 6_000}
        self.assertEqual(
            handle({"name": "jev_recipe_catalog", "arguments": {}},
                   dispatcher=lambda *_args: large_valid_result),
            large_valid_result,
        )

        self.assertEqual(
            handle({"name": "jev_route", "arguments": {"task": "x" * 33_000}},
                   dispatcher=lambda *_args: {"unexpected": True}),
            {"error": "HERMES_TOOL_TOO_LARGE"},
        )
        self.assertEqual(
            handle({"name": "jev_recall", "arguments": {
                "workspace_path": "/synthetic", "receipt_id": "a" * 64,
            }}, dispatcher=lambda *_args: {"text": "x" * 17_000}),
            {"error": "HERMES_TOOL_RESULT_INVALID"},
        )

    def test_max_valid_verify_response_compacts_echoes_and_preserves_decision(self):
        from jev_auto.common import canonical
        from jev_auto.hermes_tool import MAX_RESULT_BYTES, handle
        from jev_auto.verify import compile_verify, summarise

        extraction = {f"field_{index:02d}": "v" * 1_900 for index in range(15)}
        compile_verify("source", extraction)  # verifies this is a valid maximum-sized extraction
        answers = {f"f{index}": {"type": "noul", "noul": 0.1} for index in range(15)}
        provider_result = summarise(extraction, answers)
        provider_result.update({"provider": "typesafe", "model": "jev-test", "receipt_id": "a" * 64})

        result = handle(
            {"name": "jev_verify", "arguments": {
                "workspace_path": "/synthetic", "source_text": "source", "extraction": extraction,
                "data_classification": "public",
            }},
            dispatcher=lambda *_args: provider_result,
        )

        self.assertEqual(result["status"], "ADVISORY")
        self.assertTrue(result["trustworthy"])
        self.assertEqual(result["receipt_id"], "a" * 64)
        self.assertFalse(result["execution_authorized"])
        self.assertIn("jev_recall", result["details_note"])
        self.assertEqual(len(result["fields"]), 15)
        self.assertEqual(result["fields"][0], {"field": "field_00", "p_wrong": 0.1, "status": "ok"})
        self.assertTrue(all("value" not in item for item in result["fields"]))
        self.assertLessEqual(len(canonical(result)) + 1, MAX_RESULT_BYTES)

    def test_result_wire_cap_includes_the_newline(self):
        from jev_auto.common import canonical
        from jev_auto.hermes_tool import MAX_RESULT_BYTES, handle

        empty = {"text": ""}
        exact = {"text": "x" * (MAX_RESULT_BYTES - len(canonical(empty)) - 1)}
        self.assertEqual(len(canonical(exact)) + 1, MAX_RESULT_BYTES)
        self.assertEqual(handle({"name": "jev_recipe_catalog", "arguments": {}},
                                dispatcher=lambda *_args: exact), exact)
        over = {"text": exact["text"] + "x"}
        self.assertEqual(handle({"name": "jev_recipe_catalog", "arguments": {}},
                                dispatcher=lambda *_args: over),
                         {"error": "HERMES_TOOL_RESULT_INVALID"})

    def test_windows_workspace_paths_reject_remote_and_device_namespaces_for_every_tool(self):
        from jev_auto import hermes_tool

        def sample(spec):
            if "enum" in spec:
                return spec["enum"][0]
            kind = spec.get("type")
            if kind == "string":
                if spec.get("pattern") == "^[a-f0-9]{64}$":
                    return "a" * 64
                return "x" * max(1, spec.get("minLength", 1))
            if kind == "object":
                return {}
            if kind == "array":
                return [sample(spec.get("items", {"type": "object"}))
                        for _ in range(max(1, spec.get("minItems", 1)))]
            if kind == "integer":
                return max(1, spec.get("minimum", 1))
            if kind == "number":
                return 0.5
            raise AssertionError(f"Unhandled schema type: {kind}")

        tested = []
        with patch.object(hermes_tool.sys, "platform", "win32"):
            for tool in self._registered_tools():
                schema = tool["schema"]["parameters"]
                properties = schema["properties"]
                if "workspace_path" not in properties:
                    continue
                arguments = {name: sample(properties[name]) for name in schema["required"]}
                arguments["workspace_path"] = r"\\server\share\workspace"
                with self.subTest(tool=tool["name"]):
                    self.assertEqual(
                        hermes_tool.handle({"name": tool["name"], "arguments": arguments},
                                           dispatcher=lambda *_args: {"unexpected": True}),
                        {"error": "HERMES_TOOL_ARGUMENTS"},
                    )
                tested.append(tool["name"])
        self.assertGreaterEqual(len(tested), 10)

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
                "jev_recipe_try", "jev_recall", "jev_verify", "jev_rerank"}

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
