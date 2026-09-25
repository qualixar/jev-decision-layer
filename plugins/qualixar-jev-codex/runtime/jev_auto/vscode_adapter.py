"""VS Code host adapter.

VS Code exposes no hook surface, so the adapter registers the same stdio
launcher as a workspace MCP server in `.vscode/mcp.json`. That file keys
servers under `servers`; `mcpServers` is silently ignored there, producing no
error and no server.

The mechanics are shared with the other registration-only hosts and live in
`host_mcp`. This module is the VS Code view of it, so callers that only care
about VS Code do not have to pass a host name.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .host_mcp import SERVER_NAME, TARGETS, launcher_path
from .host_mcp import install as _install
from .host_mcp import merge as _merge
from .host_mcp import plan as _plan
from .host_mcp import render as _render
from .host_mcp import server_entry as _server_entry

HOST = "vscode"
CONFIG_RELPATH = TARGETS[HOST].workspace_relative
MAX_CONFIG_BYTES = 256_000

__all__ = ["SERVER_NAME", "CONFIG_RELPATH", "MAX_CONFIG_BYTES", "HOST",
           "launcher_path", "server_entry", "render", "merge", "plan", "install"]


def server_entry(launcher: Path | None = None) -> dict[str, Any]:
    return _server_entry(HOST, launcher)


def render(launcher: Path | None = None) -> dict[str, Any]:
    return _render(HOST, launcher)


def merge(existing: dict[str, Any] | None, launcher: Path | None = None) -> dict[str, Any]:
    return _merge(HOST, existing, launcher)


def plan(workspace: Path, launcher: Path | None = None) -> dict[str, Any]:
    return _plan(HOST, workspace, launcher)


def install(workspace: Path, launcher: Path | None = None) -> dict[str, Any]:
    return _install(HOST, workspace, launcher)
