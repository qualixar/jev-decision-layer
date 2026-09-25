"""VS Code host adapter: register this layer as a workspace MCP server.

WHY VS CODE IS DIFFERENT FROM THE OTHER FOUR HOSTS
--------------------------------------------------
Codex, Antigravity, Hermes and Claude Code each expose a hook or tool surface
this package can ship a file into. VS Code does not. What it does expose is a
workspace MCP registration at `.vscode/mcp.json`, which Copilot agent mode
reads, so that is the adapter: the same stdio launcher the other hosts use,
declared where VS Code looks for it.

The file format is NOT the one the other hosts use. VS Code keys servers under
a top-level `servers` object; `mcpServers` is silently ignored there. Getting
this wrong produces no error and no server, which is the worst failure mode, so
the key is asserted in a test rather than trusted to review.

WRITING INTO SOMEONE ELSE'S WORKSPACE
-------------------------------------
`.vscode/mcp.json` belongs to the user, not to this package. Anything already
in it stays: the merge replaces exactly one named entry and touches nothing
else. A file that does not parse is never rewritten, because rewriting it would
discard settings this module cannot read. `plan()` is the default and writes
nothing; a caller has to ask for the write explicitly.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .common import AutoError

SERVER_NAME = "qualixar-jev"
CONFIG_RELPATH = Path(".vscode") / "mcp.json"
MAX_CONFIG_BYTES = 256_000


def launcher_path() -> Path:
    """The stdio entry point, resolved absolutely: VS Code expands no variables."""
    path = Path(__file__).resolve().parents[2] / "scripts" / "launch-jev"
    if not path.is_file():
        raise AutoError("VSCODE_LAUNCHER_MISSING")
    return path


def server_entry(launcher: Path | None = None) -> dict[str, Any]:
    return {"type": "stdio", "command": str(launcher or launcher_path()), "args": [], "env": {}}


def render(launcher: Path | None = None) -> dict[str, Any]:
    """A complete, minimal `.vscode/mcp.json` for a workspace with no other servers."""
    return {"servers": {SERVER_NAME: server_entry(launcher)}}


def _read(config: Path) -> dict[str, Any] | None:
    if not config.exists():
        return None
    if config.is_symlink() or not config.is_file() or config.stat().st_size > MAX_CONFIG_BYTES:
        raise AutoError("VSCODE_CONFIG_UNREADABLE")
    try:
        document = json.loads(config.read_text())
    except (OSError, ValueError):
        # Refusing here keeps a hand-edited file intact. Overwriting it would
        # silently drop servers the user configured.
        raise AutoError("VSCODE_CONFIG_UNPARSEABLE") from None
    if not isinstance(document, dict):
        raise AutoError("VSCODE_CONFIG_UNPARSEABLE")
    return document


def merge(existing: dict[str, Any] | None, launcher: Path | None = None) -> dict[str, Any]:
    """Add or update exactly one server. Every other key is carried through."""
    document = dict(existing or {})
    servers = dict(document.get("servers") or {})
    if not isinstance(document.get("servers", {}), dict):
        raise AutoError("VSCODE_CONFIG_UNPARSEABLE")
    servers[SERVER_NAME] = server_entry(launcher)
    document["servers"] = servers
    return document


def plan(workspace: Path, launcher: Path | None = None) -> dict[str, Any]:
    """What a write would do. Reads the workspace; changes nothing."""
    config = Path(workspace) / CONFIG_RELPATH
    existing = _read(config)
    current = (existing or {}).get("servers", {}).get(SERVER_NAME) if existing else None
    proposed = merge(existing, launcher)
    return {
        "host": "vscode",
        "config_path": str(config),
        "config_exists": existing is not None,
        "action": "unchanged" if current == proposed["servers"][SERVER_NAME]
                  else "update" if current is not None
                  else "create" if existing is None else "add",
        "preserved_servers": sorted(set((existing or {}).get("servers", {})) - {SERVER_NAME}),
        "document": proposed,
        "written": False,
    }


def install(workspace: Path, launcher: Path | None = None) -> dict[str, Any]:
    """Apply the plan. Creates `.vscode/` if absent; never follows a symlink."""
    outcome = plan(workspace, launcher)
    config = Path(outcome["config_path"])
    if outcome["action"] == "unchanged":
        return outcome
    directory = config.parent
    if directory.is_symlink():
        raise AutoError("VSCODE_CONFIG_UNREADABLE")
    directory.mkdir(parents=True, exist_ok=True)
    if config.is_symlink():
        raise AutoError("VSCODE_CONFIG_UNREADABLE")
    config.write_text(json.dumps(outcome["document"], indent=2, sort_keys=True) + "\n")
    return {**outcome, "written": True}
