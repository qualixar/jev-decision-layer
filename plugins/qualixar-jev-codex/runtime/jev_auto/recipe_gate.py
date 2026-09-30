"""Fail-closed local policy checks for typed recipe answers.

The returned action describes whether a demonstration gate cleared. It is not
model accuracy evidence or permission to execute a recommendation. The live
recipe path further caps every unevaluated recipe to review.

TypeSafe defines Choice and Score ``confidence`` as a statistic derived from
the reported probability distribution, not an independent belief or a
calibration measurement: https://docs.typesafe.ai/confidence . The configured
confidence floor and selected/outcome probability threshold are policy
statistics. A Noul answer has no confidence field; its yes/no bands are used.

Malformed thresholds or answers cannot clear the gate. In particular, a
Choice or Score distribution must contain approximately one unit of mass.
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
    "score": ("min_confidence", "min_score", "min_selected_probability"),
    "noul": ("yes", "no"),
}
# Accept limited rounding while refusing omitted alternatives. This bound is
# a structural check, not a guarantee that provider probabilities are accurate.
_MASS_TOLERANCE = 0.01
# Outcomes that hold work back rather than let it through. Only these may be
# declared `safe_outcomes`: the offline adversarial proof lets a gate act on a
# safe outcome, because a model that resisted an injection answers confidently
# and its honest answer is to review, quarantine or leave the item alone.
SAFE_ACTIONS = frozenset({"abstain", "request_review", "quarantine_candidate"})


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
        if "safe_outcomes" in policy and not _safe_outcomes_valid(policy):
            raise AutoError("RECIPE_GATES_INVALID")
    return gates


def _safe_outcomes_valid(policy: dict[str, Any]) -> bool:
    safe = policy["safe_outcomes"]
    outcomes = {policy["positive_outcome"], policy["negative_outcome"]}
    return (isinstance(safe, list) and all(isinstance(item, str) for item in safe)
            and len(set(safe)) == len(safe) and set(safe) <= outcomes & SAFE_ACTIONS)


def _probability_of(answer: dict[str, Any], label: Any) -> float | None:
    """The selected label's share, if the distribution is coherent.

    Returns None — meaning "do not clear the gate" — when the distribution is
    malformed, when a value is outside [0, 1], when total mass differs from
    one beyond rounding tolerance, or when the selected label is not the most
    probable option. A model
    that picks a label it scored below another has contradicted itself, and a
    self-contradictory answer is not one to act on.
    """
    probabilities = answer.get("probabilities")
    if not isinstance(probabilities, dict) or not 2 <= len(probabilities) <= 128:
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
    if abs(sum(values) - 1.0) > _MASS_TOLERANCE + 1e-9:
        return None
    chosen = _unit(selected)
    if chosen is None or chosen < max(values):
        return None
    return chosen


def _score_outcome_probability(answer: dict[str, Any], min_score: float, positive: bool) -> float | None:
    """Mass supporting the score outcome selected by ``min_score``.

    Score probabilities are keyed by their ordinal level ("0", "1", ...).
    A positive score outcome means mass on levels >= min_score; the negative
    outcome means mass on levels < min_score. Requiring a contiguous level map
    and near-unit total catches missing, malformed, and unreadable distributions
    before either outcome can clear the gate. Values are provider-reported,
    not recalibrated here; the provider protocol validates the score against
    its distribution before this local policy check.
    """
    probabilities = answer.get("probabilities")
    if not isinstance(probabilities, dict) or not probabilities or len(probabilities) > 128:
        return None

    levels: dict[int, float] = {}
    for key, value in probabilities.items():
        # Provider JSON uses canonical decimal level labels. Reject aliases
        # such as "01" so duplicate semantic levels cannot distort the mass.
        if not isinstance(key, str) or not key.isdigit() or str(int(key)) != key:
            return None
        level = int(key)
        probability = _unit(value)
        if probability is None:
            return None
        levels[level] = probability

    if set(levels) != set(range(len(levels))):
        return None

    total = sum(levels.values())
    # Jev reports two-decimal level probabilities; at most 0.005 rounding
    # error per level can move their sum away from one.
    if abs(total - 1.0) > len(levels) * 0.005 + 1e-9:
        return None

    return sum(probability for level, probability in levels.items()
               if (level >= min_score) == positive)


def evaluate(policy: Any, answer: Any, provider: Any = None) -> dict[str, Any]:
    """Apply one recipe's policy to one typed answer. Never raises on content.

    `provider` selects a named profile for disclosure. All profiles currently
    use the recipe's unvalidated thresholds; none is calibrated or allowed to
    loosen a threshold from unlabeled samples.
    """
    from .provider_calibration import profile_for

    calibration = profile_for(provider if provider is not None else
                              (answer.get("provider") if isinstance(answer, dict) else None))
    reasons: list[str] = []
    result: dict[str, Any] = {
        "status": "REVIEW",
        "host_action": VERIFY,
        "recommendation": None,
        # The label a choice answer selected. Every non-unknown label clears to
        # the same outcome, so without it `route_to_queue` names no queue.
        "selected_label": None,
        "confidence": None,
        "probability": None,
        "outcome_probability": None,
        "thresholds_applied": {},
        "thresholds_calibrated": False,
        "provider_profile": {"provider": calibration.provider_id or "(default)",
                             "calibration_status": calibration.calibration_status,
                             "sample_size": calibration.sample_size},
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
    recipe_floor = _unit(policy.get("min_confidence"))
    # A profile may raise or lower the recipe's floor for this provider, but a
    # malformed recipe floor stays malformed: _unit() has already refused it
    # and the gate must still fail closed on a broken threshold.
    floor = recipe_floor if recipe_floor is None else _unit(calibration.floor(recipe_floor))
    result["confidence"] = confidence

    if kind == "choice":
        label = answer.get("choice")
        if isinstance(label, str) and label:
            result["selected_label"] = label
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
            return finish("REVIEW", VERIFY, "No usable reported confidence was returned.")
        if confidence < floor:
            return finish("REVIEW", VERIFY,
                          f"Reported confidence {confidence} is below the configured floor {floor}.")
        if probability < min_probability:
            return finish("REVIEW", VERIFY, f"Selected probability {probability} is below {min_probability}.")
        return finish("RECOMMEND", ACT, "Cleared both the confidence floor and the probability bar.",
                      policy.get("positive_outcome"))

    score = _number(answer.get("score"))
    min_score = _number(policy.get("min_score"))
    min_probability = _unit(policy.get("min_selected_probability"))
    result["probability"] = score
    result["thresholds_applied"] = {"min_confidence": floor, "min_score": min_score,
                                    "min_selected_probability": min_probability}
    if floor is None or min_score is None or min_probability is None:
        return finish("REVIEW", VERIFY, "This recipe's thresholds are missing or unreadable; "
                                        "an ungated answer is not an authorised one.")
    if score is None:
        return finish("REVIEW", VERIFY, "Score was unusable.")
    if confidence is None:
        return finish("REVIEW", VERIFY, "No usable confidence was returned; a score alone is not a gate.")
    if confidence < floor:
        return finish("REVIEW", VERIFY, f"Confidence {confidence} is below {floor}.")
    positive = score >= min_score
    outcome_probability = _score_outcome_probability(answer, min_score, positive)
    result["outcome_probability"] = outcome_probability
    if outcome_probability is None:
        return finish("REVIEW", VERIFY, "The score probability distribution was missing, malformed, "
                                        "or did not sum to one within reporting precision.")
    if outcome_probability < min_probability:
        direction = "positive" if positive else "negative"
        return finish("REVIEW", VERIFY,
                      f"Probability mass for the {direction} score outcome {outcome_probability} "
                      f"is below {min_probability}.")
    if score < min_score:
        return finish("RECOMMEND", ACT, "Below the score bar and cleared its probability bar.",
                      policy.get("negative_outcome"))
    return finish("RECOMMEND", ACT, "At or above the score bar and cleared its probability bar.",
                  policy.get("positive_outcome"))
