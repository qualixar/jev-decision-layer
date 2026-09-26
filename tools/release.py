"""Set the release version everywhere, and regenerate everything derived from it.

Five host adapters cannot be version-bumped by hand. They already were not:
1.0.6 bumped a module docstring to "1.0.6" and left `__version__ = '1.0.0'` on
the line below it, so every host that asked the server which release it was
talking to got 1.0.0 -- for six releases running.

So the version lives in exactly one place per artefact kind, every one of them
is listed here, and `--check` fails if any of them disagrees. A site nobody
listed is a site the release cannot reach, which is how this started.

    python3 tools/release.py --version 1.0.7    # apply
    python3 tools/release.py --check            # verify, changes nothing

The generated Codex package and the runtime manifest are rebuilt from source
rather than edited, because a derived artefact edited by hand is a fork.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "qualixar-jev-decision-layer"
CODEX_PACKAGE = ROOT / "plugins" / "qualixar-jev-codex"

SEMVER = re.compile(r"^\d+\.\d+\.\d+$")

# JSON manifests whose top-level "version" is the release.
JSON_VERSION_SITES = (
    PLUGIN / "plugin.json",
    PLUGIN / ".claude-plugin" / "plugin.json",
    PLUGIN / ".codex-plugin" / "plugin.json",
    ROOT / "docs" / "capabilities.json",
)

# Everything else, as (path, regex with one capturing group for the version).
# The pattern must match exactly once, so that a site which moves or is
# reworded fails loudly instead of being silently skipped.
TEXT_VERSION_SITES: tuple[tuple[Path, str], ...] = (
    (PLUGIN / "plugin.yaml", r"^version: (\d+\.\d+\.\d+)$"),
    (PLUGIN / "runtime" / "jev_auto" / "__init__.py", r"^__version__ = '(\d+\.\d+\.\d+)'$"),
    (PLUGIN / "runtime" / "jevkit" / "__init__.py", r'^__version__ = "(\d+\.\d+\.\d+)"$'),
    (ROOT / "README.md", r"\*\*Current release: (\d+\.\d+\.\d+)\*\*"),
)

# Documentation that names the installed path, which is version-pinned. These
# may legitimately appear more than once.
PATH_VERSION_SITES = (ROOT / "README.md", ROOT / "docs" / "HOSTS.md")
PATH_PATTERN = r"(qualixar-jev-decision-layer/)(\d+\.\d+\.\d+)(/scripts/launch-jev)"

# Badge URLs carry the version twice: in the link target and in the image.
BADGE_SITES = (ROOT / "README.md",)
BADGE_PATTERNS = (
    r"(releases/tag/v)(\d+\.\d+\.\d+)",
    r"(badge/version-)(\d+\.\d+\.\d+)(-)",
    r"(alt=\"Version )(\d+\.\d+\.\d+)(\")",
)


class ReleaseError(Exception):
    """A site the release cannot reach, or one that disagrees."""


def _read_current() -> str:
    return json.loads((PLUGIN / "plugin.json").read_text())["version"]


def _json_site(path: Path, version: str, *, check: bool) -> list[str]:
    document = json.loads(path.read_text())
    if document.get("version") == version:
        return []
    if check:
        return [f"{path.relative_to(ROOT)}: {document.get('version')} != {version}"]
    document["version"] = version
    path.write_text(json.dumps(document, indent=2) + "\n")
    return []


def _text_site(path: Path, pattern: str, version: str, *, check: bool) -> list[str]:
    source = path.read_text()
    matches = list(re.finditer(pattern, source, flags=re.MULTILINE))
    if len(matches) != 1:
        raise ReleaseError(
            f"{path.relative_to(ROOT)}: pattern {pattern!r} matched {len(matches)} times, "
            "expected exactly 1. The site moved or was reworded; update tools/release.py.")
    found = matches[0].group(1)
    if found == version:
        return []
    if check:
        return [f"{path.relative_to(ROOT)}: {found} != {version}"]
    start, end = matches[0].span(1)
    path.write_text(source[:start] + version + source[end:])
    return []


def _pattern_site(path: Path, pattern: str, version: str, *, check: bool) -> list[str]:
    """Rewrite every occurrence; used where a version legitimately repeats."""
    source = path.read_text()
    stale = {match.group(2) for match in re.finditer(pattern, source)} - {version}
    if not stale:
        return []
    if check:
        return [f"{path.relative_to(ROOT)}: {sorted(stale)} != {version}"]
    groups = re.compile(pattern).groups
    replacement = r"\g<1>" + version + (r"\g<3>" if groups >= 3 else "")
    path.write_text(re.sub(pattern, replacement, source))
    return []


def _reseal_runtime_manifest() -> list[str]:
    """Re-hash runtime files this release changed, and name every one.

    RUNTIME_MANIFEST.json is a tamper check: build_recipes.py verifies each
    runtime file against it and refuses a mismatch. It has no generator on
    purpose, so a modified runtime file cannot quietly re-bless itself.

    A release legitimately changes runtime files, so re-sealing is part of
    cutting one -- but it prints every path it re-hashes, because a file in
    that list nobody expected is the exact event the check exists to surface.
    The manifest only ever covers files it already names: a new runtime file
    must be added deliberately, not swept in by a release.
    """
    path = PLUGIN / "runtime" / "RUNTIME_MANIFEST.json"
    manifest = json.loads(path.read_text())
    import hashlib

    resealed: list[str] = []
    for name, recorded in sorted(manifest["files"].items()):
        target = PLUGIN / "runtime" / name
        if target.is_symlink() or not target.is_file():
            raise ReleaseError(f"runtime manifest names a missing file: {name}")
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        if digest != recorded:
            manifest["files"][name] = digest
            resealed.append(name)
    if resealed:
        path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return resealed


def _regenerate() -> None:
    """Rebuild every derived artefact. A hand-edited derivative is a fork."""
    resealed = _reseal_runtime_manifest()
    print(f"  re-sealed {len(resealed)} runtime file(s) in RUNTIME_MANIFEST.json")
    for name in resealed:
        print(f"    {name}")
    for command in (
        [sys.executable, str(ROOT / "tools" / "build_recipes.py")],
        [sys.executable, str(ROOT / "tools" / "build_codex_package.py")],
    ):
        result = subprocess.run(command, capture_output=True, text=True, cwd=ROOT)
        if result.returncode != 0:
            raise ReleaseError(f"{Path(command[1]).name} failed:\n{result.stdout}{result.stderr}")
        print(f"  regenerated via {Path(command[1]).name}")


def apply(version: str, *, check: bool) -> int:
    if not SEMVER.match(version):
        raise ReleaseError(f"not a semantic version: {version!r}")
    disagreements: list[str] = []
    for path in JSON_VERSION_SITES:
        disagreements += _json_site(path, version, check=check)
    for path, pattern in TEXT_VERSION_SITES:
        disagreements += _text_site(path, pattern, version, check=check)
    for path in PATH_VERSION_SITES:
        disagreements += _pattern_site(path, PATH_PATTERN, version, check=check)
    for path in BADGE_SITES:
        for pattern in BADGE_PATTERNS:
            disagreements += _pattern_site(path, pattern, version, check=check)

    if check:
        if disagreements:
            print(f"Release {version} is NOT consistent:")
            for line in disagreements:
                print(f"  {line}")
            return 1
        print(f"Release {version} is consistent across "
              f"{len(JSON_VERSION_SITES) + len(TEXT_VERSION_SITES)} declared sites.")
        return 0

    print(f"Set {version} across {len(JSON_VERSION_SITES) + len(TEXT_VERSION_SITES)} sites.")
    _regenerate()
    return apply(version, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", help="the release to set; defaults to the current one")
    parser.add_argument("--check", action="store_true", help="verify only, change nothing")
    arguments = parser.parse_args()
    version = arguments.version or _read_current()
    try:
        return apply(version, check=arguments.check)
    except ReleaseError as error:
        print(f"release: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
