"""Baseball card payloads."""

from __future__ import annotations

from dataclasses import dataclass

from omni.cards.base import ScoreboardCard

# Baseball value objects live in the domain layer; re-exported for back-compat.
from omni.domain.baseball import BaseballBaseState, BaseballCount, HalfInning

__all__ = ["BaseballBaseState", "LiveBaseballCardPayload", "LiveBaseballCard"]


@dataclass(frozen=True, slots=True, kw_only=True)
class LiveBaseballCardPayload:
    """The live state a baseball card needs to render; teams come from the contest."""

    away_score: int
    home_score: int
    inning: int
    half: HalfInning
    count: BaseballCount
    bases: BaseballBaseState
    last_play: str | None = None

    def __post_init__(self) -> None:
        if self.away_score < 0 or self.home_score < 0:
            raise ValueError("scores cannot be negative")
        if self.inning < 1:
            raise ValueError("inning must be >= 1")


# A live baseball card is a ScoreboardCard carrying the live payload above.
LiveBaseballCard = ScoreboardCard[LiveBaseballCardPayload]
