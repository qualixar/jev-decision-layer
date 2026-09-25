"""Isolated Hermes turn preparation; broker text never becomes prompt content."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any, Callable

from .common import canonical, decode, require_clean, workspace
from .ipc import ensure, request
from .settings import load_policy


_ELIGIBLE = re.compile(r"(?i)\b(fix|implement|refactor|debug|research|browser|review|test|build)\b")
_ID = re.compile(r"(?:file|skill|guidance):[A-Za-z0-9_./-]{1,200}\Z")
_PREFIX = "Qualixar Jev local shortlist (advisory):"
_JEV_PREFIX = "Qualixar Jev decision (advisory):"
_RECEIPT = re.compile(r"[a-f0-9]{64}\Z")
MAX_BROKER_SECONDS = 20


def _safe_ids(value: Any) -> list[str]:
    if not isinstance(value, list) or not 1 <= len(value) <= 16:
        return []
    selected = []
    for item in value[:5]:
        if not isinstance(item, str) or _ID.fullmatch(item) is None:
            return []
        relative = item.split(":", 1)[1]
        if relative.startswith("/") or any(part in ("", ".", "..") for part in relative.split("/")):
            return []
        selected.append(item)
    return selected


def handle(
    event: Any,
    *,
    policy_loader: Callable[[Path], dict[str, Any]] = load_policy,
    starter: Callable[[Path], Any] = ensure,
    caller: Callable[[Path, dict[str, Any], int], dict[str, Any]] | None = None,
) -> dict[str, str]:
    if not isinstance(event, dict):
        return {}
    goal = event.get("user_message")
    if not isinstance(goal, str) or not 60 <= len(goal) <= 16_000 or _ELIGIBLE.search(goal) is None:
        return {}
    try:
        require_clean(goal)
        path = workspace(event.get("cwd"))
        policy = policy_loader(path)
        if policy.get("prepare_context") is not True:
            return {}
        starter(path)
        invoke = caller or (lambda target, payload, timeout: request(target, payload, timeout=timeout))
        result = invoke(path, {"op": "prepare", "goal": goal}, min(int(policy["timeout_seconds"]) + 5, MAX_BROKER_SECONDS))
        selected = _safe_ids(result.get("selected")) if isinstance(result, dict) else []
        if not selected:
            return {}
        receipt = result.get("receipt_id") if isinstance(result, dict) else None
        if result.get("reason") == "jev_candidate_selection" and isinstance(receipt, str) and _RECEIPT.fullmatch(receipt):
            context = _JEV_PREFIX + "\nReceipt: " + receipt + "\n" + "\n".join(f"- {item}" for item in selected)
        else:
            context = _PREFIX + "\n" + "\n".join(f"- {item}" for item in selected)
        return {"context": context} if len(context) <= 1600 else {}
    except Exception:
        return {}
    return {}


def main() -> None:
    try:
        raw = sys.stdin.buffer.read(32_769)
        result = handle(decode(raw, 32_768)) if len(raw) <= 32_768 else {}
    except Exception:
        result = {}
    sys.stdout.write(canonical(result).decode("ascii") + "\n")


if __name__ == "__main__":
    main()
