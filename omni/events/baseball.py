"""Baseball event taxonomy, play payload, and typed event."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from omni.core.enum import StrEnumMixin
from omni.events.base import GameEvent

__all__ = [
    "BaseballGameEventType",
    "HalfInning",
    "BaseballCount",
    "BaseballPlayPayload",
    "BaseballGameEvent",
]


class BaseballGameEventType(StrEnumMixin, str, Enum):
    GAME_SCHEDULED = "game_scheduled"
    GAME_STARTED = "game_started"
    PITCH = "pitch"
    BALL = "ball"
    CALLED_STRIKE = "called_strike"
    SWINGING_STRIKE = "swinging_strike"
    FOUL = "foul"
    BALL_IN_PLAY = "ball_in_play"
    STRIKEOUT_LOOKING = "strikeout_looking"
    STRIKEOUT_SWINGING = "strikeout_swinging"
    WALK = "walk"
    HIT_BY_PITCH = "hit_by_pitch"
    SINGLE = "single"
    DOUBLE = "double"
    TRIPLE = "triple"
    HOME_RUN = "home_run"
    SAC_FLY = "sac_fly"
    SAC_BUNT = "sac_bunt"
    DOUBLE_PLAY = "double_play"
    TRIPLE_PLAY = "triple_play"
    RUN_SCORED = "run_scored"
    RBI = "rbi"
    PITCHING_CHANGE = "pitching_change"
    CHALLENGE = "challenge"
    ABS_CHALLENGE = "abs_challenge"
    INNING_START = "inning_start"
    HALF_INNING_END = "half_inning_end"
    GAME_FINAL = "game_final"
    NO_HITTER_ACTIVE = "no_hitter_active"
    NO_HITTER_BROKEN = "no_hitter_broken"
    PERFECT_GAME_ACTIVE = "perfect_game_active"
    PERFECT_GAME_BROKEN = "perfect_game_broken"


class HalfInning(StrEnumMixin, str, Enum):
    """Top or bottom of an inning (replaces a stringly-typed ``half``)."""

    TOP = "top"
    BOTTOM = "bottom"


@dataclass(frozen=True, slots=True, kw_only=True)
class BaseballCount:
    """Balls/strikes/outs at the moment of a play."""

    balls: int
    strikes: int
    outs: int

    def __post_init__(self) -> None:
        if self.balls < 0 or self.strikes < 0 or self.outs < 0:
            raise ValueError("balls, strikes, and outs must be non-negative")
        # Terminal maxima: 4th ball (walk), 3rd strike (K), 3rd out (inning end).
        if self.balls > 4 or self.strikes > 3 or self.outs > 3:
            raise ValueError("balls/strikes/outs exceed their terminal maximums (4/3/3)")


@dataclass(frozen=True, slots=True, kw_only=True)
class BaseballPlayPayload:
    """The baseball-specific body of a `BaseballGameEvent`.

    `fielder_sequence` is structured as ints (e.g. ``(9, 6, 4)``), not a string
    like ``"9-6-4-"``; rendering joins it late and avoids dangling delimiters.
    """

    inning: int
    half: HalfInning
    description: str
    count: BaseballCount | None = None
    rbi: int = 0
    pitch_type: str | None = None
    fielder_sequence: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class BaseballGameEvent(GameEvent[BaseballGameEventType, BaseballPlayPayload]):
    """A `GameEvent` specialized to baseball event types and play payloads."""
