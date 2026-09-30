"""Claude Code adapter: advisory context for enrolled workspaces.

The plugin registers ``SessionStart``, ``UserPromptSubmit`` and
``SubagentStart``. Claude Code's hook reference says exit-0 plain-text stdout
on the first two is added as model context, and that ``SubagentStart`` adds
``hookSpecificOutput.additionalContext`` at the start of the subagent's
conversation. None of these events controls tool permissions, and
``SubagentStart`` cannot block. A subagent does not inherit SessionStart
context, which is why it is registered at all.

``PreToolUse`` is deliberately not registered. Although the current Claude
Code reference documents that exit 0 with no output leaves the normal
permission flow in place, Jev has not verified that behavior in a native
Claude Code hook run. Keep it out of the manifest until that native check is
recorded. The runtime ignores any unregistered event passed to it, which
prevents stale or manually copied hook configuration from gaining behavior.

The guidance names the exact ``workspace_path``, ``data_classification`` and
``provider`` the grant accepts, and advertises typed tools only when the grant
enables generic typed queries. A model told to "use jev_route" without those
arguments guesses them, and a wrong guess is a failed call it never repeats.

HARD RULES
----------
* Never exit 2. Never emit ``permissionDecision``, ``permissionOverrides``,
  ``decision`` or ``continue``. The only structured output is
  ``hookSpecificOutput.additionalContext`` on ``SubagentStart``. This layer is
  advisory and must stay advisory.
* A withdrawn (revoked or refused) workspace is untouched: no output, exit 0.
* A workspace with no active grant gets one line at SessionStart saying so,
  and that the model must not enroll it. Prompts and subagents stay silent.
* Any exception, any unexpected payload shape, any oversized input - emit
  nothing and exit 0. A decision aid must never be why a prompt fails.
* An UNRECOGNISED payload must be DISTINGUISHABLE from "nothing to do" in the
  log. Silence in both cases is how a live hook masquerades as a dead one.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Callable

from .common import workspace
from .settings import enrollment_state

MAX_INPUT_BYTES = 65_536
MAX_PATH_CHARS = 1_024
_EVENTS = ("SessionStart", "UserPromptSubmit", "SubagentStart")
_FULL_GUIDANCE_EVENTS = ("SessionStart", "SubagentStart")
_TYPED_TOOLS = (
    "`jev_route` (pick one task, tool or skill from a closed list), "
    "`jev_typed_decide` (any bounded typed question), "
    "`jev_verify` (does an extraction match its source), "
    "`jev_rerank` (do retrieved passages answer the question at all), "
    "`jev_review_diff` (where to focus a review), "
    "`jev_recipe_try` (a shipped decision recipe)"
)
_LOCAL_TOOLS = (
    "`jev_prepare` (file shortlist for a narrow task), "
    "`jev_reduce` (recoverable context reduction), `jev_recall`, `jev_auto_status`"
)
_ADVISORY = (
    "Gate on the returned confidence, not the top probability. Every answer is "
    "ADVISORY: it never replaces your judgment, the native permission prompt, or "
    "a completion check. Never create or widen a Jev grant yourself."
)
_UNENROLLED = (
    "Qualixar Jev Decision Layer is installed, but no active grant covers this "
    "folder, so its workspace tools will return WORKSPACE_NOT_ENROLLED here. Do "
    "not call them and do not try to enroll the folder. If the user wants Jev "
    "here, they can run /jev-setup themselves; `/jev-setup <parent folder>` with "
    "child coverage ticked covers every project under that folder once, for every harness."
)


def _log(message: str) -> None:
    """Best-effort diagnostic. Never raises, never writes to stdout."""
    target = os.environ.get("JEV_CLAUDE_HOOK_LOG")
    if not target:
        return
    try:
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(message + "\n")
    except OSError:
        pass


def _workspace_path(event: dict[str, Any]) -> str | None:
    """Claude Code supplies `cwd`. Accept the documented field only."""
    value = event.get("cwd")
    return value if isinstance(value, str) and value else None


def _quoted(path: Path) -> str | None:
    """JSON-quote a path so a directory name cannot rewrite the guidance."""
    text = json.dumps(str(path))
    return text if len(text) <= MAX_PATH_CHARS else None


def _guidance(event_name: str, binding: dict[str, Any], here: Path) -> str:
    policy = binding["policy"]
    path = _quoted(here)
    root = _quoted(binding["workspace"])
    if path is None or root is None:
        return ""
    classification = json.dumps(policy.get("data_classification", "public"))
    provider = json.dumps(policy.get("provider"))
    typed = policy.get("generic_query_enabled") is True
    coverage = ("" if binding.get("scope") == "exact"
                else f" This folder is covered by the grant on {root}.")
    arguments = f"Pass workspace_path={path}"
    if typed:
        arguments += f" and data_classification={classification} (plus provider={provider} for jev_typed_decide)"
    arguments += "."
    if typed and policy.get("local_laya_enabled") is True and policy.get("provider") != "laya-mlx":
        # Jev + Laya: only a restricted decision stays local, so say how to ask for one.
        arguments += (' For private or client content pass data_classification="restricted" instead'
                      ' (and provider="laya-mlx" for jev_typed_decide): it is decided by Laya on this Mac'
                      ' and never sent to Jev.')

    if event_name not in _FULL_GUIDANCE_EVENTS:
        use = ("For a bounded decision (routing, ranking, triage, claim verification) use the Jev MCP tools"
               if typed else "Jev can shortlist files (`jev_prepare`) or reduce long text (`jev_reduce`)")
        return f"Qualixar Jev is enrolled for this workspace. {use}. {arguments} Ignore this if it does not apply."

    if typed:
        body = (
            "When a bounded choice comes up, prefer a Jev MCP tool over re-reasoning it yourself: "
            f"{_TYPED_TOOLS}. Also available: {_LOCAL_TOOLS}."
        )
    else:
        body = (
            f"This grant does not enable generic typed queries, so jev_route, jev_typed_decide, "
            "jev_verify, jev_rerank, jev_review_diff and jev_recipe_try will return "
            f"GENERIC_QUERY_NOT_ENROLLED; do not call them. Available: {_LOCAL_TOOLS}."
        )
    return (
        f"Qualixar Jev Decision Layer is enrolled for this workspace.{coverage} {body} "
        f"{arguments} Put relative paths, not absolute home paths, inside content fields: "
        f"the secret screen rejects those. {_ADVISORY}"
    )


def guidance(event_name: str, binding: dict[str, Any], here: Path) -> str:
    """The same argument guidance, for every host adapter that injects context."""
    return _guidance(event_name, binding, here)


def handle(
    event: Any,
    *,
    state_loader: Callable[[Path], dict[str, Any]] | None = None,
) -> str:
    """Return the text to emit as context. Empty string means emit nothing."""
    if not isinstance(event, dict):
        _log(f"UNRECOGNISED-PAYLOAD type={type(event).__name__}")
        return ""

    name = event.get("hook_event_name")
    if name not in _EVENTS:
        _log(f"UNHANDLED-EVENT {name!r} keys={sorted(event)}")
        return ""

    raw_path = _workspace_path(event)
    if raw_path is None:
        _log(f"NO-CWD-FIELD event={name} keys={sorted(event)}")
        return ""

    try:
        here = workspace(raw_path)
        state = (state_loader or enrollment_state)(here)
    except Exception as exc:  # noqa: BLE001 - never propagate into the host
        _log(f"POLICY-LOAD-FAILED {type(exc).__name__}: {exc}")
        return ""

    kind = state.get("state")
    if kind == "enrolled":
        return _guidance(name, state, here)
    if kind in ("not_enrolled", "expired") and name == "SessionStart":
        return _UNENROLLED
    return ""  # withdrawn, or a quiet event: silent by design, not a failure


def emit(event_name: Any, text: str) -> str:
    """Render guidance in the shape each registered event documents."""
    if not text:
        return ""
    if event_name == "SubagentStart":
        return json.dumps({"hookSpecificOutput": {
            "hookEventName": "SubagentStart", "additionalContext": text}}) + "\n"
    return text + "\n"


def main() -> None:
    output = ""
    try:
        raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
        if len(raw) <= MAX_INPUT_BYTES:
            event = json.loads(raw.decode("utf-8")) if raw else {}
            name = event.get("hook_event_name") if isinstance(event, dict) else None
            output = emit(name, handle(event))
        else:
            _log(f"OVERSIZED-INPUT {len(raw)} bytes")
    except Exception as exc:  # noqa: BLE001 - fail open on anything at all
        _log(f"FAIL-OPEN {type(exc).__name__}: {exc}")
        output = ""

    if output:
        sys.stdout.write(output)
    # Always 0. Exit 2 would BLOCK the user's prompt or tool call, and this
    # layer has no authority to do that.
    raise SystemExit(0)


if __name__ == "__main__":
    main()
