"""Read-only, offline-first diagnosis for a Jev workspace installation.

The normal diagnostic deliberately avoids IPC and provider imports.  A user
can therefore inspect an installation before enrollment without starting a
broker, reserving a budget, or exposing workspace data.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import sys
from pathlib import Path
from typing import Any

from .common import AutoError, safe_path, state_dir
from .recipe_fixtures import selftest
from .settings import load_policy


RUNTIME = Path(__file__).resolve().parents[1]
MANIFEST = RUNTIME / "RUNTIME_MANIFEST.json"
_HASH = set("0123456789abcdef")


def _check(identifier: str, status: str, **fields: Any) -> dict[str, Any]:
    return {"id": identifier, "status": status, **fields}


def _valid_relative_path(name: Any) -> bool:
    candidate = Path(name) if isinstance(name, str) else None
    return bool(candidate and not candidate.is_absolute() and ".." not in candidate.parts and name != "")


def _runtime_manifest() -> dict[str, Any]:
    """Verify every sealed runtime file without loading provider code."""
    try:
        document = json.loads(MANIFEST.read_text(encoding="utf-8"))
        files = document.get("files") if isinstance(document, dict) else None
        if not isinstance(files, dict) or not files:
            raise ValueError("manifest files")
        for relative, expected in files.items():
            if (not _valid_relative_path(relative) or not isinstance(expected, str)
                    or len(expected) != 64 or set(expected) - _HASH):
                raise ValueError("manifest entry")
            target = safe_path(RUNTIME / relative)
            if target.is_dir() or not target.is_file():
                raise ValueError("missing runtime file")
            if hashlib.sha256(target.read_bytes()).hexdigest() != expected:
                raise ValueError("runtime hash")
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError, AutoError):
        return _check("runtime_manifest", "FAILED", code="RUNTIME_MANIFEST_INVALID")
    return _check("runtime_manifest", "PASS", detail="runtime files verified")


def _python() -> dict[str, Any]:
    if sys.version_info >= (3, 11):
        return _check("python", "PASS", version="%d.%d" % sys.version_info[:2])
    return _check("python", "FAILED", code="PYTHON_3_11_REQUIRED")


def _policy(path: Path) -> tuple[dict[str, Any], dict[str, Any] | None]:
    try:
        policy = load_policy(path)
    except AutoError as error:
        code = str(error)
        if code in {"WORKSPACE_NOT_ENROLLED", "AUTO_DISABLED_OR_EXPIRED"}:
            return _check("workspace_policy", "ACTION_REQUIRED", code=(
                "NOT_ENROLLED" if code == "WORKSPACE_NOT_ENROLLED" else "POLICY_EXPIRED"
            )), None
        return _check("workspace_policy", "FAILED", code=code), None
    return _check("workspace_policy", "PASS", provider=policy["provider"]), policy


def _offline_gate() -> dict[str, Any]:
    try:
        result = selftest()
    except Exception:
        return _check("offline_gate", "FAILED", code="FIXTURE_INTEGRITY_FAILURE")
    if result.get("all_passed") is True and isinstance(result.get("cases"), int):
        return _check("offline_gate", "PASS", cases=result["cases"])
    return _check(
        "offline_gate", "FAILED", code="FIXTURE_INTEGRITY_FAILURE",
        cases=result.get("cases", 0) if isinstance(result.get("cases"), int) else 0,
    )


def _receipt_index(path: Path) -> dict[str, Any]:
    """Count local evidence rows without constructing Store or reading receipt bodies."""
    try:
        database = safe_path(state_dir(path) / "auto.sqlite3")
        if not database.exists():
            return _check("receipt_index", "NONE", count=0)
        metadata = database.stat()
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1
                or metadata.st_mode & 0o077
                or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())):
            raise OSError("unsafe database")
        connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
        try:
            count = connection.execute("SELECT count(*) FROM evidence").fetchone()[0]
        finally:
            connection.close()
        if not isinstance(count, int) or count < 0:
            raise OSError("invalid evidence count")
    except (OSError, ValueError, sqlite3.Error, AutoError):
        return _check("receipt_index", "FAILED", code="RECEIPT_INDEX_UNAVAILABLE")
    return _check("receipt_index", "PRESENT" if count else "NONE", count=count)


def diagnose(path: Path) -> dict[str, Any]:
    """Return the stable public diagnostic schema without any provider activity."""
    policy_check, _policy_document = _policy(path)
    checks = [
        _runtime_manifest(),
        _python(),
        policy_check,
        _offline_gate(),
        _check("host_surface", "PORTABLE_RUNTIME", package="qualixar-jev-decision-layer",
               detail="portable runtime present; Codex installation and live host turn are not checked"),
        _receipt_index(path),
    ]
    failed = any(check["status"] == "FAILED" for check in checks)
    action = next((check for check in checks if check["status"] == "ACTION_REQUIRED"), None)
    overall = "FAILED" if failed else "ACTION_REQUIRED" if action else "READY"
    next_action = None
    if action:
        next_action = {
            "action": ("OPEN_PRIVATE_SETUP_WIZARD" if action["code"] == "NOT_ENROLLED"
                       else "REVIEW_PRIVATE_SETUP_WIZARD"),
            "instruction": "Use the jev_setup tool for this workspace; it opens the private setup wizard.",
            "reason": action["code"],
        }
    elif failed:
        next_action = {"command": "jev doctor --workspace " + str(path), "reason": "REPAIR_INSTALLATION"}
    return {
        "schema_version": 1,
        "mode": "offline",
        "overall": overall,
        "checks": checks,
        "next_action": next_action,
    }


def exit_code(result: dict[str, Any]) -> int:
    return 0 if result.get("overall") == "READY" else 2
