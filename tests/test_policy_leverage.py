"""A game's leverage weight: the labeled proxy from released game state, and a model's reading over time
(`BOARD_POLICY.md` §3.1)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from omni.core.clock import MonotonicTime
from omni.domain.baseball import BaseballBaseState, BaseballCount, BaseballField, BaseballGameState, InningPhase
from omni.domain.coverage import CoverageMap, FieldCoverage
from omni.domain.football import FootballField, FootballGameState, FootballInterval
from omni.policy.leverage import (
    Leverage,
    LeveragePolicy,
    LeverageTracker,
    MarginScale,
    ModelReading,
    ModelStatus,
    ReadingKind,
    baseball_proxy,
    basketball_proxy,
    football_proxy,
    model_z,
    proxy_leverage,
)

_DEFAULTED = FieldCoverage.DEFAULTED


def _mlb(
    inning: int,
    phase: InningPhase,
    *,
    outs: int = 0,
    away: int = 0,
    home: int = 0,
    coverage: dict[BaseballField, FieldCoverage] | None = None,
) -> BaseballGameState:
    return BaseballGameState(
        away_score=away,
        home_score=home,
        inning=inning,
        phase=phase,
        count=BaseballCount(balls=0, strikes=0, outs=outs),
        bases=BaseballBaseState(),
        coverage=CoverageMap.of(coverage or {}),
    )


def _nfl(
    period: int,
    clock: int,
    *,
    away: int = 0,
    home: int = 0,
    interval: FootballInterval | None = None,
    coverage: dict[FootballField, FieldCoverage] | None = None,
) -> FootballGameState:
    return FootballGameState(
        period=period,
        clock_seconds=clock,
        away_score=away,
        home_score=home,
        interval=interval,
        coverage=CoverageMap.of(coverage or {}),
    )


def _is_proxy(reading: Leverage, g: float) -> bool:
    """The labeled proxy's reading, at ``g`` to six places, with no model behind it."""
    return reading.status is ModelStatus.PROXY and reading.model_id is None and round(reading.g, 6) == g


# --- the proxy: MLB --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "state, g",
    [
        (_mlb(1, InningPhase.TOP), 0.30),  # nothing played, nothing between them
        (_mlb(9, InningPhase.BOTTOM, outs=2, away=3, home=3), 0.974314),  # 53 of 54 outs, tied
        (_mlb(7, InningPhase.MIDDLE, away=8, home=1), 0.031300),  # a blowout late
        (_mlb(10, InningPhase.TOP, outs=1, away=4, home=3), 0.5),  # extras: no regulation left, a one-run game
        (_mlb(9, InningPhase.END, away=2, home=2), 1.0),  # tied after nine: all 54 outs, nothing between them
    ],
)
def test_an_mlb_game_s_proxy_reads_its_completed_scheduled_outs_and_its_margin(
    state: BaseballGameState, g: float
) -> None:
    assert _is_proxy(baseball_proxy(state), g)


def test_an_mlb_half_s_third_out_and_the_break_after_it_count_once() -> None:
    assert baseball_proxy(_mlb(5, InningPhase.TOP, outs=3, away=1)) == baseball_proxy(
        _mlb(5, InningPhase.MIDDLE, away=1)
    )
    assert baseball_proxy(_mlb(4, InningPhase.END, home=2)) == baseball_proxy(_mlb(5, InningPhase.TOP, home=2))


@pytest.mark.parametrize(
    "state",
    [
        _mlb(6, InningPhase.TOP, coverage={BaseballField.SCORE: _DEFAULTED}),
        _mlb(6, InningPhase.TOP, coverage={BaseballField.INNING: _DEFAULTED}),
        _mlb(6, InningPhase.TOP, coverage={BaseballField.HALF: FieldCoverage.UNAVAILABLE}),
        _mlb(6, InningPhase.BOTTOM, coverage={BaseballField.OUTS: _DEFAULTED}),  # the 0 outs is a stand-in
    ],
)
def test_an_mlb_score_inning_half_or_outs_the_feed_left_out_is_unknown_not_zero(state: BaseballGameState) -> None:
    assert baseball_proxy(state) == Leverage(g=0.2, status=ModelStatus.UNKNOWN)


def test_between_halves_an_mlb_game_needs_no_outs_count() -> None:
    missing = baseball_proxy(_mlb(7, InningPhase.MIDDLE, away=8, home=1, coverage={BaseballField.OUTS: _DEFAULTED}))
    assert _is_proxy(missing, 0.0313)


# --- the proxy: NFL and NBA ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "state, g",
    [
        (_nfl(1, 900), 0.30),
        (_nfl(4, 120, away=20, home=17), 0.531823),  # 2:00 left, a field goal between them
        (_nfl(2, 0, away=7), 0.283041),  # the half's last second
        (_nfl(5, 600, away=3), 0.721888),  # overtime: the scale reads 10:00 of overtime over regulation
    ],
)
def test_an_nfl_game_s_proxy_reads_the_regulation_seconds_played(state: FootballGameState, g: float) -> None:
    assert _is_proxy(football_proxy(state), g)


_NO_HALF_END = LeveragePolicy(half_end=timedelta(0))


def test_the_first_half_s_last_two_minutes_get_the_game_s_late_boost_at_the_same_score() -> None:
    # The two-minute warning, a field goal between them: weighed as at 2:00 of the fourth quarter, more than before.
    assert football_proxy(_nfl(2, 120, away=3)) == football_proxy(_nfl(4, 120, away=3))
    assert football_proxy(_nfl(2, 120, away=3)).g > football_proxy(_nfl(2, 120, away=3), policy=_NO_HALF_END).g


@pytest.mark.parametrize("state", [_nfl(2, 1, away=21), _nfl(2, 45, away=14, home=7)])
def test_a_lead_that_would_settle_the_game_that_late_never_weighs_less_at_the_half(state: FootballGameState) -> None:
    assert football_proxy(state) == football_proxy(state, policy=_NO_HALF_END)


def test_before_the_two_minute_warning_and_at_the_half_s_end_the_first_half_weighs_by_the_game_s_clock() -> None:
    assert _is_proxy(football_proxy(_nfl(2, 121, away=3)), 0.405836)  # one second before the window
    assert _is_proxy(football_proxy(_nfl(2, 0, away=7)), 0.283041)  # 0:00: the half is over, its break weighs as before
    for period in (1, 3):  # only the first half's own end
        assert football_proxy(_nfl(period, 60, away=3)) == football_proxy(_nfl(period, 60, away=3), policy=_NO_HALF_END)


def test_the_half_s_last_minutes_read_the_policy_s_window() -> None:
    narrow = LeveragePolicy(half_end=timedelta(seconds=30))
    assert football_proxy(_nfl(2, 60, away=3), policy=narrow) == football_proxy(
        _nfl(2, 60, away=3), policy=_NO_HALF_END
    )
    assert football_proxy(_nfl(2, 30, away=3), policy=narrow) == football_proxy(_nfl(4, 30, away=3), policy=narrow)


def test_an_nba_first_half_s_last_two_minutes_get_the_game_s_late_boost() -> None:
    half = basketball_proxy(period=2, clock_seconds=60, away_score=50, home_score=52)
    assert half == basketball_proxy(period=4, clock_seconds=60, away_score=50, home_score=52)
    assert _is_proxy(basketball_proxy(period=2, clock_seconds=121, away_score=50, home_score=52), 0.437943)


def test_at_an_nfl_break_an_unreported_clock_reads_the_period_s_end() -> None:
    halftime = _nfl(2, 0, away=7, interval=FootballInterval.HALFTIME, coverage={FootballField.CLOCK: _DEFAULTED})
    assert _is_proxy(football_proxy(halftime), 0.283041)
    quarter = _nfl(1, 0, interval=FootballInterval.END_OF_QUARTER, coverage={FootballField.CLOCK: _DEFAULTED})
    assert football_proxy(quarter) == football_proxy(_nfl(2, 900))


@pytest.mark.parametrize(
    "state",
    [
        _nfl(3, 400, coverage={FootballField.SCORE: _DEFAULTED}),
        _nfl(3, 400, coverage={FootballField.PERIOD: _DEFAULTED}),
        _nfl(3, 400, coverage={FootballField.CLOCK: _DEFAULTED}),  # in play, a 0:00 stand-in isn't the clock
        _nfl(
            2,
            0,
            interval=FootballInterval.HALFTIME,
            coverage={FootballField.CLOCK: _DEFAULTED, FootballField.INTERVAL: _DEFAULTED},
        ),
    ],
)
def test_an_nfl_score_period_or_clock_the_feed_left_out_is_unknown_not_zero(state: FootballGameState) -> None:
    assert football_proxy(state) == Leverage(g=0.2, status=ModelStatus.UNKNOWN)


def test_an_nfl_final_has_no_next_outcome_to_weigh() -> None:
    assert _is_proxy(football_proxy(_nfl(4, 0, away=10, home=10, interval=FootballInterval.FINAL)), 0.0)


def test_an_nba_game_s_proxy_reads_regulation_seconds_on_a_square_root_margin_scale() -> None:
    assert _is_proxy(basketball_proxy(period=4, clock_seconds=60, away_score=100, home_score=102), 0.844513)
    assert _is_proxy(basketball_proxy(period=5, clock_seconds=150, away_score=110, home_score=106), 0.720565)
    assert _is_proxy(basketball_proxy(period=1, clock_seconds=720, away_score=0, home_score=0), 0.30)


@pytest.mark.parametrize(
    "period, clock, away, home",
    [(None, 60, 1, 2), (4, None, 1, 2), (4, 60, None, 2), (4, 60, 1, None), (0, 60, 1, 2)],
)
def test_an_nba_game_without_score_period_or_clock_is_unknown(
    period: int | None, clock: int | None, away: int | None, home: int | None
) -> None:
    reading = basketball_proxy(period=period, clock_seconds=clock, away_score=away, home_score=home)
    assert reading == Leverage(g=0.2, status=ModelStatus.UNKNOWN)


def test_the_proxy_holds_its_inputs_to_their_range_and_is_labeled_never_a_model() -> None:
    reading = proxy_leverage(margin=0, progress=1.4, remaining=-0.2, scale=MarginScale(base=1.0, slope=2.0))
    assert reading == Leverage(g=1.0, status=ModelStatus.PROXY)
    assert not reading.qualifies_action and reading.model_id is None and reading.z is None


@pytest.mark.parametrize("base, slope, exponent", [(0.0, 2.0, 1.0), (1.0, -1.0, 1.0), (1.0, 2.0, 0.0)])
def test_a_margin_scale_refuses_one_that_could_shrink_to_nothing(base: float, slope: float, exponent: float) -> None:
    with pytest.raises(ValueError, match="margin scale"):
        MarginScale(base=base, slope=slope, exponent=exponent)


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"mlb_scheduled_innings": 0}, "at least one inning"),
        ({"nfl_regulation": timedelta(0)}, "regulation"),
        ({"nba_regulation": timedelta(seconds=-1)}, "regulation"),
        ({"li_span": 0.0}, "half-life"),
        ({"half_life": timedelta(0)}, "half-life"),
        ({"uncertain_cap": timedelta(seconds=-1)}, "half-life"),
        ({"half_end": timedelta(seconds=-1)}, "half's last minutes"),
    ],
)
def test_a_leverage_policy_refuses_constants_that_cant_work(changes: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        LeveragePolicy(**changes)  # type: ignore[arg-type]


# --- a model's reading over time ---------------------------------------------------------------------------------


def _reading(li: float, at: float, kind: ReadingKind = ReadingKind.FRESH, *, reduced: bool = False) -> ModelReading:
    status = ModelStatus.REDUCED if reduced else ModelStatus.VALIDATED
    return ModelReading(li=li, status=status, model_id="mlb-li/1", at=MonotonicTime(at), kind=kind)


@pytest.mark.parametrize("li, z", [(0.2, 0.0), (0.5, 0.0), (1.0, 0.2), (1.75, 0.5), (3.0, 1.0), (7.0, 1.0)])
def test_a_leverage_index_maps_onto_z_between_half_and_three(li: float, z: float) -> None:
    assert model_z(li) == pytest.approx(z)


def test_the_first_reading_starts_the_average_at_its_own_z() -> None:
    weight = LeverageTracker().read(_reading(3.0, 10.0))
    assert weight == Leverage(g=1.0, z=1.0, status=ModelStatus.VALIDATED, model_id="mlb-li/1")
    assert weight.qualifies_action


def test_the_average_takes_in_a_fresh_reading_by_its_active_time_with_a_90_second_half_life() -> None:
    tracker = LeverageTracker()
    tracker.read(_reading(3.0, 0.0))
    # 10 s of action: R keeps 2^(-10/90) of its old 1.0; G = 0.75*0 + 0.25*R.
    assert tracker.read(_reading(0.5, 10.0)).g == pytest.approx(0.25 * 0.925875, abs=1e-6)


def test_one_uncertain_interval_counts_for_at_most_30_seconds() -> None:
    tracker = LeverageTracker()
    tracker.read(_reading(3.0, 0.0))
    assert tracker.read(_reading(0.5, 600.0)).g == pytest.approx(0.198425, abs=1e-6)  # 30 s, not 10 min


def test_a_duplicate_poll_holds_the_average_and_the_interval_keeps_running() -> None:
    tracker = LeverageTracker()
    tracker.read(_reading(3.0, 0.0))
    held = tracker.read(_reading(3.0, 5.0, ReadingKind.DUPLICATE))
    assert held.g == 1.0
    assert tracker.read(_reading(0.5, 10.0)).g == pytest.approx(0.25 * 0.925875, abs=1e-6)  # still 10 s


def test_a_confirmed_break_holds_the_average_and_its_time_isnt_action() -> None:
    tracker = LeverageTracker()
    tracker.read(_reading(3.0, 0.0))
    in_break = tracker.read(_reading(0.5, 120.0, ReadingKind.IN_BREAK))
    assert in_break.g == pytest.approx(0.25)  # z is 0 now, R still 1
    # Back in action 10 s after the break's last reading: only those 10 s count.
    assert tracker.read(_reading(0.5, 130.0)).g == pytest.approx(0.25 * 0.925875, abs=1e-6)


def test_after_an_outage_the_average_restarts_from_the_current_reading() -> None:
    tracker = LeverageTracker()
    tracker.read(_reading(3.0, 0.0))
    assert tracker.read(_reading(0.5, 3600.0, ReadingKind.AFTER_OUTAGE)).g == 0.0


def test_a_reduced_model_s_reading_weighs_at_most_two_tenths() -> None:
    weight = LeverageTracker().read(_reading(3.0, 0.0, reduced=True))
    assert weight == Leverage(g=0.2, z=1.0, status=ModelStatus.REDUCED, model_id="mlb-li/1")
    assert not weight.qualifies_action


@pytest.mark.parametrize(
    "li, status, message",
    [
        (1.0, ModelStatus.PROXY, "validated or reduced"),
        (1.0, ModelStatus.UNKNOWN, "validated or reduced"),
        (-0.1, ModelStatus.VALIDATED, "negative"),
        (float("nan"), ModelStatus.VALIDATED, "negative"),
    ],
)
def test_a_model_reading_refuses_what_cant_be(li: float, status: ModelStatus, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        ModelReading(li=li, status=status, model_id="m", at=MonotonicTime(0.0))
