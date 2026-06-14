"""Omni typed enum mixins and initial enums.

Cribbed in spirit from the user's Alpine enum.py:

- StrEnumMixin: string identifier enums where the string is canonical.
- IntEnumMixin: ordered severity/priority enums backed by IntEnum.
- try_coerce_enum: tolerant coercion for config/fixture/replay readers.

Strict construction paths should still fail fast. Use try_coerce_enum only at
boundaries where malformed historical input should not crash inspection tools.
"""

from __future__ import annotations

from enum import Enum, IntEnum
from typing import Any, TypeVar

E = TypeVar("E", bound=Enum)


def try_coerce_enum(enum_cls: type[E], raw: Any) -> E | None:
    """Best-effort coerce raw input into an enum member; return None on failure."""
    if isinstance(raw, enum_cls):
        return raw
    if isinstance(raw, bool):
        return None
    try:
        return enum_cls(raw)
    except (ValueError, KeyError, TypeError):
        pass
    if isinstance(raw, str):
        try:
            return enum_cls[raw.upper()]
        except KeyError:
            return None
    return None


class EnumMixin:
    """Base for Omni typed enum mixins."""

    def to_json_value(self) -> str | int:
        return self.value  # type: ignore[attr-defined]


class StrEnumMixin(EnumMixin):
    """Mixin for str-backed enums where str(member) returns the raw value."""

    def __str__(self) -> str:
        return self.value  # type: ignore[attr-defined]


class IntEnumMixin(EnumMixin):
    """Mixin for ordered IntEnum-backed severities/priorities."""

    def __str__(self) -> str:
        return self.name.lower()  # type: ignore[attr-defined]

    def to_json_value(self) -> str:
        return self.name.lower()  # type: ignore[attr-defined]


class Sport(StrEnumMixin, str, Enum):
    BASEBALL = "baseball"
    FOOTBALL = "football"
    BASKETBALL = "basketball"
    HOCKEY = "hockey"
    GOLF = "golf"


class League(StrEnumMixin, str, Enum):
    MLB = "mlb"
    NFL = "nfl"
    NBA = "nba"
    NHL = "nhl"
    PGA = "pga"
    NCAAF = "ncaaf"
    NCAAB = "ncaab"

    @property
    def sport(self) -> Sport:
        return {
            League.MLB: Sport.BASEBALL,
            League.NFL: Sport.FOOTBALL,
            League.NBA: Sport.BASKETBALL,
            League.NHL: Sport.HOCKEY,
            League.PGA: Sport.GOLF,
            League.NCAAF: Sport.FOOTBALL,
            League.NCAAB: Sport.BASKETBALL,
        }[self]


class PanelProfile(StrEnumMixin, str, Enum):
    SINGLE_64X32 = "single_64x32"
    STACK_64X64 = "stack_64x64"
    QUAD_128X64 = "quad_128x64"


class GameStatus(StrEnumMixin, str, Enum):
    SCHEDULED = "scheduled"
    PREGAME = "pregame"
    LIVE = "live"
    DELAYED = "delayed"
    SUSPENDED = "suspended"
    FINAL = "final"
    POSTPONED = "postponed"
    CANCELED = "canceled"
    UNKNOWN = "unknown"


class CardKind(StrEnumMixin, str, Enum):
    LIVE_GAME = "live_game"
    BIG_PLAY = "big_play"
    PREGAME = "pregame"
    FINAL = "final"
    TICKER = "ticker"
    ALERT = "alert"
    LEADERBOARD = "leaderboard"
    SETUP = "setup"
    OFFDAY = "offday"


class DisplayPriority(IntEnumMixin, IntEnum):
    BACKGROUND = 0
    NORMAL = 10
    FAVORITE = 20
    HIGH_LEVERAGE = 30
    ALERT = 40
    STICKY = 50


class UpdateUrgency(IntEnumMixin, IntEnum):
    LOW = 0
    NORMAL = 1
    HIGH = 2
    LIVE_CRITICAL = 3


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


class FootballGameEventType(StrEnumMixin, str, Enum):
    GAME_SCHEDULED = "game_scheduled"
    GAME_STARTED = "game_started"
    DRIVE_START = "drive_start"
    RED_ZONE = "red_zone"
    SCORE = "score"
    TOUCHDOWN = "touchdown"
    FIELD_GOAL = "field_goal"
    TURNOVER = "turnover"
    TWO_MINUTE_WARNING = "two_minute_warning"
    GAME_FINAL = "game_final"


class BasketballGameEventType(StrEnumMixin, str, Enum):
    GAME_SCHEDULED = "game_scheduled"
    GAME_STARTED = "game_started"
    SCORE_CHANGE = "score_change"
    LEAD_CHANGE = "lead_change"
    CLUTCH_TIME = "clutch_time"
    TIMEOUT = "timeout"
    GAME_FINAL = "game_final"


class HockeyGameEventType(StrEnumMixin, str, Enum):
    GAME_SCHEDULED = "game_scheduled"
    GAME_STARTED = "game_started"
    GOAL = "goal"
    POWER_PLAY = "power_play"
    PENALTY = "penalty"
    EMPTY_NET = "empty_net"
    OVERTIME = "overtime"
    SHOOTOUT = "shootout"
    GAME_FINAL = "game_final"


class GolfEventType(StrEnumMixin, str, Enum):
    TOURNAMENT_SCHEDULED = "tournament_scheduled"
    ROUND_STARTED = "round_started"
    LEADERBOARD_UPDATE = "leaderboard_update"
    FAVORITE_MOVED = "favorite_moved"
    CUT_LINE_UPDATE = "cut_line_update"
    LEAD_CHANGE = "lead_change"
    PLAYOFF = "playoff"
    TOURNAMENT_FINAL = "tournament_final"
