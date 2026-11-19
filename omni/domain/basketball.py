"""Basketball's game state, and the numbers a board shows beside it.

The state is one read of a game under way or done: the phase (live, at a period's end, at the half, final), the
period, the sampled game clock, and each side's score by period, timeouts left and bonus. A game not yet under way
has no state; its card carries the tip and the pregame numbers instead, and its phase (scheduled or postponed).

Beside the state sit the numbers the cards draw: a club's record and standing ranks, the favorite's betting line,
each player's box-score line (as the stat rows carry it), a team's box summed from its players, and a scoring run.

A timeout's length class lives here too. The first and second timeouts of a regulation quarter are the long TV
timeouts; later ones, and every overtime timeout, are short. Only break planning reads it: the data can't tell a
team's own timeout from a mandatory one charged to it, so no caption says which.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Generic, TypeVar

from omni.core.enum import HomeAway, StrEnumMixin

__all__ = [
    "FOUL_LIMIT",
    "REGULATION_PERIODS",
    "TIMEOUTS_PER_GAME",
    "BasketballGameState",
    "BasketballPhase",
    "BasketballSide",
    "BySide",
    "CallChange",
    "FavoriteLine",
    "GameClock",
    "PlayerCallChange",
    "PlayerLine",
    "ScoringRun",
    "StandingRanks",
    "TeamBox",
    "TimeoutLength",
    "UNSTARTED",
    "best_player",
    "by_plus_minus",
    "period_label",
    "timeout_length",
]

REGULATION_PERIODS = 4
TIMEOUTS_PER_GAME = 7
FOUL_LIMIT = 6  # the sixth personal foul puts a player out of the game
_PERIOD_TENTHS = 12 * 60 * 10  # a regulation quarter's clock, in tenths of a second
_LONG_TIMEOUTS_PER_QUARTER = 2
_CONFERENCE_TEAMS = 15
_DIVISION_TEAMS = 5
_MIN_PRICE = 100  # American odds never sit between -100 and +100

T = TypeVar("T")


class BasketballPhase(StrEnumMixin, str, Enum):
    """Where a game stands, as far as a card is concerned."""

    SCHEDULED = "scheduled"
    POSTPONED = "postponed"  # moved off its date; it may or may not have a new tip yet
    LIVE = "live"
    END_OF_PERIOD = "end_of_period"
    HALFTIME = "halftime"
    FINAL = "final"


UNSTARTED = frozenset({BasketballPhase.SCHEDULED, BasketballPhase.POSTPONED})


@dataclass(frozen=True, slots=True)
class BySide(Generic[T]):
    """One value for each side of a game, read by `HomeAway`."""

    away: T
    home: T

    def __getitem__(self, side: HomeAway) -> T:
        return self.away if side is HomeAway.AWAY else self.home


def period_label(period: int) -> str:
    """A period as a board names it: ``Q1`` to ``Q4``, then ``OT``, ``2OT``, ``3OT``."""
    if period < 1:
        raise ValueError("periods count from 1")
    if period <= REGULATION_PERIODS:
        return f"Q{period}"
    overtime = period - REGULATION_PERIODS
    return "OT" if overtime == 1 else f"{overtime}OT"


@dataclass(frozen=True, slots=True)
class GameClock:
    """The sampled game clock: what's left in the period, in tenths of a second.

    The feed reports tenths only under a minute (``:48.2``); above it, ``7:18``, and the tenths are zero.
    """

    tenths: int

    def __post_init__(self) -> None:
        if not 0 <= self.tenths <= _PERIOD_TENTHS:
            raise ValueError("a period's clock runs from 12:00 down to zero")

    @classmethod
    def of(cls, minutes: int, seconds: int, tenths: int = 0) -> GameClock:
        """The clock reading ``minutes:seconds``, plus ``tenths`` under a minute."""
        if not 0 <= seconds < 60 or not 0 <= tenths < 10 or minutes < 0:
            raise ValueError("a clock reads minutes, seconds 0-59 and tenths 0-9")
        return cls(tenths=(minutes * 60 + seconds) * 10 + tenths)

    @property
    def text(self) -> str:
        """``10:13`` at a minute or more; ``:48.2`` under one."""
        seconds, tenth = divmod(self.tenths, 10)
        if seconds < 60:
            return f":{seconds:02d}.{tenth}"
        return f"{seconds // 60}:{seconds % 60:02d}"


@dataclass(frozen=True, slots=True, kw_only=True)
class BasketballSide:
    """One side's part of a read: its score, its points by period (Q1 first, then each overtime), its timeouts
    left (None when the feed hasn't said), and whether it's in the bonus."""

    score: int
    periods: tuple[int, ...] = ()
    timeouts: int | None = None
    in_bonus: bool = False

    def __post_init__(self) -> None:
        if self.score < 0 or any(points < 0 for points in self.periods):
            raise ValueError("points can't be negative")
        if self.timeouts is not None and not 0 <= self.timeouts <= TIMEOUTS_PER_GAME:
            raise ValueError(f"timeouts left are 0..{TIMEOUTS_PER_GAME}")


@dataclass(frozen=True, slots=True, kw_only=True)
class BasketballGameState:
    """One read of a game under way or over.

    ``period`` counts on past 4 into overtime. ``clock`` is the game clock while play is on (None if the feed
    hasn't said), and nothing at a break. ``timeout`` is the side whose timeout play is stopped for, while it
    lasts. A final can't be tied.
    """

    phase: BasketballPhase
    period: int
    away: BasketballSide
    home: BasketballSide
    clock: GameClock | None = None
    timeout: HomeAway | None = None

    def __post_init__(self) -> None:
        if self.phase in UNSTARTED:
            raise ValueError("a game not under way has no state; its card is the pregame")
        if self.period < 1:
            raise ValueError("period must be >= 1")
        if self.phase is BasketballPhase.HALFTIME and self.period != 2:
            raise ValueError("halftime follows the second period")
        if self.phase is BasketballPhase.FINAL:
            if self.period < REGULATION_PERIODS:
                raise ValueError("a final has played at least four periods")
            if self.away.score == self.home.score:
                raise ValueError("a basketball game can't end tied")
        if self.phase is not BasketballPhase.LIVE and (self.clock is not None or self.timeout is not None):
            raise ValueError("only a game in play has a running clock or a timeout on")
        for side in (self.away, self.home):
            if len(side.periods) > self.period:
                raise ValueError("points by period can't run past the period under way")

    def side(self, which: HomeAway) -> BasketballSide:
        return self.away if which is HomeAway.AWAY else self.home

    @property
    def is_overtime(self) -> bool:
        return self.period > REGULATION_PERIODS

    @property
    def winner(self) -> HomeAway | None:
        """The side a final went to; None until the game is final."""
        if self.phase is not BasketballPhase.FINAL:
            return None
        return HomeAway.AWAY if self.away.score > self.home.score else HomeAway.HOME


@dataclass(frozen=True, slots=True, kw_only=True)
class StandingRanks:
    """Where a club ranks in its conference and in its division."""

    conference: int
    division: int

    def __post_init__(self) -> None:
        if not 1 <= self.conference <= _CONFERENCE_TEAMS:
            raise ValueError(f"a conference rank is 1..{_CONFERENCE_TEAMS}")
        if not 1 <= self.division <= _DIVISION_TEAMS:
            raise ValueError(f"a division rank is 1..{_DIVISION_TEAMS}")


@dataclass(frozen=True, slots=True, kw_only=True)
class FavoriteLine:
    """The favorite's pregame line: its moneyline in American odds, and the points it gives (``-6.5``)."""

    favorite: HomeAway
    moneyline: int
    spread: float

    def __post_init__(self) -> None:
        if abs(self.moneyline) < _MIN_PRICE:
            raise ValueError("American odds are -100 or below, or +100 or above")
        if self.spread > 0:
            raise ValueError("the favorite gives points: its spread is zero or below")


@dataclass(frozen=True, slots=True, kw_only=True)
class PlayerLine:
    """One player's box-score line, as the stat rows carry it; ``name`` is the short form a board shows."""

    name: str
    points: int
    fg_made: int
    fg_attempted: int
    threes_made: int
    threes_attempted: int
    ft_made: int
    ft_attempted: int
    offensive_rebounds: int
    defensive_rebounds: int
    assists: int
    steals: int
    blocks: int
    turnovers: int
    fouls: int
    plus_minus: int

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("a player line needs a name")
        counts = (
            self.fg_made,
            self.fg_attempted,
            self.threes_made,
            self.threes_attempted,
            self.ft_made,
            self.ft_attempted,
            self.offensive_rebounds,
            self.defensive_rebounds,
            self.assists,
            self.steals,
            self.blocks,
            self.turnovers,
        )
        if any(count < 0 for count in counts):
            raise ValueError("a box-score count can't be negative")
        if not 0 <= self.fouls <= FOUL_LIMIT:
            raise ValueError(f"fouls are 0..{FOUL_LIMIT}")
        made_attempted = (
            (self.fg_made, self.fg_attempted),
            (self.threes_made, self.threes_attempted),
            (self.ft_made, self.ft_attempted),
        )
        if any(made > attempted for made, attempted in made_attempted):
            raise ValueError("makes can't outnumber attempts")
        if self.threes_made > self.fg_made or self.threes_attempted > self.fg_attempted:
            raise ValueError("threes are field goals: they can't outnumber them")
        if self.points != 2 * self.fg_made + self.threes_made + self.ft_made:
            raise ValueError("points must add up from the makes")

    @property
    def rebounds(self) -> int:
        return self.offensive_rebounds + self.defensive_rebounds


def by_plus_minus(lines: Iterable[PlayerLine]) -> tuple[PlayerLine, ...]:
    """Players best plus-minus first, the higher points breaking a tie (then the order given)."""
    return tuple(sorted(lines, key=lambda line: (-line.plus_minus, -line.points)))


def best_player(lines: Iterable[PlayerLine]) -> PlayerLine | None:
    """The player with the best plus-minus, the higher points on a tie; None with no lines."""
    order = by_plus_minus(lines)
    return order[0] if order else None


@dataclass(frozen=True, slots=True, kw_only=True)
class TeamBox:
    """A team's box-score line: field-goal percentage, threes made and tried, rebounds, assists, turnovers."""

    fg_pct: int
    threes_made: int
    threes_attempted: int
    rebounds: int
    assists: int
    turnovers: int

    def __post_init__(self) -> None:
        if not 0 <= self.fg_pct <= 100:
            raise ValueError("a percentage is 0..100")
        if min(self.threes_made, self.rebounds, self.assists, self.turnovers) < 0:
            raise ValueError("a box-score count can't be negative")
        if self.threes_made > self.threes_attempted:
            raise ValueError("makes can't outnumber attempts")

    @classmethod
    def of(cls, lines: Sequence[PlayerLine]) -> TeamBox:
        """The team's line, summed from its players'; a team that hasn't tried a shot shoots 0%."""
        attempted = sum(line.fg_attempted for line in lines)
        made = sum(line.fg_made for line in lines)
        return cls(
            fg_pct=round(100 * made / attempted) if attempted else 0,
            threes_made=sum(line.threes_made for line in lines),
            threes_attempted=sum(line.threes_attempted for line in lines),
            rebounds=sum(line.rebounds for line in lines),
            assists=sum(line.assists for line in lines),
            turnovers=sum(line.turnovers for line in lines),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ScoringRun:
    """A run: the side on it, the points it scored and allowed, and the game-clock seconds it has spanned so far."""

    team: HomeAway
    scored: int
    allowed: int
    span_seconds: int

    def __post_init__(self) -> None:
        if self.allowed < 0 or self.scored <= self.allowed:
            raise ValueError("a run outscores the other side")
        if self.span_seconds < 0:
            raise ValueError("a run's span can't be negative")


class TimeoutLength(StrEnumMixin, str, Enum):
    """How long a timeout's break should run, for break planning only.

    Play resumes about 3.5 minutes after the clock stopped for a long one, 1.5 to 2 minutes for a short one.
    """

    LONG = "long"
    SHORT = "short"


def timeout_length(period: int, nth_in_period: int) -> TimeoutLength:
    """The length class of the period's ``nth_in_period`` timeout (1 for the first).

    The first and second of a regulation quarter are long; the third and later, and every overtime timeout, short.
    """
    if period < 1 or nth_in_period < 1:
        raise ValueError("periods and timeouts count from 1")
    if period <= REGULATION_PERIODS and nth_in_period <= _LONG_TIMEOUTS_PER_QUARTER:
        return TimeoutLength.LONG
    return TimeoutLength.SHORT


class CallChange(StrEnumMixin, str, Enum):
    """How a call taken back shows in one player's line."""

    FOUL_OFF = "foul_off"  # one foul fewer
    FOUL_TO_BLOCK = "foul_to_block"  # a block more, a foul fewer
    OFFENSIVE_FOUL_OFF = "offensive_foul_off"  # a turnover fewer, a foul fewer


@dataclass(frozen=True, slots=True, kw_only=True)
class PlayerCallChange:
    """A call taken back, and the player whose line shows it."""

    change: CallChange
    player: str

    def __post_init__(self) -> None:
        if not self.player:
            raise ValueError("a call change names its player")
