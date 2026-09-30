"""Reading Claude Code's managed settings sources: locations, ranking and merging.

Each rule here is quoted from Claude Code's managed settings documentation.
All files are synthetic and live in temporary folders.
"""

from __future__ import annotations

import json
import plistlib
import subprocess
import sys
import tempfile
import types
import unittest
from contextlib import chdir
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "plugins" / "qualixar-jev-decision-layer" / "runtime"
sys.path.insert(0, str(RUNTIME))

from jev_auto import policy_sources as ps  # noqa: E402

_HOOKS_ONLY = {"allowManagedHooksOnly": True}


class _Folders(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.home, self.system, self.prefs = self.root / "home", self.root / "system", self.root / "prefs"
        for folder in (self.home, self.system, self.prefs):
            folder.mkdir()

    def discover(self, platform="linux", environ=None, **kwargs):
        options = dict(platform=platform, environ=environ or {}, home=self.home, system=self.system,
                       managed_preferences=self.prefs, user=lambda: "person", launchd=lambda: None)
        options.update(kwargs)
        return ps.discover(**options)


class MergeTests(unittest.TestCase):
    """"When two files set the same key, Claude Code combines them by these rules"."""

    def test_a_later_single_value_replaces_an_earlier_one(self):
        self.assertEqual(ps.merge_documents({"model": "a", "x": 1}, {"model": "b"}), {"model": "b", "x": 1})

    def test_lists_combine_with_duplicates_removed(self):
        merged = ps.merge_documents({"strictKnownMarketplaces": [{"source": "github", "repo": "a/b"}]},
                                    {"strictKnownMarketplaces": [{"source": "github", "repo": "a/b"},
                                                                 {"source": "github", "repo": "c/d"}]})
        self.assertEqual(merged["strictKnownMarketplaces"],
                         [{"source": "github", "repo": "a/b"}, {"source": "github", "repo": "c/d"}])

    def test_nested_blocks_merge_key_by_key(self):
        merged = ps.merge_documents({"enabledPlugins": {"a@m": True, "b@m": True}},
                                    {"enabledPlugins": {"b@m": False, "c@m": True}})
        self.assertEqual(merged["enabledPlugins"], {"a@m": True, "b@m": False, "c@m": True})
        deep = ps.merge_documents({"sandbox": {"network": {"allowedDomains": ["a"]}}},
                                  {"sandbox": {"network": {"allowedDomains": ["b"]}}})
        self.assertEqual(deep["sandbox"]["network"]["allowedDomains"], ["a", "b"])

    def test_the_documented_whole_replacements(self):
        self.assertEqual(ps.merge_documents({"fallbackModel": ["a", "b"]}, {"fallbackModel": ["c"]})["fallbackModel"], ["c"])
        self.assertEqual(ps.merge_documents({"modelPicker": ["a"]}, {"modelPicker": ["b"]})["modelPicker"], ["b"])
        merged = ps.merge_documents({"extraKnownMarketplaces": {"m": {"source": {"source": "github", "repo": "a/b"},
                                                                      "autoUpdate": True}}},
                                    {"extraKnownMarketplaces": {"m": {"source": {"source": "github", "repo": "c/d"}}}})
        self.assertEqual(merged["extraKnownMarketplaces"], {"m": {"source": {"source": "github", "repo": "c/d"}}})

    def test_a_type_change_takes_the_later_value(self):
        self.assertEqual(ps.merge_documents({"allowedMcpServers": [1]}, {"allowedMcpServers": True}),
                         {"allowedMcpServers": True})


class ManagedFileTests(_Folders):
    def test_the_file_and_its_drop_ins_become_one_merged_source_named_by_its_folder(self):
        (self.system / "managed-settings.json").write_text(json.dumps(_HOOKS_ONLY))
        drop_in = self.system / "managed-settings.d"
        drop_in.mkdir()
        (drop_in / "50-acme-project-falcon.json").write_text(json.dumps(
            {"enabledPlugins": {"qualixar-jev-decision-layer@qualixar": True}}))
        (drop_in / ".hidden.json").write_text(json.dumps({"model": "hidden"}))
        (drop_in / "notes.txt").write_text("{}")
        (drop_in / "30-broken.json").write_text("{not json")
        machine = self.discover()
        self.assertEqual(len(machine.sources), 1)
        source = machine.sources[0]
        self.assertEqual((source.label, source.location, source.rank), (ps.MANAGED_FILE, str(self.system), ps.RANK_FILE))
        self.assertEqual(source.document, {"allowManagedHooksOnly": True,
                                           "enabledPlugins": {"qualixar-jev-decision-layer@qualixar": True}})
        self.assertNotIn("falcon", json.dumps(machine.sources))

    def test_drop_ins_apply_in_name_order(self):
        drop_in = self.system / "managed-settings.d"
        drop_in.mkdir()
        (drop_in / "20-b.json").write_text(json.dumps({"model": "second"}))
        (drop_in / "10-a.json").write_text(json.dumps({"model": "first"}))
        self.assertEqual(self.discover().sources[0].document, {"model": "second"})

    def test_the_drop_in_scan_is_bounded(self):
        drop_in = self.system / "managed-settings.d"
        drop_in.mkdir()
        for index in range(5):
            (drop_in / f"{index:02d}.json").write_text(json.dumps({f"k{index}": index}))
        with patch.object(ps, "MAX_SCANNED", 5):
            self.assertEqual(len(self.discover().sources[0].document), 5)
            (drop_in / "notes.txt").write_text("")
            self.assertEqual(self.discover().sources, [])  # no drop-in rather than an arbitrary subset
        with patch.object(ps, "MAX_DROP_INS", 2):
            self.assertEqual(sorted(self.discover().sources[0].document), ["k0", "k1"])

    def test_an_oversized_or_unreadable_file_is_skipped(self):
        (self.system / "managed-settings.json").write_text(" " * (ps.MAX_BYTES + 1) + "{}")
        self.assertEqual(self.discover().sources, [])
        (self.system / "managed-settings.json").write_text(" " * (ps.MAX_BYTES - 2) + "{}")
        self.assertEqual(len(self.discover().sources), 1)  # exactly at the limit is read
        (self.system / "managed-settings.json").unlink()
        (self.system / "managed-settings.json").mkdir()
        self.assertEqual(self.discover().sources, [])

    def test_the_documented_system_directories(self):
        self.assertEqual(str(ps.system_directory("darwin")), "/Library/Application Support/ClaudeCode")
        self.assertEqual(str(ps.system_directory("linux")), "/etc/claude-code")
        self.assertIn("ClaudeCode", str(ps.system_directory("win32")))
        self.assertIsNone(ps.system_directory("sunos5"))


class ManagedMcpTests(_Folders):
    def test_absent_means_no_exclusive_control(self):
        machine = self.discover()
        self.assertIsNone(machine.managed_mcp)
        self.assertIsNone(machine.managed_mcp_location)

    def test_a_file_that_does_not_name_jev_excludes_it_and_one_that_does_admits_it(self):
        path = self.system / "managed-mcp.json"
        for body, expected in (({"mcpServers": {}}, "excludes-jev"), ({"mcpServers": {"other": {}}}, "excludes-jev"),
                               ({"mcpServers": {"qualixar-jev": {"command": "/x"}}}, "defines-jev")):
            with self.subTest(body=body):
                path.write_text(json.dumps(body))
                machine = self.discover()
                self.assertEqual(machine.managed_mcp, expected)
                self.assertEqual(machine.managed_mcp_location, str(path))

    def test_an_unreadable_file_still_counts_as_exclusive(self):
        (self.system / "managed-mcp.json").write_text("{not json")
        self.assertEqual(self.discover().managed_mcp, "excludes-jev")


class ConfigFolderTests(_Folders):
    def setUp(self):
        super().setUp()
        self.config = self.root / "custom-config"
        self.config.mkdir()
        (self.config / "remote-settings.json").write_text(json.dumps(_HOOKS_ONLY))

    def locations(self, machine):
        return [source.location for source in machine.sources]

    def test_the_configured_folder_and_the_default_are_both_read(self):
        (self.home / ".claude").mkdir()
        (self.home / ".claude" / "remote-settings.json").write_text(json.dumps({"model": "x"}))
        machine = self.discover(environ={"CLAUDE_CONFIG_DIR": str(self.config)})
        self.assertEqual(self.locations(machine), [str(self.config / "remote-settings.json"),
                                                   str(self.home / ".claude" / "remote-settings.json")])
        self.assertEqual({source.rank for source in machine.sources}, {ps.RANK_SERVER})

    def test_a_macos_process_without_the_variable_asks_launchd(self):
        machine = self.discover("darwin", launchd=lambda: str(self.config))
        self.assertEqual(self.locations(machine), [str(self.config / "remote-settings.json")])

    def test_the_process_environment_wins_and_launchd_is_not_asked(self):
        other = self.root / "shell-config"
        other.mkdir()
        refuse = lambda: self.fail("launchd must not be asked when the variable is set")  # noqa: E731
        self.assertEqual(self.discover("darwin", {"CLAUDE_CONFIG_DIR": str(other)}, launchd=refuse).sources, [])

    def test_launchd_is_macos_only(self):
        refuse = lambda: self.fail("launchd exists only on macOS")  # noqa: E731
        self.assertEqual(self.discover("linux", launchd=refuse).sources, [])
        self.assertEqual(self.discover("win32", launchd=refuse, registry=lambda: []).sources, [])

    def test_an_unusable_answer_is_ignored_even_when_it_would_name_a_real_policy(self):
        for answer in (None, "", "/bad\x00path", "/" + "a" * 5000):
            with self.subTest(answer=str(answer)[:12]):
                self.assertEqual(self.discover("darwin", launchd=lambda: answer).sources, [])
        with chdir(self.root):
            self.assertTrue((Path("custom-config") / "remote-settings.json").is_file())
            self.assertEqual(self.discover("darwin", launchd=lambda: "custom-config").sources, [])

    def test_only_a_bounded_absolute_path_is_usable(self):
        self.assertEqual(ps.usable_directory("/a/b"), Path("/a/b"))
        self.assertEqual(ps.usable_directory("/" + "a" * 4095), Path("/" + "a" * 4095))
        for value in ("/" + "a" * 4096, "a/b", "", None, 7, "/a\x00b"):
            with self.subTest(value=str(value)[:12]):
                self.assertIsNone(ps.usable_directory(value))

    def test_the_launchd_reader_runs_launchctl_by_absolute_path_without_a_shell(self):
        calls = []

        def run(command, **kwargs):
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(command, 0, "/Users/person/.claude-custom\n", "")

        with patch.object(ps.subprocess, "run", side_effect=run):
            self.assertEqual(ps.launchd_config_dir(), "/Users/person/.claude-custom")
        command, kwargs = calls[0]
        self.assertEqual(command, ["/bin/launchctl", "getenv", "CLAUDE_CONFIG_DIR"])
        self.assertNotIn("shell", kwargs)
        self.assertLessEqual(kwargs["timeout"], 2)

    def test_the_launchd_reader_never_raises(self):
        outcomes = [subprocess.CompletedProcess([], 1, "/looks/like/a/dir\n", "err"),
                    subprocess.CompletedProcess([], 0, "\n", ""), subprocess.TimeoutExpired("launchctl", 2),
                    OSError("missing"), ValueError("bad")]
        for outcome in outcomes:
            behaviour = ({"side_effect": outcome} if isinstance(outcome, BaseException) else {"return_value": outcome})
            with self.subTest(outcome=type(outcome).__name__), patch.object(ps.subprocess, "run", **behaviour):
                self.assertIsNone(ps.launchd_config_dir())

    def test_the_default_discovery_uses_the_launchd_reader(self):
        self.assertIs(ps.discover.__kwdefaults__["launchd"], ps.launchd_config_dir)


class RecordedStateTests(_Folders):
    def test_the_marketplace_source_comes_from_claude_codes_own_record(self):
        plugins = self.home / ".claude" / "plugins"
        plugins.mkdir(parents=True)
        source = {"source": "directory", "path": "/opt/checkout"}
        (plugins / "known_marketplaces.json").write_text(json.dumps(
            {"other": {"source": {"source": "github", "repo": "a/b"}}, "qualixar": {"source": source}}))
        self.assertEqual(self.discover().marketplace_source, source)
        (plugins / "known_marketplaces.json").write_text(json.dumps({"qualixar": {"source": "not-an-object"}}))
        self.assertIsNone(self.discover().marketplace_source)

    def test_the_users_own_mcp_allowlist_is_read_from_the_active_folder(self):
        (self.home / ".claude").mkdir()
        (self.home / ".claude" / "settings.json").write_text(json.dumps({"allowedMcpServers": [{"serverName": "x"}]}))
        self.assertEqual(self.discover().user_mcp_allowlist, [{"serverName": "x"}])
        self.assertIsNone(ps._user_mcp_allowlist([]))


class ProfileAndRegistryTests(_Folders):
    def test_mdm_profiles_are_read_on_macos_with_the_login_name_kept_out_of_the_location(self):
        (self.prefs / "person").mkdir()
        with (self.prefs / "person" / ps.PLIST_NAME).open("wb") as stream:
            plistlib.dump(_HOOKS_ONLY, stream)
        with (self.prefs / ps.PLIST_NAME).open("wb") as stream:
            plistlib.dump({"strictKnownMarketplaces": []}, stream, fmt=plistlib.FMT_BINARY)
        machine = self.discover("darwin")
        self.assertEqual([(s.label, s.rank) for s in machine.sources], [(ps.MDM_PROFILE, ps.RANK_MDM)] * 2)
        self.assertEqual(machine.sources[0].location, str(self.prefs / "<user>" / ps.PLIST_NAME))
        self.assertNotIn("person", json.dumps([s.location for s in machine.sources]))
        self.assertEqual(self.discover("linux").sources, [])
        (self.prefs / ps.PLIST_NAME).write_bytes(b"not a plist")
        self.assertEqual(len(self.discover("darwin").sources), 1)
        self.assertEqual(len(self.discover("darwin", user=lambda: "../person").sources), 0)

    def test_registry_hives_are_ranked_hklm_above_files_and_hkcu_below(self):
        (self.system / "managed-settings.json").write_text("{}")
        values = [("HKLM\\x", json.dumps(_HOOKS_ONLY)), ("HKCU\\y", json.dumps({"model": "user"})),
                  ("HKCU\\z", "{broken"), ("HKCU\\w", 7), ("HKCU\\v", "[]")]
        machine = self.discover("win32", registry=lambda: values)
        self.assertEqual([(s.label, s.rank) for s in machine.sources],
                         [(ps.REGISTRY_HKLM, ps.RANK_MDM), (ps.MANAGED_FILE, ps.RANK_FILE),
                          (ps.REGISTRY_HKCU, ps.RANK_USER)])
        self.assertEqual(self.discover("linux", registry=lambda: values).sources[0].label, ps.MANAGED_FILE)

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
            values = ps.registry_values()
        self.assertEqual(seen, [("HKLM", ps.REGISTRY_KEY), ("HKCU", ps.REGISTRY_KEY)])
        self.assertEqual(values, [("HKLM\\" + ps.REGISTRY_KEY, '{"a": 1}')])
        with patch.dict(sys.modules, {"winreg": None}):
            self.assertEqual(ps.registry_values(), [])


if __name__ == "__main__":
    unittest.main()
