"""Read-only notice: can a Claude Code organization policy stop the Jev plugin here?

policy_sources.py reads the documented managed settings sources and
policy_rules.py applies Claude Code's documented rules to them. This module
turns the result into a notice made only of fixed codes, source labels and
locations, and fixed advice. It never repeats any other value from a policy.

When nothing stops Jev it returns None, so on an ordinary computer the output
of every caller is unchanged.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping

from .host_mcp import in_protected_folder
from .policy_rules import PLUGIN_ID, PLUGIN_ROOT, PUBLIC_SOURCE, SERVER_NAME, Verdict, judge
from .policy_sources import Machine, discover

GUIDE = "https://github.com/qualixar/jev-decision-layer/blob/main/docs/HOSTS.md#enterprise-managed-claude-code"

_EFFECTS = {
    "POLICY_COMPUTED_AT_RUNTIME": ("Your organization computes the Claude Code policy with a helper program, "
                                   "so Jev cannot read which settings apply."),
    "PLUGIN_DISABLED": "Your organization has turned the Jev plugin off in Claude Code.",
    "MARKETPLACE_BLOCKED": ("The Jev marketplace is on your organization's blocked list, so the plugin does not "
                            "load in Claude Code."),
    "MARKETPLACE_NOT_ALLOWED": ("The source the Jev plugin comes from is not on your organization's allowed list, "
                                "so the plugin, its /jev-* commands, skills, hooks and MCP server may not load in "
                                "Claude Code."),
    "ALL_HOOKS_DISABLED": ("Your organization has turned off all hooks in Claude Code, so sessions get no automatic "
                           "Jev guidance."),
    "PLUGIN_HOOKS_BLOCKED": ("Only hooks your organization deploys run in Claude Code, so sessions get no automatic "
                             "Jev guidance."),
    "MCP_EXCLUSIVE_CONFIG": ("Your organization fixes which MCP servers Claude Code may load (managed-mcp.json), so "
                             "the plugin's Jev tools do not load in Claude Code."),
    "MCP_SERVER_DENIED": "Your organization has blocked the Jev MCP server in Claude Code.",
    "MCP_SERVER_NOT_ALLOWED": ("Your organization's MCP server allowlist does not name this install's Jev launcher, "
                               "so Claude Code may not start the plugin's Jev tools."),
}
# An organization that turned Jev off chose to; the notice offers no way around it.
_DELIBERATE = {"PLUGIN_DISABLED", "MARKETPLACE_BLOCKED", "MCP_SERVER_DENIED"}
_MCP_LIMITS = {"MCP_EXCLUSIVE_CONFIG", "MCP_SERVER_NOT_ALLOWED", "MCP_SERVER_DENIED"}


def _compact(value: object) -> str:
    return json.dumps(value, separators=(", ", ": "))


def _display(path: str, home: Path) -> str:
    prefix = str(home).rstrip("/\\")
    if prefix and (path == prefix or path.startswith(prefix + os.sep)):
        return "~" + path[len(prefix):]
    return path


def _portable(path: str, home: Path) -> str:
    """A command path an administrator can use for every user: Claude Code expands ${HOME}."""
    shown = _display(path, home)
    return "${HOME}" + shown[1:] if shown.startswith("~") else shown


def _administrator(codes: set[str], verdict: Verdict, home: Path) -> list[str]:
    source = verdict.marketplace_source
    public = dict(source) == PUBLIC_SOURCE
    shown = (_compact(PUBLIC_SOURCE) if public
             else "the source your Jev marketplace was added from (`claude plugin marketplace list` shows it)")
    launcher = _portable(verdict.launcher, home)
    asks = {
        "POLICY_COMPUTED_AT_RUNTIME": "check the policy the helper produces (`claude doctor` shows it) and allow the Jev plugin there",
        "PLUGIN_DISABLED": f"remove the false entry for {PLUGIN_ID} in managed enabledPlugins, if Jev is approved for you",
        "MARKETPLACE_BLOCKED": (f"remove the blockedMarketplaces entry that matches {shown}, if Jev is approved for you; "
                                "the blocklist is checked first, so allowing it as well does nothing"),
        "MARKETPLACE_NOT_ALLOWED": (f"add {shown} to strictKnownMarketplaces and register it in "
                                    "extraKnownMarketplaces under the name qualixar"),
        "PLUGIN_HOOKS_BLOCKED": (f"force-enable {PLUGIN_ID} in managed enabledPlugins; hooks of a force-enabled "
                                 "plugin still run under allowManagedHooksOnly"),
        "MCP_EXCLUSIVE_CONFIG": f"define the {SERVER_NAME} server in managed-mcp.json with the command {launcher}",
        "MCP_SERVER_DENIED": "remove the deniedMcpServers entry that matches the Jev server, if Jev is approved for you",
        "MCP_SERVER_NOT_ALLOWED": (f"add {_compact({'serverCommand': [launcher]})} to allowedMcpServers"
                                   + ("; the path contains the release number, so it changes with each Jev upgrade"
                                      if "/plugins/cache/" in verdict.launcher.replace("\\", "/") else "")),
    }
    chosen = [asks[code] for code in asks if code in codes]
    return ["Ask your Claude Code administrator to " + "; ".join(chosen) + "."] if chosen else []


def _meanwhile(codes: set[str], verdict: Verdict, home: Path) -> list[str]:
    if codes & _DELIBERATE:
        return ["Your organization has chosen to block Jev in Claude Code. Do not work around it; "
                "ask whether Jev can be approved."]
    jev = _display(str(PLUGIN_ROOT / "scripts" / "jev"), home)
    desktop = ("If your organization permits it, in the Claude desktop app: quit the app, run "
               f"`{jev} host-register --host claude-desktop --write`, reopen it, and add the Jev section from "
               f"{GUIDE} to your user CLAUDE.md so Claude calls the Jev tools without hooks.")
    if in_protected_folder(PLUGIN_ROOT, home):
        desktop += (" This copy of Jev is inside Documents, Desktop or Downloads, where macOS may not let the "
                    "desktop app run it; use a copy outside those folders.")
    options = [desktop]
    if "MARKETPLACE_NOT_ALLOWED" in codes and not codes & _MCP_LIMITS and not verdict.user_mcp_blocked:
        launcher = _display(verdict.launcher, home)
        options.append("If your organization permits it, in the Claude Code CLI: run "
                       f"`claude mcp add {SERVER_NAME} -- {launcher}` and add the same CLAUDE.md section.")
    elif codes <= {"PLUGIN_HOOKS_BLOCKED", "ALL_HOOKS_DISABLED"}:
        options.append("In the Claude Code CLI the plugin's tools still load; the same CLAUDE.md section "
                       "tells Claude how to call them.")
    return options


def notice(machine: Machine | None = None, *, home: Path | None = None,
           environ: Mapping[str, str] | None = None) -> dict[str, Any] | None:
    """Explain what a managed policy stops, or return None when nothing does."""
    try:
        home = Path.home() if home is None else home
        found = discover() if machine is None else machine
        verdict = judge(found, environ)
        if not verdict.codes:
            return None
        codes = set(verdict.codes)
        sources = [{"source": source.label, "location": _display(source.location, home)}
                   for source in verdict.deciding]
        if found.managed_mcp_location and "MCP_EXCLUSIVE_CONFIG" in codes:
            sources.append({"source": "managed MCP configuration",
                            "location": _display(found.managed_mcp_location, home)})
        return {
            "summary": ("A Claude Code organization policy on this computer may stop the Jev plugin from "
                        "running on its own in Claude Code."),
            "codes": list(verdict.codes),
            "sources": sources,
            "effects": [_EFFECTS[code] for code in verdict.codes],
            "options": _administrator(codes, verdict, home) + _meanwhile(codes, verdict, home),
            "unaffected": ("If you use Jev only in Codex, VS Code, Antigravity or Hermes, nothing here affects "
                           "you. Your Jev grant is unchanged."),
            "check": ("Claude Code applies these rules itself: /status shows the managed source in force, "
                      "/plugin shows a blocked marketplace, and `claude doctor` lists policy problems."),
        }
    except Exception:  # advisory only: an unreadable policy must never break a caller
        return None
