"""Hockey event taxonomy. Payloads/typed event follow with the NHL provider."""

from __future__ import annotations

from enum import Enum

from omni.core.enum import StrEnumMixin

__all__ = ["HockeyGameEventType"]


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
