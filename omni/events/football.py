"""Football event taxonomy. Payloads/typed event follow with the NFL provider."""

from __future__ import annotations

from enum import Enum

from omni.core.enum import StrEnumMixin

__all__ = ["FootballGameEventType"]


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
