"""Tests for omni.cards.baseball payloads: base state, HalfInning, validation."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from omni.cards.baseball import BaseballBaseState, LiveBaseballCardPayload, PregameCardPayload
from omni.events.baseball import BaseballCount, HalfInning

_COUNT = BaseballCount(balls=2, strikes=1, outs=2)


def test_base_state_defaults_to_empty() -> None:
    bases = BaseballBaseState()
    assert (bases.first, bases.second, bases.third) == (False, False, False)


def test_live_payload_uses_half_inning_enum() -> None:
    payload = LiveBaseballCardPayload(
        away_score=1, home_score=0, inning=3, half=HalfInning.BOTTOM, count=_COUNT, bases=BaseballBaseState()
    )
    assert payload.half is HalfInning.BOTTOM


def test_live_payload_rejects_negative_scores() -> None:
    with pytest.raises(ValueError):
        LiveBaseballCardPayload(
            away_score=-1, home_score=0, inning=1, half=HalfInning.TOP, count=_COUNT, bases=BaseballBaseState()
        )


def test_live_payload_rejects_inning_below_one() -> None:
    with pytest.raises(ValueError):
        LiveBaseballCardPayload(
            away_score=0, home_score=0, inning=0, half=HalfInning.TOP, count=_COUNT, bases=BaseballBaseState()
        )


def test_pregame_payload_holds_scheduled_start() -> None:
    start = datetime(2026, 6, 17, 23, 30, tzinfo=timezone.utc)
    assert PregameCardPayload(scheduled_start=start).scheduled_start == start


def test_pregame_payload_rejects_naive_start() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        PregameCardPayload(scheduled_start=datetime(2026, 6, 17, 23, 30))
