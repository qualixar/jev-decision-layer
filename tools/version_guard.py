"""Refuse a shipped change that reuses an already-released version number.

Users do not install a tag. `plugin marketplace add qualixar/jev-decision-layer`
clones the default branch and resolves each plugin by the relative path in
`.claude-plugin/marketplace.json`, so whatever is on `main` is what every host
installs -- Claude Code, Codex, Antigravity, Hermes and VS Code alike.

That is the ordinary first-party arrangement and not a fault: Anthropic's own
marketplace uses the same unpinned relative form for the plugins it owns, and
pins `ref`+`sha` only when pointing into somebody else's repository.

It does mean one rule has to hold, and nothing was enforcing it:

    a change to a shipped file must not land on main under a version number
    that has already been released.

When it does, two different trees answer to one version. A host that already
holds that version sees no upgrade to perform -- `plugin update` reports
"already at 1.0.7" and does nothing -- so the user keeps running the old code
while every version check agrees they are current. That is close to
undiagnosable remotely, because the number everyone would ask for is right.

It is not hypothetical. It happened here while 1.0.7 was being built: the
runtime changed twice under one version and the installed cache went stale
exactly as described, which is what this guard exists to stop.

WHAT COUNTS AS SHIPPED. Everything under `plugins/` -- both packages, all five
hosts, runtime and hooks and manifests and skills and scripts -- plus the
marketplace file users resolve through. Docs, tests and tools are not shipped
into an install and are deliberately exempt: a README fix needs no new version.

    python3 tools/version_guard.py            # check, exit 1 on a violation
    python3 tools/version_guard.py --verbose  # also list what it compared
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "qualixar-jev-decision-layer"

# Paths whose contents reach a user's installation. Given to `git diff` as
# pathspecs, so a directory covers everything beneath it.
SHIPPED: tuple[str, ...] = ("plugins", ".claude-plugin/marketplace.json")

SKIPPED = "skipped"
CLEAN = "clean"
UNRELEASED = "unreleased"
VIOLATION = "violation"


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(("git", "-C", str(ROOT), *args),
                          capture_output=True, text=True, check=False)


def declared_version() -> str:
    return json.loads((PLUGIN / "plugin.json").read_text())["version"]


def evaluate() -> dict:
    """Classify the working tree against the tag its version claims.

    Returns a verdict rather than printing, so the test can assert on it.
    """
    version = declared_version()
    tag = f"v{version}"

    if _git("rev-parse", "--git-dir").returncode != 0:
        return {"status": SKIPPED, "version": version, "tag": tag, "changed": (),
                "reason": "not a git repository -- an unpacked archive cannot be compared"}

    # `tag^{commit}` resolves an annotated tag to the commit it points at; a
    # missing tag fails here rather than silently resolving to something else.
    if _git("rev-parse", "--verify", "--quiet", f"{tag}^{{commit}}").returncode != 0:
        return {"status": UNRELEASED, "version": version, "tag": tag, "changed": (),
                "reason": f"{tag} does not exist, so this version is unreleased and free to change"}

    diff = _git("diff", "--name-only", f"{tag}^{{commit}}", "--", *SHIPPED)
    if diff.returncode != 0:
        return {"status": SKIPPED, "version": version, "tag": tag, "changed": (),
                "reason": f"git diff against {tag} failed: {diff.stderr.strip()}"}

    changed = tuple(line for line in diff.stdout.splitlines() if line)
    if not changed:
        return {"status": CLEAN, "version": version, "tag": tag, "changed": (),
                "reason": f"no shipped file differs from {tag}"}
    return {"status": VIOLATION, "version": version, "tag": tag, "changed": changed,
            "reason": f"{len(changed)} shipped file(s) differ from {tag} "
                      f"while the version is still {version}"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--verbose", action="store_true",
                        help="list the shipped paths that were compared")
    args = parser.parse_args()

    verdict = evaluate()
    if args.verbose:
        print(f"comparing {', '.join(SHIPPED)} against {verdict['tag']}")

    if verdict["status"] == VIOLATION:
        print(f"Version guard FAILED: {verdict['reason']}", file=sys.stderr)
        for path in verdict["changed"][:20]:
            print(f"    {path}", file=sys.stderr)
        if len(verdict["changed"]) > 20:
            print(f"    ... and {len(verdict['changed']) - 20} more", file=sys.stderr)
        print(f"\nA host that already holds {verdict['version']} will not upgrade to this,"
              f"\nso the user keeps the old code while every version check says current."
              f"\n\nBump the version before pushing:"
              f"\n    python3 tools/release.py --version <next>", file=sys.stderr)
        return 1

    print(f"Version guard: {verdict['status']} -- {verdict['reason']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
