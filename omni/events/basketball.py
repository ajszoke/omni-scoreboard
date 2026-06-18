"""Basketball event taxonomy. Payloads/typed event follow with the NBA provider."""

from __future__ import annotations

from enum import Enum

from omni.core.enum import StrEnumMixin

__all__ = ["BasketballGameEventType"]


class BasketballGameEventType(StrEnumMixin, str, Enum):
    GAME_SCHEDULED = "game_scheduled"
    GAME_STARTED = "game_started"
    SCORE_CHANGE = "score_change"
    LEAD_CHANGE = "lead_change"
    CLUTCH_TIME = "clutch_time"
    TIMEOUT = "timeout"
    GAME_FINAL = "game_final"
