"""Tests for baseball domain value objects and the live game-state snapshot."""

from __future__ import annotations

import pytest

from omni.domain.baseball import BaseballBaseState, BaseballCount, BaseballGameState, InningPhase


def _state(**overrides: object) -> BaseballGameState:
    base: dict[str, object] = dict(
        away_score=3,
        home_score=5,
        inning=7,
        phase=InningPhase.TOP,
        count=BaseballCount(balls=2, strikes=1, outs=2),
        bases=BaseballBaseState(first=True, third=True),
    )
    base.update(overrides)
    return BaseballGameState(**base)  # type: ignore[arg-type]


def test_game_state_holds_the_snapshot() -> None:
    state = _state()
    assert state.away_score == 3 and state.home_score == 5
    assert state.inning == 7 and state.phase is InningPhase.TOP
    assert state.count.outs == 2
    assert state.bases.first and state.bases.third and not state.bases.second


def test_game_state_rejects_negative_scores() -> None:
    with pytest.raises(ValueError):
        _state(away_score=-1)
    with pytest.raises(ValueError):
        _state(home_score=-2)


def test_game_state_requires_inning_at_least_one() -> None:
    with pytest.raises(ValueError):
        _state(inning=0)


def test_game_state_is_frozen() -> None:
    state = _state()
    with pytest.raises(AttributeError):
        state.inning = 8  # type: ignore[misc]


def test_inning_phase_breaks() -> None:
    assert not InningPhase.TOP.is_break and not InningPhase.BOTTOM.is_break  # active halves
    assert InningPhase.MIDDLE.is_break and InningPhase.END.is_break  # between-halves breaks
