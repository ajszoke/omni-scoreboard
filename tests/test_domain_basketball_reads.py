"""A basketball feed's reads: what each kind accepts."""

from __future__ import annotations

import pytest

from omni.core.clock import MonotonicTime
from omni.core.enum import HomeAway
from omni.domain.basketball import BasketballGameState, BasketballPhase, BasketballSide, GameClock
from omni.domain.basketball_reads import LinesRead, PlayerKey, RosterLine, SlateRead
from tests.basketball_fixtures import player

_AT = MonotonicTime(100.0)


def _roster(key: int, name: str, side: HomeAway = HomeAway.AWAY) -> RosterLine:
    return RosterLine(key=PlayerKey(key), side=side, line=player(name, 0, 1, 2, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1))


def test_a_player_key_is_positive() -> None:
    assert PlayerKey(7) < PlayerKey(8)
    with pytest.raises(ValueError, match="positive"):
        PlayerKey(0)


def test_a_slate_read_carries_the_state_it_arrived_with() -> None:
    state = BasketballGameState(
        phase=BasketballPhase.LIVE,
        period=1,
        away=BasketballSide(score=2),
        home=BasketballSide(score=0),
        clock=GameClock.of(11, 40),
    )
    read = SlateRead(at=_AT, state=state)
    assert read.state.away.score == 2


def test_a_lines_read_keys_each_player_once_and_keeps_the_feeds_order() -> None:
    rows = (_roster(3, "A.Edwards", HomeAway.HOME), _roster(1, "S.Henderson"), _roster(2, "R.Gobert", HomeAway.HOME))
    read = LinesRead(at=_AT, period=2, clock=GameClock.of(0, 48, 2), lines=rows)
    assert [roster.key for roster in read.side(HomeAway.HOME)] == [PlayerKey(3), PlayerKey(2)]
    assert [roster.key for roster in read.side(HomeAway.AWAY)] == [PlayerKey(1)]
    with pytest.raises(ValueError, match="one line"):
        LinesRead(at=_AT, period=2, clock=None, lines=(_roster(1, "A"), _roster(1, "B", HomeAway.HOME)))


def test_a_lines_read_is_in_a_period() -> None:
    with pytest.raises(ValueError, match="period"):
        LinesRead(at=_AT, period=0, clock=None, lines=())
