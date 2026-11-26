"""Interrupt values, the classifiers that read our typed events, and the bars (`BOARD_POLICY.md` §4)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone

import pytest

from omni.core.enum import DisplayPriority, GameStatus, League, UpdateUrgency
from omni.core.ids import LeagueScopedId, SourceRef
from omni.domain.baseball import (
    BaseballBaseState,
    BaseballCount,
    BaseballField,
    BaseballGameState,
    InningPhase,
    WinProbability,
    scoring_impact,
)
from omni.domain.contest import Contest
from omni.domain.coverage import CoverageMap, FieldCoverage
from omni.domain.football import FootballField, FootballGameState, FootballInterval, PlaySummary, PlayTone
from omni.events.base import EventImportance
from omni.events.baseball import BaseballGameEvent, BaseballGameEventType, BaseballPlayPayload
from omni.events.football import FootballEvent, FootballEventKind
from omni.policy.interrupts import (
    BarContext,
    ClassBasis,
    Classification,
    FavoriteActivity,
    InterruptBars,
    InterruptClass,
    InterruptPolicy,
    InterruptValue,
    LeverageBasis,
    SWING_KINDS,
    Stakes,
    bar_context,
    classify_baseball_final,
    classify_baseball_play,
    classify_football_event,
    interrupt_value,
    win_swing,
)
from omni.policy.leverage import Leverage, ModelStatus
from omni.policy.promotion import PromotionKind
from omni.tower.decisions import event_words

T = datetime(2026, 9, 20, 23, 0, tzinfo=timezone.utc)
MLB = SourceRef("mlb_statsapi")
GAME = Contest(id=LeagueScopedId(League.MLB, MLB, "746"), league=League.MLB, status=GameStatus.LIVE, scheduled_start=T)
MODEL = "mlb-wp/1"


def _validated(z: float) -> Leverage:
    return Leverage(g=z, z=z, status=ModelStatus.VALIDATED, model_id=MODEL)


# --- Reference values ----------------------------------------------------------------------------------------------


def test_a_last_second_winner_between_the_two_worst_teams_is_worth_0_78_and_waits_for_a_real_break() -> None:
    # NBA on Free: the proxy reads the closest, latest state as g = 1; the worst teams' records give Q = 0.
    value = interrupt_value(
        InterruptClass.DECISIVE_LATE_WINNER,
        predecessor=Leverage(g=1.0, status=ModelStatus.PROXY),
        stakes=Stakes(quality=0.0),
    )
    assert value.value == pytest.approx(0.78, abs=5e-3)
    assert value.basis is LeverageBasis.PROXY and not value.action_evidence
    bars = InterruptBars()
    assert not bars.qualifies_action(value)
    assert value.value < bars.for_context(BarContext.MICRO_BREAK)
    assert value.value >= bars.for_context(BarContext.OPEN)


def test_a_confirmed_perfect_game_in_a_blowout_is_worth_1_1375_and_clears_the_action_bar() -> None:
    # A rare feat ignores the blowout's leverage (z_pre = 0); unknown records count as Q = 0.
    value = interrupt_value(InterruptClass.PERFECT_GAME, predecessor=_validated(0.0), stakes=Stakes())
    assert value.value == pytest.approx(1.1375, abs=5e-5)
    assert value.z_pre == 0.0 and value.basis is LeverageBasis.FEAT
    assert InterruptBars().qualifies_action(value)


def test_a_no_hitter_at_average_quality_is_worth_1_155_even_on_proxy_leverage() -> None:
    value = interrupt_value(
        InterruptClass.NO_HITTER, predecessor=Leverage(g=0.9, status=ModelStatus.PROXY), stakes=Stakes(quality=0.5)
    )
    assert value.value == pytest.approx(1.155, abs=5e-4)
    assert InterruptBars().qualifies_action(value)


def test_an_average_walk_off_is_worth_0_99_and_waits_for_a_micro_break() -> None:
    value = interrupt_value(
        InterruptClass.DECISIVE_LATE_WINNER, predecessor=_validated(1.0), stakes=Stakes(quality=0.5)
    )
    assert value.value == pytest.approx(0.99, abs=5e-3)
    bars = InterruptBars()
    assert value.action_evidence and not bars.qualifies_action(value)
    assert value.value >= bars.for_context(BarContext.MICRO_BREAK)


# --- z_pre and stakes ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "predecessor, z_pre, basis",
    [
        (Leverage(g=0.6, status=ModelStatus.VALIDATED, model_id=MODEL), 0.6, LeverageBasis.MODEL),
        (Leverage(g=0.6, z=0.8, status=ModelStatus.VALIDATED, model_id=MODEL), 0.8, LeverageBasis.MODEL),
        (Leverage(g=0.2, z=0.9, status=ModelStatus.REDUCED, model_id=MODEL), 0.2, LeverageBasis.REDUCED),
        (Leverage(g=0.7, status=ModelStatus.PROXY), 0.7, LeverageBasis.PROXY),
        (Leverage(g=0.2, status=ModelStatus.UNKNOWN), 0.0, LeverageBasis.UNKNOWN),
        (None, 0.0, LeverageBasis.NONE),
    ],
)
def test_only_a_validated_predecessor_lets_a_non_feat_cut_into_action(
    predecessor: Leverage | None, z_pre: float, basis: LeverageBasis
) -> None:
    value = interrupt_value(InterruptClass.MAJOR, predecessor=predecessor)
    assert value.z_pre == z_pre and value.basis is basis
    assert value.action_evidence is (basis is LeverageBasis.MODEL)
    assert value.value == pytest.approx(0.8 * (0.5 + 0.5 * z_pre) * 0.65)


def test_postseason_and_season_stakes_raise_the_value() -> None:
    value = interrupt_value(
        InterruptClass.DECISIVE_LATE_WINNER,
        predecessor=_validated(1.0),
        stakes=Stakes(quality=0.5, postseason=1.0, season=1.0),
    )
    assert value.value == pytest.approx(1.2 * (0.65 + 0.175 + 0.35 + 0.25))


def test_routine_events_are_worth_nothing() -> None:
    assert interrupt_value(InterruptClass.ROUTINE, predecessor=_validated(1.0)).value == 0.0


def test_a_policy_can_reweight_a_class() -> None:
    policy = InterruptPolicy(major=1.0)
    assert interrupt_value(InterruptClass.MAJOR, predecessor=None, policy=policy).value == pytest.approx(0.325)


@pytest.mark.parametrize(
    "build, message",
    [
        (lambda: Stakes(quality=1.2), "within 0..1"),
        (lambda: Stakes(postseason=-0.5), "within 0..1"),
        (
            lambda: InterruptValue(event_class=InterruptClass.MAJOR, value=-1, z_pre=0, basis=LeverageBasis.NONE),
            "z_pre",
        ),
        (lambda: InterruptValue(event_class=InterruptClass.MAJOR, value=1, z_pre=2, basis=LeverageBasis.NONE), "z_pre"),
        (lambda: InterruptPolicy(major=-0.1), "can't be negative"),
        (lambda: InterruptPolicy(win_swing=-0.1), "can't be negative"),
        (lambda: InterruptPolicy(win_swing_points=0.0), "threshold must be positive"),
        (lambda: InterruptPolicy(feat_innings=0), "count from one"),
        (lambda: InterruptBars(open=0.9, micro_break=0.85), "rise"),
    ],
)
def test_interrupt_values_refuse_what_cant_be(build: Callable[[], object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        build()


# --- The bars ------------------------------------------------------------------------------------------------------


def test_one_favorites_timeout_doesnt_lower_the_bar_over_anothers_live_drive() -> None:
    bars = InterruptBars()
    assert bar_context([FavoriteActivity.MICRO_BREAK, FavoriteActivity.IN_ACTION]) is BarContext.ACTION
    assert bars.for_context(BarContext.ACTION) == 1.10


def test_the_bar_drops_for_a_micro_break_and_again_for_ordinary_breaks_or_no_live_favorite() -> None:
    bars = InterruptBars()
    micro = bar_context([FavoriteActivity.MICRO_BREAK, FavoriteActivity.ORDINARY_BREAK])
    assert micro is BarContext.MICRO_BREAK and bars.for_context(micro) == 0.85
    assert bar_context([FavoriteActivity.ORDINARY_BREAK, FavoriteActivity.ORDINARY_BREAK]) is BarContext.OPEN
    assert bar_context([]) is BarContext.OPEN and bars.for_context(BarContext.OPEN) == 0.35


# --- MLB -----------------------------------------------------------------------------------------------------------


def _state(
    *,
    inning: int = 9,
    phase: InningPhase = InningPhase.BOTTOM,
    away: int = 0,
    home: int = 0,
    away_hits: int = 4,
    home_hits: int = 4,
    away_reached: bool | None = True,
    home_reached: bool | None = True,
    unknown: tuple[BaseballField, ...] = (),
) -> BaseballGameState:
    return BaseballGameState(
        away_score=away,
        home_score=home,
        inning=inning,
        phase=phase,
        count=BaseballCount(balls=0, strikes=0, outs=0),
        bases=BaseballBaseState(),
        away_hits=away_hits,
        home_hits=home_hits,
        away_reached_base=away_reached,
        home_reached_base=home_reached,
        coverage=CoverageMap.of(dict.fromkeys(unknown, FieldCoverage.DEFAULTED)),
    )


@pytest.mark.parametrize(
    "state, final, expected",
    [
        # The home staff no-hit the visitors, and the play log shows none of them reached.
        (_state(inning=9, phase=InningPhase.MIDDLE, away_hits=0, away_reached=False), True, ClassBasis.PERFECT_GAME),
        (_state(inning=9, phase=InningPhase.MIDDLE, away_hits=0, away_reached=True), True, ClassBasis.NO_HITTER),
        (_state(inning=9, phase=InningPhase.MIDDLE, away_hits=0, away_reached=None), True, ClassBasis.REACH_UNVERIFIED),
        # The visitors no-hit the home side through a full ninth.
        (_state(inning=9, phase=InningPhase.END, home_hits=0, home_reached=False), True, ClassBasis.PERFECT_GAME),
        (_state(inning=9, phase=InningPhase.MIDDLE, away_hits=0), False, ClassBasis.NOT_FINAL),
        (_state(away_hits=0, unknown=(BaseballField.HITS,)), True, ClassBasis.HITS_UNVERIFIED),
        (_state(inning=9, phase=InningPhase.END), True, ClassBasis.NO_FEAT),
        # A home win without the bottom of the ninth stays "Top" in StatsAPI's final: with the home side ahead, the
        # top is over and the home staff threw nine.
        (
            _state(inning=9, phase=InningPhase.TOP, home=2, away_hits=0, away_reached=False),
            True,
            ClassBasis.PERFECT_GAME,
        ),
        (
            _state(inning=9, phase=InningPhase.TOP, home=1, away_hits=0, away_reached=None),
            True,
            ClassBasis.REACH_UNVERIFIED,
        ),
        # A final that reads as the top of the ninth with no home lead can't vouch for the home staff's ninth inning.
        (_state(inning=9, phase=InningPhase.TOP, away_hits=0, away_reached=False), True, ClassBasis.NO_FEAT),
        # The visitors' staff in that game threw eight: no feat for them.
        (_state(inning=9, phase=InningPhase.TOP, home=2, home_hits=0, home_reached=False), True, ClassBasis.NO_FEAT),
        # A visiting staff that lost without pitching the bottom of the ninth threw eight innings.
        (_state(inning=9, phase=InningPhase.MIDDLE, home_hits=0, home_reached=False), True, ClassBasis.NO_FEAT),
        # A game called after seven.
        (_state(inning=7, phase=InningPhase.END, away_hits=0, away_reached=False), True, ClassBasis.NO_FEAT),
    ],
)
def test_a_feat_needs_the_feeds_own_final_verified_hits_nine_innings_and_for_perfection_a_complete_ledger(
    state: BaseballGameState, final: bool, expected: ClassBasis
) -> None:
    classification = classify_baseball_final(state, final=final)
    assert classification.basis is expected
    expected_class = {
        ClassBasis.PERFECT_GAME: InterruptClass.PERFECT_GAME,
        ClassBasis.NO_HITTER: InterruptClass.NO_HITTER,
        ClassBasis.REACH_UNVERIFIED: InterruptClass.NO_HITTER,
    }.get(expected, InterruptClass.ROUTINE)
    assert classification.event_class is expected_class


def _play(
    event_type: BaseballGameEventType,
    *,
    inning: int,
    phase: InningPhase,
    rbi: int,
    away: int | None,
    home: int | None,
    impact: bool = True,
) -> BaseballGameEvent:
    return BaseballGameEvent(
        id=LeagueScopedId(League.MLB, MLB, f"746:ab:{inning}{phase.value}"),
        contest=GAME,
        event_type=event_type,
        source=MLB,
        source_time=T,
        observed_at=T,
        importance=EventImportance(
            priority=DisplayPriority.NORMAL, urgency=UpdateUrgency.NORMAL, leverage=0, rarity=0, favorite_relevance=0
        ),
        payload=BaseballPlayPayload(
            inning=inning,
            phase=phase,
            description="play",
            rbi=rbi,
            away_score=away,
            home_score=home,
            scoring_impact=(
                scoring_impact(phase=phase, inning=inning, rbi=rbi, away_score=away, home_score=home)
                if impact
                else None
            ),
        ),
    )


_HR = BaseballGameEventType.HOME_RUN
_SINGLE = BaseballGameEventType.SINGLE
_BOTTOM, _TOP = InningPhase.BOTTOM, InningPhase.TOP


def test_a_walk_off_with_a_coherent_predecessor_and_final_is_a_decisive_late_winner() -> None:
    play = _play(_SINGLE, inning=9, phase=_BOTTOM, rbi=1, away=3, home=4)
    before = _state(inning=9, phase=_BOTTOM, away=3, home=3)
    after = _state(inning=9, phase=_BOTTOM, away=3, home=4)
    assert classify_baseball_play(play, before=before, after=after) == Classification(
        InterruptClass.DECISIVE_LATE_WINNER, ClassBasis.WALK_OFF
    )
    # The predecessor may be the break before the half: a leadoff walk-off homer in the 10th.
    homer = _play(_HR, inning=10, phase=_BOTTOM, rbi=1, away=3, home=4)
    before = _state(inning=10, phase=InningPhase.MIDDLE, away=3, home=3)
    after = _state(inning=10, phase=_BOTTOM, away=3, home=4)
    assert classify_baseball_play(homer, before=before, after=after).basis is ClassBasis.WALK_OFF


@pytest.mark.parametrize(
    "play, before, after",
    [
        # The successor doesn't show the score the walk-off left.
        (
            _play(_SINGLE, inning=9, phase=_BOTTOM, rbi=1, away=3, home=4),
            _state(away=3, home=3),
            _state(away=3, home=5),
        ),
        # No released predecessor.
        (_play(_SINGLE, inning=9, phase=_BOTTOM, rbi=1, away=3, home=4), None, _state(away=3, home=4)),
        # The predecessor's score was a stand-in.
        (
            _play(_SINGLE, inning=9, phase=_BOTTOM, rbi=1, away=3, home=4),
            _state(away=3, home=3, unknown=(BaseballField.SCORE,)),
            _state(away=3, home=4),
        ),
        # The predecessor comes after the play.
        (
            _play(_SINGLE, inning=8, phase=_BOTTOM, rbi=1, away=3, home=4),
            _state(inning=9, phase=_TOP, away=3, home=3),
            _state(inning=9, phase=_TOP, away=3, home=4),
        ),
        # The predecessor already shows the run: nothing was added between them.
        (
            _play(_HR, inning=3, phase=_TOP, rbi=1, away=2, home=1),
            _state(inning=3, phase=_TOP, away=2, home=1),
            _state(inning=3, phase=_TOP, away=2, home=1),
        ),
        # A side's score went down.
        (
            _play(_HR, inning=3, phase=_TOP, rbi=1, away=2, home=1),
            _state(inning=3, phase=_TOP, away=1, home=1),
            _state(inning=3, phase=_TOP, away=2, home=0),
        ),
        # The play's score wasn't given.
        (
            _play(_HR, inning=3, phase=_TOP, rbi=1, away=None, home=None),
            _state(inning=3, phase=_TOP),
            _state(inning=3, phase=_TOP),
        ),
    ],
)
def test_a_scoring_play_without_coherent_states_around_it_is_classified_routine(
    play: BaseballGameEvent, before: BaseballGameState | None, after: BaseballGameState
) -> None:
    assert classify_baseball_play(play, before=before, after=after) == Classification(
        InterruptClass.ROUTINE, ClassBasis.INCOHERENT
    )


@pytest.mark.parametrize(
    "play, before, after, expected",
    [
        (  # a tying single in the top of the 9th
            _play(_SINGLE, inning=9, phase=_TOP, rbi=1, away=3, home=3),
            _state(inning=9, phase=_TOP, away=2, home=3),
            _state(inning=9, phase=_TOP, away=3, home=3),
            Classification(InterruptClass.MAJOR, ClassBasis.LATE_TIE_OR_LEAD),
        ),
        (  # a solo homer in the 3rd that makes it 2-1
            _play(_HR, inning=3, phase=_TOP, rbi=1, away=2, home=1),
            _state(inning=3, phase=_TOP, away=1, home=1),
            _state(inning=3, phase=InningPhase.MIDDLE, away=2, home=1),
            Classification(InterruptClass.MAJOR, ClassBasis.CLOSE_HOME_RUN),
        ),
        (  # a grand slam that turns 0-3 into 4-3
            _play(_HR, inning=4, phase=_TOP, rbi=4, away=4, home=3),
            _state(inning=4, phase=_TOP, away=0, home=3),
            _state(inning=4, phase=_TOP, away=4, home=3),
            Classification(InterruptClass.MAJOR, ClassBasis.CLOSE_HOME_RUN),
        ),
        (  # a homer in a blowout
            _play(_HR, inning=3, phase=_TOP, rbi=1, away=7, home=1),
            _state(inning=3, phase=_TOP, away=6, home=1),
            _state(inning=3, phase=_TOP, away=7, home=1),
            Classification(InterruptClass.ROUTINE, ClassBasis.NOT_CLOSE),
        ),
        (  # an early go-ahead single isn't a home run, nor late
            _play(_SINGLE, inning=3, phase=_TOP, rbi=1, away=2, home=1),
            _state(inning=3, phase=_TOP, away=1, home=1),
            _state(inning=3, phase=_TOP, away=2, home=1),
            Classification(InterruptClass.ROUTINE, ClassBasis.NOT_CLOSE),
        ),
        (  # no run driven in
            _play(_SINGLE, inning=9, phase=_BOTTOM, rbi=0, away=3, home=3),
            _state(away=3, home=3),
            _state(away=3, home=3),
            Classification(InterruptClass.ROUTINE, ClassBasis.NOT_SCORING),
        ),
        (  # the scoring impact wasn't worked out
            _play(_HR, inning=9, phase=_BOTTOM, rbi=1, away=3, home=4, impact=False),
            _state(away=3, home=3),
            _state(away=3, home=4),
            Classification(InterruptClass.ROUTINE, ClassBasis.NOT_SCORING),
        ),
    ],
)
def test_close_and_late_scoring_plays_are_major_and_the_rest_routine(
    play: BaseballGameEvent, before: BaseballGameState, after: BaseballGameState, expected: Classification
) -> None:
    assert classify_baseball_play(play, before=before, after=after) == expected


# --- NFL -----------------------------------------------------------------------------------------------------------


def _football(
    *,
    period: int = 4,
    clock: int = 240,
    away: int = 17,
    home: int = 20,
    interval: FootballInterval | None = None,
    unknown: tuple[FootballField, ...] = (),
) -> FootballGameState:
    return FootballGameState(
        period=period,
        clock_seconds=clock,
        away_score=away,
        home_score=home,
        interval=interval,
        coverage=CoverageMap.of(dict.fromkeys(unknown, FieldCoverage.UNAVAILABLE)),
    )


def _strip(
    kind: FootballEventKind = FootballEventKind.PLAY, tone: PlayTone = PlayTone.SCORE, headline: str = "TOUCHDOWN CAR"
) -> FootballEvent:
    return FootballEvent(key="401:1", kind=kind, summary=PlaySummary(tone=tone, headline=headline))


_TOUCHDOWN = _strip()
_RULING = _strip(FootballEventKind.REVIEW, PlayTone.ALERT, "REVIEW: REVERSED")
_PICK = _strip(tone=PlayTone.TURNOVER, headline="INTERCEPTION")
_FINAL = FootballInterval.FINAL


@pytest.mark.parametrize(
    "event, before, after, expected",
    [
        (  # an overtime field goal that wins it
            _strip(headline="FIELD GOAL GOOD"),
            _football(period=5, clock=320, away=20, home=20),
            _football(period=5, clock=310, away=23, home=20, interval=_FINAL),
            Classification(InterruptClass.DECISIVE_LATE_WINNER, ClassBasis.GAME_ENDING_LEAD_CHANGE),
        ),
        (  # a late ruling that takes a touchdown off and the lead with it
            _RULING,
            _football(clock=120, away=24, home=20),
            _football(clock=120, away=17, home=20),
            Classification(InterruptClass.DECISIVE_LATE_WINNER, ClassBasis.REVERSAL),
        ),
        (  # a go-ahead touchdown at 4:00 that doesn't end the game is only major
            _TOUCHDOWN,
            _football(clock=240, away=17, home=20),
            _football(clock=236, away=23, home=20),
            Classification(InterruptClass.MAJOR, ClassBasis.LATE_ONE_SCORE),
        ),
        (  # a ruling that changes who leads at 5:01 isn't in the late window
            _RULING,
            _football(clock=301, away=24, home=20),
            _football(clock=301, away=17, home=20),
            Classification(InterruptClass.MAJOR, ClassBasis.LATE_ONE_SCORE),
        ),
        (  # with the clock unreported, a fourth-quarter lead change can't be called late
            _RULING,
            _football(clock=120, away=24, home=20, unknown=(FootballField.CLOCK,)),
            _football(clock=120, away=17, home=20, unknown=(FootballField.CLOCK,)),
            Classification(InterruptClass.MAJOR, ClassBasis.LATE_ONE_SCORE),
        ),
        (  # a turnover in the third quarter of a one-score game
            _PICK,
            _football(period=3, clock=200, away=17, home=20),
            _football(period=3, clock=195, away=17, home=20),
            Classification(InterruptClass.ROUTINE, ClassBasis.NOT_CLOSE),
        ),
        (  # a late turnover in a two-score game
            _PICK,
            _football(clock=200, away=6, home=20),
            _football(clock=195, away=6, home=20),
            Classification(InterruptClass.ROUTINE, ClassBasis.NOT_CLOSE),
        ),
        (  # a routine play
            _strip(tone=PlayTone.STANDARD, headline="4 YARD RUN"),
            _football(),
            _football(),
            Classification(InterruptClass.ROUTINE, ClassBasis.NOT_MAJOR),
        ),
    ],
)
def test_nfl_events_classify_by_lateness_closeness_and_whether_they_ended_it(
    event: FootballEvent, before: FootballGameState, after: FootballGameState, expected: Classification
) -> None:
    assert classify_football_event(event, before=before, after=after) == expected


@pytest.mark.parametrize(
    "before, after",
    [
        (None, _football()),
        (_football(unknown=(FootballField.SCORE,)), _football()),
        (_football(period=4), _football(period=3)),
        (_football(clock=200), _football(clock=210)),  # the clock ran backward
        (_football(away=17, home=20), _football(away=17, home=14)),  # points came off without a ruling
    ],
)
def test_an_nfl_event_without_coherent_states_around_it_is_routine(
    before: FootballGameState | None, after: FootballGameState
) -> None:
    assert classify_football_event(_TOUCHDOWN, before=before, after=after).basis is ClassBasis.INCOHERENT


def test_with_either_clock_unreported_the_order_rests_on_the_period() -> None:
    before = _football(clock=200, unknown=(FootballField.CLOCK,))
    after = _football(clock=210, away=23)
    assert classify_football_event(_TOUCHDOWN, before=before, after=after).basis is ClassBasis.LATE_ONE_SCORE


def test_the_late_window_and_one_score_margin_are_policy() -> None:
    policy = InterruptPolicy(nfl_late_clock=timedelta(minutes=6))
    result = classify_football_event(
        _RULING, before=_football(clock=330, away=24, home=20), after=_football(clock=330, away=17), policy=policy
    )
    assert result.event_class is InterruptClass.DECISIVE_LATE_WINNER


# --- NFL's win swing -----------------------------------------------------------------------------------------------


def _away(percent: float) -> WinProbability:
    return WinProbability(home=100.0 - percent, away=percent)


def _home_priced(price: float) -> WinProbability:
    """A reading as the exchange's quote gives it: the home side's price, in points."""
    home = price * 100.0
    return WinProbability(home=home, away=100.0 - home)


def test_a_win_swing_is_worth_1_2_whatever_the_leverage_on_its_predecessor_s_basis() -> None:
    proxy = interrupt_value(InterruptClass.WIN_SWING, predecessor=Leverage(g=0.1, status=ModelStatus.PROXY))
    assert proxy.z_pre == 1.0 and proxy.basis is LeverageBasis.PROXY and not proxy.action_evidence
    assert proxy.value == pytest.approx(1.2 * 1.0 * 0.65)  # 0.78: above the open bar, below a micro-break's
    bars = InterruptBars()
    assert bars.for_context(BarContext.OPEN) <= proxy.value < bars.for_context(BarContext.MICRO_BREAK)
    model = interrupt_value(InterruptClass.WIN_SWING, predecessor=_validated(0.1))
    assert model.z_pre == 1.0 and model.basis is LeverageBasis.MODEL
    assert interrupt_value(InterruptClass.WIN_SWING, predecessor=None).basis is LeverageBasis.NONE
    reweighted = interrupt_value(InterruptClass.WIN_SWING, predecessor=None, policy=InterruptPolicy(win_swing=1.0))
    assert reweighted.value == pytest.approx(0.65)


def test_a_win_swing_is_how_far_the_away_side_moved_either_way_and_none_without_both_readings() -> None:
    assert win_swing(_away(40.0), _away(52.5)) == 12.5
    assert win_swing(_away(52.5), _away(40.0)) == 12.5
    assert win_swing(None, _away(40.0)) is None and win_swing(_away(40.0), None) is None
    before, after = _home_priced(0.182), _home_priced(0.282)  # a ten-cent move, as the quotes' floats give it
    assert abs(after.away - before.away) < 10.0 and win_swing(before, after) == 10.0


def test_a_win_swing_says_so_in_a_decision() -> None:
    value = interrupt_value(InterruptClass.WIN_SWING, predecessor=None)
    assert event_words(PromotionKind.MAJOR, value) == "win swing"


_LONG_PASS = _strip(tone=PlayTone.STANDARD, headline="45 YARD PASS")
_EARLY = _football(period=1, clock=600, away=0, home=0)
_EARLY_AFTER = _football(period=1, clock=590, away=0, home=0)


@pytest.mark.parametrize(
    "event, before, after, swing, expected",
    [
        (  # a routine first-quarter play that moved the market ten points
            _LONG_PASS,
            _EARLY,
            _EARLY_AFTER,
            10.0,
            Classification(InterruptClass.WIN_SWING, ClassBasis.WIN_SWING),
        ),
        (  # a tenth short
            _LONG_PASS,
            _EARLY,
            _EARLY_AFTER,
            9.9,
            Classification(InterruptClass.ROUTINE, ClassBasis.NOT_MAJOR),
        ),
        (  # unread: as today
            _LONG_PASS,
            _EARLY,
            _EARLY_AFTER,
            None,
            Classification(InterruptClass.ROUTINE, ClassBasis.NOT_MAJOR),
        ),
        (  # a second-quarter pick that swings the game
            _PICK,
            _football(period=2, clock=400, away=7, home=7),
            _football(period=2, clock=395, away=7, home=7),
            18.0,
            Classification(InterruptClass.WIN_SWING, ClassBasis.WIN_SWING),
        ),
        (  # the same pick, a tenth short: early, it's routine
            _PICK,
            _football(period=2, clock=400, away=7, home=7),
            _football(period=2, clock=395, away=7, home=7),
            9.9,
            Classification(InterruptClass.ROUTINE, ClassBasis.NOT_CLOSE),
        ),
        (  # a late one-score touchdown that swung it outranks a major event
            _TOUCHDOWN,
            _football(clock=240, away=17, home=20),
            _football(clock=236, away=23, home=20),
            25.0,
            Classification(InterruptClass.WIN_SWING, ClassBasis.WIN_SWING),
        ),
        (  # a decisive late winner stays one, however far it swung
            _strip(headline="FIELD GOAL GOOD"),
            _football(period=5, clock=320, away=20, home=20),
            _football(period=5, clock=310, away=23, home=20, interval=_FINAL),
            45.0,
            Classification(InterruptClass.DECISIVE_LATE_WINNER, ClassBasis.GAME_ENDING_LEAD_CHANGE),
        ),
        (  # a late routine play that ends the game ahead isn't decisive: only a major event can be
            _LONG_PASS,
            _football(period=5, clock=320, away=20, home=20),
            _football(period=5, clock=310, away=26, home=20, interval=_FINAL),
            45.0,
            Classification(InterruptClass.WIN_SWING, ClassBasis.WIN_SWING),
        ),
        (  # a swing across states that don't line up is no evidence
            _LONG_PASS,
            _football(clock=200),
            _football(clock=210),
            30.0,
            Classification(InterruptClass.ROUTINE, ClassBasis.INCOHERENT),
        ),
        (  # nor without the state before it
            _LONG_PASS,
            None,
            _EARLY_AFTER,
            30.0,
            Classification(InterruptClass.ROUTINE, ClassBasis.INCOHERENT),
        ),
    ],
)
def test_a_play_that_moved_the_win_probability_ten_points_is_a_win_swing_whatever_the_clock(
    event: FootballEvent,
    before: FootballGameState | None,
    after: FootballGameState,
    swing: float | None,
    expected: Classification,
) -> None:
    assert classify_football_event(event, before=before, after=after, swing=swing) == expected


def test_a_win_swing_is_put_down_only_to_a_play_a_score_a_try_or_a_ruling() -> None:
    assert SWING_KINDS == {
        FootballEventKind.PLAY,
        FootballEventKind.SCORE,
        FootballEventKind.CONVERSION,
        FootballEventKind.REVIEW,
    }
    for kind in SWING_KINDS:
        swung = classify_football_event(_strip(kind, PlayTone.STANDARD), before=_EARLY, after=_EARLY_AFTER, swing=20.0)
        assert swung.event_class is InterruptClass.WIN_SWING
    for kind in set(FootballEventKind) - SWING_KINDS:  # a timeout, the officials', an injury, the two-minute warning
        stoppage = _strip(kind, PlayTone.ALERT, kind.value.upper())
        assert classify_football_event(stoppage, before=_EARLY, after=_EARLY_AFTER, swing=40.0) == Classification(
            InterruptClass.ROUTINE, ClassBasis.NOT_MAJOR
        )


def test_the_win_swing_s_threshold_is_policy() -> None:
    policy = InterruptPolicy(win_swing_points=15.0)
    assert InterruptPolicy().win_swing_points == 10.0
    result = classify_football_event(_LONG_PASS, before=_EARLY, after=_EARLY_AFTER, swing=14.9, policy=policy)
    assert result.event_class is InterruptClass.ROUTINE
    result = classify_football_event(_LONG_PASS, before=_EARLY, after=_EARLY_AFTER, swing=15.0, policy=policy)
    assert result.event_class is InterruptClass.WIN_SWING
