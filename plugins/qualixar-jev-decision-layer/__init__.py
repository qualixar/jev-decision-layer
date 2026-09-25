"""Hermes general-plugin adapter using an isolated, bounded Jev sidecar."""

from __future__ import annotations

import json
import os
import re
import selectors
import signal
import subprocess
import time
from pathlib import Path
from typing import Any, Callable


_PLUGIN_ROOT = Path(__file__).resolve().parent
_LAUNCHER = _PLUGIN_ROOT / "scripts" / "launch-hermes-hook"
_TOOL_LAUNCHER = _PLUGIN_ROOT / "scripts" / "launch-hermes-tool"
_ELIGIBLE = re.compile(r"(?i)\b(fix|implement|refactor|debug|research|browser|review|test|build)\b")
_CONTEXT_HEADER = "Qualixar Jev local shortlist (advisory):"
_JEV_HEADER = "Qualixar Jev decision (advisory):"
_ID = re.compile(r"(?:file|skill|guidance):[A-Za-z0-9_./-]{1,200}\Z")
_RECEIPT = re.compile(r"Receipt: [a-f0-9]{64}\Z")
HOOK_DEADLINE_SECONDS = 25
MAX_TOOL_ARGUMENT_BYTES = 32_768
MAX_TOOL_RESULT_BYTES = 4_096
MAX_CONTEXT_WORKSPACE_PATH_CHARS = 1_024
MAX_CONTEXT_GOAL_CHARS = 512
MAX_CONTEXT_REDUCE_TEXT_CHARS = 20_000
MAX_CONTEXT_RECALL_LINE = 1_000_000


def _bounded_child_payload(payload: dict[str, Any]) -> bytes | None:
    """Encode one child request without materialising an unbounded JSON blob.

    Hermes passes the complete tool arguments over stdin.  The host can hand us
    arbitrarily nested objects, so the size check has to happen before a child
    exists and before any blocking pipe write.  ``iterencode`` lets us stop as
    soon as the canonical envelope crosses the fixed boundary.
    """
    encoded = bytearray()
    try:
        encoder = json.JSONEncoder(ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)
        for chunk in encoder.iterencode(payload):
            part = chunk.encode("ascii")
            if len(part) > MAX_TOOL_ARGUMENT_BYTES - len(encoded):
                return None
            encoded.extend(part)
    except (TypeError, ValueError, RecursionError, UnicodeError):
        return None
    if len(encoded) >= MAX_TOOL_ARGUMENT_BYTES:
        return None
    encoded.append(0x0A)
    return bytes(encoded)


def _run_child(payload: dict[str, Any], *, launcher: Path = _LAUNCHER) -> dict[str, Any]:
    if os.name != "posix":
        return {}
    encoded = _bounded_child_payload(payload)
    if encoded is None:
        return {}
    environment = {"PATH": os.defpath, "PYTHONDONTWRITEBYTECODE": "1"}
    for name in ("XDG_STATE_HOME", "XDG_CONFIG_HOME"):
        if os.environ.get(name):
            environment[name] = os.environ[name]
    process = None
    selector = None
    try:
        process = subprocess.Popen(
            [str(launcher)], cwd=_PLUGIN_ROOT, env=environment,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            start_new_session=True, close_fds=True,
        )
        selector = selectors.DefaultSelector()
        os.set_blocking(process.stdin.fileno(), False)
        selector.register(process.stdin, selectors.EVENT_WRITE)
        selector.register(process.stdout, selectors.EVENT_READ)
        deadline = time.monotonic() + HOOK_DEADLINE_SECONDS
        output = bytearray()
        written = 0
        while True:
            remaining = deadline - time.monotonic()
            events = selector.select(remaining) if remaining > 0 else []
            if not events:
                return {}
            stdout_closed = False
            for key, ready in events:
                if key.fileobj is process.stdin and ready & selectors.EVENT_WRITE:
                    try:
                        written += os.write(process.stdin.fileno(), encoded[written:])
                    except BlockingIOError:
                        continue
                    except BrokenPipeError:
                        return {}
                    if written == len(encoded):
                        selector.unregister(process.stdin)
                        process.stdin.close()
                if key.fileobj is process.stdout and ready & selectors.EVENT_READ:
                    chunk = os.read(process.stdout.fileno(), min(1024, MAX_TOOL_RESULT_BYTES + 1 - len(output)))
                    if not chunk:
                        stdout_closed = True
                        continue
                    output.extend(chunk)
                    if len(output) > MAX_TOOL_RESULT_BYTES:
                        return {}
            if stdout_closed:
                break
        if process.wait(timeout=max(0.1, deadline - time.monotonic())) != 0:
            return {}
        result = json.loads(output)
        return result if isinstance(result, dict) else {}
    except Exception:
        return {}
    finally:
        if selector is not None:
            selector.close()
        if process is not None:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.wait(timeout=1)
            if process.stdin is not None and not process.stdin.closed:
                process.stdin.close()
            if process.stdout is not None:
                process.stdout.close()


def _valid_context(value: Any) -> bool:
    if not isinstance(value, str) or len(value) > 1600:
        return False
    lines = value.split("\n")
    if not 2 <= len(lines) <= 7:
        return False
    if lines[0] == _JEV_HEADER:
        if len(lines) < 3 or _RECEIPT.fullmatch(lines[1]) is None:
            return False
        entries = lines[2:]
    elif lines[0] == _CONTEXT_HEADER:
        entries = lines[1:]
    else:
        return False
    for line in entries:
        if not line.startswith("- ") or _ID.fullmatch(line[2:]) is None:
            return False
        relative = line[2:].split(":", 1)[1]
        if relative.startswith("/") or any(part in ("", ".", "..") for part in relative.split("/")):
            return False
    return True


def prepare_context(user_message: Any, *, cwd: Path | str | None = None,
                    runner: Callable[[dict[str, str]], dict[str, Any]] | None = None) -> dict[str, str] | None:
    if not isinstance(user_message, str) or not 60 <= len(user_message) <= 16_000 or _ELIGIBLE.search(user_message) is None:
        return None
    try:
        result = (runner or _run_child)({"user_message": user_message, "cwd": str(cwd or os.getcwd())})
        context = result.get("context") if isinstance(result, dict) else None
        return {"context": context} if _valid_context(context) else None
    except Exception:
        return None


def pre_llm_call(user_message: Any = None, **kwargs: Any) -> dict[str, str] | None:
    return prepare_context(user_message, cwd=kwargs.get("cwd"))


def _tool_schema(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required,
                           "additionalProperties": False}}


_WORKSPACE = {"type": "string", "description": "Absolute workspace path reviewed in Jev setup."}
_CLASSIFICATION = {"type": "string", "enum": ["public", "internal-minimized", "restricted"]}
# Path and goal limits are deliberately narrower than direct MCP. Reduce text
# remains useful under the default sieve policy; its serialized JSON envelope
# is independently bounded before crossing the Hermes child-process boundary.
_MCP_WORKSPACE = {"type": "string", "minLength": 1, "maxLength": MAX_CONTEXT_WORKSPACE_PATH_CHARS}
_MCP_GOAL = {"type": "string", "minLength": 1, "maxLength": MAX_CONTEXT_GOAL_CHARS}
_TOOL_SCHEMAS = (
    _tool_schema("jev_setup", "Open the private local Jev setup or scope-review wizard; never put a key in chat.",
                 {"workspace_path": _WORKSPACE}, ["workspace_path"]),
    _tool_schema("jev_auto_status", "Read Jev provider readiness and local budget counters without a model call.",
                 {"workspace_path": _WORKSPACE}, ["workspace_path"]),
    _tool_schema("jev_prepare", "Prepare a compact file/optional-guidance shortlist for a narrow task. Mandatory instructions and SLM are unchanged.",
                 {"workspace_path": _MCP_WORKSPACE, "goal": _MCP_GOAL}, ["workspace_path", "goal"]),
    _tool_schema("jev_reduce", "Select relevant blocks from supplied text, keeping omissions exactly recoverable. Never pass secrets.",
                  {"workspace_path": _MCP_WORKSPACE, "goal": _MCP_GOAL,
                  "text": {"type": "string", "maxLength": MAX_CONTEXT_REDUCE_TEXT_CHARS,
                           "description": "Up to 20,000 characters. The Hermes serialized request envelope is capped at 32 KiB, so non-ASCII or heavily escaped text may be rejected earlier."}},
                 ["workspace_path", "goal", "text"]),
    _tool_schema("jev_recall", "Fetch a local receipt or exact omitted line range. No model inference or cloud request.",
                 {"workspace_path": _MCP_WORKSPACE,
                  "receipt_id": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
                  "start": {"type": "integer", "minimum": 1, "maximum": MAX_CONTEXT_RECALL_LINE},
                  "end": {"type": "integer", "minimum": 1, "maximum": MAX_CONTEXT_RECALL_LINE}},
                 ["workspace_path", "receipt_id"]),
    _tool_schema("jev_route", "Ask Jev to advise on one task, tool, or skill from a closed shortlist. Does not execute it.",
                 {"workspace_path": _WORKSPACE, "kind": {"type": "string", "enum": ["task", "tool", "skill"]},
                  "task": {"type": "string"}, "candidates": {"type": "array", "items": {"type": "object"}},
                  "data_classification": _CLASSIFICATION},
                 ["workspace_path", "kind", "task", "candidates", "data_classification"]),
    _tool_schema("jev_recipe_catalog", "List reviewed Jev use-case recipes without a provider call.", {}, []),
    _tool_schema("jev_recipe_try", "Try one enrolled recipe on explicitly supplied input; advisory only.",
                 {"workspace_path": _WORKSPACE, "recipe_id": {"type": "string"},
                  "input": {"type": "object"}, "data_classification": _CLASSIFICATION},
                 ["workspace_path", "recipe_id", "input", "data_classification"]),
    _tool_schema("jev_review_diff", "Ask Jev for advisory code-review focus; tests and independent review still required.",
                 {"workspace_path": _WORKSPACE, "goal": {"type": "string"},
                  "diff": {"type": "string"}, "data_classification": _CLASSIFICATION},
                 ["workspace_path", "goal", "diff", "data_classification"]),
)


def _native_tool(name: str, args: dict[str, Any], **_kwargs: Any) -> str:
    if not isinstance(args, dict):
        return json.dumps({"error": "HERMES_TOOL_ARGUMENTS"})
    result = _run_child({"name": name, "arguments": args}, launcher=_TOOL_LAUNCHER)
    return json.dumps(result or {"error": "HERMES_TOOL_UNAVAILABLE"}, separators=(",", ":"), ensure_ascii=True)


def register(ctx: Any) -> None:
    for schema in _TOOL_SCHEMAS:
        name = schema["name"]
        ctx.register_tool(name=name, toolset="qualixar_jev", schema=schema,
                          handler=lambda args, _name=name, **kwargs: _native_tool(_name, args, **kwargs))
    for folder in sorted((_PLUGIN_ROOT / "skills").iterdir()):
        skill = folder / "SKILL.md"
        if folder.is_dir() and skill.is_file():
            ctx.register_skill(folder.name, skill)
    ctx.register_hook("pre_llm_call", pre_llm_call)
