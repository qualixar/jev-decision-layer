"""Claude Code adapter: advisory-only hints for enrolled workspaces.

WHY THIS EXISTS SEPARATELY FROM agy_hook
----------------------------------------
Antigravity's documented PreToolUse stdout contract requires a permission
``decision`` and can broaden host trust via ``permissionOverrides``, which is
why ``agy_hook`` deliberately declines to register PreToolUse at all.

Claude Code's contract is different and *does* have a safe advisory-only
path, verified against code.claude.com/docs/en/hooks:

  * ``UserPromptSubmit`` - exit 0 and plain stdout becomes context the model
    can see. Emitting nothing changes nothing.
  * ``SessionStart``     - same stdout-as-context rule.
  * ``PreToolUse``       - exit 0 WITHOUT a ``permissionDecision`` leaves the
    normal permission flow completely untouched. Only exit 2, or an explicit
    ``"permissionDecision": "deny"``, blocks a call. This adapter emits
    neither, ever.

So on Claude Code we can offer a hint before a tool call without taking any
authority. Native permissions stay in control, exactly as on every other host.

HARD RULES
----------
* Never exit 2. Never emit ``permissionDecision``. Never emit
  ``permissionOverrides``. This layer is advisory and must stay advisory.
* Unenrolled workspaces are untouched: no output, exit 0.
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
from .settings import load_policy

MAX_INPUT_BYTES = 65_536

_SESSION_HINT = (
    "Qualixar Jev Decision Layer is enrolled for this workspace. For a bounded "
    "choice among a closed candidate list - which task, which tool, which skill, "
    "which files matter - prefer the `jev_route` or `jev_typed_decide` MCP tool "
    "over guessing. Answers are typed and carry a calibrated confidence; gate on "
    "the confidence, not on the top probability. Every answer is ADVISORY: it "
    "never replaces your judgment, the native permission prompt, or a completion "
    "check."
)

_PROMPT_HINT = (
    "Qualixar Jev is available for this workspace. If this request needs a "
    "bounded decision (routing, ranking, triage, claim verification), consider "
    "`jev_route` / `jev_recipe_try`. Ignore this if it does not apply."
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


def handle(
    event: Any,
    *,
    policy_loader: Callable[[Path], dict[str, Any]] = load_policy,
) -> str:
    """Return the text to emit as context. Empty string means emit nothing."""
    if not isinstance(event, dict):
        _log(f"UNRECOGNISED-PAYLOAD type={type(event).__name__}")
        return ""

    name = event.get("hook_event_name")
    if name not in ("SessionStart", "UserPromptSubmit", "PreToolUse", "SubagentStart"):
        _log(f"UNHANDLED-EVENT {name!r} keys={sorted(event)}")
        return ""

    raw_path = _workspace_path(event)
    if raw_path is None:
        _log(f"NO-CWD-FIELD event={name} keys={sorted(event)}")
        return ""

    try:
        policy = policy_loader(workspace(raw_path))
    except Exception as exc:  # noqa: BLE001 - never propagate into the host
        _log(f"POLICY-LOAD-FAILED {type(exc).__name__}: {exc}")
        return ""

    if policy.get("enabled") is not True:
        return ""  # unenrolled: silent by design, not a failure

    if name in ("SessionStart", "SubagentStart"):
        return _SESSION_HINT
    if name == "UserPromptSubmit":
        return _PROMPT_HINT
    return ""  # PreToolUse: registered, but this version stays silent


def main() -> None:
    text = ""
    try:
        raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
        if len(raw) <= MAX_INPUT_BYTES:
            text = handle(json.loads(raw.decode("utf-8")) if raw else {})
        else:
            _log(f"OVERSIZED-INPUT {len(raw)} bytes")
    except Exception as exc:  # noqa: BLE001 - fail open on anything at all
        _log(f"FAIL-OPEN {type(exc).__name__}: {exc}")
        text = ""

    if text:
        sys.stdout.write(text + "\n")
    # Always 0. Exit 2 would BLOCK the user's prompt or tool call, and this
    # layer has no authority to do that.
    raise SystemExit(0)


if __name__ == "__main__":
    main()
