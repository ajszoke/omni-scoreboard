"""A synthetic NBA game for the feed deriver's tests: each player's line as it stands, read out as the feed reads it.

A test changes lines (`Game.bump`), then reads them (`Game.lines`) or the slate (`Game.slate`), each read arriving a
few seconds after the last, and gets the deriver's update back. The players' rows keep the order they joined in,
as the feed's do. A slate's score is the lines' own unless a test gives one.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from types import EllipsisType
from typing import TypeVar

from omni.core.clock import MonotonicTime
from omni.core.enum import HomeAway
from omni.domain.basketball import BasketballGameState, BasketballPhase, BasketballSide, GameClock, PlayerLine
from omni.domain.basketball_reads import LinesRead, PlayerKey, RosterLine, SlateRead
from omni.events.basketball import BasketballCaption
from omni.events.basketball_feed import BasketballFeedDeriver, FeedUpdate

A, H = HomeAway.AWAY, HomeAway.HOME
T = TypeVar("T")
LIVE = BasketballPhase.LIVE

# A test's short names for the counts it changes.
_COUNT = {
    "fg": "fg_made",
    "three": "threes_made",
    "ftm": "ft_made",
    "fta": "ft_attempted",
    "pf": "fouls",
    "ast": "assists",
    "tov": "turnovers",
    "blk": "blocks",
    "stl": "steals",
    "dreb": "defensive_rebounds",
}
_ATTEMPTS = {"fg_made": "fg_attempted", "threes_made": "threes_attempted"}


def clock(minutes: int, seconds: int = 0) -> int:
    """A game clock reading, in tenths."""
    return (minutes * 60 + seconds) * 10


@dataclass
class _Player:
    side: HomeAway
    name: str
    counts: Counter[str] = field(default_factory=Counter)

    def line(self) -> PlayerLine:
        c = self.counts
        return PlayerLine(
            name=self.name,
            points=2 * c["fg_made"] + c["threes_made"] + c["ft_made"],
            fg_made=c["fg_made"],
            fg_attempted=c["fg_attempted"],
            threes_made=c["threes_made"],
            threes_attempted=c["threes_attempted"],
            ft_made=c["ft_made"],
            ft_attempted=c["ft_attempted"],
            offensive_rebounds=c["offensive_rebounds"],
            defensive_rebounds=c["defensive_rebounds"],
            assists=c["assists"],
            steals=c["steals"],
            blocks=c["blocks"],
            turnovers=c["turnovers"],
            fouls=c["fouls"],
            plus_minus=0,
        )


class Game:
    """One synthetic game and the deriver reading it.

    ``players`` are (key, side, short name), in the feed's row order; they are on the board from the first read.
    """

    def __init__(self, *players: tuple[int, HomeAway, str], period: int = 1, at_clock: int | None = clock(6)) -> None:
        self.deriver = BasketballFeedDeriver()
        self.at = 1000.0
        self.period = period
        self.clock = at_clock
        self.timeouts: tuple[int | None, int | None] = (7, 7)
        self.players: dict[int, _Player] = {}
        for key, side, name in players:
            self.join(key, side, name)

    def join(self, key: int, side: HomeAway, name: str, **counts: int) -> None:
        """A player whose first line is in the next lines read."""
        self.players[key] = _Player(side, name)
        self.bump(key, **counts)

    def bump(self, key: int, **counts: int) -> None:
        """Change a player's counts; a make adds its attempt."""
        player = self.players[key]
        for short, delta in counts.items():
            name = _COUNT[short]
            player.counts[name] += delta
            if name in _ATTEMPTS and delta > 0:
                player.counts[_ATTEMPTS[name]] += delta

    def lines(
        self, *, at_clock: int | None | EllipsisType = ..., period: int | None = None, wait: float = 5.0
    ) -> FeedUpdate:
        """Read every player's line ``wait`` seconds after the last read, the clock moved to ``at_clock`` (None: no
        running clock; left out: where it stands)."""
        self._move(at_clock, period, wait)
        rosters = tuple(
            RosterLine(key=PlayerKey(key), side=player.side, line=player.line()) for key, player in self.players.items()
        )
        clock_now = GameClock(self.clock) if self.clock is not None else None
        read = LinesRead(at=MonotonicTime(self.at), period=self.period, clock=clock_now, lines=rosters)
        return self.deriver.read(read)

    def slate(
        self,
        *,
        at_clock: int | None | EllipsisType = ...,
        period: int | None = None,
        timeouts: tuple[int | None, int | None] | None = None,
        phase: BasketballPhase = LIVE,
        score: tuple[int, int] | None = None,
        wait: float = 5.0,
    ) -> FeedUpdate:
        """Read the slate ``wait`` seconds after the last read, with each side's timeouts left as given (or as they
        stood)."""
        self._move(at_clock, period, wait)
        if timeouts is not None:
            self.timeouts = timeouts
        points = score if score is not None else self.points()
        away, home = (BasketballSide(score=points[i], timeouts=self.timeouts[i]) for i in (0, 1))
        state = BasketballGameState(
            phase=phase,
            period=self.period,
            away=away,
            home=home,
            clock=GameClock(self.clock) if phase is LIVE and self.clock is not None else None,
        )
        return self.deriver.read(SlateRead(at=MonotonicTime(self.at), state=state))

    def points(self) -> tuple[int, int]:
        lines = [(player.side, player.line().points) for player in self.players.values()]
        return sum(p for side, p in lines if side is A), sum(p for side, p in lines if side is H)

    def _move(self, at_clock: int | None | EllipsisType, period: int | None, wait: float) -> None:
        self.at += wait
        if period is not None:
            self.period = period
        if not isinstance(at_clock, EllipsisType):
            self.clock = at_clock


def told(update: FeedUpdate, *kinds: type) -> list[BasketballCaption]:
    """The captions an update told, in order; only those of ``kinds`` when any are given."""
    return [told.caption for told in update.captions if not kinds or isinstance(told.caption, kinds)]


def told_as(update: FeedUpdate, kind: type[T]) -> list[T]:
    """The captions of one kind an update told, in order."""
    return [told.caption for told in update.captions if isinstance(told.caption, kind)]
