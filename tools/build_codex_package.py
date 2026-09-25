"""Build/check the Codex-only package from the shared portable plugin source.

Codex 0.156 does not register plugin hooks when a portable root plugin.json is
present. The Codex marketplace therefore points at a compatibility-only copy;
the portable source remains available for the other host adapters.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "plugins" / "qualixar-jev-decision-layer"
TARGET = ROOT / "plugins" / "qualixar-jev-codex"
DIRECTORIES = (".codex-plugin", "assets", "hooks", "licenses", "runtime", "scripts", "skills")
FILES = (".mcp.json", "THIRD_PARTY_NOTICES.md")


def _included(path: Path) -> bool:
    return not any(part == "__pycache__" or part.startswith(".pytest_cache") for part in path.parts) and path.suffix != ".pyc"


def _files(root: Path) -> set[Path]:
    return {path.relative_to(root) for path in root.rglob("*") if path.is_file() and _included(path.relative_to(root))}


def build(*, check: bool) -> None:
    expected = set()
    for directory in DIRECTORIES:
        expected.update(path.relative_to(SOURCE) for path in (SOURCE / directory).rglob("*") if path.is_file() and _included(path.relative_to(SOURCE)))
    expected.update(Path(name) for name in FILES)
    if check:
        if (TARGET / "plugin.json").exists() or _files(TARGET) != expected:
            raise ValueError("CODEX_PACKAGE_FILE_SET_MISMATCH")
        for name in expected:
            if hashlib.sha256((SOURCE / name).read_bytes()).digest() != hashlib.sha256((TARGET / name).read_bytes()).digest():
                raise ValueError(f"CODEX_PACKAGE_HASH_MISMATCH:{name}")
        print(f"Codex package verified: {len(expected)} files, no root portable manifest.")
        return
    TARGET.mkdir(parents=True, exist_ok=True)
    for directory in DIRECTORIES:
        shutil.copytree(SOURCE / directory, TARGET / directory, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache"))
    for name in FILES:
        shutil.copy2(SOURCE / name, TARGET / name)
    build(check=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    build(check=parser.parse_args().check)
