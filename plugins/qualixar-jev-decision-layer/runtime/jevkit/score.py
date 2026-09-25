"""Shared interval check for provider-rounded Jev Score answers."""

from __future__ import annotations


def score_matches_rounded_probabilities(score: float, probabilities: dict[str, float]) -> bool:
    """Accept only scores explainable by two-decimal reported probabilities.

    Each underlying level probability lies within 0.005 of its displayed
    value and the underlying distribution sums to one. The reported Score
    itself may be rounded by 0.005.
    """
    radius = 0.005
    levels = sorted(int(level) for level in probabilities)
    lower = {level: max(0.0, probabilities[str(level)] - radius) for level in levels}
    upper = {level: min(1.0, probabilities[str(level)] + radius) for level in levels}
    base = sum(lower.values())
    if base > 1 + 1e-9 or sum(upper.values()) < 1 - 1e-9:
        return False

    def extreme(order):
        values = dict(lower)
        remaining = max(0.0, 1 - base)
        for level in order:
            addition = min(remaining, upper[level] - values[level])
            values[level] += addition
            remaining -= addition
        if remaining > 1e-8:
            return None
        return sum(level * values[level] for level in levels)

    minimum = extreme(levels)
    maximum = extreme(reversed(levels))
    return (minimum is not None and maximum is not None
            and minimum - radius - 1e-9 <= score <= maximum + radius + 1e-9)
