"""Would Claude Code stop Jev? The documented rules, and the ones checked live.

Marketplace and precedence rules are quoted from Claude Code's managed
settings and plugin documentation. The MCP allow and deny behaviour for a
plugin's server was measured by running Claude Code 2.1.229 against the
installed plugin: a plain server name matches nothing, the scoped name
plugin:<plugin>:<server> matches in a denylist, and an allowlist admits the
server only through a serverCommand equal to the command it runs.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "plugins" / "qualixar-jev-decision-layer" / "runtime"
sys.path.insert(0, str(RUNTIME))

from jev_auto import __version__, policy_rules as pr  # noqa: E402
from jev_auto.policy_sources import (RANK_FILE, RANK_MDM, RANK_SERVER, RANK_USER, Machine,  # noqa: E402
                                     PolicySource, merge_documents)

PUBLIC = pr.PUBLIC_SOURCE
LAUNCHER = str(pr.PLUGIN_ROOT / "scripts" / "launch-jev")
HOME = Path("/home/person")
CACHED = str(HOME / ".claude" / "plugins" / "cache" / "qualixar" / "qualixar-jev-decision-layer"
             / __version__ / "scripts" / "launch-jev")


def source(document, rank=RANK_FILE, label="managed settings file"):
    return PolicySource(label, "/where", document, rank)


def machine(*sources, managed_mcp=None, marketplace=None, user=None):
    return Machine(list(sources), managed_mcp, "/sys/managed-mcp.json" if managed_mcp else None,
                   marketplace, user, [HOME / ".claude"])


def codes(*documents, environ=None, **kwargs):
    sources = [document if isinstance(document, PolicySource) else source(document) for document in documents]
    return pr.judge(machine(*sources, **kwargs), {"HOME": str(HOME)} if environ is None else environ).codes


class SelectionTests(unittest.TestCase):
    """"Claude Code by default uses the first source that delivers at least one policy key"."""

    def test_the_first_source_with_a_policy_key_decides(self):
        self.assertEqual(codes(source({"model": "x"}, RANK_SERVER), source({"allowManagedHooksOnly": True})), [])
        self.assertEqual(codes(source({"allowManagedHooksOnly": True}, RANK_SERVER), source({"model": "x"})),
                         ["PLUGIN_HOOKS_BLOCKED"])

    def test_control_keys_and_nulls_are_not_policy_keys(self):
        for document in ({"managedSourcesBehavior": "first-wins"}, {"wslInheritsWindowsSettings": True},
                         {"model": None}):
            with self.subTest(document=document):
                self.assertEqual(codes(source(document, RANK_SERVER), source({"allowManagedHooksOnly": True})),
                                 ["PLUGIN_HOOKS_BLOCKED"])

    def test_merge_applies_every_admin_source(self):
        merging = source({"managedSourcesBehavior": "merge", "model": "x"}, RANK_SERVER)
        self.assertEqual(codes(merging, source({"allowManagedHooksOnly": True})), ["PLUGIN_HOOKS_BLOCKED"])
        self.assertEqual(pr.deciding_sources([source({"model": "x"}, RANK_SERVER), source({"y": 1})])[0].rank,
                         RANK_SERVER)

    def test_the_user_registry_applies_only_without_an_admin_source(self):
        hkcu = source({"allowManagedHooksOnly": True}, RANK_USER, "user registry policy (HKCU)")
        self.assertEqual(codes(hkcu), ["PLUGIN_HOOKS_BLOCKED"])
        self.assertEqual(codes(source({"model": "x"}, RANK_MDM), hkcu), [])
        # "A document is present when it sets any policy key": control keys alone are not.
        self.assertEqual(codes(source({"managedSourcesBehavior": "first-wins"}, RANK_SERVER), hkcu),
                         ["PLUGIN_HOOKS_BLOCKED"])

    def test_sources_are_ranked_whatever_order_they_arrive_in(self):
        self.assertEqual(codes(source({"allowManagedHooksOnly": True}), source({"model": "x"}, RANK_SERVER)), [])


class HookTests(unittest.TestCase):
    def test_an_unreadable_lock_reads_as_its_restrictive_value(self):
        for value in (True, "true", 1, "yes", {}, [], "False"):
            with self.subTest(value=value):
                self.assertTrue(pr.lock(value))
                self.assertEqual(codes({"allowManagedHooksOnly": value}), ["PLUGIN_HOOKS_BLOCKED"])
        for value in (False, "false", None):
            with self.subTest(value=value):
                self.assertFalse(pr.lock(value))
                self.assertEqual(codes({"allowManagedHooksOnly": value}), [])

    def test_a_force_enabled_plugin_keeps_its_hooks_including_through_a_drop_in(self):
        forced = {"enabledPlugins": {pr.PLUGIN_ID: True}}
        self.assertEqual(codes({"allowManagedHooksOnly": True, **forced}), [])
        self.assertEqual(codes(merge_documents({"allowManagedHooksOnly": True}, forced)), [])
        self.assertEqual(codes({"allowManagedHooksOnly": True, "enabledPlugins": {"other@x": True}}),
                         ["PLUGIN_HOOKS_BLOCKED"])
        self.assertEqual(codes({"allowManagedHooksOnly": True, "enabledPlugins": {pr.PLUGIN_ID: "true"}}),
                         ["PLUGIN_HOOKS_BLOCKED"])

    def test_disable_all_hooks_needs_a_literal_true_and_force_enabling_does_not_help(self):
        forced = {"enabledPlugins": {pr.PLUGIN_ID: True}}
        self.assertEqual(codes({"disableAllHooks": True, "allowManagedHooksOnly": True, **forced}),
                         ["ALL_HOOKS_DISABLED"])
        self.assertEqual(codes({"disableAllHooks": "true"}), [])  # an invalid value is dropped
        self.assertEqual(codes({"disableAllHooks": False}), [])

    def test_a_plugin_turned_off_is_reported_once(self):
        self.assertEqual(codes({"allowManagedHooksOnly": True, "enabledPlugins": {pr.PLUGIN_ID: False}}),
                         ["PLUGIN_DISABLED"])


class MarketplaceAllowTests(unittest.TestCase):
    def test_entries_that_admit_the_public_source(self):
        for entry in ({"source": "github", "repo": "qualixar/jev-decision-layer"},
                      {"source": "github", "repo": "qualixar/*"},
                      {"source": "hostPattern", "hostPattern": "^github\\.com$"},
                      {"source": "hostPattern", "hostPattern": "github"},
                      {"source": "hostPattern", "hostPattern": "hub\\.com$"},
                      {"source": "hostPattern", "hostPattern": "^github\\.com$" + " " * 0}):
            with self.subTest(entry=entry):
                self.assertTrue(pr.admits_marketplace(entry, PUBLIC))

    def test_entries_that_do_not_admit_it(self):
        for entry in ({"source": "github", "repo": "qualixar/jev-decision-layer", "ref": "main"},
                      {"source": "github", "repo": "qualixar/*", "path": "plugins"},
                      {"source": "github", "repo": "qualixar/*", "ref": "main"},
                      {"source": "github", "repo": "Qualixar/jev-decision-layer"},
                      {"source": "github", "repo": "Qualixar/*"},
                      {"source": "github", "repo": "example-org/*"},
                      {"source": "github", "repo": "*/jev-decision-layer"},
                      {"source": "github", "repo": "qualixar/jev-*"},
                      {"source": "github", "repo": ["qualixar/*"]},
                      {"source": "hostPattern", "hostPattern": "("},
                      {"source": "hostPattern", "hostPattern": "(?i)^GITHUB\\.COM$"},
                      {"source": "hostPattern", "hostPattern": "\\Agithub\\.com\\Z"},
                      {"source": "git", "url": "https://github.com/qualixar/jev-decision-layer.git"},
                      {"source": "url", "url": "https://github.com/qualixar/jev-decision-layer"},
                      {"source": "pathPattern", "pathPattern": ".*"},
                      {"source": "skills-dir"},
                      "https://github.com/qualixar/jev-decision-layer",
                      {"source": ["github"]}, None, 7):
            with self.subTest(entry=entry):
                self.assertFalse(pr.admits_marketplace(entry, PUBLIC))

    def test_the_host_pattern_length_limit(self):
        at_limit = "^github\\.com$" + "|x" * ((1024 - 13) // 2)
        self.assertLessEqual(len(at_limit), 1024)
        self.assertTrue(pr.admits_marketplace({"source": "hostPattern", "hostPattern": at_limit}, PUBLIC))
        over = "github" + "|" + "x" * (1024 - 6)
        self.assertEqual(len(over), 1025)
        self.assertFalse(pr.admits_marketplace({"source": "hostPattern", "hostPattern": over}, PUBLIC))

    def test_the_source_the_user_actually_installed_from_is_the_one_matched(self):
        local = {"source": "directory", "path": "/opt/checkout"}
        self.assertTrue(pr.admits_marketplace({"source": "directory", "path": "/opt/checkout"}, local))
        self.assertFalse(pr.admits_marketplace({"source": "directory", "path": "/opt/other"}, local))
        self.assertFalse(pr.admits_marketplace({"source": "github", "repo": "qualixar/*"}, local))
        self.assertFalse(pr.admits_marketplace({"source": "hostPattern", "hostPattern": "."}, local))
        git = {"source": "git", "url": "git@gitlab.example.com:tools/jev.git"}
        self.assertTrue(pr.admits_marketplace({"source": "git", "url": "git@gitlab.example.com:tools/jev.git"}, git))
        self.assertTrue(pr.admits_marketplace({"source": "hostPattern", "hostPattern": "^gitlab\\.example\\.com$"}, git))
        self.assertEqual(codes({"strictKnownMarketplaces": [{"source": "github", "repo": "qualixar/*"}]},
                               marketplace=local), ["MARKETPLACE_NOT_ALLOWED"])

    def test_an_allowlist_value_and_its_alias(self):
        allowed = [{"source": "github", "repo": "qualixar/*"}]
        self.assertEqual(codes({"strictKnownMarketplaces": []}), ["MARKETPLACE_NOT_ALLOWED"])
        self.assertEqual(codes({"strictKnownMarketplaces": True}), ["MARKETPLACE_NOT_ALLOWED"])
        self.assertEqual(codes({"strictKnownMarketplaces": allowed}), [])
        self.assertEqual(codes({"allowedMarketplaces": []}), ["MARKETPLACE_NOT_ALLOWED"])
        self.assertEqual(codes({"strictKnownMarketplaces": allowed, "allowedMarketplaces": []}), [])
        self.assertEqual(codes({"strictKnownMarketplaces": None, "allowedMarketplaces": []}), ["MARKETPLACE_NOT_ALLOWED"])
        self.assertEqual(codes({"strictKnownMarketplaces": None}), [])

    def test_allowlists_in_drop_ins_combine(self):
        merged = merge_documents({"strictKnownMarketplaces": [{"source": "github", "repo": "anthropics/x"}]},
                                 {"strictKnownMarketplaces": [{"source": "github", "repo": "qualixar/*"}]})
        self.assertEqual(codes(merged), [])


class MarketplaceBlockTests(unittest.TestCase):
    def test_entries_that_block_the_public_source(self):
        for entry in ({"source": "github", "repo": "Qualixar/*"},
                      {"source": "github", "repo": "qualixar/jev-decision-layer"},
                      {"source": "git", "url": "git@github.com:qualixar/jev-decision-layer.git"},
                      {"source": "git", "url": "ssh://git@github.com/qualixar/jev-decision-layer"},
                      {"source": "git", "url": "https://GitHub.com/qualixar/jev-decision-layer/"},
                      {"source": "hostPattern", "hostPattern": "github"}):
            with self.subTest(entry=entry):
                self.assertTrue(pr.blocks_marketplace(entry, PUBLIC))
                self.assertEqual(codes({"blockedMarketplaces": [entry]}), ["MARKETPLACE_BLOCKED"])

    def test_entries_that_do_not_block_it(self):
        for entry in ({"source": "github", "repo": "example-org/*"},
                      {"source": "github", "repo": "qualixar/jev-decision-layer", "ref": "v0"},
                      {"source": "github", "repo": "qualixar/jev-decision-layer", "path": "x"},
                      {"source": "github", "repo": "Qualixar/jev-decision-layer"},
                      {"source": "url", "url": "https://github.com/qualixar/jev-decision-layer"},
                      {"source": "skills-dir"}, {"source": "github"}, "qualixar/*", None):
            with self.subTest(entry=entry):
                self.assertFalse(pr.blocks_marketplace(entry, PUBLIC))

    def test_a_local_source_is_blocked_only_by_its_own_path(self):
        local = {"source": "directory", "path": "/opt/checkout"}
        self.assertTrue(pr.blocks_marketplace({"source": "directory", "path": "/opt/checkout"}, local))
        self.assertFalse(pr.blocks_marketplace({"source": "github", "repo": "qualixar/*"}, local))


class McpServerTests(unittest.TestCase):
    def test_what_running_claude_code_showed_for_the_allowlist(self):
        cases = [
            ([{"serverName": "qualixar-jev"}], ["MCP_SERVER_NOT_ALLOWED"]),
            ([{"serverName": "plugin:qualixar-jev-decision-layer:qualixar-jev"}], ["MCP_SERVER_NOT_ALLOWED"]),
            ([{"serverCommand": [LAUNCHER]}], []),
            ([{"serverCommand": [LAUNCHER, "--x"]}], ["MCP_SERVER_NOT_ALLOWED"]),
            ([{"serverCommand": [LAUNCHER]}, {"serverName": "other"}], []),
            ([{"serverCommand": ["/usr/bin/true"]}], ["MCP_SERVER_NOT_ALLOWED"]),
        ]
        for servers, expected in cases:
            with self.subTest(servers=servers):
                self.assertEqual(codes({"allowedMcpServers": servers}), expected)

    def test_what_running_claude_code_showed_for_the_denylist(self):
        self.assertEqual(codes({"deniedMcpServers": [{"serverName": pr.SCOPED_SERVER_NAME}]}), ["MCP_SERVER_DENIED"])
        self.assertEqual(codes({"deniedMcpServers": [{"serverCommand": [LAUNCHER]}]}), ["MCP_SERVER_DENIED"])
        self.assertEqual(codes({"deniedMcpServers": [{"serverName": "qualixar-jev"}]}), [])

    def test_a_command_entry_is_expanded_and_must_match_exactly(self):
        portable = "${HOME}/.claude/plugins/cache/qualixar/qualixar-jev-decision-layer/" + __version__ + "/scripts/launch-jev"
        self.assertEqual(codes({"allowedMcpServers": [{"serverCommand": [portable]}]}), [])
        self.assertEqual(codes({"allowedMcpServers": [{"serverCommand": [portable]}]}, environ={}),
                         ["MCP_SERVER_NOT_ALLOWED"])
        fallback = "${MISSING:-/home/person}/.claude/plugins/cache/qualixar/qualixar-jev-decision-layer/" + __version__ + "/scripts/launch-jev"
        self.assertEqual(codes({"allowedMcpServers": [{"serverCommand": [fallback]}]}, environ={}), [])
        older = CACHED.replace(__version__, "0.0.1")
        self.assertEqual(codes({"allowedMcpServers": [{"serverCommand": [older]}]}), ["MCP_SERVER_NOT_ALLOWED"])
        self.assertEqual(codes({"allowedMcpServers": [{"serverCommand": ["launch-jev"]}]}), ["MCP_SERVER_NOT_ALLOWED"])

    def test_only_well_formed_entries_count(self):
        good = {"serverCommand": [LAUNCHER]}
        for invalid in ({"serverCommand": [LAUNCHER], "serverName": "x"}, {"serverCommand": LAUNCHER},
                        {"serverCommand": []}, {"serverCommand": [LAUNCHER, 1]}, {"other": 1}, "qualixar-jev", None):
            with self.subTest(invalid=invalid):
                self.assertEqual(codes({"allowedMcpServers": [invalid]}), ["MCP_SERVER_NOT_ALLOWED"])
                self.assertEqual(codes({"allowedMcpServers": [invalid, good]}), [])

    def test_allowlist_values(self):
        self.assertEqual(codes({"allowedMcpServers": []}), ["MCP_SERVER_NOT_ALLOWED"])
        self.assertEqual(codes({"allowedMcpServers": True}), ["MCP_SERVER_NOT_ALLOWED"])
        self.assertEqual(codes({"allowedMcpServers": None}), [])

    def test_the_users_own_allowlist_broadens_unless_the_organization_locks_it(self):
        mine = [{"serverCommand": [LAUNCHER]}]
        self.assertEqual(codes({"allowedMcpServers": []}, user=mine), [])
        self.assertEqual(codes({"allowedMcpServers": [], "allowManagedMcpServersOnly": True}, user=mine),
                         ["MCP_SERVER_NOT_ALLOWED"])
        self.assertEqual(codes({"model": "x"}, user=[]), [])  # a user's own list is not an organization block

    def test_the_lock_and_the_denylist_are_read_from_every_admin_source(self):
        deciding = source({"model": "x"}, RANK_SERVER)
        locked = source({"allowManagedMcpServersOnly": True, "allowedMcpServers": []})
        self.assertEqual(codes(deciding, locked), ["MCP_SERVER_NOT_ALLOWED"])
        denying = source({"deniedMcpServers": [{"serverName": pr.SCOPED_SERVER_NAME}]})
        self.assertEqual(codes(deciding, denying), ["MCP_SERVER_DENIED"])
        user_deny = source({"deniedMcpServers": [{"serverName": pr.SCOPED_SERVER_NAME}]}, RANK_USER)
        self.assertEqual(codes(deciding, user_deny), [])

    def test_exclusive_managed_mcp_configuration(self):
        self.assertEqual(codes(managed_mcp="excludes-jev"), ["MCP_EXCLUSIVE_CONFIG"])
        self.assertEqual(codes(managed_mcp="defines-jev"), [])
        self.assertTrue(pr.judge(machine(managed_mcp="defines-jev"), {}).user_mcp_blocked)

    def test_what_blocks_a_user_registered_server(self):
        for value, blocked in ((True, True), (["mcp"], True), ({"mcp": True}, True), (["skills"], False),
                               ({"mcp": False}, False), (False, False), ("mcp", False)):
            with self.subTest(value=value):
                verdict = pr.judge(machine(source({"strictPluginOnlyCustomization": value})), {})
                self.assertEqual(verdict.user_mcp_blocked, blocked)
        named = pr.judge(machine(source({"deniedMcpServers": [{"serverName": "qualixar-jev"}]})), {})
        self.assertTrue(named.user_mcp_blocked)
        self.assertEqual(named.codes, [])


class LauncherTests(unittest.TestCase):
    def test_the_commands_claude_code_may_run_for_this_plugin(self):
        commands = pr.launcher_commands([HOME / ".claude", Path("/cfg")], {"CLAUDE_PLUGIN_ROOT": "/plugin/root"})
        self.assertIn(LAUNCHER, commands)
        self.assertIn(CACHED, commands)
        self.assertIn("/cfg/plugins/cache/qualixar/qualixar-jev-decision-layer/" + __version__ + "/scripts/launch-jev",
                      commands)
        self.assertIn("/plugin/root/scripts/launch-jev", commands)
        self.assertNotIn("relative/scripts/launch-jev",
                         pr.launcher_commands([], {"CLAUDE_PLUGIN_ROOT": "relative"}))


class HelperAndOrderTests(unittest.TestCase):
    def test_a_policy_helper_makes_every_other_key_unreadable(self):
        self.assertEqual(codes({"policyHelper": {"path": "/x"}, "allowManagedHooksOnly": True,
                                "strictKnownMarketplaces": []}), ["POLICY_COMPUTED_AT_RUNTIME"])
        self.assertEqual(codes({"policyHelper": {"path": "/x"}}, managed_mcp="excludes-jev"),
                         ["POLICY_COMPUTED_AT_RUNTIME", "MCP_EXCLUSIVE_CONFIG"])
        self.assertEqual(codes(source({"model": "x"}, RANK_SERVER), source({"policyHelper": {"path": "/x"}})), [])

    def test_codes_arrive_in_a_stable_order(self):
        result = codes({"allowedMcpServers": [], "allowManagedHooksOnly": True, "strictKnownMarketplaces": [],
                        "deniedMcpServers": [{"serverName": pr.SCOPED_SERVER_NAME}],
                        "blockedMarketplaces": [{"source": "github", "repo": "qualixar/*"}],
                        "enabledPlugins": {pr.PLUGIN_ID: False}}, managed_mcp="excludes-jev")
        self.assertEqual(result, ["PLUGIN_DISABLED", "MARKETPLACE_BLOCKED", "MARKETPLACE_NOT_ALLOWED",
                                  "MCP_EXCLUSIVE_CONFIG", "MCP_SERVER_DENIED", "MCP_SERVER_NOT_ALLOWED"])
        self.assertEqual(list(pr.ORDER), sorted(pr.ORDER, key=pr.ORDER.index))

    def test_unexpected_value_types_never_raise(self):
        odd = [None, 1, 1.5, True, "text", b"bytes", [], {}, [[1]], {"k": [1]}, {1: 2}, [None]]
        keys = ("allowManagedHooksOnly", "disableAllHooks", "enabledPlugins", "strictKnownMarketplaces",
                "allowedMarketplaces", "blockedMarketplaces", "allowedMcpServers", "deniedMcpServers",
                "allowManagedMcpServersOnly", "strictPluginOnlyCustomization", "policyHelper", "managedSourcesBehavior")
        for value in odd:
            with self.subTest(value=value):
                self.assertLessEqual(set(codes({key: value for key in keys})), set(pr.ORDER))
                nested = {"strictKnownMarketplaces": [{"source": value, "repo": value, "hostPattern": value}],
                          "blockedMarketplaces": [{"source": value, "repo": value, "url": value}],
                          "allowedMcpServers": [{"serverName": value}, {"serverCommand": value}, value]}
                self.assertLessEqual({"MARKETPLACE_NOT_ALLOWED", "MCP_SERVER_NOT_ALLOWED"}, set(codes(nested)))
                pr.judge(machine(source(nested), marketplace={"source": value, "url": value, "repo": value}), {})


if __name__ == "__main__":
    unittest.main()
