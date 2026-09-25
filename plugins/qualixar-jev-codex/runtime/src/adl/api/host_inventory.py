"""Read-only host discovery for a truthful shared settings surface.

Presence is not native adapter conformance. This inventory never executes a
detected binary or changes any host configuration.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Callable


_HOSTS = (
    ("codex", "Codex", ("codex",), ("/Applications/Codex.app", "/Applications/ChatGPT.app")),
    ("claude_code", "Claude Code", ("claude",), ("/Applications/Claude.app",)),
    ("antigravity", "Google Antigravity", ("agy",), ("/Applications/Antigravity.app",)),
    ("hermes", "Hermes Agent", ("hermes", "hermes-agent"), ()),
    ("vscode", "VS Code built-in Copilot", ("code",), ("/Applications/Visual Studio Code.app",)),
)


def inventory_hosts(
    *,
    which: Callable[[str], str | None] = shutil.which,
    app_exists: Callable[[Path], bool] | None = None,
) -> list[dict[str, object]]:
    """Report detectable surfaces, never native verification or Auto readiness."""
    exists = app_exists if app_exists is not None else Path.is_dir
    rows: list[dict[str, object]] = []
    for host_id, label, commands, bundles in _HOSTS:
        cli = any(which(command) is not None for command in commands)
        app = any(exists(Path(bundle)) for bundle in bundles)
        source = "cli_and_app" if cli and app else "cli_path" if cli else "app_bundle" if app else "not_detected"
        rows.append({
            "id": host_id,
            "label": label,
            "detected": cli or app,
            "detection_source": source,
            "version": "NOT_MEASURED",
            "native_status": "NOT_RUN",
            "auto_mode_allowed": False,
        })
    return rows
