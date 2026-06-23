"""Tests for the generic event model and the baseball event vertical."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from omni.core.enum import DisplayPriority, GameStatus, League, UpdateUrgency
from omni.core.ids import LeagueScopedId, SourceRef
from omni.domain.baseball import BaseballCount, InningPhase
from omni.domain.contest import Contest
from omni.events.base import EventImportance, GameEvent
from omni.events.baseball import (
    BaseballGameEvent,
    BaseballGameEventType,
    BaseballPlayPayload,
)

T = datetime(2026, 6, 17, 19, 5, tzinfo=timezone.utc)
SOURCE = SourceRef("mlb_statsapi")
CONTEST = Contest(
    id=LeagueScopedId(League.MLB, SOURCE, "g1"),
    league=League.MLB,
    status=GameStatus.LIVE,
    scheduled_start=T,
)


def make_importance(
    *,
    priority: DisplayPriority = DisplayPriority.HIGH_LEVERAGE,
    urgency: UpdateUrgency = UpdateUrgency.HIGH,
    leverage: float = 0.8,
    rarity: float = 0.5,
    favorite_relevance: float = 0.3,
) -> EventImportance:
    return EventImportance(
        priority=priority,
        urgency=urgency,
        leverage=leverage,
        rarity=rarity,
        favorite_relevance=favorite_relevance,
    )


def test_importance_combined_score_weights_components() -> None:
    # 30 + 2*5 + 0.8*20 + 0.5*15 + 0.3*20 = 69.5
    assert make_importance().combined_score() == pytest.approx(69.5)


def test_importance_rejects_unnormalized_components() -> None:
    with pytest.raises(ValueError):
        make_importance(leverage=1.5)
    with pytest.raises(ValueError):
        make_importance(rarity=-0.1)
    with pytest.raises(ValueError):
        make_importance(favorite_relevance=2.0)


def test_baseball_count_rejects_negative() -> None:
    assert BaseballCount(balls=3, strikes=2, outs=1).strikes == 2
    with pytest.raises(ValueError):
        BaseballCount(balls=-1, strikes=0, outs=0)


def test_play_payload_keeps_fielder_sequence_structured() -> None:
    payload = BaseballPlayPayload(
        inning=7,
        phase=InningPhase.BOTTOM,
        description="6-4-3 double play",
        count=BaseballCount(balls=1, strikes=2, outs=1),
        fielder_sequence=(6, 4, 3),
    )
    assert payload.fielder_sequence == (6, 4, 3)
    assert isinstance(payload.fielder_sequence, tuple)
    assert payload.rbi == 0  # default


def test_baseball_game_event_is_typed_and_derives_league() -> None:
    event = BaseballGameEvent(
        id=LeagueScopedId(League.MLB, SOURCE, "e1"),
        contest=CONTEST,
        event_type=BaseballGameEventType.HOME_RUN,
        source=SOURCE,
        source_time=T,
        observed_at=T,
        importance=make_importance(),
        payload=BaseballPlayPayload(inning=9, phase=InningPhase.TOP, description="walk-off homer", rbi=1),
    )
    assert isinstance(event, GameEvent)
    assert event.league is League.MLB
    assert event.event_type is BaseballGameEventType.HOME_RUN
    assert event.payload.rbi == 1
    assert event.competitors == ()  # default
    assert event.importance.combined_score() > 0


def test_importance_accepts_inclusive_0_and_1_boundaries() -> None:
    imp = make_importance(leverage=0.0, rarity=1.0, favorite_relevance=0.0)
    assert imp.leverage == 0.0 and imp.rarity == 1.0


def test_importance_combined_score_is_monotonic_in_each_component() -> None:
    base = make_importance(leverage=0.4, rarity=0.4, favorite_relevance=0.4).combined_score()
    assert make_importance(leverage=0.6, rarity=0.4, favorite_relevance=0.4).combined_score() > base
    assert make_importance(leverage=0.4, rarity=0.6, favorite_relevance=0.4).combined_score() > base
    assert make_importance(leverage=0.4, rarity=0.4, favorite_relevance=0.6).combined_score() > base


def test_baseball_count_rejects_impossible_values() -> None:
    BaseballCount(balls=4, strikes=3, outs=3)  # terminal maxima are allowed
    with pytest.raises(ValueError):
        BaseballCount(balls=5, strikes=0, outs=0)
    with pytest.raises(ValueError):
        BaseballCount(balls=0, strikes=4, outs=0)
    with pytest.raises(ValueError):
        BaseballCount(balls=0, strikes=0, outs=4)


def test_half_inning_enum_serializes() -> None:
    assert InningPhase.TOP.to_json_value() == "top"
    assert InningPhase("bottom") is InningPhase.BOTTOM
