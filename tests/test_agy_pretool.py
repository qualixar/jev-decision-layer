"""G-M7: Antigravity PreToolUse is not a safe advisory Jev trigger in v1.

Primary Antigravity hooks docs (local installed copy of
https://www.antigravity.google/docs/hooks) require PreToolUse handlers to emit a
permission ``decision`` (``allow`` / ``deny`` / ``ask`` / ``force_ask``). That
contract can broaden permissions via ``permissionOverrides`` and does not
document an advisory-only / no-op response that leaves host trust unchanged.

Therefore this package keeps Antigravity limited to PreInvocation advisory
injection and must not register a PreToolUse hook that would claim tool
interception or call Jev before tools.
"""

from __future__ import annotations

import ast
import inspect
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "qualixar-jev-decision-layer"
RUNTIME = PLUGIN / "runtime"
AGY_HOOK = RUNTIME / "jev_auto" / "agy_hook.py"
HOOKS_JSON = PLUGIN / "hooks.json"

# Installed Antigravity hooks documentation used as the live local mirror of
# https://www.antigravity.google/docs/hooks for offline verification.
_AGY_HOOKS_DOCS = Path.home() / ".gemini/antigravity/builtin/skills/agy-customizations/docs/hooks.md"


def _load_agy_hook():
    if str(RUNTIME) not in sys.path:
        sys.path.insert(0, str(RUNTIME))
    from jev_auto.agy_hook import handle, main

    return handle, main


class AgyPretoolGapTests(unittest.TestCase):
    def test_root_hooks_json_declares_only_pre_invocation(self):
        config = json.loads(HOOKS_JSON.read_text())
        self.assertEqual(set(config), {"qualixar-jev"})
        self.assertEqual(set(config["qualixar-jev"]), {"PreInvocation"})
        self.assertNotIn("PreToolUse", config["qualixar-jev"])
        self.assertNotIn("PostToolUse", config["qualixar-jev"])
        handler = config["qualixar-jev"]["PreInvocation"][0]
        self.assertEqual(handler["type"], "command")
        self.assertEqual(handler["command"], "./scripts/launch-agy-hook")
        self.assertEqual(handler["timeout"], 5)
        self.assertTrue((PLUGIN / "scripts" / "launch-agy-hook").is_file())

    def test_agy_hook_source_has_no_pretool_or_permission_surface(self):
        source = AGY_HOOK.read_text()
        tree = ast.parse(source, filename=str(AGY_HOOK))
        # Inspect executable identifiers only. The module docstring may name the
        # rejected PreToolUse contract without implementing it.
        executable = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Assign, ast.AnnAssign)):
                executable.append(node)
        names = {node.id for node in ast.walk(ast.Module(body=executable, type_ignores=[])) if isinstance(node, ast.Name)}
        attrs = {
            node.attr for node in ast.walk(ast.Module(body=executable, type_ignores=[])) if isinstance(node, ast.Attribute)
        }
        string_consts = {
            node.value
            for node in ast.walk(ast.Module(body=executable, type_ignores=[]))
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        forbidden = {
            "PreToolUse",
            "decision",
            "permissionOverrides",
            "permissionDecision",
            "allow",
            "deny",
            "force_ask",
            "toolCall",
            "overwrite",
        }
        self.assertTrue(forbidden.isdisjoint(names | attrs), sorted((names | attrs) & forbidden))
        self.assertTrue(all("permissionOverrides" not in value for value in string_consts))
        self.assertTrue(all("PreToolUse" not in value for value in string_consts))
        self.assertIn("PreInvocation", source)
        self.assertIn("injectSteps", source)
        self.assertIn("native permissions", source)

    def test_preinvocation_advisory_only_for_enrolled_first_invocation(self):
        handle, _main = _load_agy_hook()
        with tempfile.TemporaryDirectory() as directory:
            event = {
                "invocationNum": 0,
                "workspacePaths": [directory],
                "conversationId": "synthetic-g-m7",
            }
            enabled = handle(event, policy_loader=lambda _path: {"enabled": True})
            later = handle({**event, "invocationNum": 1}, policy_loader=lambda _path: {"enabled": True})
            unenrolled = handle(
                event,
                policy_loader=lambda _path: (_ for _ in ()).throw(RuntimeError("not enrolled")),
            )
            disabled = handle(event, policy_loader=lambda _path: {"enabled": False})

        self.assertEqual(set(enabled), {"injectSteps"})
        self.assertEqual(len(enabled["injectSteps"]), 1)
        message = enabled["injectSteps"][0]["ephemeralMessage"]
        self.assertIn("Jev", message)
        self.assertIn("advisory", message.lower())
        self.assertIn("native permissions", message)
        lowered = message.lower()
        for phrase in (
            "permission decision",
            "permissionoverrides",
            "decision: allow",
            "decision: deny",
            "decision: ask",
            "force_ask",
        ):
            self.assertNotIn(phrase, lowered)
        self.assertLessEqual(len(message), 500)
        self.assertEqual(later, {})
        self.assertEqual(unenrolled, {})
        self.assertEqual(disabled, {})

    def test_tool_shaped_events_do_not_trigger_advisory_or_permission_output(self):
        handle, _main = _load_agy_hook()
        with tempfile.TemporaryDirectory() as directory:
            tool_event = {
                "invocationNum": 0,
                "workspacePaths": [directory],
                "conversationId": "synthetic-g-m7-tool",
                "stepIdx": 7,
                "toolCall": {
                    "name": "run_command",
                    "args": {"CommandLine": "npm test"},
                },
            }
            # Even if Antigravity later delivered a tool-shaped payload to this
            # PreInvocation handler, the adapter still only returns advisory
            # injectSteps and never a permission decision.
            result = handle(tool_event, policy_loader=lambda _path: {"enabled": True})

        self.assertEqual(set(result), {"injectSteps"})
        self.assertNotIn("decision", result)
        self.assertNotIn("permissionOverrides", result)
        self.assertNotIn("overwrite", result)
        self.assertNotIn("reason", result)

    def test_antigravity_docs_require_pretool_permission_decision(self):
        if not _AGY_HOOKS_DOCS.is_file():
            self.skipTest("Antigravity host documentation is not installed")
        text = _AGY_HOOKS_DOCS.read_text()
        self.assertIn("### 1. `PreToolUse` Contract", text)
        self.assertIn('**`decision`** (string, required)', text)
        for value in ('"allow"', '"deny"', '"ask"', '"force_ask"'):
            self.assertIn(value, text)
        self.assertIn("permissionOverrides", text)
        self.assertIn("Automatically allow the tool execution", text)
        # PreInvocation is the documented advisory injection path.
        self.assertIn("### 3. `PreInvocation` Contract", text)
        self.assertIn("ephemeralMessage", text)

    def test_proposed_scope_wording_rejects_pretool_interception_claim(self):
        proposed = (
            "Antigravity host support is PreInvocation advisory injection only. "
            "No PreToolUse Jev advisory/interceptor is claimed in v1 because the "
            "documented PreToolUse stdout contract requires a permission decision "
            "and may change host trust via allow/ask/deny/force_ask or "
            "permissionOverrides."
        )
        self.assertIn("PreInvocation advisory injection only", proposed)
        self.assertIn("No PreToolUse", proposed)
        self.assertIn("permission decision", proposed)
        handle, _main = _load_agy_hook()
        source = inspect.getsource(handle)
        self.assertNotIn("decision", source)
        self.assertIn("injectSteps", source)


if __name__ == "__main__":
    unittest.main()
