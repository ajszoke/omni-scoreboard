"""Tests for the generic event model and the baseball event vertical."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from omni.core.enum import DisplayPriority, GameStatus, League, UpdateUrgency
from omni.core.ids import LeagueScopedId, SourceRef
from omni.domain.contest import Contest
from omni.events.base import EventImportance, GameEvent
from omni.events.baseball import (
    BaseballCount,
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
        half="bottom",
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
        payload=BaseballPlayPayload(inning=9, half="top", description="walk-off homer", rbi=1),
    )
    assert isinstance(event, GameEvent)
    assert event.league is League.MLB
    assert event.event_type is BaseballGameEventType.HOME_RUN
    assert event.payload.rbi == 1
    assert event.competitors == ()  # default
    assert event.importance.combined_score() > 0
