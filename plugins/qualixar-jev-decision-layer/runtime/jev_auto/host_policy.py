"""Read-only notice: can a Claude Code organization policy stop the Jev plugin here?

Claude Code, not Jev, decides which managed settings source applies on a
machine and how several sources combine. This module does not repeat that
logic. It reads the documented managed sources and reports which of them set
a key that can stop the Jev plugin from running on its own in Claude Code.

It returns fixed codes, a label and location for each source, and fixed
advice. It never returns any other value from a policy document.

When no source restricts Jev it returns None, so on an ordinary computer the
output of every caller is unchanged.
"""

from __future__ import annotations

import getpass
import json
import os
import plistlib
import re
import sys
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

PLUGIN_ID = "qualixar-jev-decision-layer@qualixar"
SERVER_NAME = "qualixar-jev"
MARKETPLACE_SOURCE = {"source": "github", "repo": "qualixar/jev-decision-layer"}
_REPO = "qualixar/jev-decision-layer"
_OWNER_WILDCARD = "qualixar/*"
_REPO_URL = "https://github.com/qualixar/jev-decision-layer"
_LAUNCHERS = {"launch-jev", "launch-jev.cmd"}

_MAX_BYTES = 1_048_576
_MAX_DROP_INS = 64
_MAX_SCANNED = 1024
_MAX_PATTERN = 256
_PLIST_NAME = "com.anthropic.claudecode.plist"
_REGISTRY_KEY = r"SOFTWARE\Policies\ClaudeCode"

SERVER_CACHE = "server-managed settings (cached copy)"
MDM_PROFILE = "MDM configuration profile"
MANAGED_FILE = "managed settings file"
REGISTRY = "registry policy"

_EFFECTS = {
    "PLUGIN_DISABLED": "The Jev plugin is turned off for Claude Code.",
    "PLUGIN_HOOKS_BLOCKED": "Plugin hooks do not run, so Claude Code sessions get no automatic Jev guidance.",
    "MARKETPLACE_NOT_ALLOWED": ("The Jev marketplace is not on the allowed list, so the plugin and its "
                                "commands and hooks do not load in Claude Code."),
    "MARKETPLACE_BLOCKED": "The Jev marketplace is on the blocked list, so the plugin does not load in Claude Code.",
    "MCP_SERVER_NOT_ALLOWED": ("The MCP server allowlist does not admit qualixar-jev, so Claude Code may not "
                               "start the plugin's Jev tools. Servers the Claude desktop app delivers itself "
                               "are not decided by this list."),
}
_ORDER = tuple(_EFFECTS)

Source = tuple[str, str, Mapping[str, Any]]


# ---------------------------------------------------------------------------
# Reading the documented sources
# ---------------------------------------------------------------------------


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        if not path.is_file() or path.stat().st_size > _MAX_BYTES:
            return None
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, RecursionError):
        return None
    return document if isinstance(document, dict) else None


def _read_plist(path: Path) -> dict[str, Any] | None:
    try:
        if not path.is_file() or path.stat().st_size > _MAX_BYTES:
            return None
        with path.open("rb") as stream:
            document = plistlib.load(stream)
    except Exception:  # plistlib raises several unrelated types on bad input
        return None
    return document if isinstance(document, dict) else None


def system_directory(platform: str = sys.platform) -> Path | None:
    """The managed settings directory Claude Code documents for each platform."""
    if platform == "darwin":
        return Path("/Library/Application Support/ClaudeCode")
    if platform.startswith("linux"):
        return Path("/etc/claude-code")
    if platform == "win32":
        return Path(r"C:\Program Files\ClaudeCode")
    return None


def _managed_files(directory: Path) -> list[Source]:
    """managed-settings.json, then managed-settings.d/*.json in name order."""
    found: list[Source] = []
    paths = [directory / "managed-settings.json"]
    try:
        names: list[str] = []
        with os.scandir(directory / "managed-settings.d") as entries:
            for scanned, entry in enumerate(entries, 1):
                if scanned > _MAX_SCANNED:
                    # Read no drop-in rather than an arbitrary subset.
                    names = []
                    break
                if entry.name.endswith(".json") and not entry.name.startswith("."):
                    names.append(entry.name)
        paths += [directory / "managed-settings.d" / name for name in sorted(names)[:_MAX_DROP_INS]]
    except OSError:
        pass
    for path in paths:
        document = _read_json(path)
        if document is not None:
            found.append((MANAGED_FILE, str(path), document))
    return found


def _config_directories(environ: Mapping[str, str], home: Path) -> list[Path]:
    directories: list[Path] = []
    configured = environ.get("CLAUDE_CONFIG_DIR")
    if isinstance(configured, str) and configured and os.path.isabs(configured):
        directories.append(Path(configured))
    directories.append(home / ".claude")
    unique: dict[str, Path] = {}
    for directory in directories:
        unique.setdefault(os.path.normcase(os.path.normpath(str(directory))), directory)
    return list(unique.values())


def _server_caches(environ: Mapping[str, str], home: Path) -> list[Source]:
    found: list[Source] = []
    for directory in _config_directories(environ, home):
        path = directory / "remote-settings.json"
        document = _read_json(path)
        if document is not None:
            found.append((SERVER_CACHE, str(path), document))
    return found


def _mdm_profiles(base: Path, user: str | None) -> list[Source]:
    paths = []
    if user and user not in {".", ".."} and "/" not in user and "\\" not in user:
        paths.append(base / user / _PLIST_NAME)
    paths.append(base / _PLIST_NAME)
    found: list[Source] = []
    for path in paths:
        document = _read_plist(path)
        if document is not None:
            found.append((MDM_PROFILE, str(path), document))
    return found


def _registry_values() -> list[tuple[str, object]]:
    try:
        import winreg  # type: ignore[import-not-found]
    except ImportError:
        return []
    values: list[tuple[str, object]] = []
    for hive, name in ((winreg.HKEY_LOCAL_MACHINE, "HKLM"), (winreg.HKEY_CURRENT_USER, "HKCU")):
        try:
            with winreg.OpenKey(hive, _REGISTRY_KEY) as key:
                value, _kind = winreg.QueryValueEx(key, "Settings")
        except OSError:
            continue
        values.append((name + "\\" + _REGISTRY_KEY, value))
    return values


def _registry_policies(values: Iterable[tuple[str, object]]) -> list[Source]:
    found: list[Source] = []
    for where, value in values:
        if not isinstance(value, str) or len(value) > _MAX_BYTES:
            continue
        try:
            document = json.loads(value)
        except (ValueError, RecursionError):
            continue
        if isinstance(document, dict):
            found.append((REGISTRY, where, document))
    return found


def _current_user() -> str | None:
    try:
        return getpass.getuser()
    except Exception:  # getuser raises OSError or KeyError without a login name
        return None


def discover(*, platform: str = sys.platform, environ: Mapping[str, str] | None = None,
             home: Path | None = None, system: Path | None = None,
             managed_preferences: Path = Path("/Library/Managed Preferences"),
             registry: Callable[[], Iterable[tuple[str, object]]] = _registry_values,
             user: Callable[[], str | None] = _current_user) -> list[Source]:
    """Every readable managed source, highest documented rank first."""
    environ = os.environ if environ is None else environ
    home = Path.home() if home is None else home
    found = _server_caches(environ, home)
    if platform == "darwin":
        found += _mdm_profiles(managed_preferences, user())
    if platform == "win32":
        found += _registry_policies(registry())
    directory = system if system is not None else system_directory(platform)
    if directory is not None:
        found += _managed_files(directory)
    return found


# ---------------------------------------------------------------------------
# Judging one source
# ---------------------------------------------------------------------------


def _admits_marketplace(entry: object) -> bool:
    if isinstance(entry, str):
        return entry.rstrip("/") == _REPO_URL
    if not isinstance(entry, dict):
        return False
    kind = entry.get("source")
    if kind == "github":
        # The public install pins neither a ref nor a path, and an entry that
        # pins one does not cover a source without it.
        repo = entry.get("repo")
        return "ref" not in entry and "path" not in entry and isinstance(repo, str) and repo in {_REPO, _OWNER_WILDCARD}
    if kind == "hostPattern":
        return _host_matches(entry.get("hostPattern"))
    return False


def _host_matches(pattern: object) -> bool:
    if not isinstance(pattern, str) or len(pattern) > _MAX_PATTERN:
        return False
    try:
        return re.search(pattern, "github.com") is not None
    except re.error:
        return False


def _canonical_url(url: str) -> str:
    value = url.strip().lower().rstrip("/")
    if value.startswith("git@github.com:"):
        value = "https://github.com/" + value[len("git@github.com:"):]
    return value[:-4] if value.endswith(".git") else value


def _blocks_marketplace(entry: object) -> bool:
    if not isinstance(entry, dict) or "ref" in entry or "path" in entry:
        return False
    kind = entry.get("source")
    if kind == "github":
        repo = entry.get("repo")
        return isinstance(repo, str) and repo.lower() in {_REPO, _OWNER_WILDCARD}
    if kind == "git" or kind == "url":
        url = entry.get("url")
        return isinstance(url, str) and _canonical_url(url) == _REPO_URL
    if kind == "hostPattern":
        return _host_matches(entry.get("hostPattern"))
    return False


def _command(entry: object) -> list[str] | None:
    """A well-formed serverCommand entry; Claude Code strips any other."""
    command = entry.get("serverCommand") if isinstance(entry, dict) else None
    if isinstance(command, list) and command and all(isinstance(part, str) for part in command):
        return command
    return None


def _admits_server(entries: list[object]) -> bool:
    commands = [command for command in map(_command, entries) if command is not None]
    if commands:
        # A stdio server must match a command entry exactly once any exists,
        # and the plugin starts its launcher with no arguments.
        return any(len(command) == 1 and re.split(r"[\\/]", command[0])[-1] in _LAUNCHERS
                   for command in commands)
    return any(entry == SERVER_NAME or (isinstance(entry, dict) and entry.get("serverName") == SERVER_NAME)
               for entry in entries)


def _allowlist(document: Mapping[str, Any], *keys: str) -> list[object] | None:
    """The allowlist in force: the first key set to a non-null value wins.

    Claude Code enforces a malformed allowlist value as an empty list, so it
    is returned as one. None means no allowlist is set.
    """
    for key in keys:
        value = document.get(key)
        if value is not None:
            return value if isinstance(value, list) else []
    return None


def findings(document: Mapping[str, Any]) -> list[str]:
    """The fixed codes one policy document triggers, in a stable order."""
    codes: list[str] = []
    plugins = document.get("enabledPlugins")
    state = plugins.get(PLUGIN_ID) if isinstance(plugins, dict) else None
    if state is False:
        codes.append("PLUGIN_DISABLED")
    elif document.get("allowManagedHooksOnly") is True and state is not True:
        # Hooks of a plugin force-enabled in managed enabledPlugins are exempt.
        codes.append("PLUGIN_HOOKS_BLOCKED")
    allowed = _allowlist(document, "strictKnownMarketplaces", "allowedMarketplaces")
    if allowed is not None and not any(_admits_marketplace(entry) for entry in allowed):
        codes.append("MARKETPLACE_NOT_ALLOWED")
    blocked = document.get("blockedMarketplaces")
    if isinstance(blocked, list) and any(_blocks_marketplace(entry) for entry in blocked):
        codes.append("MARKETPLACE_BLOCKED")
    servers = _allowlist(document, "allowedMcpServers")
    if servers is not None and not _admits_server(servers):
        codes.append("MCP_SERVER_NOT_ALLOWED")
    return codes


# ---------------------------------------------------------------------------
# The notice
# ---------------------------------------------------------------------------


def _display(where: str, home: Path) -> str:
    prefix = str(home).rstrip("/\\")
    if prefix and (where == prefix or where.startswith(prefix + os.sep)):
        return "~" + where[len(prefix):]
    return where


def _administrator_request(seen: set[str]) -> str:
    asks = []
    if seen & {"MARKETPLACE_NOT_ALLOWED", "MARKETPLACE_BLOCKED"}:
        asks.append("allow the marketplace " + json.dumps(MARKETPLACE_SOURCE, separators=(", ", ": ")))
    if seen & {"PLUGIN_HOOKS_BLOCKED", "PLUGIN_DISABLED"}:
        asks.append("force-enable " + PLUGIN_ID + " in managed enabledPlugins "
                    "(hooks of a force-enabled plugin still run under allowManagedHooksOnly)")
    if "MCP_SERVER_NOT_ALLOWED" in seen:
        asks.append("admit the " + SERVER_NAME + " server in allowedMcpServers")
    return "Ask your Claude Code administrator to " + "; ".join(asks) + "."


def notice(sources: Iterable[Source] | None = None, *, home: Path | None = None) -> dict[str, Any] | None:
    """Explain what a managed policy stops, or return None when nothing does."""
    try:
        listed = list(discover() if sources is None else sources)
        home = Path.home() if home is None else home
    except Exception:  # advisory only: an unreadable policy must never break a caller
        return None
    affected: list[dict[str, Any]] = []
    for source in listed:
        try:
            label, where, document = source
            codes = findings(document) if isinstance(document, Mapping) else []
            location = _display(where, home)
        except Exception:  # one malformed source must not hide the others
            continue
        if codes:
            affected.append({"source": label, "location": location, "codes": codes})
    if not affected:
        return None
    seen = {code for source in affected for code in source["codes"]}
    return {
        "summary": "A Claude Code organization policy on this computer may stop the Jev plugin from running on its own in Claude Code.",
        "sources": affected,
        "effects": [_EFFECTS[code] for code in _ORDER if code in seen],
        "unaffected": "Codex, VS Code, Antigravity and Hermes are not affected, and your Jev grant is unchanged.",
        "options": [
            _administrator_request(seen),
            ("Until then, in the Claude desktop app: quit it, run the plugin's "
             "`scripts/jev host-register --host claude-desktop --write`, "
             "and add the Jev section from HOSTS.md (Enterprise-managed Claude Code) to your user CLAUDE.md "
             "so Claude calls the Jev tools without hooks."),
        ],
        "check": "Claude Code decides which managed source applies. Run `claude doctor` or `/status` in Claude Code to see it.",
    }
