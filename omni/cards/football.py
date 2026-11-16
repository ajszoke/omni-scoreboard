"""Football card payloads; the teams come from the card's contest."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from omni.cards.base import ScoreboardCard
from omni.domain.baseball import WinProbability
from omni.domain.football import FootballGameState, PlaySummary

__all__ = ["LiveFootballCard", "LiveFootballCardPayload"]


@dataclass(frozen=True, slots=True, kw_only=True)
class LiveFootballCardPayload:
    """What a live football card shows: the game's state, the last play, and the win meter."""

    state: FootballGameState
    last_play: PlaySummary | None = None  # None hides the play strip
    win_probability: WinProbability | None = None  # drives the per-team meter; None hides it
    break_ends_at: datetime | None = None  # when a break is expected to end; halftime counts down to it


LiveFootballCard = ScoreboardCard[LiveFootballCardPayload]
