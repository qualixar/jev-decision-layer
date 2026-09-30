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
import re
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

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
    user_path_resolver: Callable[[], Path] | None = None
    windows_only: bool = False

    def config_path(self, workspace: Path | None) -> Path:
        if self.workspace_relative is not None:
            if workspace is None:
                raise AutoError("HOST_MCP_WORKSPACE_REQUIRED")
            return Path(workspace) / self.workspace_relative
        if self.user_path is not None:
            return self.user_path.expanduser()
        if self.user_path_resolver is not None:
            return self.user_path_resolver().expanduser()
        raise AutoError("HOST_MCP_CONFIG_UNREADABLE")


def _is_windows() -> bool:
    return os.name == "nt"


def _launcher_filename(platform: str | None = None) -> str:
    """Return the checked-in launcher for the current (or requested) OS."""
    return "launch-jev.cmd" if (platform or ("nt" if _is_windows() else "posix")) == "nt" else "launch-jev"


def _claude_desktop_config_path(platform: str | None = None, home: Path | None = None) -> Path:
    """Resolve Claude Desktop's config path for the current host OS."""
    if (platform or ("nt" if _is_windows() else "posix")) == "nt":
        return (home or Path.home()) / "AppData" / "Roaming" / "Claude" / "claude_desktop_config.json"
    return Path("~/Library/Application Support/Claude/claude_desktop_config.json")


def _codex_cli_config_path() -> Path:
    configured_home = os.environ.get("CODEX_HOME")
    base = Path(configured_home).expanduser() if configured_home else Path.home() / ".codex"
    return base / "config.toml"


def _claude_code_cli_config_path() -> Path:
    configured_home = os.environ.get("CLAUDE_CONFIG_DIR")
    return ((Path(configured_home).expanduser() / ".claude.json") if configured_home
            else Path.home() / ".claude.json")


def _python_mcp_entry() -> dict[str, Any]:
    """Launch through the interpreter already running Jev; do not resolve PATH."""
    interpreter = Path(sys.executable).resolve()
    runtime = Path(__file__).resolve().parents[1]
    if not interpreter.is_absolute() or not interpreter.is_file() or interpreter.suffix.lower() != ".exe":
        raise AutoError("HOST_MCP_PYTHON_INTERPRETER_INVALID")
    # Isolated mode deliberately drops the plugin runtime from sys.path. Pass
    # both paths as argv values (never interpolate them into source) and add
    # only this packaged runtime before executing its entry point.
    bootstrap = (
        "import runpy,sys;"
        "runtime,entry=sys.argv[1:3];"
        "sys.path.insert(0,runtime);"
        "sys.argv=sys.argv[2:];"
        "runpy.run_path(entry,run_name='__main__')"
    )
    return {
        "command": str(interpreter),
        "args": [
            "-I", "-S", "-B", "-c", bootstrap,
            str(runtime), str(runtime / "auto_entry.py"), "mcp",
        ],
        "env": {},
    }


def _codex_manual_config_snippet(entry: dict[str, Any]) -> str:
    """Produce an exact manual TOML block; this is never written by Jev."""
    return "\n".join((
        f"[mcp_servers.{json.dumps(SERVER_NAME)}]",
        f"command = {json.dumps(entry['command'], ensure_ascii=False)}",
        f"args = {json.dumps(entry['args'], ensure_ascii=False)}",
    )) + "\n"


def _json_manual_config_snippet(target: Target, entry: dict[str, Any]) -> str:
    return json.dumps({target.key: {SERVER_NAME: entry}}, indent=2, ensure_ascii=False) + "\n"


TARGETS = {
    "vscode": Target("vscode", "servers", True, workspace_relative=Path(".vscode") / "mcp.json"),
    "antigravity": Target("antigravity", "mcpServers", False,
                          user_path=Path("~/.gemini/config/mcp_config.json")),
    "claude-desktop": Target("claude-desktop", "mcpServers", False,
                             user_path_resolver=_claude_desktop_config_path),
    "claude-code-cli": Target("claude-code-cli", "mcpServers", False,
                              user_path_resolver=_claude_code_cli_config_path, windows_only=True),
    "codex-cli": Target("codex-cli", "mcp_servers", False,
                        user_path_resolver=_codex_cli_config_path, windows_only=True),
}


def launcher_path() -> Path:
    """The stdio entry point, resolved absolutely: no host expands our variables."""
    path = Path(__file__).resolve().parents[2] / "scripts" / _launcher_filename()
    if not path.is_file():
        raise AutoError("HOST_MCP_LAUNCHER_MISSING")
    return path


def _target(host: str) -> Target:
    target = TARGETS.get(host)
    if target is None:
        raise AutoError("HOST_MCP_TARGET_UNKNOWN")
    if target.windows_only and not _is_windows():
        raise AutoError("HOST_MCP_WINDOWS_ONLY")
    return target


# VS Code expands its predefined variables, `${userHome}` among them, across the
# whole stdio launch configuration, `command` included. Checked 2026-10-01
# against the MCP configuration reference ("You can use predefined variables in
# the server configuration") and the VS Code source that resolves the launch.
USER_HOME = "${userHome}"


def _homes() -> tuple[Path, ...]:
    try:
        home = Path.home()
    except (KeyError, RuntimeError):
        return ()
    # "/" as a home would turn every path into a home path.
    if not home.is_absolute() or len(home.parts) < 2:
        return ()
    return tuple(dict.fromkeys((home, home.resolve())))


def _portable_home(command: str) -> str:
    """Name the home folder by VS Code's variable, so a repository file carries
    no user name and works for a teammate with the same plugin release."""
    for home in _homes():
        try:
            relative = Path(command).relative_to(home)
        except ValueError:
            continue
        return f"{USER_HOME}/{relative.as_posix()}"
    return command


def _expand_home(command: Any) -> Any:
    if isinstance(command, str) and command.startswith(USER_HOME + "/") and _homes():
        return str(_homes()[0]) + command[len(USER_HOME):]
    return command


def server_entry(host: str, launcher: Path | None = None) -> dict[str, Any]:
    target = _target(host)
    if _is_windows():
        # Launch with the interpreter already running the registration command.
        # This avoids cmd.exe argument parsing and PATH-based interpreter lookup.
        entry = _python_mcp_entry()
        if target.include_type:
            entry = {"type": "stdio", **entry}
        return entry
    command = str(launcher or launcher_path())
    if target.workspace_relative is not None:
        # A workspace file may be committed; a user-level file is not.
        command = _portable_home(command)
    entry: dict[str, Any] = {"command": command, "args": [], "env": {}}
    if target.include_type:
        entry = {"type": "stdio", **entry}
    return entry


def render(host: str, launcher: Path | None = None) -> dict[str, Any]:
    """A complete, minimal config for a host with no other servers."""
    return {_target(host).key: {SERVER_NAME: server_entry(host, launcher)}}


def _without_comments(text: str) -> tuple[str, bool]:
    """Drop // and /* */ comments and trailing commas outside strings.

    Used only to recognise a commented (JSONC) file. Such a file is refused,
    never rewritten: writing it back as JSON would delete the user's comments.
    """
    output: list[str] = []
    found = False
    index, length = 0, len(text)
    while index < length:
        char = text[index]
        if char == '"':
            end = index + 1
            while end < length and text[end] != '"':
                end += 2 if text[end] == "\\" else 1
            output.append(text[index:end + 1])
            index = end + 1
        elif text.startswith("//", index):
            found = True
            newline = text.find("\n", index)
            index = length if newline < 0 else newline
        elif text.startswith("/*", index):
            found = True
            close = text.find("*/", index + 2)
            index = length if close < 0 else close + 2
        else:
            output.append(char)
            index += 1
    stripped = "".join(output)
    # JSONC also allows a trailing comma; recognise that only alongside comments.
    return (_drop_trailing_commas(stripped) if found else stripped), found


def _drop_trailing_commas(text: str) -> str:
    output: list[str] = []
    index, length = 0, len(text)
    while index < length:
        char = text[index]
        if char == '"':
            end = index + 1
            while end < length and text[end] != '"':
                end += 2 if text[end] == "\\" else 1
            output.append(text[index:end + 1])
            index = end + 1
            continue
        if char == "," and text[index + 1:].lstrip().startswith(("}", "]")):
            index += 1
            continue
        output.append(char)
        index += 1
    return "".join(output)


def _read(config: Path) -> dict[str, Any] | None:
    if not config.exists():
        return None
    if config.is_symlink() or not config.is_file() or config.stat().st_size > MAX_CONFIG_BYTES:
        raise AutoError("HOST_MCP_CONFIG_UNREADABLE")
    try:
        text = config.read_text()
    except (OSError, ValueError):
        raise AutoError("HOST_MCP_CONFIG_UNPARSEABLE") from None
    try:
        document = json.loads(text)
    except ValueError:
        # Refusing here keeps a hand-edited file intact. Overwriting it would
        # silently drop servers the user configured, or their comments.
        stripped, commented = _without_comments(text)
        if commented:
            try:
                json.loads(stripped)
            except ValueError:
                pass
            else:
                raise AutoError("HOST_MCP_CONFIG_HAS_COMMENTS") from None
        raise AutoError("HOST_MCP_CONFIG_UNPARSEABLE") from None
    if not isinstance(document, dict):
        raise AutoError("HOST_MCP_CONFIG_UNPARSEABLE")
    return document


_RELEASE_DIR = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")


def _release(label: str) -> tuple[int, int, int] | None:
    match = _RELEASE_DIR.fullmatch(label)
    return tuple(int(part) for part in match.groups()) if match else None


def _is_older_release_of(current: Any, proposed: dict[str, Any]) -> bool:
    """True only for our own launcher at an older versioned cache path.

    A host plugin cache lays releases out as `<cache>/<plugin>/<X.Y.Z>/scripts/<launcher>`.
    Every other key must match exactly, so a user's own `env`, `args` or any
    extra field is never discarded. A downgrade, a different install location
    or any non-release directory is still a conflict.
    """
    if not isinstance(current, dict) or set(current) != set(proposed):
        return False
    if any(current[key] != proposed[key] for key in proposed if key != "command"):
        return False
    old, new = _expand_home(current.get("command")), _expand_home(proposed.get("command"))
    if not isinstance(old, str) or not isinstance(new, str):
        return False
    # Resolve both spellings so a symlinked cache prefix is recognised, and so
    # `..` segments are normalised before the location comparison below.
    if not Path(old).is_absolute() or not Path(new).is_absolute():
        return False
    try:
        old_path, new_path = Path(old).resolve(), Path(new).resolve()
    except (OSError, RuntimeError, ValueError):
        return False
    if len(old_path.parts) < 4 or len(new_path.parts) < 4:
        return False
    if old_path.name != new_path.name or old_path.parent.name != "scripts" or new_path.parent.name != "scripts":
        return False
    if old_path.parents[2] != new_path.parents[2]:
        return False
    if old_path == new_path:
        # The same launcher file under another spelling, such as the absolute
        # home path written before 1.0.13: rewriting it changes nothing it runs.
        return True
    old_release, new_release = _release(old_path.parents[1].name), _release(new_path.parents[1].name)
    return old_release is not None and new_release is not None and old_release < new_release


def merge(host: str, existing: dict[str, Any] | None, launcher: Path | None = None) -> dict[str, Any]:
    """Add one server, leave an identical entry untouched, or replace an older
    release of our own launcher; never replace anything else."""
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
    proposed = server_entry(host, launcher)
    current = servers.get(SERVER_NAME)
    if current is not None and current != proposed and not _is_older_release_of(current, proposed):
        raise AutoError("HOST_MCP_ENTRY_CONFLICT")
    servers[SERVER_NAME] = proposed
    document[target.key] = servers
    return document


PROTECTED_FOLDERS = ("Documents", "Desktop", "Downloads")


def in_protected_folder(path: object, home: Path | None = None) -> bool:
    """Whether macOS privacy protection covers this path for apps without folder access.

    The Claude desktop app may be refused permission to run a program inside
    these folders, so a launcher registered there can fail to start.
    """
    if not isinstance(path, (str, Path)) or not str(path):
        return False
    home = Path.home() if home is None else home
    candidate = Path(os.path.normpath(str(path)))
    return any(candidate == home / name or (home / name) in candidate.parents for name in PROTECTED_FOLDERS)


def plan(host: str, workspace: Path | None = None, launcher: Path | None = None) -> dict[str, Any]:
    """Preview registration; Windows returns a manual snippet without opening host config."""
    target = _target(host)
    config = target.config_path(workspace)
    if _is_windows():
        entry = server_entry(host, launcher)
        outcome = {
            "host": host,
            "config_path": str(config),
            "config_key": target.key,
            "config_exists": None,
            "action": "manual-registration-required",
            "preserved_servers": [],
            "replaced_entry": None,
            "entry": entry,
            "written": False,
            "registration": "manual",
        }
        if host == "codex-cli":
            outcome["manual_config_snippet"] = _codex_manual_config_snippet(entry)
        else:
            outcome["manual_config_snippet"] = _json_manual_config_snippet(target, entry)
        return outcome
    existing = _read(config)
    proposed = merge(host, existing, launcher)     # validates the key before anything reads it
    present = (existing or {}).get(target.key, {}) if existing else {}
    current = present.get(SERVER_NAME) if isinstance(present, dict) else None
    entry = proposed[target.key][SERVER_NAME]
    action = "unchanged" if current == entry else "update" if current is not None else "create" if existing is None else "add"
    preserved = sorted(set(present) - {SERVER_NAME}) if isinstance(present, dict) else []
    return {
        "host": host,
        "config_path": str(config),
        "config_key": target.key,
        "config_exists": existing is not None,
        "action": action,
        "preserved_servers": preserved,
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
    """Apply a native POSIX config update. Windows hosts require manual review."""
    outcome = plan(host, workspace, launcher)
    if outcome["action"] == "unchanged":
        return outcome
    target = _target(host)
    if _is_windows():
        raise AutoError("HOST_MCP_WINDOWS_NATIVE_CONFIG_REQUIRED")
    config = Path(outcome["config_path"])
    directory = config.parent
    if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
        raise AutoError("HOST_MCP_CONFIG_UNREADABLE")
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise AutoError("HOST_MCP_CONFIG_UNREADABLE") from None
    if config.is_symlink():
        raise AutoError("HOST_MCP_CONFIG_UNREADABLE")
    document = merge(host, _read(config), launcher)
    try:
        # The user's own permissions stay; a file we create is private.
        mode = stat.S_IMODE(os.lstat(config).st_mode)
    except FileNotFoundError:
        mode = 0o600
    except OSError:
        raise AutoError("HOST_MCP_CONFIG_UNREADABLE") from None
    # Key order is the user's: no sort_keys, so only our one entry moves.
    _write_atomically(config, json.dumps(document, indent=2) + "\n", mode)
    return {**outcome, "written": True}


def _write_atomically(config: Path, payload: str, mode: int = 0o600) -> None:
    # mkstemp sat outside the try, so an unwritable or missing config
    # directory raised a bare PermissionError straight past every caller that
    # catches AutoError -- `jev vscode --write` reported it as a traceback
    # rather than a refusal. A directory we cannot write to is a refusal.
    try:
        handle, temporary = tempfile.mkstemp(dir=str(config.parent), prefix=".jev-", suffix=".json")
    except OSError:
        raise AutoError("HOST_MCP_CONFIG_UNWRITABLE") from None
    try:
        os.fchmod(handle, mode)
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
