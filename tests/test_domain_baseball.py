"""Tests for baseball domain value objects and the live game-state snapshot."""

from __future__ import annotations

import pytest

from omni.core.enum import HomeAway, try_coerce_enum
from omni.domain.baseball import (
    BaseballBaseState,
    BaseballCount,
    BaseballGameState,
    InningPhase,
    PitchingDecisions,
    PitchType,
    WinProbability,
    no_hitter_side,
)


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


def test_game_state_holds_hits_and_rejects_negative() -> None:
    assert _state(away_hits=4, home_hits=0).away_hits == 4
    with pytest.raises(ValueError):
        _state(away_hits=-1)
    with pytest.raises(ValueError):
        _state(home_hits=-1)


def test_no_hitter_side_names_the_pitching_team() -> None:
    # The hitless batting side names who is throwing it: away hitless -> home, and vice versa.
    assert no_hitter_side(_state(inning=7, away_hits=0, home_hits=3), min_inning=6) is HomeAway.HOME
    assert no_hitter_side(_state(inning=7, away_hits=3, home_hits=0), min_inning=6) is HomeAway.AWAY


def test_no_hitter_side_none_when_both_sides_have_hits() -> None:
    assert no_hitter_side(_state(inning=8, away_hits=2, home_hits=1), min_inning=6) is None


def test_no_hitter_side_reports_the_away_drought_in_a_double_no_hitter() -> None:
    assert no_hitter_side(_state(inning=7, away_hits=0, home_hits=0), min_inning=6) is HomeAway.HOME


def test_no_hitter_side_suppressed_before_the_threshold() -> None:
    assert no_hitter_side(_state(inning=5, away_hits=0, home_hits=4), min_inning=6) is None


def test_pitch_type_value_is_the_statsapi_code() -> None:
    # The enum value is the StatsAPI code, so it doubles as the short display token.
    assert PitchType.SWEEPER.value == "ST"
    assert PitchType.FOUR_SEAM_FASTBALL.value == "FF"
    assert str(PitchType.SLIDER) == "SL"  # StrEnumMixin renders as the code


def test_pitch_type_labels_are_complete_and_human() -> None:
    # Every member has a long label (no member falls through the mapping).
    assert {p.label for p in PitchType} and all(p.label for p in PitchType)
    assert PitchType.SWEEPER.label == "Sweeper"
    assert PitchType.FOUR_SEAM_FASTBALL.label == "Four-Seam Fastball"


def test_pitch_type_coerces_from_a_raw_code() -> None:
    assert try_coerce_enum(PitchType, "ST") is PitchType.SWEEPER  # the sweeper is mapped
    assert try_coerce_enum(PitchType, "CH") is PitchType.CHANGEUP
    assert try_coerce_enum(PitchType, "ZZ") is None  # an unrecognized code
    assert try_coerce_enum(PitchType, None) is None  # absent


def test_pitching_decisions_carry_the_winner_loser_and_optional_save() -> None:
    saved = PitchingDecisions(winner="Tarik Skubal", loser="Kevin Gausman", save="Will Vest")
    assert (saved.winner, saved.loser, saved.save) == ("Tarik Skubal", "Kevin Gausman", "Will Vest")
    unsaved = PitchingDecisions(winner="Tarik Skubal", loser="Kevin Gausman")
    assert unsaved.save is None  # most games have no save


def test_pitching_decisions_require_a_winner_and_a_loser() -> None:
    with pytest.raises(ValueError):
        PitchingDecisions(winner="", loser="Kevin Gausman")
    with pytest.raises(ValueError):
        PitchingDecisions(winner="Tarik Skubal", loser="")


def test_win_probability_reports_the_favoured_side_and_each_percentage() -> None:
    wp = WinProbability(home=26.0, away=74.0)
    assert wp.favored is HomeAway.AWAY
    assert wp.percent_for(HomeAway.HOME) == 26.0
    assert wp.percent_for(HomeAway.AWAY) == 74.0
    assert WinProbability(home=63.0, away=37.0).favored is HomeAway.HOME  # the other way too


def test_win_probability_is_none_favoured_at_an_exact_tie() -> None:
    assert WinProbability(home=50.0, away=50.0).favored is None


def test_win_probability_rejects_out_of_range_percentages() -> None:
    with pytest.raises(ValueError):
        WinProbability(home=120.0, away=0.0)
    with pytest.raises(ValueError):
        WinProbability(home=-1.0, away=101.0)
