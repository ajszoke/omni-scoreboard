"""The card ladder, the non-base cap, leaving a break, and the ordinary cards' timing (`BOARD_POLICY.md` §6, §3.5)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta

import pytest

from omni.core.clock import MonotonicTime
from omni.core.enum import HomeAway, League
from omni.core.ids import LeagueScopedId, SourceRef
from omni.domain.baseball import PitchingFeatKind, PitchingFeatProgress
from omni.policy.interrupts import BarContext, InterruptClass, InterruptValue, LeverageBasis
from omni.policy.ladder import (
    CardTimingPolicy,
    LadderPolicy,
    LadderRefusal,
    NonBaseRun,
    OpenTime,
    PregameRamp,
    Rung,
    RungChoice,
    RungRefusal,
    Tenure,
    bid_reminder_due,
    choose_rung,
    favorite_status_due,
    final_gone,
    final_repeat_due,
    highlight_due,
    initial_final_permitted,
    pregame_due,
    pregame_every,
    pregame_permitted,
    red_zone_reminder_due,
    yield_after_break,
)
from omni.policy.promotion import (
    PromotionBroker,
    PromotionCandidate,
    PromotionKey,
    PromotionKind,
    PromotionMoment,
    PromotionRefusal,
)

S = timedelta(seconds=1)
MINUTE = timedelta(minutes=1)
ELSEWHERE = LeagueScopedId(League.MLB, SourceRef("mlb_statsapi"), "747")


def _m(seconds: float) -> MonotonicTime:
    return MonotonicTime(float(seconds))


# --- The non-base cap ----------------------------------------------------------------------------------------------


def test_four_initial_finals_in_a_row_are_capped_then_a_full_base_turn_is_reserved() -> None:
    # No live favorite, an eligible live base, and four initial ordered finals of 8 s each.
    run = NonBaseRun()
    admitted: list[float] = []
    t = 0.0
    finals = 4
    while True:
        due = {Rung.BASE} | ({Rung.INITIAL_RESULT} if finals else set())
        choice = choose_rung(due, run, _m(t))
        if choice.rung is not Rung.INITIAL_RESULT:
            break
        admitted.append(t)
        run = run.after(Rung.INITIAL_RESULT, _m(t))
        finals -= 1
        t += 8
    assert admitted == [0, 8, 16, 24]
    assert t == 32  # the run ends when the last admitted final does
    assert choice == RungChoice(rung=Rung.BASE_RECOVERY, reserved=True)
    run = run.after(Rung.BASE_RECOVERY, _m(32))
    assert run == NonBaseRun()
    reserved_until = _m(32) + LadderPolicy().base_turn
    assert reserved_until == _m(44)


def test_promotions_and_finals_share_the_cap_but_finals_never_wait_on_the_cooldown() -> None:
    open_rotation = PromotionMoment()
    broker = PromotionBroker[str]()

    def promotion(name: str, at: float) -> PromotionCandidate[str]:
        value = InterruptValue(event_class=InterruptClass.MAJOR, value=0.9, z_pre=1.0, basis=LeverageBasis.PROXY)
        key = PromotionKey(ELSEWHERE, name)
        return PromotionCandidate(key=key, kind=PromotionKind.MAJOR, eligible=_m(at), card=name, value=value)

    broker.offer(promotion("homer", 0), _m(0))
    decision = broker.decide(open_rotation, _m(0))
    assert decision.start is not None and broker.started(decision.start, open_rotation, _m(0)).confirmed
    run = NonBaseRun().after(Rung.PROMOTION, _m(0))
    broker.offer(promotion("double", 12), _m(12))
    for t in (12, 20, 28):  # the promotion ran 0-12; ordered finals follow while the cooldown holds the next one
        assert broker.decide(open_rotation, _m(t)).reason is PromotionRefusal.COOLDOWN
        assert choose_rung({Rung.INITIAL_RESULT, Rung.BASE}, run, _m(t)).rung is Rung.INITIAL_RESULT
        run = run.after(Rung.INITIAL_RESULT, _m(t))
    assert choose_rung({Rung.INITIAL_RESULT, Rung.BASE}, run, _m(36)).rung is Rung.BASE_RECOVERY


def test_once_capped_no_non_base_card_is_admitted_while_a_base_card_waits() -> None:
    run = NonBaseRun(_m(0))
    choice = choose_rung({Rung.PROMOTION, Rung.AUXILIARY, Rung.RESCUE}, run, _m(30))
    assert choice.rung is Rung.BASE_RECOVERY and choice.reserved
    assert choice.refused == (
        RungRefusal(Rung.PROMOTION, LadderRefusal.NON_BASE_CAP),
        RungRefusal(Rung.AUXILIARY, LadderRefusal.NON_BASE_CAP),
    )


def test_device_controls_go_first_even_past_the_cap() -> None:
    run = NonBaseRun(_m(0))
    choice = choose_rung({Rung.CONTROL, Rung.PROMOTION, Rung.BASE}, run, _m(40))
    assert choice.rung is Rung.CONTROL and not choice.reserved
    assert choice.refused == (
        RungRefusal(Rung.PROMOTION, LadderRefusal.NON_BASE_CAP),
        RungRefusal(Rung.BASE, LadderRefusal.OUTRANKED),
    )
    assert run.after(Rung.CONTROL, _m(40)) == run  # a control neither ends nor extends the run


def test_with_no_base_card_waiting_the_cap_has_nothing_to_reserve() -> None:
    choice = choose_rung({Rung.INITIAL_RESULT, Rung.AUXILIARY}, NonBaseRun(_m(0)), _m(90))
    assert choice.rung is Rung.INITIAL_RESULT
    assert choice.refused == (RungRefusal(Rung.AUXILIARY, LadderRefusal.OUTRANKED),)


def test_the_ladder_takes_the_first_rung_due_in_priority_order() -> None:
    assert choose_rung([Rung.BASE, Rung.AUXILIARY, Rung.PROMOTION, Rung.RESCUE], NonBaseRun(), _m(0)).rung is (
        Rung.RESCUE
    )
    assert choose_rung([Rung.BASE, Rung.PROMOTION], NonBaseRun(), _m(0)).rung is Rung.PROMOTION
    assert choose_rung([], NonBaseRun(), _m(0)) == RungChoice(rung=None)


def test_a_non_base_card_extends_a_run_without_restarting_it() -> None:
    run = NonBaseRun().after(Rung.PROMOTION, _m(5)).after(Rung.AUXILIARY, _m(17))
    assert run == NonBaseRun(_m(5))
    assert not run.capped(_m(34)) and run.capped(_m(35))
    assert run.capped(_m(25), LadderPolicy(non_base_cap=20 * S))


def test_the_ladders_rungs_say_which_are_base() -> None:
    assert {rung for rung in Rung if rung.base} == {Rung.BASE_RECOVERY, Rung.RESCUE, Rung.BASE}
    assert {rung for rung in Rung if rung.non_base} == {Rung.PROMOTION, Rung.INITIAL_RESULT, Rung.AUXILIARY}


# --- Leaving a break -----------------------------------------------------------------------------------------------


def test_when_a_break_closes_at_104_a_break_only_guest_yields_at_108_and_a_base_recovery_stays_through_112() -> None:
    closed = _m(104)
    guest = yield_after_break(tenure=Tenure.BREAK_GUEST, started=_m(100), minimum=8 * S, turn=12 * S, closed=closed)
    recovery = yield_after_break(tenure=Tenure.FULL_TURN, started=_m(100), minimum=8 * S, turn=12 * S, closed=closed)
    assert (guest, recovery) == (_m(108), _m(112))


def test_a_break_guest_past_its_minimum_yields_at_the_close_but_never_runs_past_its_turn() -> None:
    def guest(closed: float) -> MonotonicTime:
        return yield_after_break(
            tenure=Tenure.BREAK_GUEST, started=_m(100), minimum=8 * S, turn=12 * S, closed=_m(closed)
        )

    assert guest(110) == _m(110)
    assert guest(130) == _m(112)


# --- Pregames ------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "until_start, every",
    [
        (timedelta(minutes=91), None),
        (timedelta(minutes=90), 300 * S),
        (timedelta(minutes=31), 300 * S),
        (timedelta(minutes=30), 120 * S),
        (timedelta(minutes=10), 60 * S),
        (timedelta(minutes=3), 60 * S),
        (timedelta(minutes=2), 30 * S),
        (S, 30 * S),
        (timedelta(0), None),
        (-MINUTE, None),
    ],
)
def test_a_pregame_is_due_more_often_as_its_start_nears(until_start: timedelta, every: timedelta | None) -> None:
    assert pregame_every(until_start) == every


def test_a_pregame_is_due_once_its_ramps_interval_has_passed() -> None:
    assert pregame_due(until_start=timedelta(minutes=20), since_last=None)
    assert not pregame_due(until_start=timedelta(minutes=20), since_last=119 * S)
    assert pregame_due(until_start=timedelta(minutes=20), since_last=120 * S)
    assert not pregame_due(until_start=timedelta(hours=2), since_last=None)


def test_with_live_games_a_pregame_takes_one_slot_per_four_base_turns_and_only_off_a_favorites_action() -> None:
    assert pregame_permitted(live_games=False, base_turns_since=0, context=BarContext.ACTION)
    assert pregame_permitted(live_games=True, base_turns_since=None, context=BarContext.OPEN)
    assert not pregame_permitted(live_games=True, base_turns_since=3, context=BarContext.OPEN)
    assert pregame_permitted(live_games=True, base_turns_since=4, context=BarContext.MICRO_BREAK)
    assert not pregame_permitted(live_games=True, base_turns_since=4, context=BarContext.ACTION)


# --- Finals --------------------------------------------------------------------------------------------------------


def test_while_a_favorite_acts_an_unrelated_final_waits_for_a_break() -> None:
    assert not initial_final_permitted(context=BarContext.ACTION, related=False)
    assert initial_final_permitted(context=BarContext.ACTION, related=True)
    assert initial_final_permitted(context=BarContext.MICRO_BREAK, related=False)
    assert CardTimingPolicy().final == 8 * S


@pytest.mark.parametrize(
    "since, repeats, due",
    [
        (timedelta(minutes=4, seconds=59), 0, False),
        (timedelta(minutes=5), 0, True),
        (timedelta(minutes=10), 1, False),
        (timedelta(minutes=15), 1, True),
        (timedelta(minutes=20), 2, False),
        (timedelta(minutes=30), 1, False),  # gone 30 minutes after confirmation
    ],
)
def test_a_final_repeats_at_most_twice_at_5_and_15_minutes(since: timedelta, repeats: int, due: bool) -> None:
    assert final_repeat_due(since_confirmed=since, repeats_shown=repeats) is due


def test_a_final_is_gone_30_minutes_after_confirmation() -> None:
    assert not final_gone(timedelta(minutes=29, seconds=59)) and final_gone(timedelta(minutes=30))


# --- Reminders, statuses and highlights ----------------------------------------------------------------------------


def _bid(innings: int) -> PitchingFeatProgress:
    return PitchingFeatProgress(side=HomeAway.HOME, kind=PitchingFeatKind.NO_HITTER, completed_innings=innings)


def test_a_no_hit_bid_reminds_after_six_innings_when_new_then_every_60_open_seconds_three_at_most() -> None:
    assert not bid_reminder_due(_bid(5), shown=0, open_since_last=timedelta(0))
    assert bid_reminder_due(_bid(6), shown=0, open_since_last=timedelta(0))
    assert not bid_reminder_due(_bid(7), shown=1, open_since_last=59 * S)
    assert bid_reminder_due(_bid(7), shown=1, open_since_last=60 * S)
    assert bid_reminder_due(_bid(8), shown=2, open_since_last=90 * S)
    assert not bid_reminder_due(_bid(8), shown=3, open_since_last=600 * S)


def test_a_late_red_zone_drive_reminds_once() -> None:
    assert red_zone_reminder_due(shown=0) and not red_zone_reminder_due(shown=1)


def test_a_favorites_delay_status_shows_at_most_once_per_120_open_seconds() -> None:
    assert favorite_status_due(open_since_last=None)
    assert not favorite_status_due(open_since_last=119 * S)
    assert favorite_status_due(open_since_last=120 * S)


def test_a_highlight_shows_at_most_once_per_90_s_and_twice_a_game() -> None:
    assert highlight_due(shown_this_game=0, since_last=None)
    assert not highlight_due(shown_this_game=1, since_last=89 * S)
    assert highlight_due(shown_this_game=1, since_last=90 * S)
    assert not highlight_due(shown_this_game=2, since_last=600 * S)


def test_open_time_counts_only_while_no_favorite_is_in_action() -> None:
    count = OpenTime.mark(open_now=True, at=_m(0))
    count = count.update(open_now=True, at=_m(10))  # still open: nothing changes
    count = count.update(open_now=False, at=_m(40))  # a favorite resumed
    assert count.total(_m(100)) == 40 * S
    count = count.update(open_now=False, at=_m(100))
    count = count.update(open_now=True, at=_m(130))
    assert count.total(_m(150)) == 60 * S
    assert OpenTime.mark(open_now=False, at=_m(0)).total(_m(50)) == timedelta(0)


@pytest.mark.parametrize(
    "build, message",
    [
        (lambda: LadderPolicy(non_base_cap=timedelta(0)), "positive"),
        (lambda: CardTimingPolicy(pregame_ramps=()), "ramps"),
        (lambda: CardTimingPolicy(pregame_ramps=(PregameRamp(MINUTE, 30 * S),)), "ramps"),
        (lambda: CardTimingPolicy(pregame_ramps=(PregameRamp(timedelta(0), S), PregameRamp(MINUTE, S))), "ramps"),
        (lambda: CardTimingPolicy(final_repeats=(timedelta(minutes=15), timedelta(minutes=5))), "repeats"),
        (lambda: CardTimingPolicy(final_repeats=(timedelta(minutes=45),)), "repeats"),
    ],
)
def test_ladder_values_refuse_what_cant_be(build: Callable[[], object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        build()
