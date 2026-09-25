"""Register this layer as an MCP server, in whichever shape a host expects.

WHY ONE MODULE FOR THREE HOSTS
------------------------------
Codex, Claude Code, Antigravity and Hermes each read a hook or tool surface
this package ships a file into. Three other surfaces take MCP registration
instead, and they disagree about the shape of it in ways that fail silently:

    host            file                                  key           `type`
    vscode          <workspace>/.vscode/mcp.json          servers       yes
    antigravity     ~/.gemini/config/mcp_config.json      mcpServers    no
    claude-desktop  ~/Library/.../claude_desktop_config.json  mcpServers  no

VS Code ignores `mcpServers` without an error; the Claude desktop config
rejects a `type` field it does not expect. Both wrong shapes produce no server
and no diagnostic, which is the worst failure mode there is, so the shapes live
in one table with a test per row rather than in three hand-written files.

WRITING INTO SOMEONE ELSE'S CONFIG
----------------------------------
None of these files belong to this package. Anything already in one stays: the
merge replaces exactly one named entry and touches nothing else. A file that
does not parse is never rewritten, because rewriting it would discard settings
this module cannot read. `plan()` is the default and writes nothing; a caller
has to ask for the write explicitly.

The write is atomic. Checking `is_symlink` and then calling `write_text` leaves
a window in which the path can be swapped for a link pointing somewhere else,
and a crash mid-write would leave a truncated config behind.

**A running host may overwrite the file underneath you.** The Claude desktop
app holds its config in memory and flushes it on exit, silently discarding an
external edit made while it was running. Register before starting the host, or
use that host's own settings UI.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .common import AutoError

SERVER_NAME = "qualixar-jev"
MAX_CONFIG_BYTES = 256_000


@dataclass(frozen=True)
class Target:
    host: str
    key: str
    include_type: bool
    workspace_relative: Path | None = None
    user_path: Path | None = None

    def config_path(self, workspace: Path | None) -> Path:
        if self.workspace_relative is not None:
            if workspace is None:
                raise AutoError("HOST_MCP_WORKSPACE_REQUIRED")
            return Path(workspace) / self.workspace_relative
        return Path(self.user_path).expanduser()


TARGETS = {
    "vscode": Target("vscode", "servers", True, workspace_relative=Path(".vscode") / "mcp.json"),
    "antigravity": Target("antigravity", "mcpServers", False,
                          user_path=Path("~/.gemini/config/mcp_config.json")),
    "claude-desktop": Target("claude-desktop", "mcpServers", False,
                             user_path=Path("~/Library/Application Support/Claude/claude_desktop_config.json")),
}


def launcher_path() -> Path:
    """The stdio entry point, resolved absolutely: no host expands our variables."""
    path = Path(__file__).resolve().parents[2] / "scripts" / "launch-jev"
    if not path.is_file():
        raise AutoError("HOST_MCP_LAUNCHER_MISSING")
    return path


def _target(host: str) -> Target:
    target = TARGETS.get(host)
    if target is None:
        raise AutoError("HOST_MCP_TARGET_UNKNOWN")
    return target


def server_entry(host: str, launcher: Path | None = None) -> dict[str, Any]:
    target = _target(host)
    entry: dict[str, Any] = {"command": str(launcher or launcher_path()), "args": [], "env": {}}
    if target.include_type:
        entry = {"type": "stdio", **entry}
    return entry


def render(host: str, launcher: Path | None = None) -> dict[str, Any]:
    """A complete, minimal config for a host with no other servers."""
    return {_target(host).key: {SERVER_NAME: server_entry(host, launcher)}}


def _read(config: Path) -> dict[str, Any] | None:
    if not config.exists():
        return None
    if config.is_symlink() or not config.is_file() or config.stat().st_size > MAX_CONFIG_BYTES:
        raise AutoError("HOST_MCP_CONFIG_UNREADABLE")
    try:
        document = json.loads(config.read_text())
    except (OSError, ValueError):
        # Refusing here keeps a hand-edited file intact. Overwriting it would
        # silently drop servers the user configured.
        raise AutoError("HOST_MCP_CONFIG_UNPARSEABLE") from None
    if not isinstance(document, dict):
        raise AutoError("HOST_MCP_CONFIG_UNPARSEABLE")
    return document


def merge(host: str, existing: dict[str, Any] | None, launcher: Path | None = None) -> dict[str, Any]:
    """Add or update exactly one server. Every other key is carried through."""
    target = _target(host)
    if existing is not None and not isinstance(existing, dict):
        raise AutoError("HOST_MCP_CONFIG_UNPARSEABLE")
    document = dict(existing or {})
    # Absent is fine — we create it. Present but not an object is not ours to
    # reinterpret, and that includes an explicit null.
    servers = document.get(target.key, {}) if target.key in document else {}
    if not isinstance(servers, dict):
        raise AutoError("HOST_MCP_CONFIG_UNPARSEABLE")
    servers = dict(servers)
    servers[SERVER_NAME] = server_entry(host, launcher)
    document[target.key] = servers
    return document


def plan(host: str, workspace: Path | None = None, launcher: Path | None = None) -> dict[str, Any]:
    """What a write would do. Reads the config; changes nothing."""
    target = _target(host)
    config = target.config_path(workspace)
    existing = _read(config)
    proposed = merge(host, existing, launcher)     # validates the key before anything reads it
    present = (existing or {}).get(target.key, {}) if existing else {}
    current = present.get(SERVER_NAME) if isinstance(present, dict) else None
    entry = proposed[target.key][SERVER_NAME]
    return {
        "host": host,
        "config_path": str(config),
        "config_key": target.key,
        "config_exists": existing is not None,
        "action": "unchanged" if current == entry
                  else "update" if current is not None
                  else "create" if existing is None else "add",
        "preserved_servers": sorted(set(present) - {SERVER_NAME}) if isinstance(present, dict) else [],
        # Anything under our own name is about to be discarded. Surfacing it
        # lets a caller show the user what they are losing before it happens.
        "replaced_entry": _redacted(current) if current != entry else None,
        # The merged document is deliberately NOT returned. These configs hold
        # other servers' `env` blocks, and those hold API keys: a caller that
        # prints a plan would print every secret in the file. Only our own
        # entry, which contains nothing secret, is shown.
        "entry": entry,
        "written": False,
    }


def _redacted(entry: Any) -> Any:
    """An entry under our own name may still carry a user's secret in `env`."""
    if not isinstance(entry, dict):
        return entry
    environment = entry.get("env")
    if not isinstance(environment, dict) or not environment:
        return entry
    return {**entry, "env": {name: "[REDACTED]" for name in environment}}


def install(host: str, workspace: Path | None = None, launcher: Path | None = None) -> dict[str, Any]:
    """Apply the plan. Creates the parent directory; never follows a symlink."""
    outcome = plan(host, workspace, launcher)
    config = Path(outcome["config_path"])
    if outcome["action"] == "unchanged":
        return outcome
    document = merge(host, _read(config), launcher)
    directory = config.parent
    if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
        raise AutoError("HOST_MCP_CONFIG_UNREADABLE")
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise AutoError("HOST_MCP_CONFIG_UNREADABLE") from None
    if config.is_symlink():
        raise AutoError("HOST_MCP_CONFIG_UNREADABLE")
    _write_atomically(config, json.dumps(document, indent=2, sort_keys=True) + "\n")
    return {**outcome, "written": True}


def _write_atomically(config: Path, payload: str) -> None:
    handle, temporary = tempfile.mkstemp(dir=str(config.parent), prefix=".jev-", suffix=".json")
    try:
        with os.fdopen(handle, "w") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, config)
    except OSError:
        raise AutoError("HOST_MCP_CONFIG_UNREADABLE") from None
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
