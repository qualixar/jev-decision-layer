"""Local gate evaluation for recipe answers.

WHY THIS EXISTS
---------------
The product's whole economy is: a Jev token is ~free, a host-model token
(Claude Code, Codex, Antigravity) is expensive. The layer earns its keep only
when it stops the host model doing work.

Before this module, a recipe's `policy` block was authored in the source
recipe, stripped by the catalog builder, and read by nothing. The host model
received a typed answer with **no gate**, so it had to decide for itself
whether to trust that answer — re-deriving, in expensive host context, the
exact judgment the cheap model was called to settle. The calibration was paid
for and thrown away.

This evaluates the gate locally and hands the host a `host_action` it can obey
without re-reasoning:

    act     -> the answer cleared every threshold. Use it. Do not re-derive.
    verify  -> the answer is a starting point, not a conclusion. Cheaper to
               check than to redo from nothing.
    ignore  -> below the floor, or explicitly `unknown`. Decide normally; the
               call cost ~nothing and removed a bad option.

THE GATE FAILS CLOSED. ALWAYS.
------------------------------
An earlier version treated an unreadable threshold as an absent one: a policy
with `min_confidence: "high"` silently dropped the floor and returned `act` on
a confidence of 0.1. That is the single worst behaviour this module can have.
`act` is a promise that the host need not think again, so anything it cannot
positively verify must degrade to `verify`, never to `act`.

Concretely, every one of these now refuses to clear the gate rather than
waving it through: a threshold that is missing, malformed, or out of range; a
value outside its own domain (a Noul of 1.5, a confidence of 999, a
probability of -0.5); a selected label that is not a string, or not the most
probable option in its own distribution; a distribution whose mass exceeds
one; and an `act` that would carry no recommendation for the host to act on.

CONFIDENCE IS NOT PROBABILITY
-----------------------------
Measured against jev-1.13: a Choice returned `technical` at probability 0.85
with confidence 0.77. Probability says WHICH; confidence says how much the
answer can be trusted at all. Gating on probability alone passes answers the
model is not actually sure of, which is the expensive failure — the host acts,
is wrong, and pays again to undo it.

**A Noul has NO confidence field.** Only Choice and Score carry one, so only
those recipes declare `min_confidence`. For a Noul, distance from 0.5 IS the
certainty and the yes/no bands already express it.
"""

from __future__ import annotations

from typing import Any

from .common import AutoError

ACT = "act"
VERIFY = "verify"
IGNORE = "ignore"

_KINDS = {"choice", "score", "noul"}
# Thresholds a gate of each kind must supply. A gate missing one of these is
# incomplete, and an incomplete gate is not a gate.
_REQUIRED_THRESHOLDS = {
    "choice": ("min_confidence", "min_selected_probability"),
    "score": ("min_confidence", "min_score"),
    "noul": ("yes", "no"),
}
# Tolerance on a distribution's total mass. Providers round; they do not
# invent half a unit of probability.
_MASS_TOLERANCE = 1.01


def _number(value: Any) -> float | None:
    """Finite floats only. A NaN would pass every `<` test silently."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if value == value and value not in (float("inf"), float("-inf")) else None


def _unit(value: Any) -> float | None:
    """A finite number that is actually inside [0, 1].

    Confidences, probabilities and Noul values all live here. Accepting 999 as
    a confidence let a hostile or broken provider clear any floor.
    """
    number = _number(value)
    return number if number is not None and 0.0 <= number <= 1.0 else None


def validate_gates(gates: Any) -> list[dict[str, Any]]:
    """Reject a catalog whose gates could not be enforced.

    Checked here so a bad gate fails at load, loudly, instead of degrading a
    decision quietly later.
    """
    if not isinstance(gates, list) or not 1 <= len(gates) <= 128:
        raise AutoError("RECIPE_GATES_INVALID")
    seen = set()
    for gate in gates:
        if not isinstance(gate, dict) or set(gate) != {"id", "policy"}:
            raise AutoError("RECIPE_GATES_INVALID")
        identifier, policy = gate["id"], gate["policy"]
        if not isinstance(identifier, str) or identifier in seen:
            raise AutoError("RECIPE_GATES_INVALID")
        seen.add(identifier)
        if not isinstance(policy, dict) or policy.get("kind") not in _KINDS:
            raise AutoError("RECIPE_GATES_INVALID")
        kind = policy["kind"]
        for name in _REQUIRED_THRESHOLDS[kind]:
            # `min_score` is a score on the recipe's own scale, not a unit value.
            reader = _number if name == "min_score" else _unit
            if reader(policy.get(name)) is None:
                raise AutoError("RECIPE_GATES_INVALID")
        if kind == "noul" and not _unit(policy["no"]) < _unit(policy["yes"]):
            # Inverted bands make the `>= yes` branch fire first and turn the
            # middle of the range into a confident answer.
            raise AutoError("RECIPE_GATES_INVALID")
        if kind == "noul" and "min_confidence" in policy:
            raise AutoError("RECIPE_GATES_INVALID")  # a Noul answer has no confidence field
        for name in ("positive_outcome", "negative_outcome"):
            if not isinstance(policy.get(name), str) or not policy[name]:
                raise AutoError("RECIPE_GATES_INVALID")
        if policy["positive_outcome"] == policy["negative_outcome"]:
            # A gate that cannot tell clean from dirty costs the host a review
            # it did not need, which is the cost this layer exists to remove.
            raise AutoError("RECIPE_GATES_INVALID")
    return gates


def _probability_of(answer: dict[str, Any], label: Any) -> float | None:
    """The selected label's share, if the distribution is coherent.

    Returns None — meaning "do not clear the gate" — when the distribution is
    malformed, when a value is outside [0, 1], when the total mass exceeds
    one, or when the selected label is not the most probable option. A model
    that picks a label it scored below another has contradicted itself, and a
    self-contradictory answer is not one to act on.
    """
    probabilities = answer.get("probabilities")
    if not isinstance(probabilities, dict) or not probabilities:
        return None
    try:
        selected = probabilities.get(label)
    except TypeError:
        return None  # an unhashable label cannot index anything
    values = []
    for value in probabilities.values():
        unit = _unit(value)
        if unit is None:
            return None
        values.append(unit)
    if sum(values) > _MASS_TOLERANCE:
        return None
    chosen = _unit(selected)
    if chosen is None or chosen < max(values):
        return None
    return chosen


def evaluate(policy: Any, answer: Any) -> dict[str, Any]:
    """Apply one recipe's policy to one typed answer. Never raises on content."""
    reasons: list[str] = []
    result: dict[str, Any] = {
        "status": "REVIEW",
        "host_action": VERIFY,
        "recommendation": None,
        "confidence": None,
        "probability": None,
        "thresholds_applied": {},
        "thresholds_calibrated": False,
        "reasons": reasons,
        "execution_authorized": False,
    }

    def finish(status: str, host_action: str, reason: str, recommendation: Any = None):
        # `act` is a promise the host need not think again. Without something
        # to act on it is a worse answer than `verify`.
        if host_action == ACT and not isinstance(recommendation, str):
            host_action, status = VERIFY, "REVIEW"
            reason = f"{reason} The gate cleared but the policy named no outcome to act on."
            recommendation = None
        result["status"] = status
        result["host_action"] = host_action
        result["recommendation"] = recommendation
        reasons.append(reason)
        return result

    if not isinstance(policy, dict):
        return finish("REVIEW", VERIFY, "No usable policy was supplied.")
    kind = policy.get("kind")
    if not isinstance(answer, dict):
        return finish("REVIEW", VERIFY, "No typed answer was returned.")

    if kind == "noul":
        value = _unit(answer.get("noul"))
        yes, no = _unit(policy.get("yes")), _unit(policy.get("no"))
        result["thresholds_applied"] = {"yes": yes, "no": no}
        if value is None or yes is None or no is None or no >= yes:
            return finish("REVIEW", VERIFY, "Noul value or its bands were unusable.")
        result["confidence"] = round(abs(value - 0.5) * 2.0, 4)  # no confidence field exists
        result["probability"] = value
        if value >= yes:
            return finish("RECOMMEND", ACT, "Above the yes band.", policy.get("positive_outcome"))
        if value <= no:
            return finish("RECOMMEND", ACT, "Below the no band.", policy.get("negative_outcome"))
        return finish("REVIEW", VERIFY, "Between the bands; the model is not committing.")

    if kind not in ("choice", "score"):
        return finish("REVIEW", VERIFY, f"Unknown policy kind {kind!r}.")

    confidence = _unit(answer.get("confidence"))
    floor = _unit(policy.get("min_confidence"))
    result["confidence"] = confidence

    if kind == "choice":
        label = answer.get("choice")
        probability = _probability_of(answer, label)
        min_probability = _unit(policy.get("min_selected_probability"))
        result["probability"] = probability
        result["thresholds_applied"] = {"min_confidence": floor, "min_selected_probability": min_probability}

        if not isinstance(label, str) or not label:
            return finish("REVIEW", VERIFY, "The selected choice was not a label.")
        if label == policy.get("unknown_choice", "unknown"):
            return finish("REVIEW", IGNORE, "The model selected the unknown option.")
        if floor is None or min_probability is None:
            # Fail closed: an unreadable threshold is a broken gate, not an absent one.
            return finish("REVIEW", VERIFY, "This recipe's thresholds are missing or unreadable; "
                                            "an ungated answer is not an authorised one.")
        if probability is None:
            return finish("REVIEW", VERIFY, "The answer distribution was unusable or did not "
                                            "rank the selected option highest.")
        if confidence is None:
            return finish("REVIEW", VERIFY, "No usable confidence was returned; probability alone is not a gate.")
        if confidence < floor:
            return finish("REVIEW", VERIFY,
                          f"Confidence {confidence} is below {floor}; the distribution looks decisive "
                          "but the answer is not calibrated.")
        if probability < min_probability:
            return finish("REVIEW", VERIFY, f"Selected probability {probability} is below {min_probability}.")
        return finish("RECOMMEND", ACT, "Cleared both the confidence floor and the probability bar.",
                      policy.get("positive_outcome"))

    score = _number(answer.get("score"))
    min_score = _number(policy.get("min_score"))
    result["probability"] = score
    result["thresholds_applied"] = {"min_confidence": floor, "min_score": min_score}
    if floor is None or min_score is None:
        return finish("REVIEW", VERIFY, "This recipe's thresholds are missing or unreadable; "
                                        "an ungated answer is not an authorised one.")
    if score is None:
        return finish("REVIEW", VERIFY, "Score was unusable.")
    if confidence is None:
        return finish("REVIEW", VERIFY, "No usable confidence was returned; a score alone is not a gate.")
    if confidence < floor:
        return finish("REVIEW", VERIFY, f"Confidence {confidence} is below {floor}.")
    if score < min_score:
        return finish("RECOMMEND", ACT, "Below the score bar.", policy.get("negative_outcome"))
    return finish("RECOMMEND", ACT, "At or above the score bar.", policy.get("positive_outcome"))
