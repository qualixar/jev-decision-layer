"""Antigravity PreInvocation hint: one small advisory for enrolled workspaces.

G-M7 / v1 scope: do not register PreToolUse. Antigravity's documented PreToolUse
stdout contract requires a permission ``decision`` and can broaden host trust
via ``permissionOverrides``. There is no safe advisory-only PreToolUse path that
avoids calling Jev on every matched tool while leaving permissions unchanged.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable

from .claude_hook import guidance
from .common import canonical, decode, workspace
from .settings import enrollment_binding

_ADVISORY = " Its answers are advisory and never replace native permissions or completion checks."


def handle(event: Any, *, binding_loader: Callable[[Path], dict[str, Any]] = enrollment_binding) -> dict[str, Any]:
    if not isinstance(event, dict) or type(event.get("invocationNum")) is not int or event["invocationNum"] != 0:
        return {}
    paths = event.get("workspacePaths")
    if not isinstance(paths, list) or not 1 <= len(paths) <= 16 or not isinstance(paths[0], str):
        return {}
    try:
        path = workspace(paths[0])
        binding = binding_loader(path)
        if binding["policy"].get("enabled") is not True:
            return {}
        # The short form carries the exact arguments; the full form is too
        # long for a one-step ephemeral message.
        hint = guidance("UserPromptSubmit", binding, path)
    except Exception:
        return {}
    return {"injectSteps": [{"ephemeralMessage": hint + _ADVISORY}]} if hint else {}


def main() -> None:
    try:
        raw = sys.stdin.buffer.read(65_537)
        result = handle(decode(raw, 65_536)) if len(raw) <= 65_536 else {}
    except Exception:
        result = {}
    sys.stdout.write(canonical(result).decode("ascii") + "\n")


if __name__ == "__main__":
    main()
