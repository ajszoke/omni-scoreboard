"""Baseball card payloads."""

from __future__ import annotations

from dataclasses import dataclass

from omni.cards.base import ScoreboardCard
from omni.events.baseball import BaseballCount

__all__ = ["BaseballBaseState", "LiveBaseballCardPayload", "LiveBaseballCard"]


@dataclass(frozen=True, slots=True, kw_only=True)
class BaseballBaseState:
    """Base occupancy for rendering the diamond (a player model comes later)."""

    first: bool = False
    second: bool = False
    third: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class LiveBaseballCardPayload:
    """The live state a baseball card needs to render; teams come from the contest."""

    away_score: int
    home_score: int
    inning: int
    half: str  # "top" / "bottom"; later a HalfInning enum
    count: BaseballCount
    bases: BaseballBaseState
    last_play: str | None = None


# A live baseball card is a ScoreboardCard carrying the live payload above.
LiveBaseballCard = ScoreboardCard[LiveBaseballCardPayload]
