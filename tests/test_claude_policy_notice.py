"""The notice a person sees when a Claude Code organization policy stops Jev.

It is read-only and advisory. On a computer without such a policy every
surface (doctor, jev_auto_status, the setup wizard) produces exactly the 1.0.11
output. With a policy it carries fixed codes and fixed advice, never another
value from the policy, and it never suggests working around a deliberate block.
"""

from __future__ import annotations

import io
import json
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

from jev_auto import __version__, host_policy  # noqa: E402
from jev_auto.policy_rules import PLUGIN_ID, PLUGIN_ROOT, SCOPED_SERVER_NAME  # noqa: E402
from jev_auto.policy_sources import RANK_FILE, RANK_SERVER, Machine, PolicySource  # noqa: E402

_PRIVATE = "ORG-PRIVATE-MARKER"
HOME = Path("/home/person")


def machine(*documents, managed_mcp=None, marketplace=None, rank=RANK_FILE,
            location="/etc/claude-code", label="managed settings file"):
    sources = [PolicySource(label, location, document, rank) for document in documents]
    return Machine(sources, managed_mcp, "/etc/claude-code/managed-mcp.json" if managed_mcp else None,
                   marketplace, None, [HOME / ".claude"])


def notice(*documents, **kwargs):
    return host_policy.notice(machine(*documents, **kwargs), home=HOME, environ={"HOME": str(HOME)})


class ContentTests(unittest.TestCase):
    def test_nothing_that_stops_jev_means_no_notice(self):
        self.assertIsNone(host_policy.notice(machine(), home=HOME, environ={}))
        self.assertIsNone(notice({"permissions": {"deny": ["Bash(curl *)"]}, "model": _PRIVATE}))
        self.assertIsNone(notice({"allowManagedHooksOnly": False}))

    def test_the_notice_carries_codes_labels_and_fixed_advice_only(self):
        result = notice({"allowManagedHooksOnly": True,
                         "strictKnownMarketplaces": [{"source": "github", "repo": "example-org/" + _PRIVATE}],
                         "allowedMcpServers": [{"serverName": _PRIVATE.lower()}],
                         "companyAnnouncements": [_PRIVATE], "env": {"ORG_VALUE": _PRIVATE}},
                        rank=RANK_SERVER, label="server-managed settings (cached copy)",
                        location=str(HOME / ".claude-custom" / "remote-settings.json"))
        text = json.dumps(result)
        for secret in (_PRIVATE, _PRIVATE.lower(), "example-org", "/home/person/"):
            self.assertNotIn(secret, text)
        self.assertEqual(result["codes"], ["MARKETPLACE_NOT_ALLOWED", "PLUGIN_HOOKS_BLOCKED", "MCP_SERVER_NOT_ALLOWED"])
        self.assertEqual(result["sources"], [{"source": "server-managed settings (cached copy)",
                                              "location": "~/.claude-custom/remote-settings.json"}])
        self.assertEqual(result["effects"], [host_policy._EFFECTS[code] for code in result["codes"]])
        self.assertIn("Codex, VS Code, Antigravity or Hermes", result["unaffected"])
        self.assertIn("/status", result["check"])

    def test_one_administrator_request_names_exactly_what_is_blocked(self):
        hooks = notice({"allowManagedHooksOnly": True})["options"][0]
        self.assertEqual(hooks, "Ask your Claude Code administrator to force-enable " + PLUGIN_ID
                         + " in managed enabledPlugins; hooks of a force-enabled plugin still run under "
                           "allowManagedHooksOnly.")
        market = notice({"strictKnownMarketplaces": []})["options"][0]
        self.assertIn('add {"source": "github", "repo": "qualixar/jev-decision-layer"} to strictKnownMarketplaces', market)
        self.assertIn("extraKnownMarketplaces under the name qualixar", market)
        self.assertNotIn("force-enable", market)
        both = notice({"allowManagedHooksOnly": True, "strictKnownMarketplaces": []})["options"][0]
        self.assertEqual(both.count("; "), 2)

    def test_a_local_install_is_not_told_to_allow_the_public_source(self):
        local = notice({"strictKnownMarketplaces": []},
                       marketplace={"source": "directory", "path": "/opt/checkout"})["options"][0]
        self.assertIn("the source your Jev marketplace was added from", local)
        self.assertNotIn("qualixar/jev-decision-layer", local)
        self.assertNotIn("/opt/checkout", local)

    def test_the_mcp_allowlist_request_gives_the_exact_portable_command(self):
        result = notice({"allowedMcpServers": [{"serverName": "qualixar-jev"}]})
        launcher = str(PLUGIN_ROOT / "scripts" / "launch-jev")
        self.assertIn(json.dumps({"serverCommand": [launcher]}, separators=(", ", ": ")), result["options"][0])
        cached = HOME / ".claude" / "plugins" / "cache" / "qualixar" / "qualixar-jev-decision-layer" / __version__
        with patch.object(host_policy, "PLUGIN_ROOT", cached), \
                patch("jev_auto.policy_rules.PLUGIN_ROOT", cached):
            portable = notice({"allowedMcpServers": []})["options"][0]
        self.assertIn('"${HOME}/.claude/plugins/cache/qualixar/qualixar-jev-decision-layer/', portable)
        self.assertIn("changes with each Jev upgrade", portable)
        self.assertNotIn("changes with each Jev upgrade", result["options"][0])

    def test_a_blocklist_entry_must_be_removed_not_outweighed(self):
        result = notice({"blockedMarketplaces": [{"source": "github", "repo": "qualixar/*"}]})
        self.assertIn("remove the blockedMarketplaces entry", result["options"][0])
        self.assertIn("allowing it as well does nothing", result["options"][0])

    def test_a_deliberate_block_is_never_worked_around(self):
        for document in ({"enabledPlugins": {PLUGIN_ID: False}},
                         {"blockedMarketplaces": [{"source": "github", "repo": "qualixar/*"}]},
                         {"deniedMcpServers": [{"serverName": SCOPED_SERVER_NAME}]},
                         {"allowManagedHooksOnly": True, "enabledPlugins": {PLUGIN_ID: False}}):
            with self.subTest(document=document):
                options = notice(document)["options"]
                self.assertTrue(options[-1].startswith("Your organization has chosen to block Jev"))
                self.assertFalse(any("host-register" in option or "claude mcp add" in option for option in options))

    def test_the_workaround_is_offered_only_where_the_organization_may_permit_it(self):
        options = notice({"allowManagedHooksOnly": True})["options"]
        self.assertTrue(options[1].startswith("If your organization permits it, in the Claude desktop app"))
        self.assertIn("host-register --host claude-desktop --write", options[1])
        self.assertIn(host_policy.GUIDE, options[1])
        self.assertIn("the plugin's tools still load", options[2])

    def test_the_cli_route_is_suggested_only_when_nothing_blocks_a_user_server(self):
        open_route = notice({"strictKnownMarketplaces": []})["options"]
        self.assertTrue(any("claude mcp add qualixar-jev --" in option for option in open_route))
        for extra in ({"allowedMcpServers": []}, {"strictPluginOnlyCustomization": True},
                      {"deniedMcpServers": [{"serverName": "qualixar-jev"}]}):
            with self.subTest(extra=extra):
                options = notice({"strictKnownMarketplaces": [], **extra})["options"]
                self.assertFalse(any("claude mcp add" in option for option in options))
        exclusive = notice({"strictKnownMarketplaces": []}, managed_mcp="excludes-jev")["options"]
        self.assertFalse(any("claude mcp add" in option for option in exclusive))

    def test_a_copy_inside_a_protected_folder_is_flagged(self):
        protected = HOME / "Documents" / "jev" / "plugins" / "qualixar-jev-decision-layer"
        with patch.object(host_policy, "PLUGIN_ROOT", protected):
            desktop = notice({"allowManagedHooksOnly": True})["options"][1]
        self.assertIn("inside Documents, Desktop or Downloads", desktop)
        self.assertIn("~/Documents/jev/plugins/qualixar-jev-decision-layer/scripts/jev", desktop)
        elsewhere = HOME / ".local" / "share" / "jev" / "plugins" / "qualixar-jev-decision-layer"
        with patch.object(host_policy, "PLUGIN_ROOT", elsewhere):
            self.assertNotIn("inside Documents", notice({"allowManagedHooksOnly": True})["options"][1])

    def test_exclusive_mcp_configuration_names_its_file(self):
        result = notice(managed_mcp="excludes-jev")
        self.assertEqual(result["codes"], ["MCP_EXCLUSIVE_CONFIG"])
        self.assertIn({"source": "managed MCP configuration", "location": "/etc/claude-code/managed-mcp.json"},
                      result["sources"])
        self.assertIn("define the qualixar-jev server in managed-mcp.json", result["options"][0])

    def test_home_is_shortened_only_on_a_path_boundary(self):
        self.assertEqual(host_policy._display("/home/person/x", HOME), "~/x")
        self.assertEqual(host_policy._display("/home/person2/x", HOME), "/home/person2/x")
        self.assertEqual(host_policy._display("/home/person/x", Path("/home/person/")), "~/x")
        self.assertEqual(host_policy._portable("/home/person/a", HOME), "${HOME}/a")
        self.assertEqual(host_policy._portable("/opt/a", HOME), "/opt/a")

    def test_a_failing_discovery_or_judgement_is_silent(self):
        with patch.object(host_policy, "discover", side_effect=RuntimeError("boom")):
            self.assertIsNone(host_policy.notice(home=HOME))
        with patch.object(host_policy, "judge", side_effect=RuntimeError("boom")):
            self.assertIsNone(notice({"allowManagedHooksOnly": True}))


class SurfaceTests(unittest.TestCase):
    """No policy means 1.0.11 output exactly; a policy adds one clearly named notice."""

    _NOTICE = {"summary": "S", "codes": ["X"], "sources": [], "effects": ["E1"], "unaffected": "U",
               "options": ["O1 <b> `cmd`", "O2"], "check": "C"}

    def test_doctor_is_unchanged_without_a_policy_and_never_fails_because_of_one(self):
        from jev_auto.doctor import diagnose, exit_code

        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {"XDG_STATE_HOME": directory}):
            plain = diagnose(Path(directory), claude_policy=lambda: None)
            flagged = diagnose(Path(directory), claude_policy=lambda: dict(self._NOTICE))
        self.assertEqual([check["id"] for check in plain["checks"]],
                         ["runtime_manifest", "python", "workspace_policy", "offline_gate", "host_surface",
                          "receipt_index"])
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

        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {"XDG_STATE_HOME": directory}):
            folder = Path(directory) / "Documents"
            folder.mkdir()
            pages = {}
            for name, provider in (("plain", lambda: None), ("flagged", lambda: dict(self._NOTICE)),
                                   ("broken", lambda: (_ for _ in ()).throw(RuntimeError("boom"))),
                                   ("odd", lambda: ["not", "a", "dict"])):
                wizard = _Server(SetupController(folder, local_config=lambda: None))
                wizard.server.claude_policy = provider
                try:
                    status, pages[name] = wizard.request("GET", "/setup")
                finally:
                    wizard.close()
                self.assertEqual(status, 200)
        for name in ("plain", "broken", "odd"):
            self.assertNotIn("Claude Code on this computer", pages[name])
        flagged = pages["flagged"]
        self.assertIn("Claude Code on this computer", flagged)
        self.assertLess(flagged.index("<li>E1</li>"), flagged.index("<li>O1"))
        self.assertIn("<li>O1 &lt;b&gt; <code>cmd</code></li>", flagged)
        self.assertIn("You can still save this setup", flagged)

    def test_the_default_surfaces_use_the_detector(self):
        from jev_auto import doctor, mcp
        from src.adl.api import setup_server

        self.assertIs(setup_server.SetupServer.__init__.__kwdefaults__["claude_policy"], host_policy.notice)
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {"XDG_STATE_HOME": directory}), \
                patch.object(doctor, "claude_policy_notice", return_value=dict(self._NOTICE)):
            self.assertEqual(doctor.diagnose(Path(directory))["checks"][-1]["id"], "claude_code_policy")
        with patch.object(host_policy, "notice", return_value=dict(self._NOTICE)) as detector:
            self.assertEqual(mcp._claude_policy(), self._NOTICE)
        detector.assert_called_once_with()


class HostRegisterMessageTests(unittest.TestCase):
    def _run(self, argv, *, planned=None, installed=None, platform="darwin"):
        from jev_auto import cli

        out, err = io.StringIO(), io.StringIO()
        with patch("jev_auto.host_mcp.plan", return_value=planned), \
                patch("jev_auto.host_mcp.install", return_value=installed), \
                patch.object(cli.sys, "platform", platform), redirect_stdout(out), redirect_stderr(err):
            code = cli.main(argv)
        return code, err.getvalue()

    def test_no_reminder_for_a_preview_or_an_unchanged_entry(self):
        base = ["host-register", "--host", "claude-desktop"]
        _code, preview = self._run(base, planned={"action": "create", "entry": {"command": "/opt/x"}})
        self.assertNotIn("quit it now", preview)
        self.assertIn("Nothing written", preview)
        _code, unchanged = self._run(base + ["--write"], installed={"action": "unchanged"})
        self.assertNotIn("quit it now", unchanged)

    def test_the_reminder_follows_a_real_write_and_says_what_to_do(self):
        code, written = self._run(["host-register", "--host", "claude-desktop", "--write"],
                                  installed={"action": "update", "written": True, "entry": {"command": "/opt/x"}})
        self.assertEqual(code, 0)
        self.assertIn("quit it now and run this command again", written)
        _code, other = self._run(["host-register", "--host", "antigravity", "--write"],
                                 installed={"action": "update", "written": True})
        self.assertNotIn("quit it now", other)

    def test_a_launcher_in_a_protected_folder_is_flagged_on_macos_only(self):
        inside = {"action": "create", "entry": {"command": str(Path.home() / "Documents" / "jev" / "launch-jev")}}
        _code, mac = self._run(["host-register", "--host", "claude-desktop"], planned=inside)
        self.assertIn("inside Documents, Desktop or Downloads", mac)
        _code, linux = self._run(["host-register", "--host", "claude-desktop"], planned=inside, platform="linux")
        self.assertNotIn("inside Documents", linux)
        outside = {"action": "create", "entry": {"command": str(Path.home() / ".local" / "jev" / "launch-jev")}}
        _code, fine = self._run(["host-register", "--host", "claude-desktop"], planned=outside)
        self.assertNotIn("inside Documents", fine)


if __name__ == "__main__":
    unittest.main()
