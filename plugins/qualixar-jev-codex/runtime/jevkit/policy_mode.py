"""Deprecated local advisory compatibility surface for the Jev 1.0 plugin."""
from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Any

from .security import private_dir, screen


POLICY_MODES = ("off", "assist")
DEFAULT_POLICY_MODE = "assist"
_RETIRED_POLICY_MODES = frozenset({"enforce"})
_SENSITIVE = frozenset(
    {
        "API_KEY",
        "BEARER_TOKEN",
        "CREDENTIAL",
        "CREDENTIAL_ASSIGNMENT",
        "CREDENTIAL_URL",
        "JWT",
        "PRIVATE_KEY",
    }
)
_ROUTES = (
    ("06-injection-triage", (r"prompt injection", r"suspicious instruction", r"untrusted instruction")),
    ("07-claim-verification", (r"verify (?:this )?claim", r"fact[- ]?check", r"claim against")),
    ("08-completion-gate", (r"completion gate", r"acceptance evidence", r"is (?:this|the work) complete")),
    ("09-patch-review", (r"review (?:this )?patch", r"patch risk", r"diff risk")),
    ("10-semantic-lint", (r"semantic lint", r"meaning[- ]level contradiction", r"contradiction")),
    ("11-failure-classification", (r"classify (?:this )?failure", r"failure category", r"root cause route")),
    ("13-issue-triage", (r"triage (?:this )?issue", r"issue priority", r"issue severity")),
    ("14-incident-triage", (r"triage (?:this )?incident", r"incident priority", r"incident severity")),
    ("15-test-selection", (r"select (?:the )?(?:right )?tests?", r"which tests?", r"test set")),
    ("16-documentation-drift", (r"documentation drift", r"docs? (?:still )?match", r"stale documentation")),
    ("17-security-review-routing", (r"security review route", r"route (?:this )?security", r"security finding")),
    ("18-support-triage", (r"support triage", r"route (?:this )?support", r"support request")),
    ("19-research-ranking", (r"rank (?:these )?sources?", r"best sources?", r"research ranking")),
    ("20-memory-admission", (r"save (?:this )?(?:to )?memory", r"memory admission", r"remember (?:this|that)")),
    ("04-file-ranking", (r"rank (?:these )?(?:candidate )?files?", r"relevant files?", r"which files?")),
    ("05-context-sieve", (r"context sieve", r"select (?:the )?context", r"keep relevant context")),
    ("01-skill-routing", (r"choose (?:the )?(?:best|right|smallest) skill", r"skill routing", r"which skill")),
    ("02-task-routing", (r"route (?:this )?task", r"task routing", r"which workflow")),
    ("03-tool-selection", (r"choose (?:the )?(?:best|right) tool", r"tool selection", r"which tool")),
    ("12-worker-routing", (r"choose (?:the )?(?:best|right) (?:worker|agent)", r"worker routing", r"which agent")),
)


def _config_root() -> Path:
    configured = os.environ.get("XDG_CONFIG_HOME")
    return (
        Path(configured).expanduser() / "qualixar-jev-decision-layer"
        if configured
        else Path.home() / ".config" / "qualixar-jev-decision-layer"
    )


def _advisory_mode(value: str | None) -> str:
    """Downgrade retired enforcement configuration to the safe advisory mode."""
    candidate = value.strip().lower() if isinstance(value, str) else ""
    if candidate in _RETIRED_POLICY_MODES:
        return DEFAULT_POLICY_MODE
    return candidate if candidate in POLICY_MODES else DEFAULT_POLICY_MODE


def policy_mode(config_root: Path | None = None) -> str:
    override = os.environ.get("QUALIXAR_JEV_POLICY_MODE")
    if override is not None:
        return _advisory_mode(override)
    path = (config_root or _config_root()) / "policy-mode"
    try:
        candidate = path.read_text().strip().lower()
    except (OSError, UnicodeError):
        return DEFAULT_POLICY_MODE
    return _advisory_mode(candidate)


def write_policy_mode(mode: str, config_root: Path | None = None, *, overwrite: bool = True) -> Path:
    candidate = mode.strip().lower()
    if candidate not in POLICY_MODES:
        raise ValueError("INVALID_POLICY_MODE")
    root = config_root or _config_root()
    private_dir(root)
    path = root / "policy-mode"
    if path.is_symlink():
        raise ValueError("UNSAFE_POLICY_PATH")
    if path.exists() and not overwrite:
        return path
    descriptor, temporary_name = tempfile.mkstemp(prefix="policy-mode.tmp-", dir=root)
    temporary = Path(temporary_name)
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            descriptor = -1
            stream.write(candidate + "\n")
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary.exists():
            temporary.unlink()
    return path


def classify_intent(intent: str, *, mode: str | None = None) -> dict[str, Any]:
    selected_mode = _advisory_mode(mode) if mode is not None else policy_mode()
    if not isinstance(intent, str) or not intent.strip():
        return {"status": "SKIP", "case_id": None, "reason_code": "EMPTY_INTENT"}
    _cleaned, findings = screen(intent)
    if _SENSITIVE.intersection(findings):
        return {"status": "BLOCK", "case_id": None, "reason_code": "SENSITIVE_INPUT"}
    if selected_mode == "off":
        return {"status": "SKIP", "case_id": None, "reason_code": "POLICY_OFF"}
    normalized = " ".join(intent.lower().split())
    for case_id, patterns in _ROUTES:
        if any(re.search(pattern, normalized) for pattern in patterns):
            return {
                "status": "SUGGEST",
                "case_id": case_id,
                "reason_code": "BOUNDED_SEMANTIC_DECISION",
            }
    return {"status": "SKIP", "case_id": None, "reason_code": "DETERMINISTIC_OR_UNMATCHED"}


def policy_status(config_root: Path | None = None) -> dict[str, Any]:
    return {
        "version": "1.0.0",
        "mode": policy_mode(config_root),
        "default_mode": DEFAULT_POLICY_MODE,
        "modes": list(POLICY_MODES),
        "retired_modes": sorted(_RETIRED_POLICY_MODES),
        "deprecated": True,
        "enforcement_active": False,
        "local_classifier": True,
        "classifier_external_calls": 0,
        "prompt_text_persisted": False,
        "governed_tools": [],
        "execution_authorized": False,
    }


def handle_hook_event(event: dict[str, Any], *, data_root: Path, mode: str | None = None) -> dict[str, Any] | None:
    """Return legacy advisory context without recording or denying tool activity.

    The v1 plugin routes hooks through ``jev_auto.hooks``.  This retained
    compatibility entry point must never turn old configuration or a stale
    ledger into a Codex permission decision.
    """
    del data_root
    if not isinstance(event, dict):
        return None
    if event.get("hook_event_name") != "UserPromptSubmit":
        return None
    prompt = event.get("prompt")
    if not isinstance(prompt, str) or len(prompt.encode("utf-8")) > 64_000:
        return None
    decision = classify_intent(prompt, mode=mode)
    if decision["status"] == "SKIP":
        return None
    if decision["status"] == "BLOCK":
        context = (
            "Jev Policy Mode advisory: sensitive material was detected locally; do not send "
            "this state to Jev. Derive a reviewed synthetic or public representation first."
        )
    else:
        context = (
            f"Jev Policy Mode advisory: SUGGEST case={decision['case_id']}. Use jev_describe, "
            "minimize and classify the state, then use jev_evaluate only with a valid workspace "
            "grant. Jev advice never authorizes execution."
        )
    return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": context}}
