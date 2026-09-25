"""One-shot native Hermes bridge to the same bounded Jev MCP contracts."""

from __future__ import annotations

import sys
from typing import Any, Callable

from .common import AutoError, canonical, decode

ALLOWED = frozenset({
    "jev_setup", "jev_auto_status", "jev_prepare", "jev_reduce", "jev_recall",
    "jev_route", "jev_recipe_catalog", "jev_recipe_try", "jev_review_diff",
})
_CONTEXT_TOOLS = frozenset({"jev_prepare", "jev_reduce", "jev_recall"})
_RECEIPT_ID = frozenset("0123456789abcdef")
MAX_ARGUMENT_BYTES = 32_768
MAX_RESULT_BYTES = 4_096
MAX_WORKSPACE_PATH_CHARS = 1_024
MAX_GOAL_CHARS = 512
MAX_REDUCE_TEXT_CHARS = 20_000
MAX_RECALL_LINE = 1_000_000


def _is_workspace_path(value: Any) -> bool:
    return (isinstance(value, str) and 1 <= len(value) <= MAX_WORKSPACE_PATH_CHARS
            and value.startswith("/") and "\x00" not in value)


def _is_positive_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= MAX_RECALL_LINE


def _valid_context_arguments(name: str, arguments: dict[str, Any]) -> bool:
    if name not in _CONTEXT_TOOLS:
        return True
    if not _is_workspace_path(arguments.get("workspace_path")):
        return False
    if name == "jev_prepare":
        return (set(arguments) == {"workspace_path", "goal"}
                and isinstance(arguments["goal"], str) and 1 <= len(arguments["goal"]) <= MAX_GOAL_CHARS)
    if name == "jev_reduce":
        return (set(arguments) == {"workspace_path", "goal", "text"}
                and isinstance(arguments["goal"], str) and 1 <= len(arguments["goal"]) <= MAX_GOAL_CHARS
                and isinstance(arguments["text"], str) and len(arguments["text"]) <= MAX_REDUCE_TEXT_CHARS)
    receipt_id = arguments.get("receipt_id")
    if not (set(arguments) <= {"workspace_path", "receipt_id", "start", "end"}
            and {"workspace_path", "receipt_id"} <= set(arguments)
            and isinstance(receipt_id, str) and len(receipt_id) == 64
            and set(receipt_id) <= _RECEIPT_ID):
        return False
    start = arguments.get("start", 1)
    end = arguments.get("end", 120)
    return _is_positive_integer(start) and _is_positive_integer(end) and 0 <= end - start < 300


def _dispatch(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    from jevkit import mcp_server
    from .mcp import dispatch

    return dispatch(name, arguments, mcp_server)


def handle(message: Any, *, dispatcher: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None) -> dict[str, Any]:
    if not isinstance(message, dict) or message.get("name") not in ALLOWED:
        return {"error": "HERMES_TOOL_NOT_ALLOWED"}
    arguments = message.get("arguments")
    if not isinstance(arguments, dict):
        return {"error": "HERMES_TOOL_ARGUMENTS"}
    if not _valid_context_arguments(message["name"], arguments):
        return {"error": "HERMES_TOOL_ARGUMENTS"}
    try:
        # Hermes receives a child-process payload, so bounds apply to its
        # serialized envelope even though Codex's direct MCP text cap is larger.
        if len(canonical(arguments)) > MAX_ARGUMENT_BYTES:
            return {"error": "HERMES_TOOL_TOO_LARGE"}
        result = (dispatcher or _dispatch)(message["name"], arguments)
        if not isinstance(result, dict) or len(canonical(result)) > MAX_RESULT_BYTES:
            return {"error": "HERMES_TOOL_RESULT_INVALID"}
        return result
    except AutoError as error:
        return {"error": str(error)}
    except Exception:
        return {"error": "HERMES_TOOL_UNAVAILABLE"}


def main() -> None:
    try:
        raw = sys.stdin.buffer.read(MAX_ARGUMENT_BYTES + 1)
        result = handle(decode(raw, MAX_ARGUMENT_BYTES)) if len(raw) <= MAX_ARGUMENT_BYTES else {"error": "HERMES_TOOL_TOO_LARGE"}
    except Exception:
        result = {"error": "HERMES_TOOL_ARGUMENTS"}
    sys.stdout.write(canonical(result).decode("ascii") + "\n")


if __name__ == "__main__":
    main()
