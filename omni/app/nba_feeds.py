"""What NBA's cards read: each game's reads, as they arrived, from whatever source plans them.

The NBA pipeline consumes this; the planned NBA source (BALLDONTLIE behind the poll plan and the quota ledger)
provides it. A game is read two ways (`omni.domain.basketball_reads`): the slate's state of it, and every player's line
so far. Each read is taken once, in arrival order, so the feed deriver sees every change between two refreshes; the
newest stays readable for the card's freshness.

Beside the reads it hands on what a card shows besides the game itself: the slate's own row for the game (its tip,
its phase, its NBA Cup stage), the newest complete read of the season's finals, for the records and ranks, whether
the players' lines are read at all (only BALLDONTLIE's ALL-STAR tier has them), an upcoming game's favorite line, and a
game's win probability as it stood when a read arrived, each None when nothing is read for it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol

from omni.app.pipeline import MeterReading
from omni.core.clock import ClockReading
from omni.domain.basketball import FavoriteLine
from omni.domain.basketball_reads import LinesRead, SlateRead
from omni.domain.contest import TeamGame
from omni.providers.balldontlie import SlateGame
from omni.providers.nba_standings import GameResult

__all__ = ["NbaFeeds", "NbaReading", "SeasonResults"]


@dataclass(frozen=True, slots=True)
class NbaReading:
    """One read of one NBA game, and when it arrived.

    ``current_for`` is how long it counts as current from its arrival: two of the cadence its read is asked at.
    """

    read: SlateRead | LinesRead
    arrived: ClockReading
    current_for: timedelta


@dataclass(frozen=True, slots=True)
class SeasonResults:
    """A complete read of the season's finals that count toward the records, filed before the day it was read on,
    and when its last page arrived."""

    results: tuple[GameResult, ...]
    arrived: ClockReading


class NbaFeeds(Protocol):
    """Each NBA game's reads, and what its cards show beside them, for the pipeline."""

    @property
    def lines(self) -> bool:
        """Whether the players' lines are read: the captions, the box scores and the players' panels need them."""
        ...  # pragma: no cover

    def take(self, game: TeamGame) -> list[NbaReading]:
        """The reads of ``game`` that arrived since the last take, in arrival order; each is taken once."""
        ...  # pragma: no cover

    def latest(self, game: TeamGame) -> NbaReading | None:
        """The newest read of ``game``, taken or not; None before the first."""
        ...  # pragma: no cover

    def slate_game(self, game: TeamGame) -> SlateGame | None:
        """The slate's newest row for ``game``; None before the slate has read it."""
        ...  # pragma: no cover

    def season(self) -> SeasonResults | None:
        """The newest complete read of the season's finals; None before the first."""
        ...  # pragma: no cover

    def line(self, game: TeamGame) -> FavoriteLine | None:
        """An upcoming game's favorite line, as last heard; None without one."""
        ...  # pragma: no cover

    def win_probability(self, game: TeamGame, as_of: ClockReading) -> MeterReading | None:
        """``game``'s win probability as it stood at ``as_of``: the newest reading that arrived by then; None without
        one."""
        ...  # pragma: no cover
