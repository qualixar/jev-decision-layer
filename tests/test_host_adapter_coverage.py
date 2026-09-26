"""Coverage-closing tests for the four per-host adapters: hooks, hermes_hook,
hermes_tool, host_mcp.

Each class docstring says what breaks in production if the covered branch is
wrong. This file adds tests; it does not replace `test_hermes_parity.py`'s
ALLOWED-list pin or `test_gate_hardening.py`'s existing host_mcp secret-leak
tests, and does not re-test what they already cover.
"""

from __future__ import annotations

import io
import json
import os
import runpy
import sys
import tempfile
import unittest
import warnings
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "plugins" / "qualixar-jev-decision-layer" / "runtime"
sys.path.insert(0, str(RUNTIME))


# --------------------------------------------------------------------------
# hooks.py -- Codex's PostToolUse/SessionStart/etc. adapter
# --------------------------------------------------------------------------

class BoundedTextAndMcpTextHelperTests(unittest.TestCase):
    """`bounded_text`/`mcp_text` are the last gate before host output is ever
    echoed back into a hook's additionalContext. If a non-string or an
    oversized/unencodable value slips past here, a later `json.dumps` in
    `main()` can raise, turning an advisory hook into a crash that blocks the
    user's already-authorized tool call.
    """

    def test_non_string_value_is_rejected_without_raising(self):
        from jev_auto.hooks import bounded_text

        for value in (None, 123, [], {}, 1.5):
            with self.subTest(value=value):
                self.assertIsNone(bounded_text(value))

    def test_a_lone_utf16_surrogate_fails_encoding_and_is_rejected_not_raised(self):
        from jev_auto.hooks import bounded_text

        # A JSON payload can legally carry an unpaired \ud800 escape; encoding
        # it to utf-8 raises UnicodeEncodeError (a UnicodeError). The function
        # must degrade to None, never propagate the encoding error.
        self.assertIsNone(bounded_text("host said: \ud800 (unpaired)"))

    def test_text_at_the_byte_cap_is_kept_one_byte_over_is_dropped(self):
        from jev_auto.hooks import MAX_TOOL_TEXT_BYTES, bounded_text

        at_cap = "x" * MAX_TOOL_TEXT_BYTES
        over_cap = "x" * (MAX_TOOL_TEXT_BYTES + 1)
        self.assertEqual(bounded_text(at_cap), at_cap)
        self.assertIsNone(bounded_text(over_cap))

    def test_mcp_parts_individually_small_but_jointly_oversized_are_rejected(self):
        from jev_auto.hooks import MAX_TOOL_TEXT_BYTES, mcp_text

        half = MAX_TOOL_TEXT_BYTES - 2_000
        parts = [{"type": "text", "text": "a" * half}, {"type": "text", "text": "b" * half}]
        # Each part alone is under the cap; the running total is not.
        self.assertIsNone(mcp_text(parts))

    def test_mcp_parts_within_the_joint_cap_are_concatenated(self):
        from jev_auto.hooks import mcp_text

        parts = [{"type": "text", "text": "first "}, {"type": "text", "text": "second"}]
        self.assertEqual(mcp_text(parts), "first second")


class PlainOutputFallbackTests(unittest.TestCase):
    """`plain_output` decides whether a tool result is safe, plain text worth
    sieving. A string tool_response and an unusable dict response are two
    separate return points; both must degrade to None instead of guessing.
    """

    def test_a_bare_string_tool_response_is_read_directly(self):
        from jev_auto.hooks import plain_output

        self.assertEqual(plain_output({"tool_response": "ordinary text"}), "ordinary text")

    def test_a_dict_response_with_none_of_the_known_fields_yields_none(self):
        from jev_auto.hooks import plain_output

        self.assertIsNone(plain_output({"tool_response": {"isError": False, "exit_code": 0}}))

    def test_a_tool_response_that_is_neither_string_nor_dict_yields_none(self):
        from jev_auto.hooks import plain_output

        self.assertIsNone(plain_output({"tool_response": 42}))
        self.assertIsNone(plain_output({}))


class CodexHookHandleGatingTests(unittest.TestCase):
    """`handle()` is Codex's advisory hook entry point. Every early return here
    is a promise that unsupported or ineligible input leaves the user's
    ordinary tool call untouched -- no extra provider call, no broker start.
    """

    @staticmethod
    def _policy(**overrides):
        return {
            "native_output_rewrite": True,
            "provider": "typesafe",
            "routes": {"sieve": "laya-mlx"},
            "timeout_seconds": 1,
            **overrides,
        }

    def test_a_non_dict_event_is_rejected_before_any_field_is_read(self):
        from jev_auto import hooks

        self.assertIsNone(hooks.handle(None))
        self.assertIsNone(hooks.handle("not-an-event"))
        self.assertIsNone(hooks.handle([1, 2, 3]))

    def test_a_non_string_cwd_is_rejected(self):
        from jev_auto import hooks

        self.assertIsNone(hooks.handle({"hook_event_name": "SessionStart", "cwd": None}))
        self.assertIsNone(hooks.handle({"hook_event_name": "SessionStart", "cwd": 7}))

    def test_an_unenrolled_real_workspace_degrades_to_none_via_the_autoerror_path(self):
        """Exercises the (AutoError, OSError) except clause with the real,
        unmocked `workspace`/`load_policy`: an ordinary workspace that has
        never run `jev enroll` must not crash the hook, just stay silent."""
        from jev_auto import hooks

        with tempfile.TemporaryDirectory() as workspace_dir, tempfile.TemporaryDirectory() as state_dir:
            started = []
            result = hooks.handle(
                {"hook_event_name": "SessionStart", "cwd": workspace_dir},
                base=Path(state_dir),
                starter=lambda *_a: started.append(1),
            )
        self.assertIsNone(result)
        self.assertEqual(started, [])

    def test_pretooluse_always_returns_none_and_never_starts_the_broker_or_calls_out(self):
        from jev_auto import hooks

        started, called = [], []
        with patch.object(hooks, "workspace", side_effect=lambda path: Path(path)), \
             patch.object(hooks, "load_policy", side_effect=lambda *_args: self._policy()):
            result = hooks.handle(
                {"hook_event_name": "PreToolUse", "cwd": "/private/tmp"},
                starter=lambda *_a: started.append(1),
                caller=lambda *_a: called.append(1),
            )
        self.assertIsNone(result)
        self.assertEqual(started, [])
        self.assertEqual(called, [])

    def test_post_tool_use_with_rewrite_disabled_returns_none_before_the_broker_starts(self):
        from jev_auto import hooks

        started = []
        with patch.object(hooks, "workspace", side_effect=lambda path: Path(path)), \
             patch.object(hooks, "load_policy", side_effect=lambda *_args: self._policy(native_output_rewrite=False)):
            result = hooks.handle(
                {"hook_event_name": "PostToolUse", "cwd": "/private/tmp", "tool_name": "Bash",
                 "session_id": "s1", "tool_input": {}, "tool_response": {"output": "x"}},
                starter=lambda *_a: started.append(1),
            )
        self.assertIsNone(result)
        self.assertEqual(started, [])

    def test_post_tool_use_with_unusable_output_returns_none(self):
        from jev_auto import hooks

        with patch.object(hooks, "workspace", side_effect=lambda path: Path(path)), \
             patch.object(hooks, "load_policy", side_effect=lambda *_args: self._policy()):
            result = hooks.handle(
                {"hook_event_name": "PostToolUse", "cwd": "/private/tmp", "tool_name": "Bash",
                 "session_id": "s1", "tool_input": {}, "tool_response": {"isError": True}},
                starter=lambda *_a: None,
            )
        self.assertIsNone(result)

    def test_post_tool_use_missing_both_session_and_turn_id_returns_none(self):
        from jev_auto import hooks

        with patch.object(hooks, "workspace", side_effect=lambda path: Path(path)), \
             patch.object(hooks, "load_policy", side_effect=lambda *_args: self._policy()):
            result = hooks.handle(
                {"hook_event_name": "PostToolUse", "cwd": "/private/tmp", "tool_name": "Bash",
                 "tool_input": {}, "tool_response": {"output": "usable text"}},
                starter=lambda *_a: None,
            )
        self.assertIsNone(result)

    def test_subagent_start_returns_the_fixed_advisory_hint(self):
        from jev_auto import hooks

        with patch.object(hooks, "workspace", side_effect=lambda path: Path(path)), \
             patch.object(hooks, "load_policy", side_effect=lambda *_args: self._policy()):
            result = hooks.handle(
                {"hook_event_name": "SubagentStart", "cwd": "/private/tmp"},
                starter=lambda *_a: None,
            )
        self.assertEqual(result["hookSpecificOutput"]["hookEventName"], "SubagentStart")
        self.assertIn("Do not create grants", result["hookSpecificOutput"]["additionalContext"])

    def test_session_start_reports_the_running_version_and_calls_prepare_runtime(self):
        from jev_auto import hooks, __version__

        seen = []
        with patch.object(hooks, "workspace", side_effect=lambda path: Path(path)), \
             patch.object(hooks, "load_policy", side_effect=lambda *_args: self._policy()):
            result = hooks.handle(
                {"hook_event_name": "SessionStart", "cwd": "/private/tmp"},
                starter=lambda *_a: None,
                caller=lambda _path, request: seen.append(request) or {},
            )
        self.assertEqual(seen, [{"op": "prepare_runtime"}])
        self.assertIn(__version__, result["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(result["hookSpecificOutput"]["hookEventName"], "SessionStart")

    def test_user_prompt_submit_with_a_packet_surfaces_it_as_additional_context(self):
        from jev_auto import hooks

        calls = []
        with patch.object(hooks, "workspace", side_effect=lambda path: Path(path)), \
             patch.object(hooks, "load_policy", side_effect=lambda *_args: self._policy()):
            result = hooks.handle(
                {"hook_event_name": "UserPromptSubmit", "cwd": "/private/tmp",
                 "session_id": "session-1", "prompt": "do the thing"},
                starter=lambda *_a: None,
                caller=lambda _path, request: calls.append(request) or {"packet": "compact packet text"},
            )
        self.assertEqual([c["op"] for c in calls], ["set_goal", "prepare"])
        self.assertEqual(result["hookSpecificOutput"]["additionalContext"], "compact packet text")

    def test_an_exception_from_the_injected_caller_is_swallowed_not_propagated(self):
        """A broker or provider hiccup during SessionStart must never turn an
        advisory hook into a crash that blocks the user's actual prompt."""
        from jev_auto import hooks

        def explode(*_args, **_kwargs):
            raise RuntimeError("synthetic broker hiccup")

        with patch.object(hooks, "workspace", side_effect=lambda path: Path(path)), \
             patch.object(hooks, "load_policy", side_effect=lambda *_args: self._policy()):
            result = hooks.handle(
                {"hook_event_name": "SessionStart", "cwd": "/private/tmp"},
                starter=lambda *_a: None,
                caller=explode,
            )
        self.assertIsNone(result)

    def test_an_unrecognized_hook_event_name_falls_through_to_the_final_none(self):
        """Distinct from every named branch above: a hook_event_name this
        adapter has no case for (e.g. a future or host-specific lifecycle
        event) must fall through every `if` untouched, not be mistaken for
        one of them."""
        from jev_auto import hooks

        with patch.object(hooks, "workspace", side_effect=lambda path: Path(path)), \
             patch.object(hooks, "load_policy", side_effect=lambda *_args: self._policy()):
            result = hooks.handle(
                {"hook_event_name": "SomeFutureLifecycleEvent", "cwd": "/private/tmp"},
                starter=lambda *_a: None,
            )
        self.assertIsNone(result)


class CodexHookMalformedCwdRegressionTests(unittest.TestCase):
    """FIXED DEFECT (jev_auto/common.py workspace()): a malformed `cwd` used
    to make `workspace(cwd)` raise a bare ValueError/UnicodeEncodeError
    instead of this package's typed AutoError, so it escaped hooks.py's
    `except (AutoError, OSError)` (lines 74-95) -- a function whose contract
    promises AutoError leaked something else to any direct caller of
    `handle()`. Verified independently against the pre-fix file (`git show
    HEAD:.../common.py`) that BOTH shapes below raised uncaught: the embedded
    NUL (ValueError) and the unpaired surrogate (UnicodeEncodeError, itself a
    ValueError/UnicodeError subclass) -- both went through the same
    unguarded `safe_path(...).resolve()` call. `workspace()` now wraps that
    call in `except (ValueError, UnicodeError): raise AutoError(...)`, which
    is why both cases below are ordinary, currently-passing tests, not
    `expectedFailure`.

    Severity, precisely: this was a contract-level defect (raises where the
    contract says it returns), not a user-facing crash. The shipped entry
    point, `hooks.main()`, wraps the whole call in `except Exception: pass`,
    so a real host invoking this hook via `main()` degraded silently either
    way, before and after the fix. The defect mattered for `handle()` as a
    unit and for any future caller that invokes it directly (as this test
    suite itself does), not for the currently shipped hook path.
    """

    def test_a_cwd_with_an_embedded_nul_byte_degrades_to_none_instead_of_raising(self):
        from jev_auto import hooks

        # /private/tmp (not /tmp -- macOS aliases /tmp to /private/tmp via a
        # symlink, which safe_path() refuses for an unrelated, correct reason).
        event = {"hook_event_name": "SessionStart", "cwd": "/private/tmp/evil\x00cwd"}
        self.assertIsNone(hooks.handle(event))

    def test_a_cwd_with_an_unpaired_surrogate_degrades_to_none_instead_of_raising(self):
        from jev_auto import hooks

        event = {"hook_event_name": "SessionStart", "cwd": "/private/tmp/evil\ud800cwd"}
        self.assertIsNone(hooks.handle(event))


class CodexHookMainEntrypointTests(unittest.TestCase):
    """`main()` is the real process boundary: whatever `handle()` returns must
    become either exactly one JSON line on stdout, or nothing. Both branches,
    plus the `if __name__ == "__main__":` guard itself, need direct coverage.
    """

    def test_a_non_none_result_is_printed_as_one_json_line(self):
        from jev_auto import hooks

        fake_stdin = SimpleNamespace(buffer=io.BytesIO(b'{"hook_event_name": "SessionStart", "cwd": "/private/tmp"}'))
        fixed = {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "hi"}}
        output = io.StringIO()
        with patch.object(sys, "stdin", fake_stdin), \
             patch.object(hooks, "handle", return_value=fixed), \
             patch("sys.stdout", output):
            hooks.main()
        self.assertEqual(json.loads(output.getvalue()), fixed)

    def test_module_executed_as___main___runs_main_and_never_raises_on_bad_input(self):
        """Covers `if __name__ == "__main__": main()` (line 181) by actually
        re-executing the module with that name, in-process so coverage sees
        it. Deliberately invalid JSON keeps this hermetic: decode() fails
        fast, main()'s outer except swallows it, nothing is written or read
        beyond the fake stdin."""
        fake_stdin = SimpleNamespace(buffer=io.BytesIO(b"not valid json {{{"))
        output = io.StringIO()
        with patch.object(sys, "stdin", fake_stdin), patch("sys.stdout", output):
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                runpy.run_module("jev_auto.hooks", run_name="__main__")
        self.assertEqual(output.getvalue(), "")


# --------------------------------------------------------------------------
# hermes_hook.py -- Hermes' turn-preparation adapter
# --------------------------------------------------------------------------

class SafeIdsTests(unittest.TestCase):
    """`_safe_ids` is the only thing standing between a broker's `selected`
    list and text that gets woven into the next prompt. Every rejection path
    here is a path-traversal or shape-confusion guard; if any one of them
    silently accepted bad input, a broker response could inject an arbitrary
    file reference into the user's context.
    """

    def test_a_non_list_or_out_of_bounds_length_is_rejected(self):
        from jev_auto.hermes_hook import _safe_ids

        for value in ("not-a-list", {}, [], ["file:a"] * 17):
            with self.subTest(value=repr(value)[:40]):
                self.assertEqual(_safe_ids(value), [])

    def test_a_non_string_or_pattern_mismatched_item_rejects_the_whole_list(self):
        from jev_auto.hermes_hook import _safe_ids

        for value in ([42], ["not-a-valid-id"], ["file:"], ["note:a"]):
            with self.subTest(value=value):
                self.assertEqual(_safe_ids(value), [])

    def test_path_traversal_and_absolute_and_empty_segments_are_rejected(self):
        from jev_auto.hermes_hook import _safe_ids

        for bad_id in ("file:/abs/path", "file:a/../b", "file:./a", "file:a//b", "file:.."):
            with self.subTest(bad_id=bad_id):
                self.assertEqual(_safe_ids([bad_id]), [])

    def test_a_well_formed_id_is_kept(self):
        from jev_auto.hermes_hook import _safe_ids

        self.assertEqual(_safe_ids(["file:src/router.py", "skill:review"]),
                         ["file:src/router.py", "skill:review"])


class HermesHookHandleGatingTests(unittest.TestCase):
    """`handle()` must stay silent (return `{}`) for anything ineligible,
    unenrolled, or erroring -- Hermes has no PostToolUse-style opt-in gate, so
    this function is the only thing between a user's message and a broker
    call.
    """

    def test_a_non_dict_event_yields_empty_context(self):
        from jev_auto.hermes_hook import handle

        self.assertEqual(handle("not-a-dict"), {})
        self.assertEqual(handle(None), {})

    def test_a_goal_failing_length_or_keyword_eligibility_yields_empty_context(self):
        from jev_auto.hermes_hook import handle

        too_short = "fix it"  # has an eligible keyword but under 60 chars
        no_keyword = "x" * 70  # long enough, no eligible keyword at all
        self.assertEqual(handle({"user_message": too_short}), {})
        self.assertEqual(handle({"user_message": no_keyword}), {})

    # 71 chars and >=60 is required; the eligibility gate (line 47) would
    # otherwise reject the goal before any of these tests reach their target
    # line, passing for the wrong reason. Verified with len() before use.
    ELIGIBLE_GOAL = "Please implement and test this synthetic change end to end, thoroughly."

    def test_prepare_context_disabled_by_policy_yields_empty_context(self):
        from jev_auto.hermes_hook import handle

        self.assertGreaterEqual(len(self.ELIGIBLE_GOAL), 60)
        with tempfile.TemporaryDirectory() as directory:
            result = handle(
                {"cwd": directory, "user_message": self.ELIGIBLE_GOAL},
                policy_loader=lambda _path: {"prepare_context": False, "timeout_seconds": 10},
                starter=lambda _path: None,
                caller=lambda *_a: {"selected": ["file:a.py"]},
            )
        self.assertEqual(result, {})

    def test_an_empty_or_unsafe_selected_list_yields_empty_context(self):
        from jev_auto.hermes_hook import handle

        with tempfile.TemporaryDirectory() as directory:
            result = handle(
                {"cwd": directory, "user_message": self.ELIGIBLE_GOAL},
                policy_loader=lambda _path: {"prepare_context": True, "timeout_seconds": 10},
                starter=lambda _path: None,
                caller=lambda *_a: {"selected": []},
            )
        self.assertEqual(result, {})

    def test_any_exception_anywhere_in_the_pipeline_yields_empty_context_not_a_raise(self):
        from jev_auto.hermes_hook import handle

        with tempfile.TemporaryDirectory() as directory:
            result = handle(
                {"cwd": directory, "user_message": self.ELIGIBLE_GOAL},
                policy_loader=lambda _path: {"prepare_context": True, "timeout_seconds": 10},
                starter=lambda _path: None,
                caller=lambda *_a: (_ for _ in ()).throw(RuntimeError("synthetic broker failure")),
            )
        self.assertEqual(result, {})


class HermesHookMainEntrypointTests(unittest.TestCase):
    """`main()` always writes exactly one line, even for oversized or
    unparseable input, because Hermes's parent process blocks on reading a
    reply. A silent hang here is worse than a wrong answer.
    """

    def test_ordinary_input_is_decoded_and_written_as_one_line(self):
        from jev_auto import hermes_hook

        fake_stdin = SimpleNamespace(buffer=io.BytesIO(b'{"user_message": "short"}'))
        output = io.StringIO()
        with patch.object(sys, "stdin", fake_stdin), patch("sys.stdout", output):
            hermes_hook.main()
        self.assertEqual(output.getvalue(), "{}\n")

    def test_input_over_the_raw_byte_cap_short_circuits_to_an_empty_result(self):
        from jev_auto import hermes_hook

        oversized = b"x" * 32_769
        fake_stdin = SimpleNamespace(buffer=io.BytesIO(oversized))
        output = io.StringIO()
        with patch.object(sys, "stdin", fake_stdin), patch("sys.stdout", output):
            hermes_hook.main()
        self.assertEqual(output.getvalue(), "{}\n")

    def test_undecodable_input_is_caught_and_still_writes_a_line(self):
        from jev_auto import hermes_hook

        fake_stdin = SimpleNamespace(buffer=io.BytesIO(b"not valid json"))
        output = io.StringIO()
        with patch.object(sys, "stdin", fake_stdin), patch("sys.stdout", output):
            hermes_hook.main()
        self.assertEqual(output.getvalue(), "{}\n")

    def test_module_executed_as___main___runs_main_without_raising(self):
        fake_stdin = SimpleNamespace(buffer=io.BytesIO(b"not valid json {{{"))
        output = io.StringIO()
        with patch.object(sys, "stdin", fake_stdin), patch("sys.stdout", output):
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                runpy.run_module("jev_auto.hermes_hook", run_name="__main__")
        self.assertEqual(output.getvalue(), "{}\n")


# --------------------------------------------------------------------------
# hermes_tool.py -- Hermes' native bridge for the bounded context tools
# --------------------------------------------------------------------------

class HermesToolRealDispatchTests(unittest.TestCase):
    """The default `dispatcher=None` path imports `jevkit.mcp_server` and
    `.mcp.dispatch` for real (hermes_tool.py lines 64-67). Every other test in
    the suite injects a fake dispatcher; this class proves the real wiring
    still works, using `jev_recipe_selftest`, which is offline and
    workspace-free by construction, so it needs no enrollment and no network.
    """

    def test_the_real_dispatcher_answers_the_offline_selftest_tool(self):
        from jev_auto.hermes_tool import handle

        result = handle({"name": "jev_recipe_selftest", "arguments": {}})
        self.assertNotIn("error", result)
        self.assertTrue(result.get("all_passed"))
        self.assertIn("recipes", result)

    def test_the_real_dispatcher_surfaces_an_autoerror_as_a_fixed_error_string(self):
        """`variant` without `recipe_id` is a real, non-injected AutoError
        raised inside jev_auto.mcp.dispatch; this exercises hermes_tool's own
        `except AutoError as error: return {"error": str(error)}` (line 88)
        without mocking anything."""
        from jev_auto.hermes_tool import handle

        result = handle({"name": "jev_recipe_selftest", "arguments": {"variant": "nominal"}})
        self.assertEqual(result, {"error": "MCP_ARGUMENTS"})


class HermesToolBoundsOrderingTests(unittest.TestCase):
    """The brief specifically asks whether a size limit is checked before or
    after the expensive dispatch call. It must be before: dispatch can shell
    out to a provider, so paying for an oversized request before rejecting it
    would be the exact bug an offline, bounded bridge exists to prevent.
    """

    def test_an_oversized_argument_is_rejected_before_the_dispatcher_ever_runs(self):
        from jev_auto.hermes_tool import handle

        def must_not_run(*_args, **_kwargs):
            raise AssertionError("dispatcher must not run before the size gate")

        result = handle({"name": "jev_route", "arguments": {"task": "x" * 33_000}}, dispatcher=must_not_run)
        self.assertEqual(result, {"error": "HERMES_TOOL_TOO_LARGE"})

    def test_an_oversized_result_is_reported_without_leaking_its_content(self):
        from jev_auto.hermes_tool import handle

        huge = "x" * 5_000
        result = handle({"name": "jev_route", "arguments": {"task": "small"}},
                        dispatcher=lambda *_a: {"payload": huge})
        self.assertEqual(result, {"error": "HERMES_TOOL_RESULT_INVALID"})
        self.assertNotIn(huge, str(result))


class HermesToolMainEntrypointTests(unittest.TestCase):
    """Same contract as hermes_hook's `main()`: always exactly one line out,
    covering the ordinary path, the oversized-input short circuit, and the
    decode-failure path, plus the `__main__` guard itself.
    """

    def test_ordinary_input_is_decoded_and_dispatched(self):
        from jev_auto import hermes_tool

        payload = b'{"name": "jev_recipe_selftest", "arguments": {}}'
        fake_stdin = SimpleNamespace(buffer=io.BytesIO(payload))
        output = io.StringIO()
        with patch.object(sys, "stdin", fake_stdin), patch("sys.stdout", output):
            hermes_tool.main()
        self.assertIs(json.loads(output.getvalue())["all_passed"], True)

    def test_input_over_the_raw_byte_cap_short_circuits_without_decoding(self):
        from jev_auto import hermes_tool

        oversized = b"x" * (hermes_tool.MAX_ARGUMENT_BYTES + 1)
        fake_stdin = SimpleNamespace(buffer=io.BytesIO(oversized))
        output = io.StringIO()
        with patch.object(sys, "stdin", fake_stdin), patch("sys.stdout", output):
            hermes_tool.main()
        self.assertEqual(json.loads(output.getvalue()), {"error": "HERMES_TOOL_TOO_LARGE"})

    def test_undecodable_input_yields_the_fixed_arguments_error(self):
        from jev_auto import hermes_tool

        fake_stdin = SimpleNamespace(buffer=io.BytesIO(b"not valid json"))
        output = io.StringIO()
        with patch.object(sys, "stdin", fake_stdin), patch("sys.stdout", output):
            hermes_tool.main()
        self.assertEqual(json.loads(output.getvalue()), {"error": "HERMES_TOOL_ARGUMENTS"})

    def test_module_executed_as___main___runs_main_without_raising(self):
        fake_stdin = SimpleNamespace(buffer=io.BytesIO(b"not valid json {{{"))
        output = io.StringIO()
        with patch.object(sys, "stdin", fake_stdin), patch("sys.stdout", output):
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                runpy.run_module("jev_auto.hermes_tool", run_name="__main__")
        self.assertEqual(json.loads(output.getvalue()), {"error": "HERMES_TOOL_ARGUMENTS"})


# --------------------------------------------------------------------------
# host_mcp.py -- MCP registration for vscode / antigravity / claude-desktop
# --------------------------------------------------------------------------

class HostMcpTargetAndLauncherTests(unittest.TestCase):
    """`Target.config_path` and `launcher_path` are pure path computations
    with no filesystem writes of their own, but a wrong answer here points
    every later read/write at the wrong file -- silently, since a host that
    can't find its config just runs with none.
    """

    def test_a_user_level_host_resolves_its_fixed_config_path_without_a_workspace(self):
        from jev_auto import host_mcp

        target = host_mcp.TARGETS["claude-desktop"]
        resolved = target.config_path(None)
        self.assertEqual(resolved, Path("~/Library/Application Support/Claude/claude_desktop_config.json").expanduser())

    def test_the_launcher_script_resolves_to_a_real_file_by_default(self):
        from jev_auto.host_mcp import launcher_path

        path = launcher_path()
        self.assertTrue(path.is_file())
        self.assertEqual(path.name, "launch-jev")

    def test_a_missing_launcher_is_refused_with_a_fixed_error(self):
        from jev_auto.common import AutoError
        from jev_auto.host_mcp import launcher_path

        with patch.object(Path, "is_file", return_value=False):
            with self.assertRaises(AutoError):
                launcher_path()


class HostMcpConfigParsingTests(unittest.TestCase):
    """`_read` refuses to reinterpret a config that is not a JSON object, so a
    hand-edited or foreign file (e.g. a bare `null` or a JSON array) is never
    silently treated as "no servers yet"."""

    def test_valid_json_that_is_not_an_object_is_refused(self):
        from jev_auto.common import AutoError
        from jev_auto.host_mcp import TARGETS, plan

        for content in ("null", "[]", '"just a string"', "42"):
            with self.subTest(content=content), tempfile.TemporaryDirectory() as directory:
                workspace = Path(directory)
                config = workspace / TARGETS["vscode"].workspace_relative
                config.parent.mkdir(parents=True)
                config.write_text(content)
                with self.assertRaises(AutoError):
                    plan("vscode", workspace, Path("/opt/jev"))


class HostMcpPlanRedactionAndLeakTests(unittest.TestCase):
    """The whole reason `plan()` exists as a read-only preview: it must never
    become the vector that prints a live secret. Every other server in the
    host's config is exposed by NAME ONLY; only our own entry (which never
    holds a secret) is shown in full.
    """

    def test_plan_exposes_other_servers_by_name_only_never_their_configuration(self):
        from jev_auto.host_mcp import TARGETS, plan

        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            config = workspace / TARGETS["vscode"].workspace_relative
            config.parent.mkdir(parents=True)
            config.write_text(json.dumps({"servers": {
                "other-a": {"command": "/usr/bin/other-a", "args": ["--flag"],
                            "env": {"API_KEY": "sk-live-should-never-appear"}},
                "other-b": {"command": "/usr/bin/other-b", "args": [], "env": {}},
            }}))
            outcome = plan("vscode", workspace, Path("/opt/jev"))

        self.assertEqual(sorted(outcome["preserved_servers"]), ["other-a", "other-b"])
        self.assertTrue(all(isinstance(name, str) for name in outcome["preserved_servers"]))
        self.assertNotIn("document", outcome)
        serialized = json.dumps(outcome)
        for leaked in ("sk-live-should-never-appear", "/usr/bin/other-a", "/usr/bin/other-b", "--flag"):
            with self.subTest(leaked=leaked):
                self.assertNotIn(leaked, serialized)

    def test_a_prior_entry_under_our_own_name_with_nothing_to_redact_is_returned_as_is(self):
        """`_redacted` has two return points: one masks a secret, the other
        (line 174) returns the entry untouched when `env` is empty or absent.
        This is the integration-level check, through `plan()`; the object
        identity claim (does line 174 really skip rebuilding the dict) is
        checked directly against `_redacted` below, since `plan()`'s config
        goes through a JSON round-trip and can never be `is` the object this
        test constructed.
        """
        from jev_auto.host_mcp import TARGETS, plan

        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            config = workspace / TARGETS["vscode"].workspace_relative
            config.parent.mkdir(parents=True)
            stale_entry = {"command": "/old/stale/launcher", "args": [], "env": {}}
            config.write_text(json.dumps({"servers": {"qualixar-jev": stale_entry}}))
            outcome = plan("vscode", workspace, Path("/opt/jev"))

        self.assertEqual(outcome["action"], "update")
        self.assertEqual(outcome["replaced_entry"], stale_entry)

    def test_redacted_returns_the_same_object_when_there_is_nothing_to_mask(self):
        """Direct unit check of the identity claim above: called on an entry
        with no env, or an empty env, `_redacted` must return the exact same
        object, not an equal-looking rebuild -- proof that a mutation which
        always reconstructs the dict (even when there is nothing to redact)
        cannot pass by coincidence. Contrasted with the masking path, which
        must NOT be the same object (it has to swap in "[REDACTED]").
        """
        from jev_auto.host_mcp import _redacted

        no_env_key = {"command": "/x", "args": []}
        empty_env = {"command": "/x", "args": [], "env": {}}
        with_secret = {"command": "/x", "args": [], "env": {"TOKEN": "sk-live-secret"}}

        self.assertIs(_redacted(no_env_key), no_env_key)
        self.assertIs(_redacted(empty_env), empty_env)
        masked = _redacted(with_secret)
        self.assertIsNot(masked, with_secret)
        self.assertEqual(masked["env"], {"TOKEN": "[REDACTED]"})


class HostMcpSymlinkAndDirectoryRefusalTests(unittest.TestCase):
    """Every way `install()` can find its target directory unusable must
    become the module's own AutoError, never a bare filesystem exception:
    a symlink or wrong file type in the way (this class's first two tests),
    or a directory it lacks permission to create or write into (the last
    two, one already correct and one a shipped defect fixed during this
    session -- see each test's own docstring).

    The dangling (broken) symlink is the subtle case: `Path.exists()`
    follows the link and reports False for a broken link, so `_read()`'s
    `exists()`-gated symlink check (line 106-108) never even runs -- only the
    unconditional `config.is_symlink()` check right before the write (line
    192-193) catches it.
    """

    def test_a_dangling_symlink_at_the_config_path_is_refused_at_install_time(self):
        from jev_auto.common import AutoError
        from jev_auto.host_mcp import TARGETS, install

        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            config = workspace / TARGETS["vscode"].workspace_relative
            config.parent.mkdir(parents=True)
            config.symlink_to(workspace / "does-not-exist-anywhere.json")
            self.assertFalse(config.exists())  # confirms the dangling-link precondition
            self.assertTrue(config.is_symlink())
            with self.assertRaises(AutoError):
                install("vscode", workspace, Path("/opt/jev"))

    def test_a_config_directory_shadowed_by_a_plain_file_is_refused(self):
        from jev_auto.common import AutoError
        from jev_auto.host_mcp import TARGETS, install

        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            # ".vscode" is supposed to be a directory; here it is a file.
            (workspace / TARGETS["vscode"].workspace_relative.parent).write_text("not a directory")
            with self.assertRaises(AutoError):
                install("vscode", workspace, Path("/opt/jev"))

    def test_a_parent_that_refuses_new_directories_raises_autoerror_not_a_bare_oserror(self):
        """Distinct from the mkstemp defect below: here the config directory
        does not exist yet, so `mkdir(parents=True, exist_ok=True)` itself
        must create it -- and its own try/except(OSError) (line 190-191)
        correctly converts a permission failure into the module's AutoError
        contract. This one is expected to PASS; it is the sibling of the
        defect test, proving the module gets this half of the same problem
        right.
        """
        from jev_auto.common import AutoError
        from jev_auto.host_mcp import TARGETS, install

        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            os.chmod(workspace, 0o500)  # no write: ".vscode" cannot be created inside it
            try:
                with self.assertRaises(AutoError):
                    install("vscode", workspace, Path("/opt/jev"))
            finally:
                os.chmod(workspace, 0o700)
            self.assertFalse((workspace / TARGETS["vscode"].workspace_relative.parent).exists())

    def test_an_unwritable_existing_config_directory_raises_autoerror_not_a_bare_oserror(self):
        """FIXED DEFECT: `install()` -> `_write_atomically()` used to call
        `tempfile.mkstemp(dir=...)` BEFORE its own try/except(OSError), so a
        permission failure there raised a raw PermissionError instead of this
        module's AutoError contract that every sibling failure path uses --
        `jev vscode --write` against an unwritable directory reported a
        traceback rather than a refusal. `_write_atomically` now wraps the
        `mkstemp` call in its own `except OSError: raise
        AutoError('HOST_MCP_CONFIG_UNWRITABLE')`, which is why this is an
        ordinary passing test, not `expectedFailure`.
        """
        from jev_auto.common import AutoError
        from jev_auto.host_mcp import TARGETS, install

        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            config_dir = workspace / TARGETS["vscode"].workspace_relative.parent
            config_dir.mkdir(parents=True)
            os.chmod(config_dir, 0o500)  # read+execute only: existing dir stays "exists", but not writable
            try:
                with self.assertRaises(AutoError):
                    install("vscode", workspace, Path("/opt/jev"))
            finally:
                os.chmod(config_dir, 0o700)


class HostMcpAtomicWriteFailureTests(unittest.TestCase):
    """The write is documented as atomic: a temp file, then `os.replace`. If
    the replace step fails after the temp file was already created, the
    function must still (a) surface the module's own AutoError contract, and
    (b) not leave the orphaned temp file behind.
    """

    def test_a_failure_during_the_atomic_replace_is_wrapped_and_cleans_up_the_temp_file(self):
        from jev_auto import host_mcp
        from jev_auto.common import AutoError

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "config.json"
            with patch.object(host_mcp.os, "replace", side_effect=OSError("synthetic replace failure")):
                with self.assertRaises(AutoError):
                    host_mcp._write_atomically(target, "{}\n")
            leftovers = [p for p in Path(directory).iterdir() if p.name.startswith(".jev-")]
            self.assertEqual(leftovers, [], f"orphaned temp file(s) left behind: {leftovers}")


if __name__ == "__main__":
    unittest.main()
