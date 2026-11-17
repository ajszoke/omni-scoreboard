"""A game's leverage, as the policy reads it: a weight between 0 and 1, and how far it can be trusted.

Leverage is the chance for the next outcome to change who wins. Until a league's model passes validation,
the policy reads a labeled proxy from the score, the time left and the progress, which is never called a
leverage index or a win probability. Each reading says which it is, so a decision can refuse what a proxy
mustn't grant: a proxy never qualifies an interrupt during a favorite's action.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from omni.core.enum import StrEnumMixin

__all__ = ["Leverage", "ModelStatus"]


class ModelStatus(StrEnumMixin, str, Enum):
    """How far a leverage reading can be trusted."""

    VALIDATED = "validated"  # a registered model that passed its gates, on complete inputs
    REDUCED = "reduced"  # that model on partial inputs
    PROXY = "proxy"  # the labeled proxy: score, time and progress only
    UNKNOWN = "unknown"  # not enough to say: no score, time or progress


@dataclass(frozen=True, slots=True, kw_only=True)
class Leverage:
    """A game's leverage weight ``g`` (0..1), the instantaneous ``z`` behind it (0..1) when a model gives
    one, its status, and the model it came from (None for the proxy and unknown)."""

    g: float
    status: ModelStatus
    z: float | None = None
    model_id: str | None = None

    def __post_init__(self) -> None:
        for name, value in (("g", self.g), ("z", self.z)):
            if value is not None and not 0.0 <= value <= 1.0:
                raise ValueError(f"leverage {name} must be within 0..1, not {value}")
        if (self.model_id is None) != (self.status in (ModelStatus.PROXY, ModelStatus.UNKNOWN)):
            raise ValueError("a validated or reduced reading names its model; a proxy or unknown one doesn't")

    @property
    def qualifies_action(self) -> bool:
        """Whether it may qualify an interrupt during a favorite's action: only a validated model's."""
        return self.status is ModelStatus.VALIDATED
