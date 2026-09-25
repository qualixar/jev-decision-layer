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


def _number(value: Any) -> float | None:
    """Finite floats only. A NaN would pass every `<` test silently."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if value == value and value not in (float("inf"), float("-inf")) else None


def validate_gates(gates: Any) -> list[dict[str, Any]]:
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
    return gates


def evaluate(policy: dict[str, Any], answer: dict[str, Any]) -> dict[str, Any]:
    """Apply one recipe's policy to one typed answer. Never raises on content."""
    kind = policy.get("kind")
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
        result["status"] = status
        result["host_action"] = host_action
        result["recommendation"] = recommendation
        reasons.append(reason)
        return result

    if not isinstance(answer, dict):
        return finish("REVIEW", VERIFY, "No typed answer was returned.")

    if kind == "noul":
        value = _number(answer.get("noul"))
        yes, no = _number(policy.get("yes")), _number(policy.get("no"))
        result["thresholds_applied"] = {"yes": yes, "no": no}
        if value is None or yes is None or no is None:
            return finish("REVIEW", VERIFY, "Noul value or its bands were unusable.")
        result["confidence"] = round(abs(value - 0.5) * 2.0, 4)  # no confidence field exists
        result["probability"] = value
        if value >= yes:
            return finish("RECOMMEND", ACT, "Above the yes band.", policy.get("positive_outcome"))
        if value <= no:
            return finish("RECOMMEND", ACT, "Below the no band.", policy.get("negative_outcome"))
        return finish("REVIEW", VERIFY, "Between the bands; the model is not committing.")

    confidence = _number(answer.get("confidence"))
    floor = _number(policy.get("min_confidence"))
    result["confidence"] = confidence

    if kind == "choice":
        label = answer.get("choice")
        probabilities = answer.get("probabilities")
        probability = _number((probabilities or {}).get(label)) if isinstance(probabilities, dict) else None
        min_probability = _number(policy.get("min_selected_probability"))
        result["probability"] = probability
        result["thresholds_applied"] = {"min_confidence": floor, "min_selected_probability": min_probability}

        if label is None or probability is None:
            return finish("REVIEW", VERIFY, "Choice or its probability was unusable.")
        if label == policy.get("unknown_choice", "unknown"):
            return finish("REVIEW", IGNORE, "The model selected the unknown option.")
        if confidence is None:
            return finish("REVIEW", VERIFY, "No confidence was returned; probability alone is not a gate.")
        if floor is not None and confidence < floor:
            return finish("REVIEW", VERIFY,
                          f"Confidence {confidence} is below {floor}; the distribution looks decisive "
                          "but the answer is not calibrated.")
        if min_probability is not None and probability < min_probability:
            return finish("REVIEW", VERIFY, f"Selected probability {probability} is below {min_probability}.")
        return finish("RECOMMEND", ACT, "Cleared both the confidence floor and the probability bar.",
                      policy.get("positive_outcome"))

    if kind == "score":
        score = _number(answer.get("score"))
        min_score = _number(policy.get("min_score"))
        result["probability"] = score
        result["thresholds_applied"] = {"min_confidence": floor, "min_score": min_score}
        if score is None:
            return finish("REVIEW", VERIFY, "Score was unusable.")
        if confidence is None:
            return finish("REVIEW", VERIFY, "No confidence was returned; a score alone is not a gate.")
        if floor is not None and confidence < floor:
            return finish("REVIEW", VERIFY, f"Confidence {confidence} is below {floor}.")
        if min_score is not None and score < min_score:
            return finish("RECOMMEND", ACT, "Below the score bar.", policy.get("negative_outcome"))
        return finish("RECOMMEND", ACT, "At or above the score bar.", policy.get("positive_outcome"))

    return finish("REVIEW", VERIFY, f"Unknown policy kind {kind!r}.")
