"""Tests for baseball domain value objects and the live game-state snapshot."""

from __future__ import annotations

import pytest

from omni.domain.baseball import BaseballBaseState, BaseballCount, BaseballGameState, HalfInning


def _state(**overrides: object) -> BaseballGameState:
    base: dict[str, object] = dict(
        away_score=3,
        home_score=5,
        inning=7,
        half=HalfInning.TOP,
        count=BaseballCount(balls=2, strikes=1, outs=2),
        bases=BaseballBaseState(first=True, third=True),
    )
    base.update(overrides)
    return BaseballGameState(**base)  # type: ignore[arg-type]


def test_game_state_holds_the_snapshot() -> None:
    state = _state()
    assert state.away_score == 3 and state.home_score == 5
    assert state.inning == 7 and state.half is HalfInning.TOP
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


def test_value_objects_are_shared_across_layers() -> None:
    # The relocation re-exports from events/cards, so identity is preserved.
    from omni.cards.baseball import BaseballBaseState as CardsBaseState
    from omni.events.baseball import BaseballCount as EventsCount
    from omni.events.baseball import HalfInning as EventsHalf

    assert CardsBaseState is BaseballBaseState
    assert EventsCount is BaseballCount
    assert EventsHalf is HalfInning
