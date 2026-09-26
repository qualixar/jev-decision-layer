"""Per-provider gate thresholds, because confidence does not mean the same
thing in two different models.

A single threshold set was applied to every provider from 1.0.0 onward. That
held only while no provider had ever been measured. It does not hold now.

MEASURED 2026-09-26, laya-mlx 0.2.0, checkpoint `aac6fef/laya-mlx`
(ModernBERT-large, 421M), 14 choice decisions driven through this package's
own twenty shipped decision contracts on Apple Silicon:

    confidence   min 0.0085   median 0.2861   max 0.7872
    shortfall    min +0.1078  median +0.2832  max +0.4214
      (shortfall = selected probability - the model's own confidence)

Laya's confidence runs structurally far below its own distribution. That is
not a fault; it is a different calibration. But it means a threshold chosen
for a hosted model rejects almost everything Laya says: of ten answers, a
0.70 floor cleared one, where a 0.55 floor cleared five -- and nine of the ten
were correct or an honest abstention.

The reverse error is just as real. A model whose confidence tracks its
distribution closely needs the high floor, because for it a low confidence is
genuinely rare and therefore genuinely informative.

So the floor belongs to the provider, not to the recipe alone.

WHAT THIS IS NOT. Fourteen samples on one checkpoint is a measurement, not a
calibration. `provenance` says so on every profile and `calibration_status`
carries it into the result. No profile here entitles anyone to describe this
layer as calibrated.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

MEASURED_SMALL_SAMPLE = "MEASURED_SMALL_SAMPLE_NOT_CALIBRATED"
UNVALIDATED = "UNVALIDATED_DEMONSTRATION_DEFAULT"


@dataclass(frozen=True)
class ProviderProfile:
    """How far a given provider's numbers may be trusted.

    `min_confidence` of None means the recipe's own floor stands unchanged --
    the profile declines to override rather than guessing a number.
    """

    provider_id: str
    min_confidence: float | None
    max_confidence_shortfall: float
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
    max_confidence_shortfall=0.20,
    calibration_status=UNVALIDATED,
    sample_size=0,
    note="No provider measurement exists. The recipe's own floor stands.",
)

PROFILES: dict[str, ProviderProfile] = {
    "laya-mlx": ProviderProfile(
        provider_id="laya-mlx",
        # Every measured answer that cleared the probability bar reported at
        # least 0.5631. The floor sits below that with margin rather than on
        # the observed boundary, because fourteen samples cannot locate a
        # boundary.
        min_confidence=0.50,
        # Laya's own median shortfall is 0.2832 and its maximum 0.4214, so a
        # 0.20 bar would refuse twelve of fourteen answers, most of them
        # correct. Set above the observed range: for this provider the
        # shortfall check is close to inert, and pretending otherwise would be
        # dishonest. The confidence floor is what gates Laya.
        max_confidence_shortfall=0.50,
        calibration_status=MEASURED_SMALL_SAMPLE,
        sample_size=14,
        note=("laya-mlx 0.2.0 / aac6fef/laya-mlx. Confidence runs structurally "
              "below the distribution; the checkpoint also ships a choice:11+ "
              "temperature of 0.1006 that laya-mlx clamps at load and reports "
              "as uncalibrated."),
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
         "max_confidence_shortfall": item.max_confidence_shortfall,
         "calibration_status": item.calibration_status,
         "sample_size": item.sample_size, "note": item.note}
        for name, item in (("", DEFAULT), *sorted(PROFILES.items()))
    ]
