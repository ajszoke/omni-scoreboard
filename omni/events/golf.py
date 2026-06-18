"""Golf event taxonomy. Payloads/typed event follow with the PGA provider."""

from __future__ import annotations

from enum import Enum

from omni.core.enum import StrEnumMixin

__all__ = ["GolfEventType"]


class GolfEventType(StrEnumMixin, str, Enum):
    TOURNAMENT_SCHEDULED = "tournament_scheduled"
    ROUND_STARTED = "round_started"
    LEADERBOARD_UPDATE = "leaderboard_update"
    FAVORITE_MOVED = "favorite_moved"
    CUT_LINE_UPDATE = "cut_line_update"
    LEAD_CHANGE = "lead_change"
    PLAYOFF = "playoff"
    TOURNAMENT_FINAL = "tournament_final"
