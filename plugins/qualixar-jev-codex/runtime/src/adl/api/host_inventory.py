"""Read-only host discovery for a truthful shared settings surface.

Presence is not native adapter conformance. This inventory never executes a
detected binary or changes any host configuration.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Callable


# Hosts for which this plugin actually SHIPS a native adapter. This is a
# statement about the package contents, not about a conformance run: it is
# derived from files in this repository, never from probing a host.
#   codex       -> hooks/hooks.json + jev_auto/hooks.py
#   antigravity -> plugin-root hooks.json (PreInvocation) + jev_auto/agy_hook.py
#   hermes      -> jev_auto/hermes_hook.py + jev_auto/hermes_tool.py
#   claude_code -> hooks/claude-hooks.json + jev_auto/claude_hook.py
#   vscode      -> jev_auto/vscode_adapter.py, writing .vscode/mcp.json
# VS Code exposes no hook surface, so its adapter registers the same stdio
# launcher as a workspace MCP server instead. That is a shipped adapter, not a
# conformance claim: `native_status` stays NOT_RUN like every other host.
_NATIVE_ADAPTERS = frozenset({"codex", "antigravity", "hermes", "claude_code", "vscode"})


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
            "native_adapter": host_id in _NATIVE_ADAPTERS,
            "auto_mode_allowed": False,
        })
    return rows
