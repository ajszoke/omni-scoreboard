"""Football's live game state: the period and clock, who has the ball, the down, and where the ball sits.

Field position arrives the way the feeds count it, as the yards left to the opponent's goal line. That
is what the red zone and goal-to-go read from directly. Which side of midfield the ball is on depends
on who has it, so the board-facing `ball_on` is derived rather than stored.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from omni.core.enum import HomeAway, StrEnumMixin
from omni.domain.coverage import CoverageMap

__all__ = [
    "CHALLENGES_PER_GAME",
    "FieldPosition",
    "FootballField",
    "FootballGameState",
    "FootballInterval",
    "PlaySummary",
    "PlayTone",
    "SetPiece",
    "TeamChallenges",
    "TeamTimeouts",
    "other_side",
]

_REGULATION_PERIODS = 4
_RED_ZONE = 20  # yards from the goal line where the field is said to tighten
_MIDFIELD = 50
_TIMEOUTS_PER_HALF = 3
CHALLENGES_PER_GAME = 2  # a third is earned only by winning both, by which point at most one is left


class FootballInterval(StrEnumMixin, str, Enum):
    """A pause between periods, or the end of the game: no live down to show."""

    END_OF_QUARTER = "end_of_quarter"
    HALFTIME = "halftime"
    FINAL = "final"


class SetPiece(StrEnumMixin, str, Enum):
    """A restart with no down, which a board names in the down's place."""

    KICKOFF = "kickoff"


class PlayTone(StrEnumMixin, str, Enum):
    """How a play reads at a glance; a board colors its summary by this."""

    STANDARD = "standard"
    SCORE = "score"  # a field goal, an extra point, a two-point try
    ALERT = "alert"  # a penalty, a review, a challenge
    TURNOVER = "turnover"  # an interception, a lost fumble, a block, a turnover on downs


@dataclass(frozen=True, slots=True, kw_only=True)
class PlaySummary:
    """The last play, already put into a board's words: a short headline and an optional detail line."""

    tone: PlayTone
    headline: str
    detail: str | None = None

    def __post_init__(self) -> None:
        if not self.headline:
            raise ValueError("a play summary needs a headline")


@dataclass(frozen=True, slots=True, kw_only=True)
class TeamTimeouts:
    """Timeouts each side has left in the half."""

    away: int
    home: int

    def __post_init__(self) -> None:
        for left in (self.away, self.home):
            if not 0 <= left <= _TIMEOUTS_PER_HALF:
                raise ValueError(f"timeouts left must be 0..{_TIMEOUTS_PER_HALF}")


@dataclass(frozen=True, slots=True, kw_only=True)
class TeamChallenges:
    """Coach's challenges each side has left in the game."""

    away: int
    home: int

    def __post_init__(self) -> None:
        for left in (self.away, self.home):
            if not 0 <= left <= CHALLENGES_PER_GAME:
                raise ValueError(f"challenges left must be 0..{CHALLENGES_PER_GAME}")


@dataclass(frozen=True, slots=True, kw_only=True)
class FieldPosition:
    """Where the ball sits as a board names it: a side's yard line, or midfield (no side, the 50)."""

    side: HomeAway | None
    yard_line: int

    def __post_init__(self) -> None:
        if not 1 <= self.yard_line <= _MIDFIELD:
            raise ValueError("a yard line is 1..50")
        if (self.side is None) != (self.yard_line == _MIDFIELD):
            raise ValueError("only midfield has no side")


def other_side(side: HomeAway) -> HomeAway:
    """The opponent's side."""
    return HomeAway.HOME if side is HomeAway.AWAY else HomeAway.AWAY


class FootballField(StrEnumMixin, str, Enum):
    """A part of a `FootballGameState` whose coverage is kept; a two-sided one covers both sides."""

    SCORE = "score"
    PERIOD = "period"
    CLOCK = "clock"
    INTERVAL = "interval"  # whether play is on, or which break the game is in
    POSSESSION = "possession"
    DOWN_DISTANCE = "down_distance"
    SPOT = "spot"  # where the ball sits: its yards to the goal
    TIMEOUTS = "timeouts"
    CHALLENGES = "challenges"


@dataclass(frozen=True, slots=True, kw_only=True)
class FootballGameState:
    """One moment of a live football game.

    ``possession``, ``down``, ``distance`` and ``yards_to_goal`` describe the next snap and are all None
    when there isn't one to describe: a try after a score, an interval, or a feed that hasn't said. A
    kickoff is a ``set_piece``: the kicking side has the ball, ``yards_to_goal`` counted from where it
    kicks (65 from its own 35). Overtime periods count on past 4.
    """

    period: int
    clock_seconds: int
    away_score: int
    home_score: int
    possession: HomeAway | None = None
    down: int | None = None
    distance: int | None = None
    yards_to_goal: int | None = None
    timeouts: TeamTimeouts | None = None
    challenges: TeamChallenges | None = None
    interval: FootballInterval | None = None
    set_piece: SetPiece | None = None
    # What each field above rests on: whether a None is a gap in the feed or means there's nothing to show, and
    # whether a value came from the scoreboard or was worked out from the plays.
    coverage: CoverageMap[FootballField] = CoverageMap()

    def __post_init__(self) -> None:
        if self.period < 1:
            raise ValueError("period must be >= 1")
        if self.clock_seconds < 0:
            raise ValueError("the clock can't run below zero")
        if self.away_score < 0 or self.home_score < 0:
            raise ValueError("scores can't be negative")
        if self.down is not None and not 1 <= self.down <= 4:
            raise ValueError("a down is 1..4")
        if self.distance is not None and self.distance < 0:
            raise ValueError("distance can't be negative")
        if self.yards_to_goal is not None and not 1 <= self.yards_to_goal <= 99:
            raise ValueError("yards to goal is 1..99")

    def known(self, field: FootballField) -> bool:
        """Whether ``field`` holds a value to reason from: reported, or worked out from plays that vouch for it."""
        return self.coverage[field].known

    @property
    def is_overtime(self) -> bool:
        return self.period > _REGULATION_PERIODS

    @property
    def goal_to_go(self) -> bool:
        """Whether the line to gain is the goal line itself."""
        return (
            self.down is not None
            and self.distance is not None
            and self.yards_to_goal is not None
            and self.distance >= self.yards_to_goal
        )

    @property
    def in_red_zone(self) -> bool:
        return (
            self.set_piece is None
            and self.possession is not None
            and self.yards_to_goal is not None
            and self.yards_to_goal <= _RED_ZONE
        )

    @property
    def ball_on(self) -> FieldPosition | None:
        """The yard line the ball sits on, named from the side of the field it's on."""
        if self.possession is None or self.yards_to_goal is None:
            return None
        if self.yards_to_goal == _MIDFIELD:
            return FieldPosition(side=None, yard_line=_MIDFIELD)
        if self.yards_to_goal > _MIDFIELD:  # still in its own half
            return FieldPosition(side=self.possession, yard_line=100 - self.yards_to_goal)
        return FieldPosition(side=other_side(self.possession), yard_line=self.yards_to_goal)
