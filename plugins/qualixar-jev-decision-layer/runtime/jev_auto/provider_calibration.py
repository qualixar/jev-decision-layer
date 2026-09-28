"""Provider-specific policy overrides, disabled until reproducible evaluation.

The shipped recipes contain demonstration thresholds, not calibrated decision
boundaries. A former Laya override was justified by fourteen claimed samples,
but the repository contains only ten unlabeled probability/confidence pairs.
It cannot establish correctness or a safe provider-specific threshold. Both
known and unknown providers therefore retain the recipe's own conservative
floor. A future override requires labeled, reproducible provider results and
held-out evaluation; merely counting how often the gate clears is insufficient.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

UNVALIDATED = "UNVALIDATED_DEMONSTRATION_DEFAULT"


@dataclass(frozen=True)
class ProviderProfile:
    """How far a given provider's numbers may be trusted.

    `min_confidence` of None means the recipe's own floor stands unchanged --
    the profile declines to override rather than guessing a number.
    """

    provider_id: str
    min_confidence: float | None
    calibration_status: str
    sample_size: int
    note: str

    def floor(self, recipe_floor: float | None) -> float | None:
        return recipe_floor if self.min_confidence is None else self.min_confidence


# The default applies to the hosted Jev route and to any provider not named
# here. Its floor is whatever the recipe declares; nothing has been measured
# for it, and inventing a number would be worse than leaving the recipe's.
DEFAULT = ProviderProfile(
    provider_id="",
    min_confidence=None,
    calibration_status=UNVALIDATED,
    sample_size=0,
    note="No provider measurement exists. The recipe's own floor stands.",
)

PROFILES: dict[str, ProviderProfile] = {
    "laya-mlx": ProviderProfile(
        provider_id="laya-mlx",
        min_confidence=None,
        calibration_status=UNVALIDATED,
        sample_size=0,
        note="No reproducible labeled evaluation is shipped; recipe floor stands.",
    ),
}


def profile_for(provider: Any) -> ProviderProfile:
    """The profile for a provider id, falling back to the default.

    Anything that is not a known provider id -- None, a number, an unknown
    name -- gets the default. An unrecognised provider is not a reason to
    loosen a gate.
    """
    if not isinstance(provider, str):
        return DEFAULT
    return PROFILES.get(provider.strip().lower(), DEFAULT)


def describe() -> list[dict[str, Any]]:
    """Every profile, for `jev_auto_status` and the docs to report honestly."""
    return [
        {"provider": name or "(default)", "min_confidence": item.min_confidence,
         "calibration_status": item.calibration_status,
         "sample_size": item.sample_size, "note": item.note}
        for name, item in (("", DEFAULT), *sorted(PROFILES.items()))
    ]
