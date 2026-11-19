"""Basketball's game state and the numbers beside it: what each accepts, and what it derives."""

from __future__ import annotations

from typing import Any

import pytest

from omni.core.enum import HomeAway
from omni.domain.basketball import (
    BasketballGameState,
    BasketballPhase,
    BasketballSide,
    BySide,
    FavoriteLine,
    GameClock,
    PlayerCallChange,
    CallChange,
    PlayerLine,
    ScoringRun,
    StandingRanks,
    TeamBox,
    TimeoutLength,
    best_player,
    by_plus_minus,
    period_label,
    timeout_length,
)
from tests.basketball_fixtures import player

A, H = HomeAway.AWAY, HomeAway.HOME
LIVE, FINAL = BasketballPhase.LIVE, BasketballPhase.FINAL


def _state(**changes: Any) -> BasketballGameState:
    values: dict[str, Any] = {
        "phase": LIVE,
        "period": 3,
        "away": BasketballSide(score=58, periods=(26, 25, 7), timeouts=4),
        "home": BasketballSide(score=68, periods=(26, 33, 9), timeouts=5),
        "clock": GameClock(4380),
    }
    return BasketballGameState(**{**values, **changes})


# --- the period and the clock ------------------------------------------------------------------------------


@pytest.mark.parametrize("period, label", [(1, "Q1"), (4, "Q4"), (5, "OT"), (6, "2OT"), (8, "4OT")])
def test_a_period_reads_as_a_quarter_then_as_overtimes(period: int, label: str) -> None:
    assert period_label(period) == label


def test_there_is_no_period_zero() -> None:
    with pytest.raises(ValueError):
        period_label(0)


@pytest.mark.parametrize(
    "tenths, text", [(7200, "12:00"), (4380, "7:18"), (3670, "6:07"), (600, "1:00"), (599, ":59.9"), (482, ":48.2")]
)
def test_the_clock_reads_minutes_then_tenths_under_a_minute(tenths: int, text: str) -> None:
    assert GameClock(tenths).text == text


def test_a_clock_built_from_its_reading() -> None:
    assert GameClock.of(7, 18) == GameClock(4380)
    assert GameClock.of(0, 48, 2) == GameClock(482)


@pytest.mark.parametrize("tenths", [-1, 7201])
def test_the_clock_runs_from_twelve_minutes_to_zero(tenths: int) -> None:
    with pytest.raises(ValueError):
        GameClock(tenths)


@pytest.mark.parametrize("reading", [(-1, 0, 0), (1, 60, 0), (1, 0, 10), (0, -1, 0), (0, 0, -1)])
def test_a_clock_reading_has_seconds_and_tenths_in_range(reading: tuple[int, int, int]) -> None:
    with pytest.raises(ValueError):
        GameClock.of(*reading)


# --- each side and the state -------------------------------------------------------------------------------


@pytest.mark.parametrize("changes", [{"score": -1}, {"periods": (10, -1)}, {"timeouts": -1}, {"timeouts": 8}], ids=str)
def test_a_side_refuses_negative_points_and_impossible_timeouts(changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        BasketballSide(**{"score": 10, **changes})


def test_a_side_may_not_know_its_timeouts() -> None:
    assert BasketballSide(score=0).timeouts is None


@pytest.mark.parametrize(
    "changes",
    [
        {"phase": BasketballPhase.SCHEDULED},
        {"phase": BasketballPhase.POSTPONED},
        {"period": 0},
        {"phase": BasketballPhase.HALFTIME, "period": 3, "clock": None},
        {"phase": FINAL, "period": 3, "clock": None},
        {"phase": FINAL, "period": 4, "clock": None, "home": BasketballSide(score=58)},
        {"phase": BasketballPhase.END_OF_PERIOD},  # a break has no running clock
        {"phase": BasketballPhase.END_OF_PERIOD, "clock": None, "timeout": A},
        {"period": 2},  # the sides have points in three periods
    ],
    ids=str,
)
def test_the_state_refuses_what_cant_happen(changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        _state(**changes)


def test_the_state_names_its_sides_and_its_overtime() -> None:
    state = _state()
    assert state.side(A).score == 58 and state.side(H).score == 68
    assert not state.is_overtime
    assert _state(period=5).is_overtime


def test_only_a_final_has_a_winner() -> None:
    assert _state().winner is None
    assert _state(phase=FINAL, period=4, clock=None).winner is H
    away_wins = BasketballSide(score=70)
    assert _state(phase=FINAL, period=4, clock=None, away=away_wins).winner is A


def test_by_side_reads_each_side() -> None:
    pair = BySide("POR", "MIN")
    assert (pair[A], pair[H]) == ("POR", "MIN")


# --- standings, odds and runs ------------------------------------------------------------------------------


@pytest.mark.parametrize("ranks", [(0, 1), (16, 1), (1, 0), (1, 6)])
def test_ranks_stay_inside_a_conference_and_a_division(ranks: tuple[int, int]) -> None:
    with pytest.raises(ValueError):
        StandingRanks(conference=ranks[0], division=ranks[1])


@pytest.mark.parametrize("line", [(-99, -2.5), (99, -2.5), (-110, 1.5)])
def test_a_favorites_line_is_american_odds_and_gives_points(line: tuple[int, float]) -> None:
    with pytest.raises(ValueError):
        FavoriteLine(favorite=H, moneyline=line[0], spread=line[1])


def test_a_pickem_line_is_accepted() -> None:
    assert FavoriteLine(favorite=A, moneyline=-110, spread=0.0).spread == 0.0


@pytest.mark.parametrize("run", [(7, 7, 30), (7, -1, 30), (0, 0, 0), (7, 0, -1)])
def test_a_run_outscores_the_other_side_over_a_real_span(run: tuple[int, int, int]) -> None:
    with pytest.raises(ValueError):
        ScoringRun(team=H, scored=run[0], allowed=run[1], span_seconds=run[2])


# --- timeouts by length ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "period, nth, length",
    [
        (1, 1, TimeoutLength.LONG),
        (3, 2, TimeoutLength.LONG),
        (3, 3, TimeoutLength.SHORT),
        (4, 5, TimeoutLength.SHORT),
        (5, 1, TimeoutLength.SHORT),  # every overtime timeout is short
        (6, 2, TimeoutLength.SHORT),
    ],
)
def test_a_quarters_first_two_timeouts_are_long_and_the_rest_short(
    period: int, nth: int, length: TimeoutLength
) -> None:
    assert timeout_length(period, nth) is length


@pytest.mark.parametrize("period, nth", [(0, 1), (1, 0)])
def test_timeouts_and_periods_count_from_one(period: int, nth: int) -> None:
    with pytest.raises(ValueError):
        timeout_length(period, nth)


# --- player lines, the team box and the best player --------------------------------------------------------


def test_a_player_line_adds_its_points_and_rebounds() -> None:
    line = player("A.Edwards", 15, 8, 15, 4, 9, 2, 2, 0, 4, 3, 1, 0, 2, 1)
    assert line.points == 2 * 8 + 4 + 2 and line.rebounds == 4


_GOOD: dict[str, Any] = {
    "name": "J.Doe",
    "points": 5,
    "fg_made": 2,
    "fg_attempted": 4,
    "threes_made": 1,
    "threes_attempted": 2,
    "ft_made": 0,
    "ft_attempted": 0,
    "offensive_rebounds": 0,
    "defensive_rebounds": 1,
    "assists": 0,
    "steals": 0,
    "blocks": 0,
    "turnovers": 0,
    "fouls": 1,
    "plus_minus": -2,
}


@pytest.mark.parametrize(
    "changes",
    [
        {"name": ""},
        {"steals": -1},
        {"fouls": 7},
        {"fouls": -1},
        {"ft_made": 1, "points": 6},  # a make with no attempt
        {"threes_made": 3, "threes_attempted": 3, "points": 7},  # more threes than field goals
        {"threes_attempted": 5},  # more threes tried than field goals
        {"points": 6},  # not what the makes add up to
    ],
    ids=str,
)
def test_a_player_line_refuses_what_doesnt_add_up(changes: dict[str, Any]) -> None:
    PlayerLine(**_GOOD)
    with pytest.raises(ValueError):
        PlayerLine(**{**_GOOD, **changes})


def test_players_order_by_plus_minus_with_points_breaking_a_tie() -> None:
    sharpe = player("S.Sharpe", -12, 9, 24, 4, 11, 4, 4, 0, 3, 2, 1, 0, 4, 2)  # 26 points
    camara = player("T.Camara", 1, 4, 8, 3, 6, 0, 0, 1, 4, 2, 2, 1, 1, 3)
    grant = player("J.Grant", -12, 5, 11, 2, 6, 2, 2, 0, 4, 1, 0, 1, 1, 3)  # 14 points
    assert by_plus_minus([sharpe, grant, camara]) == (camara, sharpe, grant)
    assert best_player([sharpe, grant, camara]) is camara  # not the top scorer
    assert best_player([grant, sharpe]) is sharpe  # level at -12: the higher points
    assert best_player([]) is None


def test_a_team_box_sums_its_players() -> None:
    lines = (
        player("A", 3, 3, 5, 2, 3, 0, 0, 1, 3, 1, 2, 1, 0, 2),
        player("B", -4, 6, 12, 2, 5, 2, 2, 1, 5, 4, 1, 0, 2, 2),
    )
    assert TeamBox.of(lines) == TeamBox(
        fg_pct=53, threes_made=4, threes_attempted=8, rebounds=10, assists=5, turnovers=2
    )
    assert TeamBox.of(()).fg_pct == 0  # no shot tried yet


@pytest.mark.parametrize("changes", [{"fg_pct": 101}, {"fg_pct": -1}, {"rebounds": -1}, {"threes_made": 21}], ids=str)
def test_a_team_box_refuses_impossible_numbers(changes: dict[str, Any]) -> None:
    values: dict[str, Any] = {
        "fg_pct": 44,
        "threes_made": 8,
        "threes_attempted": 20,
        "rebounds": 25,
        "assists": 13,
        "turnovers": 8,
    }
    with pytest.raises(ValueError):
        TeamBox(**{**values, **changes})


def test_a_call_change_names_its_player() -> None:
    with pytest.raises(ValueError):
        PlayerCallChange(change=CallChange.FOUL_OFF, player="")
