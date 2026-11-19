"""Football card payloads; the teams come from the card's contest.

A caption names the play-feed entry it is (`StagedCaption`), with the bounds it airs within on the monotonic clock, so
whatever draws a card can tell one entry from another, and when one may first go up. A final can name the entry that
decided it (`LiveFootballCardPayload.decided_by`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from omni.cards.base import ScoreboardCard
from omni.core.clock import MonotonicTime
from omni.domain.baseball import WinProbability
from omni.domain.football import FootballGameState, PlaySummary

__all__ = ["LiveFootballCard", "LiveFootballCardPayload", "PlayRef", "StagedCaption"]


@dataclass(frozen=True, slots=True)
class PlayRef:
    """Which play-feed entry: its key, stable across reads; the play it's about (a ruling and a try share their
    play's); and how many rulings that play had had by it."""

    entry: str
    moment: str
    revision: int = 0


@dataclass(frozen=True, slots=True, kw_only=True)
class StagedCaption:
    """Which entry a card's caption is, and when it may air, on the monotonic clock: it never first goes up at or
    after ``latest_start``, and it's gone by ``hard_end``, up or not."""

    play: PlayRef
    latest_start: MonotonicTime
    hard_end: MonotonicTime


@dataclass(frozen=True, slots=True, kw_only=True)
class LiveFootballCardPayload:
    """What a live football card shows: the game's state, the last play, and the win meter."""

    state: FootballGameState
    last_play: PlaySummary | None = None  # None hides the play strip
    win_probability: WinProbability | None = None  # drives the per-team meter; None hides it
    break_ends_at: datetime | None = None  # when a break is expected to end; halftime counts down to it
    caption: StagedCaption | None = None  # which entry ``last_play`` is, and its bounds
    decided_by: PlayRef | None = None  # a final: the entry that decided it, once known


LiveFootballCard = ScoreboardCard[LiveFootballCardPayload]
