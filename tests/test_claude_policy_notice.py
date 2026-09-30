"""A Claude Code organization policy can stop the Jev plugin; say so, or say nothing.

The notice is read-only and advisory. On a computer without such a policy
every surface (doctor, jev_auto_status, the setup wizard) must produce exactly
the 1.0.11 output, and on one with a policy the notice must carry fixed codes
and advice only, never another value from the policy.
"""

from __future__ import annotations

import io
import json
import plistlib
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "plugins" / "qualixar-jev-decision-layer" / "runtime"
sys.path.insert(0, str(RUNTIME))

from jev_auto import host_policy  # noqa: E402
from jev_auto.host_policy import findings, notice  # noqa: E402

_PRIVATE = "ORG-PRIVATE-MARKER"
_HOOKS_ONLY = {"allowManagedHooksOnly": True}
_PLUGIN = host_policy.PLUGIN_ID


def _source(document, label="managed settings file", where="/etc/claude-code/managed-settings.json"):
    return [(label, where, document)]


class FindingTests(unittest.TestCase):
    def test_a_policy_that_touches_nothing_jev_uses_is_silent(self):
        self.assertIsNone(notice([]))
        self.assertIsNone(notice(_source({"permissions": {"deny": ["Bash(curl *)"]}, "model": _PRIVATE})))
        self.assertIsNone(notice(_source({"allowManagedHooksOnly": False})))

    def test_managed_hooks_only_blocks_the_plugin_hooks(self):
        self.assertEqual(findings(_HOOKS_ONLY), ["PLUGIN_HOOKS_BLOCKED"])
        self.assertEqual(findings({**_HOOKS_ONLY, "enabledPlugins": {_PLUGIN: False}}), ["PLUGIN_DISABLED"])
        self.assertEqual(findings({**_HOOKS_ONLY, "allowManagedHooksOnly": "true"}), [])

    def test_a_force_enabled_plugin_keeps_its_hooks(self):
        self.assertEqual(findings({**_HOOKS_ONLY, "enabledPlugins": {_PLUGIN: True}}), [])
        self.assertEqual(findings({**_HOOKS_ONLY, "enabledPlugins": {"other@example": True}}),
                         ["PLUGIN_HOOKS_BLOCKED"])

    def test_the_marketplace_allowlist(self):
        admitted = [
            {"source": "github", "repo": "qualixar/jev-decision-layer"},
            {"source": "github", "repo": "qualixar/*"},
            {"source": "hostPattern", "hostPattern": "^github\\.com$"},
            "https://github.com/qualixar/jev-decision-layer/",
        ]
        for entry in admitted:
            with self.subTest(entry=entry):
                self.assertEqual(findings({"strictKnownMarketplaces": [entry]}), [])
        refused = [
            {"source": "github", "repo": "qualixar/jev-decision-layer", "ref": "main"},
            {"source": "github", "repo": "qualixar/*", "path": "plugins"},
            {"source": "github", "repo": "Qualixar/jev-decision-layer"},
            {"source": "github", "repo": "example-org/*"},
            {"source": "hostPattern", "hostPattern": "("},
            {"source": "hostPattern", "hostPattern": "^" + "a" * 300},
            {"source": "git", "url": "https://github.com/qualixar/jev-decision-layer.git"},
        ]
        for entry in refused:
            with self.subTest(entry=entry):
                self.assertEqual(findings({"strictKnownMarketplaces": [entry]}), ["MARKETPLACE_NOT_ALLOWED"])
        self.assertEqual(findings({"strictKnownMarketplaces": []}), ["MARKETPLACE_NOT_ALLOWED"])

    def test_the_allowlist_alias_applies_only_without_the_canonical_key(self):
        allowed = [{"source": "github", "repo": "qualixar/*"}]
        self.assertEqual(findings({"allowedMarketplaces": []}), ["MARKETPLACE_NOT_ALLOWED"])
        self.assertEqual(findings({"allowedMarketplaces": allowed}), [])
        self.assertEqual(findings({"strictKnownMarketplaces": allowed, "allowedMarketplaces": []}), [])
        self.assertEqual(findings({"strictKnownMarketplaces": [], "allowedMarketplaces": allowed}),
                         ["MARKETPLACE_NOT_ALLOWED"])

    def test_the_marketplace_blocklist(self):
        blocking = [
            {"source": "github", "repo": "Qualixar/*"},
            {"source": "github", "repo": "qualixar/jev-decision-layer"},
            {"source": "git", "url": "git@github.com:qualixar/jev-decision-layer.git"},
            {"source": "url", "url": "https://GitHub.com/qualixar/jev-decision-layer/"},
            {"source": "hostPattern", "hostPattern": "github"},
        ]
        for entry in blocking:
            with self.subTest(entry=entry):
                self.assertEqual(findings({"blockedMarketplaces": [entry]}), ["MARKETPLACE_BLOCKED"])
        for entry in ({"source": "github", "repo": "example-org/*"},
                      {"source": "github", "repo": "qualixar/jev-decision-layer", "ref": "v0"},
                      {"source": "skills-dir"}):
            with self.subTest(entry=entry):
                self.assertEqual(findings({"blockedMarketplaces": [entry]}), [])

    def test_the_mcp_server_allowlist(self):
        self.assertEqual(findings({"allowedMcpServers": [{"serverName": "qualixar-jev"}]}), [])
        self.assertEqual(findings({"allowedMcpServers": ["qualixar-jev"]}), [])
        launcher = "/opt/cache/qualixar-jev-decision-layer/1.0.12/scripts/launch-jev"
        self.assertEqual(findings({"allowedMcpServers": [{"serverCommand": [launcher]}]}), [])
        for servers in ([], [{"serverName": "example-tool"}],
                        [{"serverName": "qualixar-jev"}, {"serverCommand": ["/usr/bin/other"]}],
                        [{"serverUrl": "https://mcp.example.com/*"}]):
            with self.subTest(servers=servers):
                self.assertEqual(findings({"allowedMcpServers": servers}), ["MCP_SERVER_NOT_ALLOWED"])

    def test_a_malformed_allowlist_is_enforced_as_empty(self):
        # Claude Code enforces an invalid strictKnownMarketplaces or allowedMcpServers
        # value as an empty allowlist; null means the key is unset.
        for value in (True, "qualixar/*", 1, {"source": "github"}):
            with self.subTest(value=value):
                self.assertEqual(findings({"strictKnownMarketplaces": value}), ["MARKETPLACE_NOT_ALLOWED"])
                self.assertEqual(findings({"allowedMarketplaces": value}), ["MARKETPLACE_NOT_ALLOWED"])
                self.assertEqual(findings({"allowedMcpServers": value}), ["MCP_SERVER_NOT_ALLOWED"])
        self.assertEqual(findings({"strictKnownMarketplaces": None}), [])
        self.assertEqual(findings({"strictKnownMarketplaces": None, "allowedMarketplaces": []}),
                         ["MARKETPLACE_NOT_ALLOWED"])
        self.assertEqual(findings({"allowedMcpServers": None}), [])

    def test_only_well_formed_command_entries_count(self):
        name = {"serverName": "qualixar-jev"}
        # An invalid entry is stripped, so it cannot hide a valid name entry.
        for invalid in ({"serverCommand": "/opt/x"}, {"serverCommand": []}, {"serverCommand": [1]}):
            with self.subTest(invalid=invalid):
                self.assertEqual(findings({"allowedMcpServers": [invalid, name]}), [])
                self.assertEqual(findings({"allowedMcpServers": [invalid]}), ["MCP_SERVER_NOT_ALLOWED"])
        # Commands match exactly; the plugin starts its launcher with no arguments.
        launcher = "/opt/cache/scripts/launch-jev"
        self.assertEqual(findings({"allowedMcpServers": [{"serverCommand": [launcher, "--flag"]}]}),
                         ["MCP_SERVER_NOT_ALLOWED"])
        self.assertEqual(findings({"allowedMcpServers": [{"serverCommand": [launcher]}]}), [])

    def test_unexpected_value_types_never_raise(self):
        odd = [None, 1, 1.5, True, "text", b"bytes", [], {}, [[1]], {"k": [1]}, {1: 2}]
        known = set(host_policy._EFFECTS)
        for value in odd:
            document = {key: value for key in ("allowManagedHooksOnly", "enabledPlugins",
                                               "strictKnownMarketplaces", "blockedMarketplaces",
                                               "allowedMcpServers")}
            with self.subTest(value=value):
                self.assertLessEqual(set(findings(document)), known)
                nested = findings({"strictKnownMarketplaces": [{"source": value, "repo": value, "hostPattern": value}],
                                   "blockedMarketplaces": [{"source": value, "repo": value, "url": value}],
                                   "allowedMcpServers": [{"serverName": value, "serverCommand": value}, value]})
                self.assertIn("MARKETPLACE_NOT_ALLOWED", nested)
                self.assertIn("MCP_SERVER_NOT_ALLOWED", nested)

    def test_one_malformed_source_does_not_hide_another(self):
        class Hostile(dict):
            def get(self, *args):
                raise RuntimeError("hostile mapping")

            def __contains__(self, key):
                raise RuntimeError("hostile mapping")

        good = ("managed settings file", "/etc/claude-code/managed-settings.json", _HOOKS_ONLY)
        for bad in (("label", "/where", ["not", "a", "mapping"]), ("label", "/where", Hostile())):
            with self.subTest(bad=type(bad[2]).__name__):
                result = notice([bad, good])
                self.assertEqual([source["location"] for source in result["sources"]],
                                 ["/etc/claude-code/managed-settings.json"])
        self.assertIsNone(notice([("label", "/where", ["not", "a", "mapping"])]))


class NoticeTests(unittest.TestCase):
    def _full(self):
        document = {
            **_HOOKS_ONLY,
            "strictKnownMarketplaces": [{"source": "github", "repo": "example-org/" + _PRIVATE}],
            "allowedMcpServers": [{"serverName": _PRIVATE.lower()}],
            "companyAnnouncements": [_PRIVATE],
            "env": {"ORG_VALUE": _PRIVATE},
        }
        return notice(_source(document, "server-managed settings (cached copy)",
                              "/home/person/.claude/remote-settings.json"), home=Path("/home/person"))

    def test_the_notice_carries_codes_and_fixed_advice_only(self):
        result = self._full()
        text = json.dumps(result)
        self.assertNotIn(_PRIVATE, text)
        self.assertNotIn(_PRIVATE.lower(), text)
        self.assertNotIn("example-org", text)
        self.assertEqual(result["sources"], [{
            "source": "server-managed settings (cached copy)",
            "location": "~/.claude/remote-settings.json",
            "codes": ["PLUGIN_HOOKS_BLOCKED", "MARKETPLACE_NOT_ALLOWED", "MCP_SERVER_NOT_ALLOWED"],
        }])
        self.assertEqual(len(result["effects"]), 3)
        self.assertIn("not affected", result["unaffected"])
        self.assertIn("allowedMcpServers", result["options"][0])
        self.assertIn('{"source": "github", "repo": "qualixar/jev-decision-layer"}', result["options"][0])
        self.assertIn("CLAUDE.md", result["options"][1])

    def test_the_administrator_request_names_only_what_is_blocked(self):
        hooks = notice(_source(_HOOKS_ONLY))["options"][0]
        self.assertIn("force-enable", hooks)
        self.assertNotIn("marketplace", hooks)
        self.assertNotIn("allowedMcpServers", hooks)
        market = notice(_source({"strictKnownMarketplaces": []}))["options"][0]
        self.assertIn("allow the marketplace", market)
        self.assertNotIn("force-enable", market)

    def test_a_location_outside_home_is_shown_as_is(self):
        result = notice(_source(_HOOKS_ONLY), home=Path("/home/person"))
        self.assertEqual(result["sources"][0]["location"], "/etc/claude-code/managed-settings.json")
        near = notice(_source(_HOOKS_ONLY, where="/home/person2/x.json"), home=Path("/home/person"))
        self.assertEqual(near["sources"][0]["location"], "/home/person2/x.json")

    def test_a_failing_discovery_is_silent(self):
        with patch.object(host_policy, "discover", side_effect=RuntimeError("boom")):
            self.assertIsNone(notice())


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.home = self.root / "home"
        self.system = self.root / "system"
        self.prefs = self.root / "prefs"
        for directory in (self.home, self.system, self.prefs):
            directory.mkdir()

    def _discover(self, platform="linux", environ=None, **kwargs):
        return host_policy.discover(platform=platform, environ=environ or {}, home=self.home,
                                    system=self.system, managed_preferences=self.prefs,
                                    user=lambda: "person", **kwargs)

    def test_nothing_on_disk_finds_nothing(self):
        self.assertEqual(self._discover(), [])

    def test_the_server_cache_follows_the_configured_directory_and_the_default(self):
        configured = self.root / "config"
        configured.mkdir()
        (configured / "remote-settings.json").write_text(json.dumps(_HOOKS_ONLY))
        (self.home / ".claude").mkdir()
        (self.home / ".claude" / "remote-settings.json").write_text(json.dumps({"model": "x"}))
        found = self._discover(environ={"CLAUDE_CONFIG_DIR": str(configured)})
        self.assertEqual([where for _label, where, _doc in found],
                         [str(configured / "remote-settings.json"), str(self.home / ".claude" / "remote-settings.json")])
        relative = self._discover(environ={"CLAUDE_CONFIG_DIR": "config"})
        self.assertEqual(len(relative), 1)
        same = self._discover(environ={"CLAUDE_CONFIG_DIR": str(self.home / ".claude")})
        self.assertEqual(len(same), 1)

    def test_managed_files_and_drop_ins_are_read_in_name_order(self):
        (self.system / "managed-settings.json").write_text("{}")
        drop_in = self.system / "managed-settings.d"
        drop_in.mkdir()
        (drop_in / "20-b.json").write_text("{}")
        (drop_in / "10-a.json").write_text("{}")
        (drop_in / ".hidden.json").write_text("{}")
        (drop_in / "notes.txt").write_text("{}")
        (drop_in / "30-broken.json").write_text("{not json")
        (drop_in / "40-list.json").write_text("[]")
        found = self._discover()
        self.assertEqual([Path(where).name for _label, where, _doc in found],
                         ["managed-settings.json", "10-a.json", "20-b.json"])
        self.assertEqual({label for label, _where, _doc in found}, {host_policy.MANAGED_FILE})

    def test_the_drop_in_scan_is_bounded(self):
        drop_in = self.system / "managed-settings.d"
        drop_in.mkdir()
        for index in range(5):
            (drop_in / f"{index:02d}.json").write_text("{}")
        with patch.object(host_policy, "_MAX_SCANNED", 5):
            self.assertEqual(len(self._discover()), 5)
            (drop_in / "notes.txt").write_text("")
            # Past the scan limit no drop-in is read, rather than an arbitrary subset.
            self.assertEqual(self._discover(), [])
        with patch.object(host_policy, "_MAX_DROP_INS", 2):
            self.assertEqual([Path(where).name for _label, where, _doc in self._discover()], ["00.json", "01.json"])

    def test_an_oversized_or_unreadable_file_is_skipped(self):
        (self.system / "managed-settings.json").write_text(" " * (host_policy._MAX_BYTES + 1) + "{}")
        self.assertEqual(self._discover(), [])
        (self.system / "managed-settings.json").unlink()
        (self.system / "managed-settings.json").mkdir()
        self.assertEqual(self._discover(), [])

    def test_mdm_profiles_are_read_on_macos_only(self):
        (self.prefs / "person").mkdir()
        with (self.prefs / "person" / "com.anthropic.claudecode.plist").open("wb") as stream:
            plistlib.dump(_HOOKS_ONLY, stream)
        with (self.prefs / "com.anthropic.claudecode.plist").open("wb") as stream:
            plistlib.dump({"strictKnownMarketplaces": []}, stream, fmt=plistlib.FMT_BINARY)
        found = self._discover(platform="darwin")
        self.assertEqual([label for label, _where, _doc in found], [host_policy.MDM_PROFILE] * 2)
        self.assertEqual(found[0][2], _HOOKS_ONLY)
        self.assertEqual(self._discover(platform="linux"), [])
        (self.prefs / "com.anthropic.claudecode.plist").write_bytes(b"not a plist")
        self.assertEqual(len(self._discover(platform="darwin")), 1)
        unsafe = host_policy.discover(platform="darwin", environ={}, home=self.home, system=self.system,
                                      managed_preferences=self.prefs, user=lambda: "../person")
        self.assertEqual(len(unsafe), 0)

    def test_registry_policies_are_parsed_on_windows_only(self):
        values = [("HKLM\\x", json.dumps(_HOOKS_ONLY)), ("HKCU\\x", "{broken"), ("HKCU\\y", 7), ("HKCU\\z", "[]")]
        found = self._discover(platform="win32", registry=lambda: values)
        self.assertEqual(found, [(host_policy.REGISTRY, "HKLM\\x", _HOOKS_ONLY)])
        self.assertEqual(self._discover(platform="linux", registry=lambda: values), [])

    def test_the_registry_reader_uses_the_documented_key_and_value(self):
        seen = []

        class Key:
            def __init__(self, hive, path):
                seen.append((hive, path))
                if hive == "HKCU":
                    raise OSError("absent")

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        fake = types.SimpleNamespace(HKEY_LOCAL_MACHINE="HKLM", HKEY_CURRENT_USER="HKCU", OpenKey=Key,
                                     QueryValueEx=lambda key, name: ('{"a": 1}', 1) if name == "Settings" else None)
        with patch.dict(sys.modules, {"winreg": fake}):
            values = host_policy._registry_values()
        self.assertEqual(seen, [("HKLM", r"SOFTWARE\Policies\ClaudeCode"), ("HKCU", r"SOFTWARE\Policies\ClaudeCode")])
        self.assertEqual(values, [(r"HKLM\SOFTWARE\Policies\ClaudeCode", '{"a": 1}')])
        with patch.dict(sys.modules, {"winreg": None}):
            self.assertEqual(host_policy._registry_values(), [])

    def test_the_documented_system_directories(self):
        self.assertEqual(str(host_policy.system_directory("darwin")), "/Library/Application Support/ClaudeCode")
        self.assertEqual(str(host_policy.system_directory("linux")), "/etc/claude-code")
        self.assertIn("ClaudeCode", str(host_policy.system_directory("win32")))
        self.assertIsNone(host_policy.system_directory("sunos5"))


class SurfaceTests(unittest.TestCase):
    """No policy means 1.0.11 output exactly; a policy adds one clearly named notice."""

    _NOTICE = {"summary": "S", "sources": [], "effects": ["E1"], "unaffected": "U",
               "options": ["O1 <b>", "O2"], "check": "C"}

    def test_doctor_is_unchanged_without_a_policy_and_never_fails_because_of_one(self):
        from jev_auto.doctor import diagnose, exit_code

        with tempfile.TemporaryDirectory() as directory, \
                patch.dict("os.environ", {"XDG_STATE_HOME": directory}):
            plain = diagnose(Path(directory), claude_policy=lambda: None)
            flagged = diagnose(Path(directory), claude_policy=lambda: dict(self._NOTICE))
        self.assertEqual([check["id"] for check in plain["checks"]],
                         ["runtime_manifest", "python", "workspace_policy", "offline_gate",
                          "host_surface", "receipt_index"])
        self.assertEqual(flagged["checks"][:-1], plain["checks"])
        self.assertEqual(flagged["checks"][-1], {"id": "claude_code_policy", "status": "NOTICE", **self._NOTICE})
        self.assertEqual(flagged["overall"], plain["overall"])
        self.assertEqual(exit_code(flagged), exit_code(plain))

    def test_auto_status_is_unchanged_without_a_policy(self):
        from jev_auto import mcp

        legacy = types.SimpleNamespace(tools=lambda scope: [])
        replies = {"health": {"ok": True}, "stats": {"calls": 0}}
        caller = lambda request: replies[request["op"]]  # noqa: E731
        args = {"workspace_path": "/work/project"}
        plain = mcp.dispatch("jev_auto_status", args, legacy, caller=caller, claude_policy=lambda: None)
        self.assertEqual(plain, {"health": {"ok": True}, "usage": {"calls": 0}})
        flagged = mcp.dispatch("jev_auto_status", args, legacy, caller=caller, claude_policy=lambda: self._NOTICE)
        self.assertEqual(flagged, {**plain, "claude_code_policy": self._NOTICE})

    def test_the_wizard_shows_the_notice_only_when_a_policy_restricts_jev(self):
        from test_setup_one_time_defaults import _Server
        from src.adl.api.setup_controller import SetupController

        with tempfile.TemporaryDirectory() as directory, \
                patch.dict("os.environ", {"XDG_STATE_HOME": directory}):
            folder = Path(directory) / "Documents"
            folder.mkdir()
            pages = {}
            for name, provider in (("plain", lambda: None), ("flagged", lambda: dict(self._NOTICE)),
                                   ("broken", lambda: (_ for _ in ()).throw(RuntimeError("boom")))):
                wizard = _Server(SetupController(folder, local_config=lambda: None))
                wizard.server.claude_policy = provider
                try:
                    status, pages[name] = wizard.request("GET", "/setup")
                finally:
                    wizard.close()
                self.assertEqual(status, 200)
        self.assertNotIn("Claude Code on this computer", pages["plain"])
        self.assertNotIn("Claude Code on this computer", pages["broken"])
        self.assertIn("Claude Code on this computer", pages["flagged"])
        self.assertIn("<li>E1</li>", pages["flagged"])
        self.assertIn("<li>O1 &lt;b&gt;</li>", pages["flagged"])
        self.assertIn("You can still save this setup", pages["flagged"])

    def test_the_default_surfaces_use_the_detector(self):
        from jev_auto import doctor
        from src.adl.api import setup_server

        self.assertIs(setup_server.SetupServer.__init__.__kwdefaults__["claude_policy"], host_policy.notice)
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict("os.environ", {"XDG_STATE_HOME": directory}), \
                patch.object(doctor, "claude_policy_notice", return_value=dict(self._NOTICE)):
            result = doctor.diagnose(Path(directory))
        self.assertEqual(result["checks"][-1]["id"], "claude_code_policy")


class HostRegisterWarningTests(unittest.TestCase):
    """The quit-the-app warning belongs only to a write that actually happened."""

    def _run(self, argv, *, planned, installed):
        from jev_auto import cli

        out, err = io.StringIO(), io.StringIO()
        with patch("jev_auto.host_mcp.plan", return_value=planned), \
                patch("jev_auto.host_mcp.install", return_value=installed), \
                redirect_stdout(out), redirect_stderr(err):
            code = cli.main(argv)
        return code, err.getvalue()

    def test_no_warning_for_a_preview_or_an_unchanged_entry(self):
        base = ["host-register", "--host", "claude-desktop"]
        _code, preview = self._run(base, planned={"action": "create"}, installed=None)
        self.assertNotIn("Quit the Claude desktop app", preview)
        self.assertIn("Nothing written", preview)
        _code, unchanged = self._run(base + ["--write"], planned=None, installed={"action": "unchanged"})
        self.assertNotIn("Quit the Claude desktop app", unchanged)

    def test_the_warning_follows_a_real_write(self):
        code, written = self._run(["host-register", "--host", "claude-desktop", "--write"],
                                  planned=None, installed={"action": "update", "written": True})
        self.assertEqual(code, 0)
        self.assertIn("Quit the Claude desktop app", written)
        _code, other = self._run(["host-register", "--host", "antigravity", "--write"],
                                 planned=None, installed={"action": "update", "written": True})
        self.assertNotIn("Quit the Claude desktop app", other)


if __name__ == "__main__":
    unittest.main()
