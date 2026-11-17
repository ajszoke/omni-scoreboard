"""Field coverage: what a game state's values rest on, and which of them are known."""

from __future__ import annotations

import pytest

from omni.domain.baseball import BaseballBaseState, BaseballCount, BaseballField, BaseballGameState, InningPhase
from omni.domain.coverage import CoverageMap, FieldCoverage
from omni.domain.football import FootballField, FootballGameState


def _baseball(coverage: CoverageMap[BaseballField] = CoverageMap()) -> BaseballGameState:
    return BaseballGameState(
        away_score=1,
        home_score=2,
        inning=3,
        phase=InningPhase.TOP,
        count=BaseballCount(balls=0, strikes=0, outs=0),
        bases=BaseballBaseState(),
        coverage=coverage,
    )


@pytest.mark.parametrize(
    ("coverage", "known"),
    [
        (FieldCoverage.REPORTED, True),
        (FieldCoverage.INFERRED, True),
        (FieldCoverage.DEFAULTED, False),
        (FieldCoverage.UNAVAILABLE, False),
        (FieldCoverage.NOT_APPLICABLE, False),
    ],
)
def test_only_a_reported_or_inferred_value_is_known(coverage: FieldCoverage, known: bool) -> None:
    assert coverage.known is known


def test_a_state_built_without_coverage_is_reported_in_every_field() -> None:
    baseball = BaseballGameState(
        away_score=1,
        home_score=2,
        inning=3,
        phase=InningPhase.TOP,
        count=BaseballCount(balls=0, strikes=0, outs=0),
        bases=BaseballBaseState(),
    )
    assert all(baseball.coverage[field] is FieldCoverage.REPORTED and baseball.known(field) for field in BaseballField)
    football = FootballGameState(period=1, clock_seconds=900, away_score=0, home_score=0)
    assert all(football.known(field) for field in FootballField)


def test_a_defaulted_field_is_not_known_and_the_rest_still_are() -> None:
    state = _baseball(CoverageMap.of({BaseballField.OUTS: FieldCoverage.DEFAULTED}))
    assert state.coverage[BaseballField.OUTS] is FieldCoverage.DEFAULTED and not state.known(BaseballField.OUTS)
    assert all(state.known(field) for field in BaseballField if field is not BaseballField.OUTS)


def test_maps_that_say_the_same_thing_are_equal_and_their_states_stay_hashable() -> None:
    plain = CoverageMap.of({BaseballField.BASES: FieldCoverage.DEFAULTED})
    spelled_out = CoverageMap.of(
        {BaseballField.BASES: FieldCoverage.DEFAULTED, BaseballField.OUTS: FieldCoverage.REPORTED}
    )
    assert plain == spelled_out and hash(plain) == hash(spelled_out)
    assert CoverageMap.of({BaseballField.OUTS: FieldCoverage.REPORTED}) == CoverageMap()
    assert hash(_baseball(plain)) == hash(_baseball(spelled_out))
    assert _baseball(plain) != _baseball()  # a stand-in is a different state from a reported value


def test_an_update_says_anew_what_those_fields_rest_on_and_keeps_the_rest() -> None:
    before = CoverageMap.of(
        {FootballField.TIMEOUTS: FieldCoverage.UNAVAILABLE, FootballField.CLOCK: FieldCoverage.DEFAULTED}
    )
    after = before.updated({FootballField.TIMEOUTS: FieldCoverage.INFERRED, FootballField.SPOT: FieldCoverage.REPORTED})
    assert after[FootballField.TIMEOUTS] is FieldCoverage.INFERRED
    assert after[FootballField.CLOCK] is FieldCoverage.DEFAULTED
    assert after == CoverageMap.of(
        {FootballField.TIMEOUTS: FieldCoverage.INFERRED, FootballField.CLOCK: FieldCoverage.DEFAULTED}
    )
    assert before[FootballField.TIMEOUTS] is FieldCoverage.UNAVAILABLE  # the original is untouched


def test_a_map_says_one_thing_of_a_field_and_leaves_reported_fields_out() -> None:
    with pytest.raises(ValueError, match="one thing at a time"):
        CoverageMap(
            frozenset({(BaseballField.OUTS, FieldCoverage.DEFAULTED), (BaseballField.OUTS, FieldCoverage.UNAVAILABLE)})
        )
    with pytest.raises(ValueError, match="left out"):
        CoverageMap(frozenset({(BaseballField.OUTS, FieldCoverage.REPORTED)}))
