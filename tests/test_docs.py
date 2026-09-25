"""The documentation set must stay universal, linked and honest.

This repository's claim is one runtime behind per-host adapters. A document
that reads as a single-host product quietly contradicts that, and nothing else
in the suite would notice.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = sorted((ROOT / "docs").glob("*.md"))
ROOT_DOCS = [ROOT / name for name in ("README.md", "CONTRIBUTING.md", "CHANGELOG.md", "llms.txt")]
HOSTS = ("Codex", "Claude Code", "Antigravity", "Hermes", "VS Code")


class Links(unittest.TestCase):
    def test_every_relative_link_resolves(self):
        for document in DOCS + ROOT_DOCS:
            for target in re.findall(r"\]\(([^)]+)\)", document.read_text()):
                if target.startswith(("https://", "http://", "#")):
                    continue
                with self.subTest(document=document.name, target=target):
                    self.assertTrue((document.parent / target).resolve().is_file())


class Universal(unittest.TestCase):
    def test_the_host_guide_covers_every_supported_harness(self):
        text = (ROOT / "docs" / "HOSTS.md").read_text()
        for host in HOSTS:
            self.assertIn(host, text)

    def test_reader_facing_docs_are_not_written_for_one_host(self):
        """Naming a host is fine; naming only one, in a shared document, is not."""
        for document in (ROOT / "docs" / "GETTING_STARTED.md", ROOT / "docs" / "USE_CASES.md",
                         ROOT / "docs" / "SECURITY.md", ROOT / "docs" / "ROLLBACK.md",
                         ROOT / "README.md", ROOT / "llms.txt"):
            text = document.read_text()
            named = [host for host in HOSTS if host in text]
            with self.subTest(document=document.name):
                self.assertGreater(len(named), 1, f"only mentions {named}")

    def test_the_index_points_at_the_host_guide_and_the_changelog(self):
        index = (ROOT / "llms.txt").read_text()
        self.assertIn("docs/HOSTS.md", index)
        self.assertIn("CHANGELOG.md", index)


class Honesty(unittest.TestCase):
    def test_no_document_claims_a_measured_saving(self):
        claim = re.compile(r"(measured|proven|verified)[^.\n]{0,40}"
                           r"(token|cost|time)[^.\n]{0,20}(saving|reduction)", re.I)
        for document in DOCS + ROOT_DOCS:
            for line in document.read_text().splitlines():
                # A line saying savings are NOT measured is the point of the rule.
                if re.search(r"\b(not|never|no|without|require)\b", line, re.I):
                    continue
                with self.subTest(document=document.name, line=line[:60]):
                    self.assertIsNone(claim.search(line))

    def test_the_changelog_records_the_current_release(self):
        changelog = (ROOT / "CHANGELOG.md").read_text()
        version = json.loads((ROOT / "plugins" / "qualixar-jev-decision-layer"
                              / ".claude-plugin" / "plugin.json").read_text())["version"]
        self.assertIn(f"[{version}]", changelog)

    def test_documented_fixture_totals_match_what_ships(self):
        catalog = json.loads((ROOT / "plugins" / "qualixar-jev-decision-layer"
                              / "runtime" / "recipe_catalog.json").read_text())
        total = sum(len(entry["cases"]) for entry in catalog["fixtures"])
        capabilities = json.loads((ROOT / "docs" / "capabilities.json").read_text())
        self.assertEqual(capabilities["offline_fixtures"]["total"], total)
        for document in (ROOT / "README.md", ROOT / "docs" / "HOSTS.md",
                         ROOT / "docs" / "GETTING_STARTED.md", ROOT / "CHANGELOG.md"):
            with self.subTest(document=document.name):
                self.assertIn(str(total), document.read_text())


class CapabilityManifest(unittest.TestCase):
    def test_the_manifest_validates_against_its_own_schema(self):
        try:
            import jsonschema
        except ImportError:
            self.skipTest("jsonschema not installed")
        jsonschema.validate(
            json.loads((ROOT / "docs" / "capabilities.json").read_text()),
            json.loads((ROOT / "docs" / "capabilities.schema.json").read_text()))

    def test_a_host_with_a_shipped_adapter_says_so(self):
        capabilities = json.loads((ROOT / "docs" / "capabilities.json").read_text())
        support = {host["id"]: host for host in capabilities["host_support"]}
        for host_id in ("codex-desktop", "claude-code", "vscode", "hermes", "antigravity"):
            with self.subTest(host_id):
                self.assertTrue(support[host_id]["installed_adapter"])

    def test_shipping_an_adapter_is_still_not_a_verified_receipt(self):
        capabilities = json.loads((ROOT / "docs" / "capabilities.json").read_text())
        for host in capabilities["host_support"]:
            with self.subTest(host["id"]):
                self.assertFalse(host["automatic_jev_hook_receipt_verified"])
                self.assertFalse(host["local_laya_receipt_verified"])


if __name__ == "__main__":
    unittest.main()
