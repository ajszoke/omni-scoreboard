"""The event-type enum taxonomy is sport-specific and JSON round-trips."""

from __future__ import annotations

from enum import Enum

import pytest

from omni.core.enum import StrEnumMixin, try_coerce_enum
from omni.events.baseball import BaseballGameEventType
from omni.events.basketball import BasketballGameEventType
from omni.events.football import FootballGameEventType
from omni.events.golf import GolfEventType
from omni.events.hockey import HockeyGameEventType

SPORT_EVENT_ENUMS = [
    BaseballGameEventType,
    FootballGameEventType,
    BasketballGameEventType,
    HockeyGameEventType,
    GolfEventType,
]


@pytest.mark.parametrize("enum_cls", SPORT_EVENT_ENUMS)
def test_event_enum_is_str_serializable_and_round_trips(enum_cls: type[Enum]) -> None:
    members = list(enum_cls)
    assert members  # non-empty taxonomy
    for member in members:
        assert isinstance(member, StrEnumMixin)
        assert try_coerce_enum(enum_cls, member.to_json_value()) is member


def test_event_taxonomies_are_sport_specific() -> None:
    # No giant universal enum: a baseball type is not a football type, even when
    # the underlying string value coincides.
    assert BaseballGameEventType.HOME_RUN not in set(FootballGameEventType)
    assert BaseballGameEventType.GAME_FINAL is not FootballGameEventType.GAME_FINAL
    assert BaseballGameEventType.GAME_FINAL.value == FootballGameEventType.GAME_FINAL.value == "game_final"
