"""Contests: the general team-or-individual competition model.

`Contest` is the base so the system never assumes team-vs-team. `TeamGame` and
`GolfTournament` specialize it; more sports follow the same pattern.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from omni.core.enum import GameStatus, League, SeasonStage, Sport
from omni.core.ids import LeagueScopedId
from omni.domain.base import Competitor
from omni.domain.teams import Team

__all__ = ["Contest", "TeamGame", "GolfTournament"]


_DOUBLEHEADER_GAMES = (1, 2)


@dataclass(frozen=True, slots=True, kw_only=True)
class Contest:
    """Any scheduled competition, team-based or individual.

    Past its identity and start, it carries the schedule's facts about it. Each defaults to "not known", so a
    source that can't say leaves it there. Identity is the `id` alone: a doubleheader's two games share their
    clubs and date and are still two contests, told apart by `game_number`.
    """

    id: LeagueScopedId
    league: League
    status: GameStatus
    scheduled_start: datetime
    competitors: tuple[Competitor, ...] = ()
    venue_name: str | None = None
    stage: SeasonStage = SeasonStage.UNKNOWN
    game_number: int | None = None  # 1 or 2 in a doubleheader; None when the game isn't one
    start_tbd: bool = False  # `scheduled_start` is a placeholder until the start time is set
    provider_date: date | None = None  # the date the source files the game under, apart from any viewer's own date
    neutral_site: bool | None = None  # None when the source doesn't say
    national_broadcast: bool | None = None  # carried by a US national TV network; None when the source doesn't say
    resumed_from: date | None = None  # for a suspended game resumed on a later day, the day it began

    def __post_init__(self) -> None:
        # `league` is also carried by `id`; they must never disagree.
        if self.league != self.id.league:
            raise ValueError(f"contest league {self.league} disagrees with id.league {self.id.league}")
        if self.game_number is not None and self.game_number not in _DOUBLEHEADER_GAMES:
            raise ValueError("a doubleheader's games are numbered 1 and 2")

    @property
    def sport(self) -> Sport:
        return self.league.sport


@dataclass(frozen=True, slots=True, kw_only=True)
class TeamGame(Contest):
    """A team-vs-team contest."""

    away: Team
    home: Team

    def __post_init__(self) -> None:
        # Explicit parent call (not `super()`): `@dataclass(slots=True)` rebuilds
        # the class, so a zero-arg `super()` would resolve to the pre-slots class.
        Contest.__post_init__(self)  # league/id consistency
        if self.away == self.home:
            raise ValueError("home and away teams must differ")
        # Always derive the general `competitors` view from home/away so the two
        # can never contradict each other; any caller-supplied tuple is ignored.
        object.__setattr__(self, "competitors", (self.away, self.home))


@dataclass(frozen=True, slots=True, kw_only=True)
class GolfTournament(Contest):
    """An individual-field contest; competitors are the golfers in the field."""

    tournament_name: str
    round_number: int | None = None
    cut_line: int | None = None
