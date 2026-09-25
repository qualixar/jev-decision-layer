"""Offline self-test for the recipe gate. No model call, no host reasoning.

WHY THIS EXISTS
---------------
A host adopting this layer has one question it cannot answer cheaply: does the
gate actually hold? Answering it by running live recipes costs provider calls
and, far worse, costs host-model context to read and judge the results.

Every recipe ships three hand-authored cases — nominal, uncertain, adversarial
— each with the typed answer a provider would plausibly return and the gate
verdict that answer must produce. `selftest()` replays all of them through the
real gate and returns a count. Nothing is inferred, nothing is generated, no
token is spent on either side.

The three variants are chosen to pin the three ways the layer earns its keep:

    nominal      a clear-cut input the gate must clear, so the host acts once
                 and does not re-derive the judgment it paid for.
    uncertain    a genuinely ambiguous input. The gate must NOT clear it. This
                 is the expensive failure to prevent: a host that acts on a
                 poorly-calibrated answer pays again to undo it.
    adversarial  an input carrying an instruction in material that is supposed
                 to be data. The gate must refuse to authorise. A layer that
                 can be talked into `act` is worse than no layer.

An expectation here is frozen output of the real gate, recorded when the
fixture was authored. If `recipe_gate` changes behaviour, these stop matching.
That is the point.
"""

from __future__ import annotations

from typing import Any

from .common import AutoError
from .recipe_gate import evaluate
from .recipe_runtime import catalog_document, prepare_recipe

VARIANTS = ("nominal", "uncertain", "adversarial")
_REQUIRED = {
    "fixture_id", "variant", "data_classification", "state", "mock_answer",
    "expected_status", "expected_host_action", "expected_recommendation", "disclaimer",
}


def validate_fixtures(entries: Any) -> list[dict[str, Any]]:
    """Structural check only. Never trusts the file to be well-formed."""
    if not isinstance(entries, list) or not 1 <= len(entries) <= 128:
        raise AutoError("RECIPE_FIXTURES_INVALID")
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"id", "cases"}:
            raise AutoError("RECIPE_FIXTURES_INVALID")
        identifier, cases = entry["id"], entry["cases"]
        if not isinstance(identifier, str) or identifier in seen:
            raise AutoError("RECIPE_FIXTURES_INVALID")
        seen.add(identifier)
        if not isinstance(cases, list) or [case.get("variant") for case in cases] != list(VARIANTS):
            raise AutoError("RECIPE_FIXTURES_INVALID")
        for case in cases:
            if set(case) != _REQUIRED or case["data_classification"] != "synthetic":
                raise AutoError("RECIPE_FIXTURES_INVALID")
            if not isinstance(case["state"], dict) or not isinstance(case["mock_answer"], dict):
                raise AutoError("RECIPE_FIXTURES_INVALID")
    return entries


def _document() -> dict[str, Any]:
    data = catalog_document()
    if data is None or "fixtures" not in data or "gates" not in data:
        raise AutoError("RECIPE_FIXTURES_UNAVAILABLE")
    validate_fixtures(data["fixtures"])
    return data


def _gate(data: dict[str, Any], recipe_id: str) -> dict[str, Any]:
    gate = next((item for item in data["gates"] if item["id"] == recipe_id), None)
    if gate is None:
        raise AutoError("RECIPE_NOT_FOUND")
    return gate["policy"]


def run_fixture(recipe_id: str, variant: str = "nominal") -> dict[str, Any]:
    """Replay one fixture. Offline: the answer is recorded, never requested."""
    if variant not in VARIANTS:
        raise AutoError("RECIPE_FIXTURE_VARIANT_INVALID")
    data = _document()
    entry = next((item for item in data["fixtures"] if item["id"] == recipe_id), None)
    if entry is None:
        raise AutoError("RECIPE_NOT_FOUND")
    case = next(item for item in entry["cases"] if item["variant"] == variant)
    # The recorded state must still satisfy the recipe's own input contract.
    # A fixture that no longer type-checks is a broken fixture, not a pass.
    prepare_recipe(recipe_id, case["state"])
    result = evaluate(_gate(data, recipe_id), case["mock_answer"])
    matched = (
        result["status"] == case["expected_status"]
        and result["host_action"] == case["expected_host_action"]
        and result["recommendation"] == case["expected_recommendation"]
    )
    return {
        "mode": "fixture",
        "data_classification": "synthetic",
        "fixture_id": case["fixture_id"],
        "recipe_id": recipe_id,
        "variant": variant,
        "matched": matched,
        "expected": {
            "status": case["expected_status"],
            "host_action": case["expected_host_action"],
            "recommendation": case["expected_recommendation"],
        },
        "observed": {
            "status": result["status"],
            "host_action": result["host_action"],
            "recommendation": result["recommendation"],
            "reasons": result["reasons"],
        },
        "disclaimer": case["disclaimer"],
    }


def selftest() -> dict[str, Any]:
    """Replay every fixture. Zero provider calls, zero host tokens."""
    data = _document()
    failures = []
    total = 0
    for entry in data["fixtures"]:
        for variant in VARIANTS:
            total += 1
            outcome = run_fixture(entry["id"], variant)
            if not outcome["matched"]:
                failures.append(outcome)
    return {
        "mode": "fixture",
        "data_classification": "synthetic",
        "recipes": len(data["fixtures"]),
        "cases": total,
        "passed": total - len(failures),
        "failures": failures,
        "all_passed": not failures,
        "disclaimer": "Offline contract check of the local gate. Not a provider accuracy benchmark.",
    }
