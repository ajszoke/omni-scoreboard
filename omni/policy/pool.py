"""The live pool: one typed entry per game the board could give a base turn (`BOARD_POLICY.md` §1, §3.2).

The board builds each entry from released state only, never a raw receipt: the contest, its leverage, the
favorite rank, whether it's in action or a confirmed break, whether it's muted, fresh and drawable, the
teams' records and the game's stakes. The allocation (`omni.policy.allocation`) turns the pool into shares.

What the caller leaves out of the pool: a suspended or delayed game is a status card, not a weighted game;
a final is a result. What the allocation sets aside itself: a muted game, a game without a drawable card,
and a favorite in a confirmed break while there's anything else to show.
"""

from __future__ import annotations

from dataclasses import dataclass

from omni.core.enum import League, SeasonStage
from omni.core.ids import LeagueScopedId
from omni.domain.baseball import WinLossRecord
from omni.domain.contest import Contest, TeamGame
from omni.policy.leverage import Leverage
from omni.preferences.favorites import Favorites

__all__ = ["MatchupRecords", "PoolEntry", "Stakes", "StartingRecords", "TeamRecord"]


@dataclass(frozen=True, slots=True, kw_only=True)
class TeamRecord:
    """A team's season record from complete, permitted results. Ties count for football."""

    wins: int
    losses: int
    ties: int = 0

    def __post_init__(self) -> None:
        if min(self.wins, self.losses, self.ties) < 0:
            raise ValueError("a record can't be negative")

    @classmethod
    def of(cls, record: WinLossRecord) -> TeamRecord:
        """A win-loss record, which has no ties."""
        return cls(wins=record.wins, losses=record.losses)

    @property
    def games(self) -> int:
        return self.wins + self.losses + self.ties


@dataclass(frozen=True, slots=True, kw_only=True)
class MatchupRecords:
    """Both teams' records, as they stood when the game started."""

    away: TeamRecord
    home: TeamRecord


class StartingRecords:
    """Each game's records as they stood at its start, so a result during the game never moves them.

    Before a game starts the latest records stand; from its start the first records seen are kept until the
    game is forgotten.
    """

    def __init__(self) -> None:
        self._kept: dict[LeagueScopedId, MatchupRecords] = {}

    def read(self, contest: LeagueScopedId, records: MatchupRecords | None, *, started: bool) -> MatchupRecords | None:
        """The records to weigh ``contest`` by, given the latest released ``records``."""
        kept = self._kept.get(contest)
        if kept is not None:
            return kept
        if started and records is not None:
            self._kept[contest] = records
        return records

    def forget(self, contest: LeagueScopedId) -> None:
        """Drop a game that's over."""
        self._kept.pop(contest, None)


@dataclass(frozen=True, slots=True, kw_only=True)
class Stakes:
    """What a game is worth beyond its own score, only from verified facts."""

    postseason: bool = False  # a confirmed postseason game
    cup_knockout: bool = False  # an NBA Cup knockout game, which `SeasonStage` can't say yet
    season_decider: bool = False  # a verified clinch or elimination game; nothing verifies one yet
    national_tv: bool = False  # verified US national TV

    @classmethod
    def of(cls, contest: Contest, *, cup_knockout: bool = False) -> Stakes:
        """A contest's stakes from its schedule: the postseason from its stage, national TV only when the
        source says so."""
        return cls(
            postseason=contest.stage is SeasonStage.POSTSEASON,
            cup_knockout=cup_knockout,
            national_tv=contest.national_broadcast is True,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class PoolEntry:
    """One game the board could give a base turn, read from one released generation.

    ``favorite_rank`` is the better of its teams' ranks among the board's favorites (None if neither is
    one). A stale entry gets no share (the allocation sets it aside), nor any favorite's boost; a muted one
    never counts, whoever plays in it.
    """

    contest: LeagueScopedId
    leverage: Leverage
    favorite_rank: int | None = None
    in_break: bool = False  # a confirmed break (`omni.domain.breaks`), never one guessed from a quiet feed
    muted: bool = False  # the league or either team is muted
    fresh: bool = True  # its released state is current
    drawable: bool = True  # the panel has a card to draw for it
    records: MatchupRecords | None = None  # as they stood at the game's start; None when unknown
    stakes: Stakes = Stakes()

    def __post_init__(self) -> None:
        if self.favorite_rank is not None and self.favorite_rank < 1:
            raise ValueError("a favorite's rank counts from 1")

    @property
    def league(self) -> League:
        return self.contest.league

    @classmethod
    def of(
        cls,
        game: TeamGame,
        *,
        leverage: Leverage,
        favorites: Favorites,
        in_break: bool = False,
        muted: bool = False,
        fresh: bool = True,
        drawable: bool = True,
        records: MatchupRecords | None = None,
        cup_knockout: bool = False,
    ) -> PoolEntry:
        """A team game's entry, ranked by the board's favorites in its own league and staked by its schedule."""
        return cls(
            contest=game.id,
            leverage=leverage,
            favorite_rank=favorites.of(game),
            in_break=in_break,
            muted=muted,
            fresh=fresh,
            drawable=drawable,
            records=records,
            stakes=Stakes.of(game, cup_knockout=cup_knockout),
        )

    @property
    def acting_favorite(self) -> bool:
        """A favorite in action: fresh, and not in a confirmed break. Only these earn the rank boost and the
        floor."""
        return self.favorite_rank is not None and self.fresh and not self.in_break
