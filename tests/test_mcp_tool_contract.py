"""The MCP tool list tells a model exactly what each tool accepts.

The nested argument schemas are checked against the code behind the tool:
every example a schema carries is accepted by the real validator, and a
boundary corpus gets the same verdict from the schema and from the code. The
few rules JSON Schema cannot state (unique ids, a byte total, a relation
between two numbers) are listed as known differences and said in the text.
"""

from __future__ import annotations

import io
import json
import re
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "plugins" / "qualixar-jev-decision-layer" / "runtime"
sys.path.insert(0, str(RUNTIME))

from jev_auto import mcp  # noqa: E402
from jev_auto.common import AutoError, canonical  # noqa: E402
from jevkit import mcp_server as legacy  # noqa: E402

# tools/list result bytes for release 1.0.12, before titles, annotations and
# nested schemas were added. The list may grow by at most a quarter.
BASELINE_TOOLS_LIST_BYTES = 11_272

_KNOWN = {"type", "properties", "required", "additionalProperties", "pattern", "minLength", "maxLength",
          "minItems", "maxItems", "items", "enum", "const", "not", "minProperties", "maxProperties",
          "propertyNames", "anyOf", "exclusiveMinimum", "minimum", "maximum", "default", "examples",
          "description", "title"}


def _pattern(pattern: str, value: str) -> bool:
    # JSON Schema patterns are ECMA-262 and unanchored; an unescaped trailing
    # `$` matches only at the very end, which Python spells `\Z`.
    if pattern.endswith("$") and not pattern.endswith("\\$"):
        pattern = pattern[:-1] + r"\Z"
    return re.search(pattern, value) is not None


def _type_ok(kind, value) -> bool:
    kinds = kind if isinstance(kind, list) else [kind]
    checks = {"string": lambda v: isinstance(v, str), "object": lambda v: isinstance(v, dict),
              "array": lambda v: isinstance(v, list), "boolean": lambda v: isinstance(v, bool),
              "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
              "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
              "null": lambda v: v is None}
    return any(checks[item](value) for item in kinds)


def valid(schema: dict, value) -> bool:
    """A small JSON Schema checker for exactly the keywords these tools use."""
    unknown = set(schema) - _KNOWN
    assert not unknown, f"checker does not implement {unknown}"
    if "type" in schema and not _type_ok(schema["type"], value):
        return False
    if "enum" in schema and value not in schema["enum"]:
        return False
    if "const" in schema and value != schema["const"]:
        return False
    if "not" in schema and valid(schema["not"], value):
        return False
    if "anyOf" in schema and not any(valid(branch, value) for branch in schema["anyOf"]):
        return False
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", float("inf")):
            return False
        if "pattern" in schema and not _pattern(schema["pattern"], value):
            return False
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "exclusiveMinimum" in schema and not value > schema["exclusiveMinimum"]:
            return False
        if "minimum" in schema and value < schema["minimum"]:
            return False
        if "maximum" in schema and value > schema["maximum"]:
            return False
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", float("inf")):
            return False
        if "items" in schema and not all(valid(schema["items"], item) for item in value):
            return False
    if isinstance(value, dict):
        if len(value) < schema.get("minProperties", 0) or len(value) > schema.get("maxProperties", float("inf")):
            return False
        if any(key not in value for key in schema.get("required", [])):
            return False
        if "propertyNames" in schema and not all(valid(schema["propertyNames"], key) for key in value):
            return False
        properties = schema.get("properties", {})
        for key, item in value.items():
            if key in properties:
                if not valid(properties[key], item):
                    return False
            elif schema.get("additionalProperties") is False:
                return False
            elif isinstance(schema.get("additionalProperties"), dict) and not valid(schema["additionalProperties"], item):
                return False
    return True


def _tools() -> dict[str, dict]:
    return {tool["name"]: tool for tool in mcp.definitions(legacy)}


def _accepts(check, value) -> bool:
    try:
        check(value)
    except (AutoError, ValueError):
        return False
    return True


def _route(candidates):
    from jev_auto.routing import compile_route

    compile_route("task", "pick one", candidates)


def _rerank(memories):
    from jev_auto.rerank import compile_rerank

    compile_rerank("what expires?", memories)


def _typed(questions):
    from src.adl.queries.typed import _bounded_shape, _validate_questions

    _bounded_shape({"state": "", "questions": questions})
    _validate_questions(questions)


class _Reached(Exception):
    """The validator accepted the value and went on to the next step."""


def _threshold(value):
    from jev_auto.engine import Engine

    stub = SimpleNamespace(policy=lambda: (_ for _ in ()).throw(_Reached()))
    try:
        Engine.verify_extraction(stub, {"source_text": "The cache holds 10 entries.",
                                        "extraction": {"entries": 10}, "threshold": value})
    except _Reached:
        return


def _recall(pair):
    from jev_auto.engine import Engine

    stub = SimpleNamespace(store=SimpleNamespace(get=lambda _key: (_ for _ in ()).throw(_Reached())))
    try:
        Engine.recall(stub, "0" * 64, *pair)
    except _Reached:
        return


def _candidate(identifier="a", description="An option"):
    return {"id": identifier, "description": description}


def _question(**fields):
    return {"type": "choice", "instructions": "Which fits?", "criteria": {"a": "A", "b": "B"}, **fields}


class SchemaMatchesValidatorTests(unittest.TestCase):
    def assertAgreement(self, schema, check, corpus, known_differences=()):
        for value in corpus:
            with self.subTest(value=repr(value)[:80]):
                self.assertEqual(valid(schema, value), _accepts(check, value))
        for value in known_differences:
            with self.subTest(known=repr(value)[:80]):
                self.assertTrue(valid(schema, value))
                self.assertFalse(_accepts(check, value))

    def assertExamplesAccepted(self, schema, check):
        examples = schema.get("examples", [])
        self.assertTrue(examples, "a schema without its own example")
        for example in examples:
            self.assertTrue(valid(schema, example))
            self.assertTrue(_accepts(check, example), example)

    def test_route_candidates(self):
        schema = _tools()["jev_route"]["inputSchema"]["properties"]["candidates"]
        self.assertExamplesAccepted(schema, _route)
        many = [_candidate(f"c{index}") for index in range(12)]
        corpus = [
            [_candidate("a"), _candidate("b")], many, many + [_candidate("z")], [_candidate("a")], [],
            [_candidate("a" * 64), _candidate("b", "x" * 250)],
            [_candidate("a" * 65), _candidate("b")], [_candidate("unknown"), _candidate("b")],
            [_candidate("1a"), _candidate("b")], [_candidate("a\n"), _candidate("b")],
            [_candidate("a-b_C9"), _candidate("b", "  padded  ")],
            [_candidate("a"), _candidate("b", "  " + "x" * 250 + "  ")],
            [_candidate("a"), _candidate("b", "x" * 251)], [_candidate("a"), _candidate("b", "")],
            [_candidate("a"), _candidate("b", "   ")], [_candidate("a"), {"id": "b"}],
            [_candidate("a"), {**_candidate("b"), "name": "Bee"}], [_candidate("a"), "b"],
            [_candidate("a"), {"id": 5, "description": "x"}],
        ]
        self.assertAgreement(schema, _route, corpus,
                             known_differences=[[_candidate("a"), _candidate("a", "again")]])

    def test_rerank_memories(self):
        schema = _tools()["jev_rerank"]["inputSchema"]["properties"]["memories"]
        self.assertExamplesAccepted(schema, _rerank)
        corpus = [
            [{"content": "x"}], [{"content": "x", "fact_id": 7, "score": "high"}], [{"content": "x" * 5000}],
            [{"content": "x"}] * 12, [{"content": "x"}] * 13, [], [{}], [{"content": ""}],
            [{"content": "   "}], [{"content": 5}], ["text"], [{"text": "a passage"}],
        ]
        self.assertAgreement(schema, _rerank, corpus)

    def test_typed_questions(self):
        schema = _tools()["jev_typed_decide"]["inputSchema"]["properties"]["questions"]
        self.assertExamplesAccepted(schema, _typed)
        corpus = [
            {"q": _question()}, {"q": {"type": "score", "instructions": "Rate it", "criteria": ["low", "high"]}},
            {"q": {"type": "noul", "instructions": "Is it true?"}},
            {"q": {"type": "noul", "instructions": "Is it true?", "criteria": {"true": "yes"}}},
            {"q": {"type": "noul", "instructions": "Is it true?", "criteria": {}}},
            {f"q{index}": _question() for index in range(60)}, {f"q{index}": _question() for index in range(61)},
            {"a" * 128: _question()}, {"a" * 129: _question()}, {"1q": _question()}, {}, {"q": "text"},
            {"q": _question(criteria={f"l{index}": "d" for index in range(64)})},
            {"q": _question(criteria={f"l{index}": "d" for index in range(65)})},
            {"q": _question(criteria={"a": "A"})}, {"q": _question(criteria={"": "A", "b": "B"})},
            {"q": _question(criteria={"a" * 128: "A", "b": "B"})}, {"q": _question(criteria={"a" * 129: "A", "b": "B"})},
            {"q": _question(criteria={"a": "", "b": "B"})}, {"q": _question(criteria={"a": "x" * 2000, "b": "B"})},
            {"q": _question(criteria={"a": "x" * 2001, "b": "B"})}, {"q": _question(criteria=["a", "b"])},
            {"q": _question(options=["a", "b"])}, {"q": _question(type="multi")},
            {"q": {"instructions": "Which fits?", "criteria": {"a": "A", "b": "B"}}},
            {"q": {"type": "choice", "criteria": {"a": "A", "b": "B"}}}, {"q": _question(instructions="   ")},
            {"q": _question(instructions="x" * 8000)}, {"q": _question(instructions="x" * 8001)},
            {"q": {"type": "score", "instructions": "Rate", "criteria": ["only"]}},
            {"q": {"type": "score", "instructions": "Rate", "criteria": [str(index) for index in range(10)]}},
            {"q": {"type": "score", "instructions": "Rate", "criteria": [str(index) for index in range(11)]}},
            {"q": {"type": "score", "instructions": "Rate", "criteria": ["low", ""]}},
            {"q": {"type": "score", "instructions": "Rate", "criteria": {"low": "l", "high": "h"}}},
            {"q": {"type": "noul", "instructions": "True?", "criteria": {"maybe": "m"}}},
            {"q": {"type": "noul", "instructions": "True?", "criteria": {"true": ""}}},
        ]
        too_big = {f"q{index}": _question(criteria={"a": "x" * 1900, "b": "y" * 1900}) for index in range(6)}
        self.assertAgreement(schema, _typed, corpus, known_differences=[too_big])

    def test_verify_threshold(self):
        schema = _tools()["jev_verify"]["inputSchema"]["properties"]["threshold"]
        self.assertTrue(valid(schema, schema["default"]))
        self.assertTrue(_accepts(_threshold, schema["default"]))
        corpus = [0.7, 1, 1.0, 0.0001, 0, 0.0, -0.1, 1.01, 7, True, "0.7"]
        self.assertAgreement(schema, _threshold, corpus)

    def test_recall_range(self):
        properties = _tools()["jev_recall"]["inputSchema"]["properties"]
        default = (properties["start"]["default"], properties["end"]["default"])
        self.assertTrue(_accepts(_recall, default))
        for start, end in [(1, 1), (1, 300), (5, 304), (0, 10), (1, 0)]:
            with self.subTest(start=start, end=end):
                schema_ok = valid(properties["start"], start) and valid(properties["end"], end)
                self.assertEqual(schema_ok, _accepts(_recall, (start, end)))
        # The relation between the two cannot be a JSON Schema rule, so the
        # description states it.
        self.assertIn("end - start under 300", _tools()["jev_recall"]["description"])
        for start, end in [(1, 301), (10, 5)]:
            self.assertTrue(valid(properties["start"], start) and valid(properties["end"], end))
            self.assertFalse(_accepts(_recall, (start, end)))


class ToolListTests(unittest.TestCase):
    def test_every_tool_has_a_title_and_honest_annotations(self):
        local = {"jev_health", "jev_policy_status", "jev_policy_check", "jev_catalog", "jev_describe",
                 "jev_run_fixture", "jev_auto_status", "jev_recall", "jev_recipe_catalog", "jev_recipe_selftest"}
        provider = {"jev_evaluate", "jev_prepare", "jev_reduce", "jev_typed_decide", "jev_route", "jev_recipe_try",
                    "jev_verify", "jev_rerank", "jev_review_diff"}
        tools = _tools()
        self.assertEqual(set(tools), local | provider | {"jev_setup"})
        for name, tool in tools.items():
            with self.subTest(tool=name):
                self.assertIsInstance(tool["title"], str)
                self.assertTrue(0 < len(tool["title"]) <= 30)
                annotations = tool["annotations"]
                self.assertIsInstance(annotations["readOnlyHint"], bool)
                self.assertIsInstance(annotations["openWorldHint"], bool)
                if name in local:
                    self.assertEqual((annotations["readOnlyHint"], annotations["openWorldHint"]), (True, False))
                elif name in provider:
                    # Sends text to the decision provider and spends the daily budget.
                    self.assertEqual((annotations["readOnlyHint"], annotations["openWorldHint"]), (False, True))
                else:
                    self.assertEqual((annotations["readOnlyHint"], annotations["openWorldHint"]), (False, False))

    def test_legacy_tools_say_so_and_name_a_current_replacement(self):
        tools = _tools()
        legacy_names = {"jev_health", "jev_policy_status", "jev_policy_check", "jev_catalog", "jev_describe",
                        "jev_run_fixture", "jev_evaluate"}
        for name in legacy_names:
            with self.subTest(tool=name):
                description = tools[name]["description"]
                self.assertTrue(description.startswith("Legacy:"))
                named = set(re.findall(r"\bjev_[a-z_]+\b", description.split(".", 1)[-1] + description))
                replacements = named - legacy_names
                self.assertTrue(replacements, description)
                self.assertTrue(replacements <= set(tools))
        for name in set(tools) - legacy_names:
            self.assertFalse(tools[name]["description"].startswith("Legacy"), name)

    def test_no_undefined_internal_names_reach_the_model(self):
        instructions = self._initialize()["instructions"]
        self.assertIn("Never create grants yourself.", instructions)
        self.assertIn("Other memory and browser tools are unchanged.", instructions)
        texts = [instructions] + [tool["description"] for tool in _tools().values()]
        for text in texts:
            self.assertNotRegex(text, r"\bSLM\b")
        self.assertIn("other memory and browser tools are unchanged", _tools()["jev_prepare"]["description"])

    def test_the_tool_list_grows_by_at_most_a_quarter(self):
        size = len(canonical({"tools": list(_tools().values())}))
        self.assertLessEqual(size, BASELINE_TOOLS_LIST_BYTES * 1.25, size)

    @staticmethod
    def _initialize():
        buffer = io.StringIO()
        request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                              "params": {"protocolVersion": "2025-06-18"}}).encode() + b"\n"
        with patch("sys.stdin", SimpleNamespace(buffer=io.BytesIO(request))), redirect_stdout(buffer):
            mcp.serve()
        return json.loads(buffer.getvalue())["result"]


class RecipeCatalogTests(unittest.TestCase):
    def test_recipe_id_returns_the_fields_recipe_try_needs(self):
        from jev_auto.recipe_runtime import _catalog, prepare_recipe

        preview = mcp.dispatch("jev_recipe_catalog", {}, legacy)
        self.assertEqual(set(preview["recipes"][0]), {"id", "title", "audience"})
        for recipe in _catalog():
            with self.subTest(recipe=recipe["id"]):
                detail = mcp.dispatch("jev_recipe_catalog", {"recipe_id": recipe["id"]}, legacy)
                self.assertEqual(detail["input_schema"], recipe["input_schema"])
                self.assertEqual(detail["limitations"], recipe["limitations"])
                self.assertNotIn("questions", detail)
                # The schema alone is enough to build a first valid input.
                schema = detail["input_schema"]
                values = {key: "A synthetic public example." * max(1, rule.get("minLength", 1) // 26 + 1)
                          for key, rule in schema["properties"].items()}
                values = {key: value[:schema["properties"][key].get("maxLength", 12_000)] for key, value in values.items()}
                self.assertEqual(prepare_recipe(recipe["id"], values)["recipe_id"], recipe["id"])

    def test_an_unknown_recipe_id_is_refused_with_the_existing_code(self):
        with self.assertRaisesRegex(AutoError, "^RECIPE_NOT_FOUND$"):
            mcp.dispatch("jev_recipe_catalog", {"recipe_id": "no.such-recipe"}, legacy)


class ErrorHintTests(unittest.TestCase):
    CODES = ("ROUTE_CANDIDATES_INVALID", "RERANK_MEMORIES_INVALID", "QUESTION_INVALID", "VERIFY_THRESHOLD_INVALID",
             "RECALL_RANGE", "WORKSPACE_REQUIRED", "BROKER_TIMEOUT", "SETUP_BROWSER_UNAVAILABLE")

    def _call(self, code):
        buffer = io.StringIO()
        requests = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "jev_route", "arguments": {}}}]
        payload = "".join(json.dumps(item) + "\n" for item in requests).encode()
        with patch.object(mcp, "dispatch", side_effect=AutoError(code)), \
                patch("sys.stdin", SimpleNamespace(buffer=io.BytesIO(payload))), redirect_stdout(buffer):
            mcp.serve()
        return json.loads(buffer.getvalue().splitlines()[1])["result"]

    def test_the_code_stays_first_and_unchanged_and_a_hint_follows(self):
        for code in self.CODES:
            with self.subTest(code=code):
                result = self._call(code)
                self.assertTrue(result["isError"])
                self.assertEqual(result["content"][0], {"type": "text", "text": code})
                self.assertEqual(len(result["content"]), 2)
                hint = result["content"][1]["text"]
                self.assertTrue(hint.startswith("Hint: "))
                self.assertLess(len(hint), 260)

    def test_other_codes_are_unchanged(self):
        result = self._call("WORKSPACE_NOT_ENROLLED")
        self.assertEqual(result["content"], [{"type": "text", "text": "WORKSPACE_NOT_ENROLLED"}])

    def test_the_hints_state_the_validators_limits(self):
        self.assertIn("^[A-Za-z][A-Za-z0-9_-]{0,63}$", mcp._HINTS["ROUTE_CANDIDATES_INVALID"])
        self.assertIn('"unknown"', mcp._HINTS["ROUTE_CANDIDATES_INVALID"])
        self.assertIn("1,200", mcp._HINTS["RERANK_MEMORIES_INVALID"])
        self.assertIn("under 300", mcp._HINTS["RECALL_RANGE"])
        self.assertIn("may be charged", mcp._HINTS["BROKER_TIMEOUT"])

    def test_enrollment_is_still_checked_before_the_arguments(self):
        bad = {"workspace_path": "/", "kind": "task", "task": "t", "data_classification": "public",
               "candidates": [{"id": "a", "description": "x", "name": "extra"}, {"id": "b", "description": "y"}]}
        with patch.object(mcp, "ensure", side_effect=AutoError("WORKSPACE_NOT_ENROLLED")), \
                patch.object(mcp, "_relative_from_unsafe_folder", return_value=False):
            with self.assertRaisesRegex(AutoError, "^WORKSPACE_NOT_ENROLLED$"):
                mcp.dispatch("jev_route", bad, legacy)


if __name__ == "__main__":
    unittest.main()
