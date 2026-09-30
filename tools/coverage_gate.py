"""Hold per-module line coverage at or above a recorded floor.

A single project-wide percentage hides the thing that matters: one module at
10% is invisible behind forty at 95%. So the floor is per module, it is
committed to the repo, and it only ever moves up.

    python3 tools/coverage_gate.py --check     # fail if any module regressed
    python3 tools/coverage_gate.py --update    # raise floors that improved
    python3 tools/coverage_gate.py --report    # show the gap to the target

`--update` never lowers a floor. Lowering one is a deliberate act: edit
coverage-floor.json by hand, in a commit that says why.

Coverage is a floor, never a target. It cannot tell a real test from a
tautological one -- this repo shipped 108 fixtures that agreed with their own
recorded expectations and proved nothing, and a test that passed with the fix
deleted. Both were 'covered'. Mutation-test anything you care about.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "plugins" / "qualixar-jev-decision-layer" / "runtime"
FLOOR_FILE = ROOT / "coverage-floor.json"
COVERAGE_DATA = ROOT / ".coverage-gate"

# The bar every module is held to unless it is exempt below.
TARGET = 90

# No exemptions. Every module is held to the target.
#
# An earlier draft of this file exempted eleven modules, including the three
# Laya/MLX ones, on the reasoning that the local route ships disabled. That was
# wrong, and wrong for an instructive reason: the exemptions were written by
# category without reading the code. mlx_preflight.py imports no model at all
# -- it takes a tokenizer as an argument and computes token budgets -- and
# mlx_worker.py runs its entire artifact-verification chain (Apple Silicon
# check, manifest pinning, per-file hashing, path-traversal refusal) BEFORE it
# imports laya_mlx. Those are the branches that stop a swapped model file being
# loaded, they are reachable with no weights on disk, and they were at 0%.
#
# A module being off by default is a reason to test its refusal paths harder,
# not a reason to skip it.
EXEMPT: dict[str, str] = {}


def _leaked_state(root: Path) -> list[str]:
    """Files the suite left in the state directory it was told to use."""
    return sorted(str(path.relative_to(root)) for path in root.rglob("*") if path.is_file())


def _real_state_roots() -> list[Path]:
    """Where a test that ignored XDG_STATE_HOME would write: the home fallback,
    and the XDG location of the environment that invoked the gate."""
    import os
    roots = {Path.home() / ".local" / "state" / "qualixar-jev-decision-layer"}
    configured = os.environ.get("XDG_STATE_HOME")
    if configured and Path(configured).is_absolute():
        roots.add(Path(configured) / "qualixar-jev-decision-layer")
    return sorted(roots)


def _entries(roots: list[Path]) -> set[str]:
    return {str(entry) for root in roots if root.is_dir() for entry in root.iterdir()}


def _measure() -> dict[str, int]:
    """Run the suite under coverage and return percent covered per module.

    The suite runs against a private XDG_STATE_HOME. Every test is meant to
    isolate its own state, so anything written there is a test that did not,
    and the gate fails naming it rather than letting the suite write grants
    into the developer's real state directory. A test can also bypass the
    variable, so the real state roots are compared before and after the run.
    """
    real_roots = _real_state_roots()
    before = _entries(real_roots)
    with tempfile.TemporaryDirectory(prefix="jev-gate-state-") as state:
        environment = {"COVERAGE_FILE": str(COVERAGE_DATA), "XDG_STATE_HOME": state}
        run = subprocess.run(
            [sys.executable, "-m", "coverage", "run", f"--source={SOURCE}", "-m",
             "pytest", "tests/", "-q"],
            cwd=ROOT, capture_output=True, text=True, env={**_environ(), **environment})
        leaked = _leaked_state(Path(state))
    leaked += sorted(_entries(real_roots) - before)
    if run.returncode != 0:
        raise SystemExit("the test suite failed; fix it before gating coverage\n"
                         + run.stdout[-4000:] + run.stderr[-2000:])
    if leaked:
        raise SystemExit("the test suite wrote to the state directory instead of isolating it:\n"
                         + "\n".join(f"  {name}" for name in leaked[:20])
                         + "\n(an entry in a real state root can also be a real enrollment made "
                           "while the gate ran)")
    report = subprocess.run(
        [sys.executable, "-m", "coverage", "json", "-o", "-"],
        cwd=ROOT, capture_output=True, text=True, env={**_environ(), **environment})
    if report.returncode != 0:
        raise SystemExit("coverage json failed\n" + report.stderr)
    document = json.loads(report.stdout)
    measured: dict[str, int] = {}
    for path, entry in document["files"].items():
        # coverage reports paths relative to its working directory; resolve
        # against ROOT rather than assuming either form.
        absolute = Path(path) if Path(path).is_absolute() else (ROOT / path)
        try:
            relative = str(absolute.resolve().relative_to(SOURCE.resolve()))
        except ValueError:
            continue  # measured something outside the runtime; not ours to gate
        if entry["summary"]["num_statements"] == 0:
            continue
        measured[relative] = int(entry["summary"]["percent_covered"])
    if not measured:
        raise SystemExit(f"coverage measured nothing under {SOURCE}")
    return dict(sorted(measured.items()))


def _environ() -> dict[str, str]:
    import os
    return dict(os.environ)


def _floors() -> dict[str, int]:
    if not FLOOR_FILE.is_file():
        return {}
    return json.loads(FLOOR_FILE.read_text())["modules"]


def _write_floors(floors: dict[str, int]) -> None:
    FLOOR_FILE.write_text(json.dumps(
        {"target": TARGET,
         "note": "Per-module line-coverage floors. Raised by tools/coverage_gate.py "
                 "--update; never lowered automatically. A floor is a floor, not a "
                 "target, and coverage cannot tell a real test from a vacuous one.",
         "exempt": EXEMPT,
         "modules": dict(sorted(floors.items()))},
        indent=2) + "\n")


def check(measured: dict[str, int], floors: dict[str, int]) -> int:
    regressions = [f"  {name}: {measured[name]}% is below its floor of {floor}%"
                   for name, floor in floors.items()
                   if name in measured and measured[name] < floor]
    missing = sorted(set(floors) - set(measured))
    new = sorted(set(measured) - set(floors))
    if regressions:
        print("Coverage regressed:")
        print("\n".join(regressions))
    if missing:
        print(f"Modules with a floor but no measurement (deleted or renamed?): {missing}")
    if new:
        print(f"New modules with no floor yet; run --update to record them: {new}")
    if regressions or missing:
        return 1
    below = {n: p for n, p in measured.items() if p < TARGET and n not in EXEMPT}
    print(f"Coverage holds. {len(measured) - len(below)}/{len(measured)} modules at "
          f"{TARGET}%+ or exempt; {len(below)} still below target.")
    return 0


def report(measured: dict[str, int]) -> int:
    below = {n: p for n, p in sorted(measured.items(), key=lambda item: item[1])
             if p < TARGET and n not in EXEMPT}
    print(f"Target {TARGET}% per module. {len(below)} below it:\n")
    for name, percent in below.items():
        print(f"  {percent:3d}%  {name}")
    print(f"\n{len(EXEMPT)} modules exempt with a recorded reason.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true")
    group.add_argument("--update", action="store_true")
    group.add_argument("--report", action="store_true")
    arguments = parser.parse_args()

    measured = _measure()
    if arguments.report:
        return report(measured)
    if arguments.update:
        floors = _floors()
        raised = {n: p for n, p in measured.items() if p > floors.get(n, -1)}
        floors.update(raised)
        _write_floors(floors)
        print(f"Recorded {len(floors)} floors; raised {len(raised)}.")
        for name in sorted(raised):
            print(f"  {name}: {raised[name]}%")
        return 0
    return check(measured, _floors())


if __name__ == "__main__":
    sys.exit(main())
