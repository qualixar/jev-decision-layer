"""Build/check the Codex-only package from the shared portable plugin source.

Codex 0.156 does not register plugin hooks when a portable root plugin.json is
present. The Codex marketplace therefore points at a compatibility-only copy;
the portable source remains available for the other host adapters.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "plugins" / "qualixar-jev-decision-layer"
TARGET = ROOT / "plugins" / "qualixar-jev-codex"
DIRECTORIES = (".codex-plugin", "assets", "hooks", "licenses", "runtime", "scripts", "skills")
FILES = ("THIRD_PARTY_NOTICES.md",)
CODEX_MCP = ROOT / "tools" / "host_mcp" / "codex.json"
# One filename for the Codex descriptor in both packages. The Codex overlay is
# copied verbatim from the portable source, so if the two packages named this
# file differently the copied manifest would point at a file that is not there.
# `.mcp.json` is Claude's in the portable package and cannot be shared.
CODEX_MCP_NAME = Path("mcp.json")
LEGACY_CODEX_HOOK_NAME = Path("hooks/hooks.json")

# Belongs to another host and must not ship here. A Claude hook file inside the
# Codex package is the same shape as the `${CLAUDE_PLUGIN_ROOT}` descriptor that
# stopped Codex starting its server for four releases: the wrong host's file,
# carried along by a wholesale copy. Deliberately NOT filtered out of the target
# listing below, so that a stale copy still fails the file-set check instead of
# becoming invisible to it.
OTHER_HOST_FILES = (Path("hooks/claude-hooks.json"),)

# Codex loads a plugin folder that has a root plugin.json (the Agent Plugins
# manifest) through a loader with no hook support, so every hook would go
# silent (openai/codex#39895). The generated package must never have one, not
# even as a dangling link.
FORBIDDEN_ROOT_FILES = (Path("plugin.json"),)


def _included(path: Path) -> bool:
    return not any(part == "__pycache__" or part.startswith(".pytest_cache") for part in path.parts) and path.suffix != ".pyc"


def _files(root: Path) -> set[Path]:
    return {path.relative_to(root) for path in root.rglob("*") if path.is_file() and _included(path.relative_to(root))}


def build(*, check: bool) -> None:
    expected = set()
    for directory in DIRECTORIES:
        expected.update(path.relative_to(SOURCE) for path in (SOURCE / directory).rglob("*") if path.is_file() and _included(path.relative_to(SOURCE)))
    expected.difference_update(OTHER_HOST_FILES)
    expected.update(Path(name) for name in FILES)
    expected.add(CODEX_MCP_NAME)
    if expected & set(FORBIDDEN_ROOT_FILES):
        raise ValueError("CODEX_PACKAGE_ROOT_MANIFEST")
    if check:
        if any(os.path.lexists(TARGET / name) for name in FORBIDDEN_ROOT_FILES):
            raise ValueError("CODEX_PACKAGE_ROOT_MANIFEST")
        if _files(TARGET) != expected:
            raise ValueError("CODEX_PACKAGE_FILE_SET_MISMATCH")
        for name in expected:
            origin = CODEX_MCP if name == CODEX_MCP_NAME else SOURCE / name
            if hashlib.sha256(origin.read_bytes()).digest() != hashlib.sha256((TARGET / name).read_bytes()).digest():
                raise ValueError(f"CODEX_PACKAGE_HASH_MISMATCH:{name}")
        print(f"Codex package verified: {len(expected)} files, no root portable manifest.")
        return
    TARGET.mkdir(parents=True, exist_ok=True)
    # The portable source now names Codex's hook file explicitly. Remove the
    # old default-discovery name from an existing generated package so Claude
    # cannot load the Codex hook descriptor when both packages share a root.
    (TARGET / LEGACY_CODEX_HOOK_NAME).unlink(missing_ok=True)
    for name in FORBIDDEN_ROOT_FILES:
        (TARGET / name).unlink(missing_ok=True)
    for directory in DIRECTORIES:
        shutil.copytree(SOURCE / directory, TARGET / directory, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache",
                                                      *(path.name for path in OTHER_HOST_FILES)))
    for name in FILES:
        shutil.copy2(SOURCE / name, TARGET / name)
    for name in OTHER_HOST_FILES:
        (TARGET / name).unlink(missing_ok=True)
    shutil.copy2(CODEX_MCP, TARGET / CODEX_MCP_NAME)
    (TARGET / ".mcp.json").unlink(missing_ok=True)
    build(check=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    build(check=parser.parse_args().check)
