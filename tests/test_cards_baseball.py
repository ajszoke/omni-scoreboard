"""Tests for omni.cards.baseball payloads: base state, InningPhase, validation."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from omni.cards.baseball import (
    BigPlayCardPayload,
    FinalCardPayload,
    LiveBaseballCardPayload,
    NoHitterCardPayload,
    PregameCardPayload,
)
from omni.core.enum import HomeAway
from omni.domain.baseball import BaseballBaseState, BaseballCount, InningPhase
from omni.events.baseball import BaseballGameEventType

_COUNT = BaseballCount(balls=2, strikes=1, outs=2)


def test_base_state_defaults_to_empty() -> None:
    bases = BaseballBaseState()
    assert (bases.first, bases.second, bases.third) == (False, False, False)


def test_live_payload_uses_inning_phase_enum() -> None:
    payload = LiveBaseballCardPayload(
        away_score=1, home_score=0, inning=3, phase=InningPhase.BOTTOM, count=_COUNT, bases=BaseballBaseState()
    )
    assert payload.phase is InningPhase.BOTTOM


def test_live_payload_rejects_negative_scores() -> None:
    with pytest.raises(ValueError):
        LiveBaseballCardPayload(
            away_score=-1, home_score=0, inning=1, phase=InningPhase.TOP, count=_COUNT, bases=BaseballBaseState()
        )


def test_live_payload_rejects_inning_below_one() -> None:
    with pytest.raises(ValueError):
        LiveBaseballCardPayload(
            away_score=0, home_score=0, inning=0, phase=InningPhase.TOP, count=_COUNT, bases=BaseballBaseState()
        )


def test_pregame_payload_holds_scheduled_start() -> None:
    start = datetime(2026, 6, 17, 23, 30, tzinfo=timezone.utc)
    assert PregameCardPayload(scheduled_start=start).scheduled_start == start


def test_pregame_payload_rejects_naive_start() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        PregameCardPayload(scheduled_start=datetime(2026, 6, 17, 23, 30))


def test_final_payload_derives_winner_from_score() -> None:
    assert FinalCardPayload(away_score=5, home_score=3).winner is HomeAway.AWAY
    assert FinalCardPayload(away_score=2, home_score=7).winner is HomeAway.HOME
    assert FinalCardPayload(away_score=4, home_score=4).winner is None  # a tie has no winner


def test_final_payload_rejects_negative_scores() -> None:
    with pytest.raises(ValueError):
        FinalCardPayload(away_score=-1, home_score=0)


def test_big_play_payload_rejects_negative_scores() -> None:
    with pytest.raises(ValueError):
        BigPlayCardPayload(event_type=BaseballGameEventType.HOME_RUN, description="x", away_score=-1, home_score=0)


def test_no_hitter_payload_holds_side_and_depth() -> None:
    payload = NoHitterCardPayload(pitching_side=HomeAway.HOME, through_inning=7, perfect=True)
    assert payload.pitching_side is HomeAway.HOME and payload.through_inning == 7 and payload.perfect is True


def test_no_hitter_payload_defaults_to_not_perfect() -> None:
    assert NoHitterCardPayload(pitching_side=HomeAway.AWAY, through_inning=6).perfect is False


def test_no_hitter_payload_rejects_inning_below_one() -> None:
    with pytest.raises(ValueError):
        NoHitterCardPayload(pitching_side=HomeAway.HOME, through_inning=0)
