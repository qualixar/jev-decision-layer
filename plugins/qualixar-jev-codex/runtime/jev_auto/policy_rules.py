"""Would Claude Code, under the managed policy read by policy_sources, stop Jev?

The rules follow Claude Code's documentation: the first managed source that
sets a policy key decides unless it opts into merging every source; a lock
with an unreadable value reads as its restrictive value; an invalid allowlist
value is enforced as empty and an invalid entry is stripped. Admission is
only ever claimed when it can be proven. Anything this module cannot judge is
reported as a possible block, never as a pass.

How Claude Code matches a plugin's MCP server was checked by running Claude
Code: a plugin server is known by the scoped name
``plugin:<plugin>:<server>`` and is admitted by an allowlist only through a
serverCommand entry equal to the command it runs.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Iterable, Mapping, NamedTuple
from urllib.parse import urlsplit

from . import __version__
from .policy_sources import RANK_USER, Machine, PolicySource

PLUGIN_ID = "qualixar-jev-decision-layer@qualixar"
SERVER_NAME = "qualixar-jev"
SCOPED_SERVER_NAME = "plugin:qualixar-jev-decision-layer:qualixar-jev"
PUBLIC_SOURCE = {"source": "github", "repo": "qualixar/jev-decision-layer"}
PLUGIN_ROOT = Path(__file__).resolve().parents[2]
_CACHE = ("plugins", "cache", "qualixar", "qualixar-jev-decision-layer")

_CONTROL_KEYS = {"managedSourcesBehavior", "wslInheritsWindowsSettings"}
_ALLOW_NAME = re.compile(r"[A-Za-z0-9_-]+\Z")
_VARIABLE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
_PYTHON_ONLY_REGEX = re.compile(r"\(\?[aiLmsux-]|\(\?P[<=]|\(\?#|\\[AZ]")
_MAX_PATTERN = 1024

ORDER = (
    "POLICY_COMPUTED_AT_RUNTIME",
    "PLUGIN_DISABLED",
    "MARKETPLACE_BLOCKED",
    "MARKETPLACE_NOT_ALLOWED",
    "ALL_HOOKS_DISABLED",
    "PLUGIN_HOOKS_BLOCKED",
    "MCP_EXCLUSIVE_CONFIG",
    "MCP_SERVER_DENIED",
    "MCP_SERVER_NOT_ALLOWED",
)


class Verdict(NamedTuple):
    codes: list[str]
    deciding: list[PolicySource]
    marketplace_source: Mapping[str, Any]
    launcher: str
    user_mcp_blocked: bool


# ---------------------------------------------------------------------------
# Which sources decide, and how a key reads across them
# ---------------------------------------------------------------------------


def _has_policy_key(document: Mapping[str, Any]) -> bool:
    return any(key not in _CONTROL_KEYS and value is not None for key, value in document.items())


def deciding_sources(sources: Iterable[PolicySource]) -> list[PolicySource]:
    """The sources Claude Code applies: the first that sets a policy key, or all under merge.

    The user-writable HKCU registry applies only when no admin source sets anything.
    """
    ranked = sorted(sources, key=lambda source: source.rank)
    admin = [source for source in ranked if source.rank < RANK_USER and _has_policy_key(source.document)]
    if admin:
        return admin if admin[0].document.get("managedSourcesBehavior") == "merge" else admin[:1]
    return [source for source in ranked if _has_policy_key(source.document)][:1]


def _value(document: Mapping[str, Any], *keys: str) -> object:
    """The first of ``keys`` set to a non-null value: a canonical key wins over its alias."""
    for key in keys:
        value = document.get(key)
        if value is not None:
            return value
    return None


def _first(deciding: list[PolicySource], *keys: str) -> object:
    for source in deciding:
        value = _value(source.document, *keys)
        if value is not None:
            return value
    return None


def lock(value: object) -> bool:
    """A boolean lock: an unreadable value reads as its restrictive value, true."""
    return value is not None and value is not False and value != "false"


def _any_lock(sources: Iterable[PolicySource], key: str) -> bool:
    return any(lock(source.document.get(key)) for source in sources)


# ---------------------------------------------------------------------------
# Marketplace sources
# ---------------------------------------------------------------------------


def _same_optional(entry: Mapping[str, Any], source: Mapping[str, Any], key: str) -> bool:
    return (key in entry) == (key in source) and entry.get(key) == source.get(key)


def _host(source: Mapping[str, Any]) -> str | None:
    kind = source.get("source")
    if kind == "github":
        return "github.com"
    url = source.get("url")
    if (kind == "git" or kind == "url") and isinstance(url, str):
        if url.startswith("git@") and ":" in url:
            return url[4:].split(":", 1)[0].lower() or None
        try:
            return (urlsplit(url).hostname or "").lower() or None
        except ValueError:
            return None
    return None


def _host_pattern_matches(pattern: object, host: str | None) -> bool:
    """Claude Code evaluates hostPattern as a JavaScript regular expression, searched anywhere."""
    if host is None or not isinstance(pattern, str) or not pattern or len(pattern) > _MAX_PATTERN:
        return False
    if _PYTHON_ONLY_REGEX.search(pattern):
        return False  # syntax JavaScript does not share; cannot be judged here
    try:
        return re.search(pattern, host) is not None
    except re.error:
        return False


def admits_marketplace(entry: object, source: Mapping[str, Any]) -> bool:
    """Whether one allowlist entry admits the marketplace source, per the documented matching."""
    if not isinstance(entry, dict):
        return False
    kind = entry.get("source")
    if kind == "hostPattern":
        return _host_pattern_matches(entry.get("hostPattern"), _host(source))
    if not isinstance(kind, str) or kind != source.get("source"):
        return False  # pathPattern is not evaluated: an admin regex against a path can run away
    if kind == "github":
        repo, wanted = entry.get("repo"), source.get("repo")
        if not isinstance(repo, str) or not isinstance(wanted, str) or not _same_optional(entry, source, "ref"):
            return False
        owner, _, name = repo.partition("/")
        if name == "*":
            return ("*" not in owner and bool(owner) and wanted.split("/", 1)[0] == owner
                    and ("path" not in entry or entry.get("path") == source.get("path")))
        return repo == wanted and _same_optional(entry, source, "path")
    if kind == "git":
        return (isinstance(entry.get("url"), str) and entry.get("url") == source.get("url")
                and _same_optional(entry, source, "ref") and _same_optional(entry, source, "path"))
    if kind == "url":
        return isinstance(entry.get("url"), str) and entry.get("url") == source.get("url")
    if kind in {"file", "directory"}:
        return isinstance(entry.get("path"), str) and entry.get("path") == source.get("path")
    return False


def _canonical_git(url: str) -> str:
    value = url.strip().rstrip("/")
    for prefix in ("git@github.com:", "ssh://git@github.com/", "git+ssh://git@github.com/"):
        if value.lower().startswith(prefix):
            value = "https://github.com/" + value[len(prefix):]
    value = value[:-4] if value.endswith(".git") else value
    scheme, _, rest = value.partition("://")
    host, _, path = rest.partition("/")
    return f"{scheme.lower()}://{host.lower()}/{path}" if rest else value


def _source_git_url(source: Mapping[str, Any]) -> str | None:
    if source.get("source") == "github" and isinstance(source.get("repo"), str):
        return "https://github.com/" + source["repo"]
    if source.get("source") == "git" and isinstance(source.get("url"), str):
        return _canonical_git(source["url"])
    return None


def blocks_marketplace(entry: object, source: Mapping[str, Any]) -> bool:
    """Whether one blocklist entry blocks the source. Blocklist matching is wider."""
    if not isinstance(entry, dict) or not isinstance(entry.get("source"), str):
        return False
    kind = entry["source"]
    if kind == "hostPattern":
        return _host_pattern_matches(entry.get("hostPattern"), _host(source))
    for key in ("ref", "path"):  # an entry without ref or path blocks every ref and path
        if key in entry and entry.get(key) != source.get(key):
            return False
    if kind == "github" and isinstance(entry.get("repo"), str):
        repo, target = entry["repo"], _source_git_url(source)
        if target is None:
            return False
        wanted = target[len("https://github.com/"):] if target.startswith("https://github.com/") else None
        if wanted is None:
            return False
        owner, _, name = repo.partition("/")
        if name == "*":
            return bool(owner) and wanted.split("/", 1)[0].lower() == owner.lower()
        return repo == wanted
    if kind == "git" and isinstance(entry.get("url"), str):
        return _canonical_git(entry["url"]) == _source_git_url(source)
    if kind == "url":
        return source.get("source") == "url" and entry.get("url") == source.get("url")
    if kind in {"file", "directory"}:
        return source.get("source") == kind and entry.get("path") == source.get("path")
    return False


def _allowlist(value: object) -> list[object] | None:
    """An allowlist in force; a malformed value is enforced as empty."""
    if value is None:
        return None
    return value if isinstance(value, list) else []


# ---------------------------------------------------------------------------
# The MCP server
# ---------------------------------------------------------------------------


def _expand(value: str, environ: Mapping[str, str]) -> str | None:
    missing = False

    def replace(match: re.Match[str]) -> str:
        nonlocal missing
        name, default = match.group(1), match.group(2)
        if name in environ:
            return environ[name]
        if default is not None:
            return default
        missing = True
        return ""

    expanded = _VARIABLE.sub(replace, value)
    return None if missing else expanded


def launcher_commands(config_dirs: Iterable[Path], environ: Mapping[str, str]) -> set[str]:
    """Commands Claude Code may run for this plugin's MCP server: its own launcher,
    the one under CLAUDE_PLUGIN_ROOT, and this release in each plugin cache."""
    candidates = [PLUGIN_ROOT / "scripts" / "launch-jev", Path(__file__).parents[2] / "scripts" / "launch-jev"]
    root = environ.get("CLAUDE_PLUGIN_ROOT")
    if isinstance(root, str) and os.path.isabs(root):
        candidates.append(Path(root) / "scripts" / "launch-jev")
    candidates += [directory.joinpath(*_CACHE, __version__, "scripts", "launch-jev") for directory in config_dirs]
    return {os.path.normpath(str(path)) for path in candidates}


def _entry(entry: object, *, allow: bool) -> tuple[str, object] | None:
    """A well-formed allow or deny entry: an object with exactly one key. Others are stripped."""
    if not isinstance(entry, dict) or len(entry) != 1:
        return None
    ((key, value),) = entry.items()
    if key == "serverCommand":
        return (key, value) if isinstance(value, list) and value and all(isinstance(part, str) for part in value) else None
    if key == "serverName":
        if not isinstance(value, str) or not value or value != value.strip():
            return None
        return (key, value) if not allow or _ALLOW_NAME.match(value) else None
    if key == "serverUrl":
        return (key, value) if isinstance(value, str) and value else None
    return None


def _command_matches(command: object, launchers: set[str], environ: Mapping[str, str]) -> bool:
    if not isinstance(command, list) or len(command) != 1:
        return False  # commands match exactly, and the launcher takes no arguments
    expanded = _expand(command[0], environ)
    return expanded is not None and os.path.normpath(expanded) in launchers


def mcp_admitted(entries: list[object], launchers: set[str], environ: Mapping[str, str]) -> bool:
    valid = [item for item in (_entry(entry, allow=True) for entry in entries) if item is not None]
    return any(key == "serverCommand" and _command_matches(value, launchers, environ) for key, value in valid)


def mcp_denied(entries: Iterable[object], launchers: set[str], environ: Mapping[str, str]) -> bool:
    for entry in entries:
        item = _entry(entry, allow=False)
        if item is None:
            continue
        key, value = item
        if key == "serverName" and value == SCOPED_SERVER_NAME:
            return True
        if key == "serverCommand" and _command_matches(value, launchers, environ):
            return True
    return False


def _name_denied(entries: Iterable[object]) -> bool:
    return any(_entry(entry, allow=False) == ("serverName", SERVER_NAME) for entry in entries)


def _plugin_only_mcp(value: object) -> bool:
    if value is True:
        return True
    if isinstance(value, list):
        return "mcp" in value
    return isinstance(value, dict) and value.get("mcp") is True


# ---------------------------------------------------------------------------
# The verdict
# ---------------------------------------------------------------------------


def _listed(sources: Iterable[PolicySource], key: str) -> list[object]:
    """Entries of a list key combined across sources; a non-list value adds nothing."""
    entries: list[object] = []
    for source in sources:
        value = source.document.get(key)
        if isinstance(value, list):
            entries += value
    return entries


def _managed_allowlist(admin: list[PolicySource], deciding: list[PolicySource]) -> object:
    if _any_lock(admin, "allowManagedMcpServersOnly"):
        return _first(admin, "allowedMcpServers")  # the highest-ranked admin source that sets one
    return _first(deciding, "allowedMcpServers")


def judge(machine: Machine, environ: Mapping[str, str] | None = None) -> Verdict:
    """Apply Claude Code's documented rules to the machine's managed policy."""
    environ = os.environ if environ is None else environ
    admin = sorted((source for source in machine.sources if source.rank < RANK_USER),
                   key=lambda source: source.rank)
    deciding = deciding_sources(machine.sources)
    marketplace = machine.marketplace_source or PUBLIC_SOURCE
    launchers = launcher_commands(machine.config_dirs, environ)
    codes: list[str] = []
    user_mcp_blocked = machine.managed_mcp is not None
    if machine.managed_mcp == "excludes-jev":
        codes.append("MCP_EXCLUSIVE_CONFIG")
    if _first(deciding[:1], "policyHelper") is not None:
        # The helper's output replaces this source; its other keys do not apply.
        codes.append("POLICY_COMPUTED_AT_RUNTIME")
        return Verdict([code for code in ORDER if code in codes], deciding, marketplace,
                       os.path.normpath(str(PLUGIN_ROOT / "scripts" / "launch-jev")), True)
    plugins = _first(deciding, "enabledPlugins")
    enabled = plugins.get(PLUGIN_ID) if isinstance(plugins, dict) else None
    enabled = enabled if isinstance(enabled, bool) else None  # an invalid entry is dropped
    if enabled is False:
        codes.append("PLUGIN_DISABLED")
    if any(blocks_marketplace(entry, marketplace) for entry in _listed(deciding, "blockedMarketplaces")):
        codes.append("MARKETPLACE_BLOCKED")
    allowed = _allowlist(_first(deciding, "strictKnownMarketplaces", "allowedMarketplaces"))
    if allowed is not None and not any(admits_marketplace(entry, marketplace) for entry in allowed):
        codes.append("MARKETPLACE_NOT_ALLOWED")
    if any(source.document.get("disableAllHooks") is True for source in deciding):
        codes.append("ALL_HOOKS_DISABLED")
    elif _any_lock(deciding, "allowManagedHooksOnly") and enabled is None:
        # Hooks of a plugin force-enabled in managed enabledPlugins are exempt.
        codes.append("PLUGIN_HOOKS_BLOCKED")
    denies = _listed(admin, "deniedMcpServers")
    if mcp_denied(denies, launchers, environ):
        codes.append("MCP_SERVER_DENIED")
    managed = _allowlist(_managed_allowlist(admin, deciding))
    if managed is not None:
        # Without the lock, the user's own allowlist broadens the organization's.
        user = None if _any_lock(admin, "allowManagedMcpServersOnly") else _allowlist(machine.user_mcp_allowlist)
        if not mcp_admitted(managed + (user or []), launchers, environ):
            codes.append("MCP_SERVER_NOT_ALLOWED")
    user_mcp_blocked = (user_mcp_blocked or _name_denied(denies)
                        or _plugin_only_mcp(_first(deciding, "strictPluginOnlyCustomization")))
    launcher = os.path.normpath(str(PLUGIN_ROOT / "scripts" / "launch-jev"))
    return Verdict([code for code in ORDER if code in codes], deciding, marketplace, launcher, user_mcp_blocked)
