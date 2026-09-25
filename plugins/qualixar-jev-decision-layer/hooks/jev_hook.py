#!/usr/bin/env python3
"""Additive Codex hook; unenrolled workspaces are unchanged."""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> None:
    plugin_root = Path(__file__).resolve().parents[1]
    runtime = plugin_root / "runtime"
    if not runtime.is_dir():
        return
    sys.path.insert(0, str(runtime))
    from jev_auto.hooks import main as run_hook

    run_hook()


if __name__ == "__main__":
    main()
