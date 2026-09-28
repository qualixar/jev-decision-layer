"""Offline self-test for the recipe gate. No model call, no host reasoning.

WHY THIS EXISTS
---------------
A host adopting this layer has one question it cannot answer cheaply: does the
gate actually hold? Answering it by running live recipes costs provider calls
and, far worse, costs host-model context to read and judge the results.

Every recipe ships three hand-authored cases — nominal, uncertain, adversarial
— each with the typed answer a provider would plausibly return and the raw gate
verdict that answer must produce. `selftest()` replays all of them through the
real gate and returns a count. It also reports the live host action after the
recipe-status cap. Nothing is inferred, nothing is generated, no token is spent.

The three variants pin the three ways the layer earns its keep:

    nominal      a clear-cut input the raw gate must clear. Experimental
                 recipes still require host verification before any action.
    uncertain    a genuinely ambiguous input. The gate must NOT clear it. This
                 is the expensive failure to prevent: a host that acts on a
                 poorly-calibrated answer pays again to undo it.
    adversarial  an input carrying an instruction in material that is supposed
                 to be data. The gate must refuse to authorise. A layer that
                 can be talked into `act` is worse than no layer.

THE VARIANTS ARE ENFORCED, NOT DESCRIBED
----------------------------------------
Those three sentences used to be a docstring and nothing more. A replay only
compared the gate's output against the expectation recorded beside it, so a
fixture whose `uncertain` case confidently cleared the gate — with
`expected_host_action: act` written next to it — reported `matched: true` and
the whole suite passed. The proof was tautological: it checked the fixture
agreed with itself.

`INTENT` below is now checked independently of the recorded expectation. A
case has to satisfy BOTH to pass, so an author cannot make a fixture green by
writing down whatever the gate happened to do.

A single broken case also no longer destroys the proof: a fixture that fails
to load, type-check or evaluate is recorded as a failure and the replay
continues. A crash in the middle would otherwise tell a host nothing about the
other ninety-five.
"""

from __future__ import annotations

from typing import Any

from .common import AutoError
from .recipe_gate import ACT, VERIFY, evaluate, validate_gates
from .recipe_runtime import catalog_document, prepare_recipe

VARIANTS = ("nominal", "uncertain", "adversarial")
# What each variant must prove, independent of what was recorded for it.
INTENT = {
    "nominal": ("must clear the gate", lambda action: action == ACT),
    "uncertain": ("must not clear the gate", lambda action: action != ACT),
    "adversarial": ("must not clear the gate", lambda action: action != ACT),
}
_REQUIRED = {
    "fixture_id", "variant", "data_classification", "state", "mock_answer",
    "expected_status", "expected_host_action", "expected_recommendation", "disclaimer",
}


def apply_recipe_status_cap(gate: dict[str, Any], recipe_status: str) -> dict[str, Any]:
    """Translate a raw gate verdict into the action a live host may take.

    The input gate is never mutated: the offline proof still tests whether its
    threshold can distinguish nominal from uncertain cases, while the same
    cap used by Engine.try_recipe prevents experimental specs from authorizing
    a host action.
    """
    effective = {**gate, "reasons": list(gate["reasons"])}
    if recipe_status == "SPECIFICATION_NOT_MODEL_EVALUATED" and gate["host_action"] == ACT:
        effective["host_action"] = VERIFY
        effective["status"] = "REVIEW"
        effective["reasons"].append(
            "This recipe is SPECIFICATION_NOT_MODEL_EVALUATED; an ACT recommendation is capped at VERIFY."
        )
    return effective


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
        if not isinstance(cases, list) or len(cases) != len(VARIANTS):
            raise AutoError("RECIPE_FIXTURES_INVALID")
        # Each case is checked to be a dict BEFORE anything is read from it:
        # `case.get(...)` on a null entry raised AttributeError instead of a
        # typed error, which a caller has no way to handle.
        for case, variant in zip(cases, VARIANTS):
            if not isinstance(case, dict) or case.get("variant") != variant:
                raise AutoError("RECIPE_FIXTURES_INVALID")
            if set(case) != _REQUIRED or case["data_classification"] != "synthetic":
                raise AutoError("RECIPE_FIXTURES_INVALID")
            if not isinstance(case["state"], dict) or not isinstance(case["mock_answer"], dict):
                raise AutoError("RECIPE_FIXTURES_INVALID")
            if not isinstance(case["fixture_id"], str) or not isinstance(case["disclaimer"], str):
                raise AutoError("RECIPE_FIXTURES_INVALID")
    return entries


def _document() -> dict[str, Any]:
    data = catalog_document()
    if not isinstance(data, dict) or "recipes" not in data or "fixtures" not in data or "gates" not in data:
        raise AutoError("RECIPE_FIXTURES_UNAVAILABLE")
    validate_fixtures(data["fixtures"])
    validate_gates(data["gates"])  # _gate below indexes these; do not trust them unchecked
    return data


def _gate(data: dict[str, Any], recipe_id: str) -> dict[str, Any]:
    gate = next((item for item in data["gates"]
                 if isinstance(item, dict) and item.get("id") == recipe_id), None)
    if gate is None or not isinstance(gate.get("policy"), dict):
        raise AutoError("RECIPE_NOT_FOUND")
    return gate["policy"]


def _outcome(case: dict[str, Any], recipe_id: str, variant: str, result: dict[str, Any] | None,
             live_result: dict[str, Any] | None = None,
             recipe_status: str | None = None,
             error: str | None = None) -> dict[str, Any]:
    description, holds = INTENT[variant]
    action = result["host_action"] if result else None
    matched = bool(result) and (
        result["status"] == case["expected_status"]
        and result["host_action"] == case["expected_host_action"]
        and result["recommendation"] == case["expected_recommendation"])
    intent_held = bool(result) and holds(action)
    capped = (recipe_status == "SPECIFICATION_NOT_MODEL_EVALUATED"
              and case["expected_host_action"] == ACT)
    expected_live_action = VERIFY if capped else case["expected_host_action"]
    expected_live_status = "REVIEW" if capped else case["expected_status"]
    live_matched = bool(live_result) and (
        live_result["host_action"] == expected_live_action
        and live_result["status"] == expected_live_status
        and live_result["recommendation"] == case["expected_recommendation"])
    return {
        "mode": "fixture",
        "data_classification": "synthetic",
        "fixture_id": case.get("fixture_id"),
        "recipe_id": recipe_id,
        "variant": variant,
        "passed": matched and intent_held and live_matched,
        "matched": matched,
        "live_matched": live_matched,
        "intent": description,
        "intent_held": intent_held,
        "error": error,
        "expected_raw_gate": {
            "status": case.get("expected_status"),
            "host_action": case.get("expected_host_action"),
            "recommendation": case.get("expected_recommendation"),
        },
        "expected_live": {
            "status": expected_live_status,
            "host_action": expected_live_action,
            "recommendation": case.get("expected_recommendation"),
        },
        "raw_gate": None if result is None else {
            "status": result["status"],
            "host_action": result["host_action"],
            "recommendation": result["recommendation"],
            "reasons": result["reasons"],
        },
        "observed": None if live_result is None else {
            "status": live_result["status"],
            "host_action": live_result["host_action"],
            "recommendation": live_result["recommendation"],
            "reasons": live_result["reasons"],
        },
        "disclaimer": case.get("disclaimer"),
    }


def run_fixture(recipe_id: str, variant: str = "nominal") -> dict[str, Any]:
    """Replay one fixture. Offline: the answer is recorded, never requested."""
    if variant not in VARIANTS:
        raise AutoError("RECIPE_FIXTURE_VARIANT_INVALID")
    data = _document()
    entry = next((item for item in data["fixtures"] if item["id"] == recipe_id), None)
    if entry is None:
        raise AutoError("RECIPE_NOT_FOUND")
    case = next(item for item in entry["cases"] if item["variant"] == variant)
    policy = _gate(data, recipe_id)
    try:
        # The recorded state must still satisfy the recipe's own input
        # contract. A fixture that no longer type-checks is a broken fixture,
        # not a pass.
        prepare_recipe(recipe_id, case["state"])
    except AutoError as error:
        return _outcome(case, recipe_id, variant, None, error=str(error))
    raw_gate = evaluate(policy, case["mock_answer"])
    recipe = next((item for item in data["recipes"] if item["id"] == recipe_id), None)
    if not isinstance(recipe, dict) or not isinstance(recipe.get("status"), str):
        raise AutoError("RECIPE_NOT_FOUND")
    return _outcome(case, recipe_id, variant, raw_gate,
                    apply_recipe_status_cap(raw_gate, recipe["status"]), recipe["status"])


def selftest() -> dict[str, Any]:
    """Replay every fixture. Zero provider calls, zero host tokens."""
    data = _document()
    failures = []
    total = 0
    for entry in data["fixtures"]:
        for variant in VARIANTS:
            total += 1
            try:
                outcome = run_fixture(entry["id"], variant)
            except AutoError as error:
                # One unusable fixture must not take the other ninety-five
                # down with it; a partial proof is still worth reporting.
                outcome = {"recipe_id": entry["id"], "variant": variant,
                           "passed": False, "error": str(error)}
            if not outcome["passed"]:
                failures.append(outcome)
    return {
        "mode": "fixture",
        "data_classification": "synthetic",
        "recipes": len(data["fixtures"]),
        "cases": total,
        "passed": total - len(failures),
        "failures": failures,
        "all_passed": not failures,
        "disclaimer": "Offline synthetic gate contract check, with the experimental live-action cap shown separately. Not provider accuracy, cost, or latency evidence.",
    }
