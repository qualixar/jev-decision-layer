"""Advisory diff-risk triage over caller-supplied, screened text."""

from __future__ import annotations

from typing import Any

from .common import AutoError


def compile_review(goal: Any, diff: Any) -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
    if not isinstance(goal, str) or not 1 <= len(goal.strip()) <= 1000:
        raise AutoError("REVIEW_GOAL_INVALID")
    if (not isinstance(diff, str) or not 1 <= len(diff) <= 16_000
            or "--- " not in diff or "+++ " not in diff):
        raise AutoError("REVIEW_DIFF_INVALID")
    state = {"goal": goal, "diff": diff}
    questions: dict[str, dict[str, Any]] = {
        "risk": {
            "type": "score",
            "instructions": "How much independent code-review attention does the supplied change warrant? Evaluate only the shown diff; missing context is uncertainty.",
            "criteria": [
                "Documentation or cosmetic change with no visible behavior change",
                "Localized behavior change that needs targeted tests and review",
                "Sensitive or cross-cutting behavior change that needs deeper independent review",
            ],
        },
        "focus": {
            "type": "choice",
            "instructions": "Which review area deserves first attention from the supplied diff? Choose unknown if the diff lacks enough evidence.",
            "criteria": {
                "correctness": "Behavior or result correctness",
                "security": "Sensitive data, permissions or untrusted input",
                "compatibility": "Public interface or host compatibility",
                "test_gap": "Missing behavioral tests or verification",
                "documentation": "User-facing documentation consistency",
                "unknown": "The shown diff does not support a focused selection",
            },
        },
    }
    return state, questions
