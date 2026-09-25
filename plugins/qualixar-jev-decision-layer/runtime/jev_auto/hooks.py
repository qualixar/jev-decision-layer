"""Codex-specific hook adapter. Unsupported/missing data leaves ordinary work intact."""

from __future__ import annotations
import json
import re
import sys
from .common import AutoError, decode, workspace
from .settings import load_policy
from .ipc import ensure, request
from .sieve import PROTECTED

POST_TOOL_READ_ONLY_TOOLS = frozenset(("Bash", "mcp__filesystem__read_file"))
MAX_TOOL_TEXT_BYTES = 48_000
MAX_MCP_CONTENT_PARTS = 16


def bounded_text(value):
    if not isinstance(value, str):
        return None
    try:
        if len(value.encode("utf-8")) > MAX_TOOL_TEXT_BYTES:
            return None
    except UnicodeError:
        return None
    return value


def mcp_text(content):
    if (
        not isinstance(content, list)
        or not content
        or len(content) > MAX_MCP_CONTENT_PARTS
    ):
        return None
    parts = []
    size = 0
    for part in content:
        if not isinstance(part, dict) or part.get("type") != "text":
            return None
        text = bounded_text(part.get("text"))
        if text is None:
            return None
        size += len(text.encode("utf-8"))
        if size > MAX_TOOL_TEXT_BYTES:
            return None
        parts.append(text)
    return "".join(parts)


def plain_output(event):
    response = event.get("tool_response")
    if isinstance(response, str):
        return bounded_text(response)
    if isinstance(response, dict):
        if response.get("isError") or response.get("exit_code", 0) not in (0, None):
            return None
        if "content" in response:
            return mcp_text(response["content"])
        for name in ("output", "stdout", "text"):
            text = bounded_text(response.get(name))
            if text is not None:
                return text
    return None


def handle(event, base=None, caller=None, starter=None):
    if not isinstance(event, dict):
        return None
    name = event.get("hook_event_name")
    cwd = event.get("cwd")
    if not isinstance(cwd, str):
        return None
    try:
        path = workspace(cwd)
        p = load_policy(path, base)
        if name == "PreToolUse":
            return None
        if name == "PostToolUse":
            if not p["native_output_rewrite"]:
                return None
            if p.get("routes", {}).get("sieve", p["provider"]) != "laya-mlx":
                return None
            tool = event.get("tool_name", "")
            if tool not in POST_TOOL_READ_ONLY_TOOLS or PROTECTED.search(
                str(event.get("tool_input", {}))
            ):
                return None
            if plain_output(event) is None:
                return None
            if not isinstance(event.get("session_id") or event.get("turn_id"), str):
                return None
        (starter or ensure)(path, base)
    except (AutoError, OSError):
        return None
    call = caller or (
        lambda path, obj: request(path, obj, base, timeout=p["timeout_seconds"] + 5)
    )
    session = event.get("session_id") or event.get("turn_id")
    try:
        if name == "SessionStart":
            call(path, {"op": "prepare_runtime"})
            return {
                "hookSpecificOutput": {
                    "hookEventName": name,
                    "additionalContext": "Qualixar Jev Decision Layer 1.0.4 is enrolled here. Eligible decisions use the standing budget; do not request per-turn grants. Preserve SLM and the existing Computer Use skill.",
                }
            }
        if name == "UserPromptSubmit" and isinstance(session, str):
            goal = event.get("prompt", "")
            call(path, {"op": "set_goal", "session": session, "goal": goal})
            result = call(path, {"op": "prepare", "goal": goal})
            if result.get("packet"):
                return {
                    "hookSpecificOutput": {
                        "hookEventName": name,
                        "additionalContext": result["packet"],
                    }
                }
        if name == "SubagentStart":
            return {
                "hookSpecificOutput": {
                    "hookEventName": name,
                    "additionalContext": "Use this workspace's Jev Decision Layer service and shared budget. Pass your narrow goal explicitly to jev_prepare or jev_reduce. Do not create grants, copy full receipts, or alter SLM.",
                }
            }
        if name == "PreToolUse":
            return None  # no extra model call before every command
        if name == "PostToolUse" and p["native_output_rewrite"]:
            # A tool result may contain confidential prose that pattern matching
            # cannot recognize. Automatic analysis is local-only.
            if p.get("routes", {}).get("sieve", p["provider"]) != "laya-mlx":
                return None
            tool = event.get("tool_name", "")
            # Never rewrite machine-consumed MCP objects or the Computer Use script result.
            if tool not in POST_TOOL_READ_ONLY_TOOLS or PROTECTED.search(
                str(event.get("tool_input", {}))
            ):
                return None
            text = plain_output(event)
            if text is None or not isinstance(session, str):
                return None
            result = call(
                path,
                {
                    "op": "sieve",
                    "session": session,
                    "text": text,
                    "tool": tool,
                    "tool_input": event.get("tool_input", {}),
                },
            )
            if result.get("changed"):
                # PostToolUse must remain additive: never suppress the host's
                # original result or inject the reduced text into its place.
                receipt = result.get("receipt_id")
                if isinstance(receipt, str) and re.fullmatch(r"[a-f0-9]{64}", receipt):
                    return {
                        "hookSpecificOutput": {
                            "hookEventName": name,
                            "additionalContext": f"Optional local reduced view: jev_recall receipt_id={receipt}. Original tool output remains authoritative.",
                        }
                    }
    except Exception:
        # Optimisation failure must not fail a user's already-authorized task.
        return None
    return None


def main():
    try:
        event = decode(sys.stdin.buffer.read(512_001))
        result = handle(event)
        if result is not None:
            print(json.dumps(result, ensure_ascii=True, allow_nan=False))
    except Exception:
        pass


if __name__ == "__main__":
    main()
