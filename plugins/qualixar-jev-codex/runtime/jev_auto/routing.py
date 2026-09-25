"""Closed advisory routing questions for tasks, tools and skills."""

from __future__ import annotations

import re
from typing import Any

from .common import AutoError


_ID = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")
_KINDS = frozenset({"skill", "tool", "task"})


def compile_route(kind: str, task: str, candidates: Any) -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
    """Compile one Jev Choice; no tool or skill is executed by this function."""
    if not isinstance(kind, str) or kind not in _KINDS or not isinstance(task, str) or not 1 <= len(task.strip()) <= 4000:
        raise AutoError("ROUTE_INPUT_INVALID")
    if not isinstance(candidates, list) or not 2 <= len(candidates) <= 12:
        raise AutoError("ROUTE_CANDIDATES_INVALID")
    criteria: dict[str, str] = {}
    for candidate in candidates:
        if not isinstance(candidate, dict) or set(candidate) != {"id", "description"}:
            raise AutoError("ROUTE_CANDIDATES_INVALID")
        identifier = candidate["id"]
        description = candidate["description"]
        if (
            not isinstance(identifier, str)
            or not _ID.fullmatch(identifier)
            or identifier == "unknown"
            or identifier in criteria
            or not isinstance(description, str)
            or not 1 <= len(description.strip()) <= 250
        ):
            raise AutoError("ROUTE_CANDIDATES_INVALID")
        criteria[identifier] = description
    criteria["unknown"] = "None of the listed options is supported by the supplied task and evidence."
    state = {"task": task, "route_kind": kind}
    questions = {
        "selected": {
            "type": "choice",
            "instructions": f"Which listed {kind} best serves the task? Choose unknown if none fits. This is advice, not permission to execute.",
            "criteria": criteria,
        }
    }
    return state, questions
