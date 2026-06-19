"""Tests for the TV-delay holding buffer."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from omni.core.time import DurationSeconds
from omni.queue.delay_buffer import DelayBuffer

T0 = datetime(2026, 6, 17, 23, 0, 0, tzinfo=timezone.utc)


def _at(seconds: int) -> datetime:
    return T0 + timedelta(seconds=seconds)


def test_zero_delay_releases_on_next_call() -> None:
    buffer: DelayBuffer[str] = DelayBuffer(DurationSeconds(0))
    buffer.push("now", observed_at=T0)
    assert buffer.release(T0) == ["now"]
    assert buffer.pending() == 0


def test_item_is_held_until_the_delay_elapses() -> None:
    buffer: DelayBuffer[str] = DelayBuffer(DurationSeconds(10))
    release_at = buffer.push("run scored", observed_at=T0)
    assert release_at == _at(10)

    assert buffer.release(_at(9)) == []  # still spoiler territory
    assert buffer.pending() == 1
    assert buffer.release(_at(10)) == ["run scored"]  # boundary is inclusive
    assert buffer.pending() == 0


def test_released_items_are_not_yielded_twice() -> None:
    buffer: DelayBuffer[str] = DelayBuffer(DurationSeconds(5))
    buffer.push("x", observed_at=T0)
    assert buffer.release(_at(5)) == ["x"]
    assert buffer.release(_at(10)) == []


def test_releases_in_push_order_as_delays_elapse() -> None:
    buffer: DelayBuffer[str] = DelayBuffer(DurationSeconds(10))
    buffer.push("a", observed_at=_at(0))  # releases at 10
    buffer.push("b", observed_at=_at(3))  # releases at 13
    buffer.push("c", observed_at=_at(3))  # releases at 13

    assert buffer.release(_at(10)) == ["a"]
    assert buffer.pending() == 2
    assert buffer.release(_at(13)) == ["b", "c"]  # FIFO among equal release times


def test_next_release_at_reports_the_earliest_hold() -> None:
    buffer: DelayBuffer[int] = DelayBuffer(DurationSeconds(10))
    assert buffer.next_release_at() is None
    buffer.push(1, observed_at=_at(5))  # releases at 15
    buffer.push(2, observed_at=_at(0))  # releases at 10
    assert buffer.next_release_at() == _at(10)
    buffer.release(_at(10))
    assert buffer.next_release_at() == _at(15)


def test_delay_is_exposed() -> None:
    assert DelayBuffer(DurationSeconds(45)).delay == DurationSeconds(45)


def test_buffer_holds_a_real_card_until_release() -> None:
    from omni.cards.factory import CardFactory
    from omni.core.enum import GameStatus, League
    from omni.core.ids import LeagueScopedId, SourceRef
    from omni.domain.baseball import BaseballBaseState, BaseballCount, BaseballGameState, HalfInning
    from omni.domain.contest import TeamGame
    from omni.providers.mlb_teams import MlbTeamRegistry

    reg = MlbTeamRegistry.from_color_file()
    game = TeamGame(
        id=LeagueScopedId(League.MLB, SourceRef("mlb_statsapi"), "g1"),
        league=League.MLB,
        status=GameStatus.LIVE,
        scheduled_start=T0,
        away=reg.resolve(115),
        home=reg.resolve(119),
    )
    state = BaseballGameState(
        away_score=3,
        home_score=5,
        inning=7,
        half=HalfInning.TOP,
        count=BaseballCount(balls=0, strikes=0, outs=0),
        bases=BaseballBaseState(),
    )
    card = CardFactory().live_baseball(game, state, now=T0)

    buffer: DelayBuffer[object] = DelayBuffer(DurationSeconds(8))
    buffer.push(card, observed_at=T0)
    assert buffer.release(_at(7)) == []
    assert buffer.release(_at(8)) == [card]
