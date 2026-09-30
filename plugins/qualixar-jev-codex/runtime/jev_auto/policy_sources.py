"""Where Claude Code's managed settings live, read without judging them.

The locations, the ranking of sources, and the way managed-settings.json
combines with its managed-settings.d drop-ins follow Claude Code's managed
settings documentation. Nothing here decides whether Jev is affected; that is
policy_rules.py. Every reader is bounded and returns nothing rather than
raising, because the notice built on top is advisory.
"""

from __future__ import annotations

import getpass
import json
import os
import plistlib
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, NamedTuple

MAX_BYTES = 1_048_576
MAX_DROP_INS = 64
MAX_SCANNED = 1024
MAX_PATH = 4096
LAUNCHCTL = "/bin/launchctl"
PLIST_NAME = "com.anthropic.claudecode.plist"
REGISTRY_KEY = r"SOFTWARE\Policies\ClaudeCode"
MARKETPLACE = "qualixar"

SERVER_CACHE = "server-managed settings (cached copy)"
MDM_PROFILE = "MDM configuration profile"
REGISTRY_HKLM = "registry policy (HKLM)"
MANAGED_FILE = "managed settings file"
REGISTRY_HKCU = "user registry policy (HKCU)"

# Documented rank within the managed tier: server-managed settings, then an
# MDM profile or the HKLM registry, then the managed settings files, then the
# user-writable HKCU registry.
RANK_SERVER, RANK_MDM, RANK_FILE, RANK_USER = 0, 1, 2, 3


class PolicySource(NamedTuple):
    label: str
    location: str
    document: Mapping[str, Any]
    rank: int


class Machine(NamedTuple):
    """Everything the rules need, read once."""

    sources: list[PolicySource]
    managed_mcp: str | None          # None, "defines-jev" or "excludes-jev"
    managed_mcp_location: str | None
    marketplace_source: Mapping[str, Any] | None
    user_mcp_allowlist: object       # the user's own allowedMcpServers value, or None
    config_dirs: list[Path]


# ---------------------------------------------------------------------------
# Bounded readers
# ---------------------------------------------------------------------------


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        if not path.is_file() or path.stat().st_size > MAX_BYTES:
            return None
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, RecursionError):
        return None
    return document if isinstance(document, dict) else None


def _read_plist(path: Path) -> dict[str, Any] | None:
    try:
        if not path.is_file() or path.stat().st_size > MAX_BYTES:
            return None
        with path.open("rb") as stream:
            document = plistlib.load(stream)
    except Exception:  # plistlib raises several unrelated types on bad input
        return None
    return document if isinstance(document, dict) else None


# ---------------------------------------------------------------------------
# The managed settings files and their drop-ins
# ---------------------------------------------------------------------------

_REPLACE_WHOLE = {"fallbackModel", "modelPicker"}
_REPLACE_BY_NAME = {"extraKnownMarketplaces", "managedMcpServers"}


def _key(value: object) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def _union(earlier: list[object], later: list[object]) -> list[object]:
    combined: list[object] = []
    seen: set[str] = set()
    for item in [*earlier, *later]:
        marker = _key(item)
        if marker not in seen:
            seen.add(marker)
            combined.append(item)
    return combined


def _combine(earlier: object, later: object) -> object:
    if isinstance(earlier, list) and isinstance(later, list):
        return _union(earlier, later)
    if isinstance(earlier, dict) and isinstance(later, dict):
        merged = dict(earlier)
        for key, value in later.items():
            merged[key] = _combine(merged[key], value) if key in merged else value
        return merged
    return later


def merge_documents(earlier: Mapping[str, Any], later: Mapping[str, Any]) -> dict[str, Any]:
    """Combine two managed settings files the way Claude Code documents.

    A later single value replaces an earlier one, lists combine without
    duplicates, nested blocks merge key by key, fallbackModel and modelPicker
    are replaced whole, and extraKnownMarketplaces and managedMcpServers
    entries with the same name are replaced whole.
    """
    merged = dict(earlier)
    for key, value in later.items():
        if key not in merged or key in _REPLACE_WHOLE:
            merged[key] = value
        elif key in _REPLACE_BY_NAME and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = _combine(merged[key], value)
    return merged


def _drop_in_names(directory: Path) -> list[str]:
    names: list[str] = []
    try:
        with os.scandir(directory) as entries:
            for scanned, entry in enumerate(entries, 1):
                if scanned > MAX_SCANNED:
                    return []  # read no drop-in rather than an arbitrary subset
                if entry.name.endswith(".json") and not entry.name.startswith("."):
                    names.append(entry.name)
    except OSError:
        return []
    return sorted(names)[:MAX_DROP_INS]


def managed_files(directory: Path) -> PolicySource | None:
    """managed-settings.json merged with its drop-ins in name order, as one source.

    The location is the directory: a drop-in's file name is the
    administrator's choice and can name a team or project.
    """
    documents = [document for document in
                 [read_json(directory / "managed-settings.json"),
                  *(read_json(directory / "managed-settings.d" / name)
                    for name in _drop_in_names(directory / "managed-settings.d"))]
                 if document is not None]
    if not documents:
        return None
    merged: dict[str, Any] = {}
    for document in documents:
        merged = merge_documents(merged, document)
    return PolicySource(MANAGED_FILE, str(directory), merged, RANK_FILE)


def managed_mcp(directory: Path, server_name: str) -> str | None:
    """Whether managed-mcp.json takes exclusive control, and whether it names Jev."""
    path = directory / "managed-mcp.json"
    try:
        if not path.is_file():
            return None
    except OSError:
        return None
    document = read_json(path)
    servers = document.get("mcpServers") if document else None
    return "defines-jev" if isinstance(servers, dict) and server_name in servers else "excludes-jev"


def system_directory(platform: str = sys.platform) -> Path | None:
    """The managed settings directory Claude Code documents for each platform."""
    if platform == "darwin":
        return Path("/Library/Application Support/ClaudeCode")
    if platform.startswith("linux"):
        return Path("/etc/claude-code")
    if platform == "win32":
        return Path(r"C:\Program Files\ClaudeCode")
    return None


# ---------------------------------------------------------------------------
# The Claude configuration folder and the server-managed cache
# ---------------------------------------------------------------------------


def launchd_config_dir() -> str | None:
    """CLAUDE_CONFIG_DIR as launchd holds it for apps the user opens on macOS.

    The Claude desktop app starts its MCP servers with only a few variables,
    so a configuration folder set with `launchctl setenv` never reaches this
    process. launchd still has it.
    """
    try:
        result = subprocess.run([LAUNCHCTL, "getenv", "CLAUDE_CONFIG_DIR"], capture_output=True,
                                text=True, timeout=2, stdin=subprocess.DEVNULL)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip() if result.returncode == 0 and isinstance(result.stdout, str) else ""
    return value or None


def usable_directory(value: object) -> Path | None:
    if (isinstance(value, str) and value and len(value) <= MAX_PATH and "\x00" not in value
            and os.path.isabs(value)):
        return Path(value)
    return None


def config_directories(environ: Mapping[str, str], home: Path, platform: str,
                       launchd: Callable[[], str | None]) -> list[Path]:
    """The folders Claude Code may use for its configuration, most specific first."""
    directories: list[Path] = []
    configured = usable_directory(environ.get("CLAUDE_CONFIG_DIR"))
    if configured is None and platform == "darwin":
        configured = usable_directory(launchd())
    if configured is not None:
        directories.append(configured)
    directories.append(home / ".claude")
    unique: dict[str, Path] = {}
    for directory in directories:
        unique.setdefault(os.path.normcase(os.path.normpath(str(directory))), directory)
    return list(unique.values())


def _registered_source(config_dirs: Iterable[Path]) -> Mapping[str, Any] | None:
    """The source the Jev marketplace was added from, as Claude Code recorded it."""
    for directory in config_dirs:
        document = read_json(directory / "plugins" / "known_marketplaces.json")
        entry = document.get(MARKETPLACE) if document else None
        source = entry.get("source") if isinstance(entry, dict) else None
        if isinstance(source, dict) and isinstance(source.get("source"), str):
            return source
    return None


def _user_mcp_allowlist(config_dirs: list[Path]) -> object:
    settings = read_json(config_dirs[0] / "settings.json") if config_dirs else None
    return settings.get("allowedMcpServers") if settings else None


# ---------------------------------------------------------------------------
# MDM profiles and the Windows registry
# ---------------------------------------------------------------------------


def _mdm_profiles(base: Path, user: str | None) -> list[PolicySource]:
    candidates = []
    if user and user not in {".", ".."} and "/" not in user and "\\" not in user:
        # The login name stays out of the reported location.
        candidates.append((base / user / PLIST_NAME, str(base / "<user>" / PLIST_NAME)))
    candidates.append((base / PLIST_NAME, str(base / PLIST_NAME)))
    found = []
    for path, location in candidates:
        document = _read_plist(path)
        if document is not None:
            found.append(PolicySource(MDM_PROFILE, location, document, RANK_MDM))
    return found


def registry_values() -> list[tuple[str, object]]:
    try:
        import winreg  # type: ignore[import-not-found]
    except ImportError:
        return []
    values: list[tuple[str, object]] = []
    for hive, name in ((winreg.HKEY_LOCAL_MACHINE, "HKLM"), (winreg.HKEY_CURRENT_USER, "HKCU")):
        try:
            with winreg.OpenKey(hive, REGISTRY_KEY) as key:
                value, _kind = winreg.QueryValueEx(key, "Settings")
        except OSError:
            continue
        values.append((name + "\\" + REGISTRY_KEY, value))
    return values


def _registry_policies(values: Iterable[tuple[str, object]]) -> list[PolicySource]:
    found = []
    for where, value in values:
        if not isinstance(value, str) or len(value) > MAX_BYTES:
            continue
        try:
            document = json.loads(value)
        except (ValueError, RecursionError):
            continue
        if isinstance(document, dict):
            user_hive = where.upper().startswith("HKCU")
            found.append(PolicySource(REGISTRY_HKCU if user_hive else REGISTRY_HKLM, where, document,
                                      RANK_USER if user_hive else RANK_MDM))
    return found


def _current_user() -> str | None:
    """The account this process runs as. USER and LOGNAME are only variables."""
    try:
        import pwd

        return pwd.getpwuid(os.getuid()).pw_name
    except (ImportError, KeyError, OSError, AttributeError):
        pass
    try:
        return getpass.getuser()
    except Exception:  # getuser raises OSError or KeyError without a login name
        return None


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def discover(*, platform: str = sys.platform, environ: Mapping[str, str] | None = None,
             home: Path | None = None, system: Path | None = None,
             managed_preferences: Path = Path("/Library/Managed Preferences"),
             registry: Callable[[], Iterable[tuple[str, object]]] = registry_values,
             user: Callable[[], str | None] = _current_user,
             launchd: Callable[[], str | None] = launchd_config_dir,
             server_name: str = "qualixar-jev") -> Machine:
    """Every readable managed source in documented rank order, plus Jev's own records."""
    environ = os.environ if environ is None else environ
    home = Path.home() if home is None else home
    config_dirs = config_directories(environ, home, platform, launchd)
    sources: list[PolicySource] = []
    for directory in config_dirs:
        document = read_json(directory / "remote-settings.json")
        if document is not None:
            sources.append(PolicySource(SERVER_CACHE, str(directory / "remote-settings.json"), document,
                                        RANK_SERVER))
    if platform == "darwin":
        sources += _mdm_profiles(managed_preferences, user())
    registry_sources = _registry_policies(registry()) if platform == "win32" else []
    sources += [source for source in registry_sources if source.rank == RANK_MDM]
    directory = system if system is not None else system_directory(platform)
    files = managed_files(directory) if directory is not None else None
    if files is not None:
        sources.append(files)
    sources += [source for source in registry_sources if source.rank == RANK_USER]
    exclusive = managed_mcp(directory, server_name) if directory is not None else None
    return Machine(
        sources=sources,
        managed_mcp=exclusive,
        managed_mcp_location=str(directory / "managed-mcp.json") if exclusive and directory else None,
        marketplace_source=_registered_source(config_dirs),
        user_mcp_allowlist=_user_mcp_allowlist(config_dirs),
        config_dirs=config_dirs,
    )
