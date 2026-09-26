"""Each host may only be handed things its own harness can actually resolve.

Codex could not start its MCP server for four releases — 1.0.1 through 1.0.5 —
because the Codex package's `.mcp.json` was copied verbatim from the Claude
package and told Codex to execute `${CLAUDE_PLUGIN_ROOT}/scripts/launch-jev`.
That variable is Claude Code's. Codex does not expand it, so the command was a
path that does not exist, and the server never started. Nothing failed loudly:
the host simply had no tools.

1.0.6 fixed that one file. This fixes the class.

The same shape had already happened once, with `hooks/hooks.json`, and the
lesson was written down in `hooks/README.md` — a table of which hook file
belongs to which host and which variable each may use. The table covers hooks.
It does not mention MCP descriptors, so the descriptor repeated the mistake.
A prose table is not a guard.

So these tests do not name files. They read each package's manifests, follow
whatever those manifests point at, and assert four properties:

1. every variable a host is handed is one that host expands;
2. nothing host-facing ships unreferenced, where a host could find it by
   convention and act on it;
3. the tool list a Hermes operator reads matches the one Hermes serves;
4. the version the server reports is the version that shipped.

Adding a host means adding a row. Adding a descriptor means declaring who
reads it. Neither can be left to memory.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import unittest
from dataclasses import dataclass, field
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "qualixar-jev-decision-layer"
CODEX_PACKAGE = ROOT / "plugins" / "qualixar-jev-codex"
RUNTIME = PLUGIN / "runtime"
sys.path.insert(0, str(RUNTIME))

VARIABLE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

# Manifest keys whose value is a path to another file the host will read.
# `extensions.com.openai.hooks` is the agent-plugins.org spelling of `hooks`.
REFERENCE_KEYS = ("hooks", "mcpServers")
OPENAI_EXTENSION = ("extensions", "com.openai", "hooks")

# Files that are manifests rather than things a manifest points at.
MANIFESTS = (
    Path("plugin.json"),
    Path("plugin.yaml"),
    Path(".claude-plugin/plugin.json"),
    Path(".codex-plugin/plugin.json"),
)


@dataclass(frozen=True)
class Host:
    """One harness, and the vocabulary it can resolve.

    `variables` is exhaustive: a variable absent from it is one this host will
    hand to the operating system verbatim.
    """

    host_id: str
    manifest: Path
    variables: frozenset[str]
    reason: str


# Verified against each harness's own documentation and, for Codex and Claude
# Code, against a launcher started from the package on this machine.
HOSTS: tuple[Host, ...] = (
    Host("claude_code", Path(".claude-plugin/plugin.json"), frozenset({"CLAUDE_PLUGIN_ROOT"}),
         "Claude Code substitutes the plugin's installed directory."),
    Host("codex", Path(".codex-plugin/plugin.json"), frozenset({"PLUGIN_ROOT"}),
         "Codex substitutes the plugin root, and does not know Claude's name for it."),
    Host("codex_portable", Path("plugin.json"), frozenset({"PLUGIN_ROOT"}),
         "The agent-plugins.org manifest is read by Codex, so it shares Codex's vocabulary."),
)

# Host-facing files no manifest names, because the host finds them by
# convention. Each needs the host that reads it and why, so that an
# unreferenced file is a decision rather than an oversight.
CONVENTION_DISCOVERED: dict[tuple[str, str], tuple[str, str]] = {
    ("qualixar-jev-decision-layer", "hooks.json"):
        ("antigravity", "Antigravity reads a PreInvocation map from the plugin root; "
                        "it has no manifest key to declare one."),
}


def _packages() -> tuple[tuple[str, Path], ...]:
    return (("qualixar-jev-decision-layer", PLUGIN), ("qualixar-jev-codex", CODEX_PACKAGE))


def _nested(document: dict, path: tuple[str, ...]):
    for key in path:
        if not isinstance(document, dict):
            return None
        document = document.get(key)
    return document


def _referenced(manifest_path: Path) -> tuple[Path, ...]:
    """Every file this manifest tells its host to read."""
    document = json.loads(manifest_path.read_text())
    values = [document.get(key) for key in REFERENCE_KEYS]
    values.append(_nested(document, OPENAI_EXTENSION))
    # removeprefix, not lstrip: lstrip strips characters, so "./.mcp.json"
    # would become "mcp.json" -- a different file that also ships.
    return tuple(
        Path(value.removeprefix("./"))
        for value in values
        if isinstance(value, str) and value.endswith(".json")
    )


def _variables(path: Path) -> frozenset[str]:
    return frozenset(VARIABLE.findall(path.read_text()))


def _provides_tools(manifest: Path) -> tuple[str, ...]:
    """Read the `provides_tools:` block without importing a YAML parser.

    The product declares no Python dependencies, and the test suite honours
    that: a clean checkout must be runnable with the standard library alone.
    The block is a flat list of names, so it needs no general parser.
    """
    names: list[str] = []
    inside = False
    for line in manifest.read_text().splitlines():
        if not line.startswith((" ", "\t", "-")) and line.strip():
            inside = line.split(":", 1)[0].strip() == "provides_tools"
            continue
        if inside and line.strip().startswith("- "):
            names.append(line.strip()[2:].strip())
    return tuple(names)


def _server_info(package: Path, command: Path, cwd: Path) -> dict:
    """Start the bundled server the way a host starts it and read `initialize`."""
    request = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
               "params": {"protocolVersion": "2025-06-18"}}
    result = subprocess.run(
        [str(command)], input=json.dumps(request) + "\n",
        capture_output=True, text=True, cwd=cwd, timeout=30, check=False,
    )
    if result.returncode != 0:
        raise AssertionError(f"{package.name} launcher exited {result.returncode}: {result.stderr}")
    return json.loads(result.stdout.splitlines()[0])["result"]["serverInfo"]


class HostVariableVocabulary(unittest.TestCase):
    """A host must never be handed a variable belonging to a different host."""

    def test_every_host_only_sees_variables_its_own_harness_expands(self):
        for name, package in _packages():
            for host in HOSTS:
                manifest = package / host.manifest
                if not manifest.is_file():
                    continue
                for reference in _referenced(manifest):
                    target = package / reference
                    with self.subTest(package=name, host=host.host_id, file=str(reference)):
                        self.assertTrue(target.is_file(),
                                        f"{manifest} points at {reference}, which does not ship")
                        foreign = _variables(target) - host.variables
                        self.assertEqual(
                            foreign, frozenset(),
                            f"{name}/{reference} hands {host.host_id} "
                            f"{sorted(foreign)}, which it does not expand. {host.reason}")

    def test_a_host_vocabulary_names_a_variable_some_shipped_file_uses(self):
        """A vocabulary entry nobody uses is a claim nothing checks."""
        shipped: set[str] = set()
        for _, package in _packages():
            for path in package.rglob("*.json"):
                if "runtime" in path.parts or "__pycache__" in path.parts:
                    continue
                shipped |= _variables(path)
        for host in HOSTS:
            with self.subTest(host=host.host_id):
                self.assertTrue(host.variables <= shipped,
                                f"{host.host_id} claims {sorted(host.variables - shipped)}, "
                                "which no shipped file uses")


class HostFacingFilesAreClaimed(unittest.TestCase):
    """Nothing host-facing ships without a host that reads it."""

    def _candidates(self, package: Path) -> tuple[Path, ...]:
        top = [p.relative_to(package) for p in package.glob("*.json")]
        hooks = [p.relative_to(package) for p in (package / "hooks").glob("*.json")]
        return tuple(sorted(p for p in top + hooks if p not in MANIFESTS))

    def test_no_shipped_host_descriptor_is_unreferenced(self):
        for name, package in _packages():
            claimed: set[Path] = set()
            for host in HOSTS:
                manifest = package / host.manifest
                if manifest.is_file():
                    claimed |= set(_referenced(manifest))
            for candidate in self._candidates(package):
                with self.subTest(package=name, file=str(candidate)):
                    if candidate in claimed:
                        continue
                    declared = CONVENTION_DISCOVERED.get((name, str(candidate)))
                    self.assertIsNotNone(
                        declared,
                        f"{name}/{candidate} ships but no manifest names it. A host that "
                        "finds it by convention will act on it. Either reference it from a "
                        "manifest, declare it in CONVENTION_DISCOVERED with the host that "
                        "reads it, or stop shipping it.")

    def test_the_convention_list_does_not_name_a_file_that_no_longer_ships(self):
        for (name, relative), (host_id, _) in CONVENTION_DISCOVERED.items():
            package = dict(_packages())[name]
            with self.subTest(package=name, file=relative, host=host_id):
                self.assertTrue((package / relative).is_file(),
                                f"convention list is stale: {name}/{relative}")


class DerivedPackagesAreInSync(unittest.TestCase):
    """The generated Codex package must match what the build would produce.

    It drifted silently while this file was being written: editing a shared
    `hooks/README.md` left the Codex copy stale and the whole suite still
    passed, because nothing ran the package check. A derived package nobody
    verifies is a second, forked product that ships under the same version.
    """

    def test_the_codex_package_matches_its_generator(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "build_codex_package.py"), "--check"],
            cwd=ROOT, text=True, capture_output=True, timeout=120)
        self.assertEqual(result.returncode, 0,
                         "the Codex package is stale; run tools/build_codex_package.py\n"
                         + result.stdout + result.stderr)

    def test_every_shipped_runtime_file_is_named_in_the_tamper_manifest(self):
        """A runtime file the manifest does not name is never hash-checked.

        RUNTIME_MANIFEST.json exists so a modified runtime file is refused at
        load. It has no generator on purpose, and tools/release.py only
        re-hashes files it ALREADY names -- so a newly added module ships
        entirely outside the integrity check, silently. That happened while
        this release was being built: jev_auto/provider_calibration.py went
        into both packages and into neither manifest.
        """
        for name, package in _packages():
            runtime = package / "runtime"
            manifest = json.loads((runtime / "RUNTIME_MANIFEST.json").read_text())
            shipped = {str(path.relative_to(runtime))
                       for path in runtime.rglob("*.py")
                       if "__pycache__" not in path.parts}
            with self.subTest(package=name):
                unnamed = sorted(shipped - set(manifest["files"]))
                self.assertEqual(unnamed, [],
                                 f"{name}: shipped but not hash-checked: {unnamed}")

    def test_the_tamper_manifest_does_not_name_a_file_that_no_longer_ships(self):
        for name, package in _packages():
            runtime = package / "runtime"
            manifest = json.loads((runtime / "RUNTIME_MANIFEST.json").read_text())
            with self.subTest(package=name):
                missing = sorted(entry for entry in manifest["files"]
                                 if not (runtime / entry).is_file())
                self.assertEqual(missing, [], f"{name}: manifest is stale: {missing}")

    def test_the_release_version_is_consistent_everywhere(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "release.py"), "--check"],
            cwd=ROOT, text=True, capture_output=True, timeout=120)
        self.assertEqual(result.returncode, 0,
                         "declared version sites disagree; run tools/release.py --version X\n"
                         + result.stdout + result.stderr)


class HermesManifestParity(unittest.TestCase):
    """The tool list an operator reads must match the one Hermes serves.

    `HostParity` in test_hermes_parity.py pins the allow-list against the
    served surface. Nothing pinned `plugin.yaml`, which is the only tool list a
    Hermes operator ever sees, so it kept the nine tools it shipped with while
    the adapter grew to twelve.
    """

    def test_hermes_manifest_declares_every_tool_hermes_serves(self):
        from jev_auto.hermes_tool import ALLOWED

        declared = set(_provides_tools(PLUGIN / "plugin.yaml"))
        self.assertEqual(
            declared, set(ALLOWED),
            f"plugin.yaml is missing {sorted(set(ALLOWED) - declared)} and over-declares "
            f"{sorted(declared - set(ALLOWED))}")

    def test_the_manifest_tool_list_has_no_duplicates(self):
        names = _provides_tools(PLUGIN / "plugin.yaml")
        self.assertEqual(len(names), len(set(names)), f"duplicated in plugin.yaml: {names}")


# A version literal that must NOT track the release, with the contract it
# belongs to. These are agreements with something outside this process -- a
# broker already running, or a document already on disk -- and moving one on a
# release breaks compatibility with the version it is talking to.
#
# The first draft of this file asserted that no version literal may differ from
# the shipped version. That would have forced a bump of the broker IPC
# handshake below, which is compared for equality across a socket: a client
# saying 1.0.7 to a broker still answering 1.0.6 raises BROKER_VERSION_MISMATCH
# and every call fails. Making that test green would have shipped a worse bug
# than the one this file exists to prevent. Product identity and wire contracts
# are different things and the guard has to know which is which.
FROZEN_VERSION_CONTRACTS: dict[str, str] = {
    "jev_auto/engine.py": "broker health handshake; compared for equality by jev_auto/ipc.py",
    "jev_auto/ipc.py": "broker health handshake; compared for equality against jev_auto/engine.py",
    "jev_auto/protocol.py": "receipt record version, written to receipts already on disk",
    "jev_auto/settings.py": "persisted settings document version",
    "jevkit/constants.py": "ADAPTER_VERSION: a contract with enrolled workspace state",
    "jevkit/policy_mode.py": "persisted policy document version",
}

# Where the product says which release is answering. A host reads these to
# find out what it is talking to, so they must track the release exactly.
PRODUCT_VERSION_SITES: tuple[tuple[str, str], ...] = (
    ("jev_auto/__init__.py", "__version__ of the runtime package"),
    ("jev_auto/mcp.py", "serverInfo.version in the MCP initialize response"),
    ("jevkit/__init__.py", "__version__ of the decision kit"),
    ("jevkit/mcp_server.py", "serverInfo.version and the health() report"),
)


class ServerReportsItsOwnVersion(unittest.TestCase):
    """A host asking the server which version answered must get the truth.

    Every release since 1.0.0 has answered `1.0.0`. That is the one question
    whose honest answer would have shown, in a single call, that a Codex
    install was running a package whose server never started.
    """

    def _shipped_version(self) -> str:
        return json.loads((PLUGIN / "plugin.json").read_text())["version"]

    def _literals(self, relative: str) -> list[tuple[int, str]]:
        path = PLUGIN / "runtime" / relative
        pattern = re.compile(r"""['"](\d+\.\d+\.\d+)['"]""")
        found = []
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if "version" not in line.lower():
                continue
            found.extend((number, literal) for literal in pattern.findall(line))
        return found

    def test_the_runtime_module_carries_the_shipped_version(self):
        import jev_auto

        self.assertEqual(jev_auto.__version__, self._shipped_version())

    def test_the_module_docstring_and_version_do_not_disagree(self):
        import jev_auto

        self.assertIn(jev_auto.__version__, jev_auto.__doc__ or "",
                      "the docstring and __version__ name different releases")

    def test_every_product_version_site_tracks_the_release(self):
        """A product site either holds the shipped version or imports it.

        Holding nothing and importing nothing is the third case, and it is the
        one that hid for six releases: a file that looks like it reports a
        version and does not.
        """
        shipped = self._shipped_version()
        for relative, role in PRODUCT_VERSION_SITES:
            source = (PLUGIN / "runtime" / relative).read_text()
            literals = self._literals(relative)
            with self.subTest(file=relative):
                self.assertTrue(literals or "__version__" in source,
                                f"{relative} is listed as {role} but neither states a version "
                                "nor imports __version__")
            for number, literal in literals:
                with self.subTest(file=relative, line=number):
                    self.assertEqual(literal, shipped,
                                     f"{relative}:{number} reports {literal} from a {shipped} "
                                     f"release. This is {role}.")

    def test_every_shipped_manifest_names_the_same_version(self):
        """Six manifests are bumped by hand; one missed is a package that lies."""
        shipped = self._shipped_version()
        manifests = [
            PLUGIN / ".claude-plugin/plugin.json",
            PLUGIN / ".codex-plugin/plugin.json",
            CODEX_PACKAGE / ".codex-plugin/plugin.json",
            ROOT / "docs/capabilities.json",
        ]
        for path in manifests:
            with self.subTest(manifest=str(path.relative_to(ROOT))):
                self.assertEqual(json.loads(path.read_text()).get("version"), shipped)
        yaml_manifest = (PLUGIN / "plugin.yaml").read_text()
        self.assertIn(f"version: {shipped}", yaml_manifest, "plugin.yaml names another release")

    def test_no_version_literal_is_left_unclassified(self):
        """Every version literal is either product identity or a frozen contract.

        A literal in neither list is one nobody has decided about, which is how
        the runtime ended up reporting 1.0.0 across six releases.
        """
        classified = set(FROZEN_VERSION_CONTRACTS) | {name for name, _ in PRODUCT_VERSION_SITES}
        unclassified: list[str] = []
        for path in sorted((PLUGIN / "runtime").rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            relative = str(path.relative_to(PLUGIN / "runtime"))
            if relative in classified or not self._literals(relative):
                continue
            unclassified.append(relative)
        self.assertEqual(unclassified, [],
                         "version literals in neither PRODUCT_VERSION_SITES nor "
                         f"FROZEN_VERSION_CONTRACTS: {unclassified}")

    def test_a_frozen_contract_still_holds_a_version_literal(self):
        """A stale freeze hides the next real one."""
        for relative, reason in FROZEN_VERSION_CONTRACTS.items():
            with self.subTest(file=relative):
                self.assertTrue(self._literals(relative),
                                f"{relative} is frozen for '{reason}' but holds no version "
                                "literal; remove the entry")

    def test_the_broker_handshake_agrees_with_itself(self):
        """Both halves of the IPC handshake must name the same version.

        `ipc.ensure` compares the broker's health version for equality. If the
        two files disagree, every call through the broker fails at startup.
        """
        served = {literal for _, literal in self._literals("jev_auto/engine.py")}
        expected = {literal for _, literal in self._literals("jev_auto/ipc.py")}
        self.assertEqual(served, expected,
                         f"broker answers {sorted(served)} but the client requires "
                         f"{sorted(expected)}")

    def test_each_package_reports_the_shipped_version_over_mcp(self):
        shipped = self._shipped_version()
        for name, package in _packages():
            with self.subTest(package=name):
                info = _server_info(package, package / "scripts/launch-jev", package)
                self.assertEqual(info["version"], shipped,
                                 f"{name} reports {info['version']} from a {shipped} install")


if __name__ == "__main__":
    unittest.main()
