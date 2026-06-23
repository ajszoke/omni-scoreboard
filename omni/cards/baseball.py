"""Baseball card payloads."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from omni.cards.base import ScoreboardCard
from omni.core.enum import HomeAway

# Domain value objects used by the payload fields below (imported for use, not re-exported).
from omni.domain.baseball import BaseballBaseState, BaseballCount, InningPhase

__all__ = [
    "LiveBaseballCardPayload",
    "LiveBaseballCard",
    "PregameCardPayload",
    "PregameCard",
    "FinalCardPayload",
    "FinalCard",
]


@dataclass(frozen=True, slots=True, kw_only=True)
class LiveBaseballCardPayload:
    """The live state a baseball card needs to render; teams come from the contest."""

    away_score: int
    home_score: int
    inning: int
    phase: InningPhase
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


@dataclass(frozen=True, slots=True, kw_only=True)
class PregameCardPayload:
    """The pregame "situation" a baseball card renders before first pitch: when the
    game starts. Teams come from the contest. Held as a self-contained snapshot (a
    copy of the scheduled start) so the renderer derives the live countdown from the
    render clock, not from a mutable contest. Probable pitchers / team records will
    join here when the provider surfaces them.
    """

    scheduled_start: datetime

    def __post_init__(self) -> None:
        if self.scheduled_start.tzinfo is None:
            raise ValueError("scheduled_start must be timezone-aware")


# A pregame baseball card is a ScoreboardCard carrying the pregame payload above.
PregameCard = ScoreboardCard[PregameCardPayload]


@dataclass(frozen=True, slots=True, kw_only=True)
class FinalCardPayload:
    """A completed game's line: final score, with the winner *derived*, not stored.

    Teams come from the contest; `winner` is computed from the scores so it can never
    contradict them (an equal score — e.g. a weather-shortened tie — yields None).
    The compressed W/L/S pitching line joins here once the provider parses the game's
    decisions.
    """

    away_score: int
    home_score: int

    def __post_init__(self) -> None:
        if self.away_score < 0 or self.home_score < 0:
            raise ValueError("scores cannot be negative")

    @property
    def winner(self) -> HomeAway | None:
        """The winning side derived from the score (None on a tie)."""
        if self.away_score > self.home_score:
            return HomeAway.AWAY
        if self.home_score > self.away_score:
            return HomeAway.HOME
        return None


# A final baseball card is a ScoreboardCard carrying the final payload above.
FinalCard = ScoreboardCard[FinalCardPayload]
