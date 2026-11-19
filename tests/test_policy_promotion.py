"""The promotion broker: the cooldown, the 24 s hold, lifetimes and dedupe (`BOARD_POLICY.md` §4), driven by a small
fake timeline that asks it at every second the board is free, the way the board asks at each legal decision."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone

import pytest

from omni.core.clock import ClockReading, MonotonicTime
from omni.core.enum import League
from omni.core.ids import LeagueScopedId, SourceRef
from omni.domain.breaks import BreakConfidence, BreakEvidence, BreakId, BreakKind
from omni.policy.interrupts import (
    BarContext,
    FavoriteActivity,
    InterruptClass,
    InterruptValue,
    LeverageBasis,
    Stakes,
    interrupt_value,
)
from omni.policy.leases import FavoriteMoment, LeaseBook
from omni.policy.leverage import Leverage, ModelStatus
from omni.policy.promotion import (
    Airtime,
    CandidateOrigin,
    HoldEpisode,
    OfferOutcome,
    PromotionBroker,
    PromotionCandidate,
    PromotionCooldown,
    PromotionDecision,
    PromotionKey,
    PromotionKind,
    PromotionMoment,
    PromotionPolicy,
    PromotionRefusal,
)

T0 = datetime(2026, 9, 20, 23, 0, tzinfo=timezone.utc)
S = timedelta(seconds=1)
FAVORITE = LeagueScopedId(League.MLB, SourceRef("mlb_statsapi"), "746")  # the Braves, say
ELSEWHERE = LeagueScopedId(League.MLB, SourceRef("mlb_statsapi"), "747")
NBA_GAME = LeagueScopedId(League.NBA, SourceRef("balldontlie"), "18")
NFL_FAVORITE = LeagueScopedId(League.NFL, SourceRef("nflmeta"), "401")

IN_ACTION = PromotionMoment((FavoriteMoment(activity=FavoriteActivity.IN_ACTION),))
OPEN_ROTATION = PromotionMoment()  # no live favorite


def _m(seconds: float) -> MonotonicTime:
    return MonotonicTime(float(seconds))


def _major(
    name: str,
    worth: float,
    *,
    eligible: float = 0,
    action: bool = True,
    contest: LeagueScopedId = ELSEWHERE,
    revision: int = 0,
    origin: CandidateOrigin = CandidateOrigin.LIVE,
) -> PromotionCandidate[str]:
    """A scored event worth ``worth``; ``action`` says whether its leverage came from a validated model."""
    value = InterruptValue(
        event_class=InterruptClass.DECISIVE_LATE_WINNER,
        value=worth,
        z_pre=1.0,
        basis=LeverageBasis.MODEL if action else LeverageBasis.PROXY,
    )
    return PromotionCandidate(
        key=PromotionKey(contest, f"play {name}"),
        kind=PromotionKind.MAJOR,
        eligible=_m(eligible),
        card=name,
        value=value,
        revision=revision,
        origin=origin,
    )


def _unscored(
    name: str, kind: PromotionKind = PromotionKind.SNAPSHOT, *, eligible: float = 0
) -> PromotionCandidate[str]:
    return PromotionCandidate(key=PromotionKey(NBA_GAME, name), kind=kind, eligible=_m(eligible), card=name)


Shows = list[tuple[str, float, float]]


def _air(
    broker: PromotionBroker[str],
    moment_at: Callable[[int], PromotionMoment],
    *,
    until: int = 130,
    tick: Callable[[int], None] = lambda t: None,
    on_start: Callable[[int], None] = lambda t: None,
) -> tuple[Shows, list[tuple[str, int]]]:
    """Each second: run ``tick`` (offers, releases), then, when no promotion is on air, ask the broker and confirm
    what it cues at once. Returns the promotions aired and the candidates retired, with when."""
    shows: Shows = []
    retired: list[tuple[str, int]] = []
    free_at = 0.0
    for t in range(until + 1):
        tick(t)
        if t < free_at:
            continue
        moment = moment_at(t)
        decision = broker.decide(moment, _m(t))
        retired.extend((candidate.card, t) for candidate in decision.retired)
        if decision.start is not None:
            assert broker.started(decision.start, moment, _m(t)).confirmed
            on_start(t)
            full = broker.policy.airtime(decision.start.kind).full.total_seconds()
            shows.append((decision.start.card, t, t + full))
            free_at = t + full
    return shows, retired


def _offers(*batches: tuple[int, PromotionCandidate[str]], broker: PromotionBroker[str]) -> Callable[[int], None]:
    def tick(t: int) -> None:
        for when, candidate in batches:
            if when == t:
                broker.offer(candidate, _m(t))

    return tick


def _break(kind: BreakKind, contest: LeagueScopedId, at: float) -> BreakEvidence:
    received = ClockReading(T0 + at * S, _m(at))
    return BreakEvidence(
        id=BreakId(contest, kind, f"@{at}"), received=received, confidence=BreakConfidence.EXPLICIT, onset=received.utc
    )


# --- Reference timing ----------------------------------------------------------------------------------------------


def test_a_mound_visit_releasing_at_18_takes_an_action_qualified_cut_in_at_18_to_30() -> None:
    broker = PromotionBroker[str]()
    book = LeaseBook()

    def tick(t: int) -> None:
        if t == 0:
            broker.offer(_major("walk-off", 1.2), _m(0))
        if t == 18:
            book.observe(_break(BreakKind.MLB_MOUND_VISIT, FAVORITE, 18), released=_m(18), poll_interval=15 * S)

    def hosted(t: int) -> None:
        book.hosted(FAVORITE, _m(t))

    shows, _ = _air(broker, lambda t: PromotionMoment((book.favorite(FAVORITE, _m(t)),)), tick=tick, on_start=hosted)
    assert shows == [("walk-off", 18, 30)]
    assert broker.cooldown == PromotionCooldown(_m(18))  # charged from 18, not from the hold's deadline


def test_with_no_break_a_1_2_event_starts_at_24_and_a_0_78_one_never_starts_in_action() -> None:
    strong = PromotionBroker[str]()
    shows, _ = _air(strong, lambda t: IN_ACTION, tick=_offers((0, _major("winner", 1.2)), broker=strong))
    assert shows == [("winner", 24, 36)]

    nba = interrupt_value(
        InterruptClass.DECISIVE_LATE_WINNER, predecessor=Leverage(g=1.0, status=ModelStatus.PROXY), stakes=Stakes()
    )
    buzzer = PromotionCandidate(
        key=PromotionKey(NBA_GAME, "play 512"), kind=PromotionKind.MAJOR, eligible=_m(0), card="buzzer", value=nba
    )
    weak = PromotionBroker[str]()
    shows, retired = _air(weak, lambda t: IN_ACTION, tick=_offers((0, buzzer), broker=weak))
    assert shows == []
    assert retired == [("buzzer", 109)]  # its latest start was +108; then it goes to the results queue


def test_four_promoted_events_in_open_rotation_promote_at_0_and_60_only() -> None:
    broker = PromotionBroker[str]()
    events = [
        (0, _major(name, worth, action=False)) for name, worth in (("a", 0.9), ("b", 0.8), ("c", 0.7), ("d", 0.6))
    ]
    shows, retired = _air(broker, lambda t: OPEN_ROTATION, tick=_offers(*events, broker=broker))
    assert shows == [("a", 0, 12), ("b", 60, 72)]
    assert sorted(card for card, _ in retired) == ["c", "d"]
    assert broker.policy.hard_end_of(events[0][1]) == _m(120)


def test_the_same_events_during_continuous_solo_favorite_action_promote_at_27_and_87() -> None:
    broker = PromotionBroker[str]()
    events = [(3, _major(name, worth, eligible=3)) for name, worth in (("a", 1.5), ("b", 1.4), ("c", 1.3), ("d", 1.2))]
    shows, retired = _air(broker, lambda t: IN_ACTION, tick=_offers(*events, broker=broker))
    assert shows == [("a", 27, 39), ("b", 87, 99)]  # the hold ends at 27, and its members never get a second one
    assert broker.policy.hard_end_of(events[0][1]) == _m(123)
    assert sorted(retired) == [("c", 112), ("d", 112)]


def test_a_better_candidate_inside_the_hold_keeps_the_episodes_deadline() -> None:
    broker = PromotionBroker[str]()
    tick = _offers((0, _major("first", 1.15)), (20, _major("better", 1.3, eligible=20)), broker=broker)
    tick(0)
    assert broker.hold(_m(0)) == HoldEpisode(_m(0), _m(24))
    shows, _ = _air(broker, lambda t: IN_ACTION, tick=lambda t: tick(t) if t else None)
    assert shows == [("better", 24, 36), ("first", 84, 96)]


def test_a_better_candidate_must_fit_its_own_lifetime_to_replace_the_choice() -> None:
    broker = PromotionBroker[str]()
    broker.offer(_major("first", 1.15), _m(0))
    # Released long ago: its latest start passed before the offer.
    assert broker.offer(_major("stale", 1.5, eligible=-90), _m(20)).refusal is PromotionRefusal.EXPIRED
    # Its latest start (+108) is 23: it can't start at the deadline, so it's retired and the first one starts.
    assert broker.offer(_major("late", 1.4, eligible=-85), _m(20)).outcome is OfferOutcome.ACCEPTED
    decision = broker.decide(IN_ACTION, _m(24))
    assert decision.start is not None and decision.start.card == "first"
    assert [candidate.card for candidate in decision.retired] == ["late"]


def test_a_hold_without_a_better_moment_waits_holding_until_its_deadline() -> None:
    broker = PromotionBroker[str]()
    broker.offer(_major("winner", 1.2), _m(0))
    decision = broker.decide(IN_ACTION, _m(23))
    assert decision.start is None and decision.reason is PromotionRefusal.HOLDING
    assert decision.context is BarContext.ACTION and decision.bar == 1.10
    assert broker.hold(_m(24)) is None
    assert broker.next_deadline(_m(0)) == _m(24)


# --- A cue isn't a grant -------------------------------------------------------------------------------------------


def _cued_into_a_timeout() -> tuple[PromotionBroker[str], LeaseBook, PromotionCandidate[str]]:
    broker = PromotionBroker[str]()
    book = LeaseBook()
    broker.offer(_major("pick six", 0.9, eligible=95, action=False), _m(95))
    book.observe(_break(BreakKind.NFL_TEAM_TIMEOUT, NFL_FAVORITE, 100), released=_m(100), poll_interval=10 * S)
    decision = broker.decide(PromotionMoment((book.favorite(NFL_FAVORITE, _m(100)),)), _m(100))
    assert decision.start is not None
    return broker, book, decision.start


def test_a_break_that_ends_before_the_cued_cards_first_frame_rejects_it_and_spends_nothing() -> None:
    broker, book, cued = _cued_into_a_timeout()
    book.action(NFL_FAVORITE, _m(100.5))
    moment = PromotionMoment((book.favorite(NFL_FAVORITE, _m(101)),))
    assert broker.started(cued, moment, _m(101)).refusal is PromotionRefusal.BREAK_OVER
    assert broker.cooldown == PromotionCooldown()
    lease = book.lease(NFL_FAVORITE)
    assert lease is not None and lease.guests == 0
    assert broker.pending() == (cued,)  # it may still find another break before its expiry


def test_a_correction_before_the_cued_cards_first_frame_rejects_it() -> None:
    broker, book, cued = _cued_into_a_timeout()
    corrected = _major("pick six", 0.95, eligible=100, action=False, revision=1)
    result = broker.offer(corrected, _m(100.2))
    assert result.outcome is OfferOutcome.CORRECTED
    assert result.standing is not None and result.standing.eligible == _m(95)  # a correction doesn't extend a lifetime
    moment = PromotionMoment((book.favorite(NFL_FAVORITE, _m(100.5)),))
    assert broker.started(cued, moment, _m(100.5)).refusal is PromotionRefusal.SUPERSEDED
    assert broker.cooldown == PromotionCooldown()


def test_a_cue_whose_latest_start_passes_before_its_first_frame_is_rejected() -> None:
    broker = PromotionBroker[str]()
    broker.offer(_major("homer", 0.6, action=False), _m(0))
    decision = broker.decide(OPEN_ROTATION, _m(108))
    assert decision.start is not None
    assert broker.started(decision.start, OPEN_ROTATION, _m(108.5)).refusal is PromotionRefusal.EXPIRED
    assert broker.cooldown.last_start is None


def test_a_confirmed_cue_needs_only_its_break_to_still_be_on() -> None:
    broker, book, cued = _cued_into_a_timeout()
    later = PromotionMoment((book.favorite(NFL_FAVORITE, _m(115)),))  # the room shrank below the card
    assert later.room is not None and later.room < 12 * S
    assert broker.started(cued, later, _m(115)).confirmed
    assert broker.started(cued, later, _m(115)).refusal is PromotionRefusal.NOT_PENDING


def test_a_confirmed_start_is_rejected_below_the_bar_the_moment_now_sets() -> None:
    broker = PromotionBroker[str]()
    broker.offer(_major("homer", 0.6, action=False), _m(0))
    decision = broker.decide(OPEN_ROTATION, _m(0))
    assert decision.start is not None
    micro = PromotionMoment((FavoriteMoment(activity=FavoriteActivity.MICRO_BREAK, room=30 * S),))
    assert broker.started(decision.start, micro, _m(0.2)).refusal is PromotionRefusal.BELOW_BAR


# --- Offers --------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "origin, refusal",
    [(CandidateOrigin.BASELINE, PromotionRefusal.BASELINE), (CandidateOrigin.CATCH_UP, PromotionRefusal.CATCH_UP)],
)
def test_a_first_baseline_or_an_outage_catch_up_never_alerts(
    origin: CandidateOrigin, refusal: PromotionRefusal
) -> None:
    broker = PromotionBroker[str]()
    result = broker.offer(_major("walk-off", 1.5, origin=origin), _m(0))
    assert result.outcome is OfferOutcome.REFUSED and result.refusal is refusal
    assert broker.pending() == () and broker.hold(_m(0)) is None


def test_a_scoring_summary_merges_with_its_play_and_an_older_revision_is_stale() -> None:
    broker = PromotionBroker[str]()
    play = _major("homer", 1.2, revision=1)
    broker.offer(play, _m(0))
    summary = PromotionCandidate(
        key=play.key, kind=PromotionKind.MAJOR, eligible=_m(1), card="summary", value=play.value, revision=1
    )
    merged = broker.offer(summary, _m(1))
    assert merged.outcome is OfferOutcome.MERGED and merged.standing == play
    stale = broker.offer(_major("homer", 1.2, revision=0), _m(2))
    assert stale.refusal is PromotionRefusal.STALE_REVISION and stale.standing == play
    assert broker.pending() == (play,)


def test_a_shown_play_is_refused_until_well_past_its_hard_end() -> None:
    broker = PromotionBroker[str]()
    broker.offer(_major("homer", 0.6, action=False), _m(0))
    decision = broker.decide(OPEN_ROTATION, _m(0))
    assert decision.start is not None and broker.started(decision.start, OPEN_ROTATION, _m(0)).confirmed
    again = _major("homer", 0.6, action=False, eligible=500, revision=2)
    assert broker.offer(again, _m(500)).refusal is PromotionRefusal.ALREADY_SHOWN
    # Remembered for 15 minutes past its hard end at 120.
    replay = _major("homer", 0.6, action=False, eligible=1020)
    assert broker.offer(replay, _m(1020)).standing == replay


def test_a_retired_play_is_refused() -> None:
    broker = PromotionBroker[str]()
    broker.offer(_major("homer", 0.6, action=False), _m(0))
    assert [c.card for c in broker.decide(IN_ACTION, _m(109)).retired] == ["homer"]
    assert broker.offer(_major("homer", 0.6, action=False, eligible=110), _m(110)).refusal is PromotionRefusal.RETIRED


def test_an_event_below_every_bar_never_waits() -> None:
    broker = PromotionBroker[str]()
    assert broker.offer(_major("solo", 0.3), _m(0)).refusal is PromotionRefusal.BELOW_EVERY_BAR
    assert broker.next_deadline(_m(0)) is None


def test_a_correction_below_every_bar_drops_the_waiting_one() -> None:
    broker = PromotionBroker[str]()
    broker.offer(_major("homer", 0.9, action=False), _m(0))
    result = broker.offer(_major("homer", 0.2, action=False, revision=1), _m(5))
    assert result.refusal is PromotionRefusal.BELOW_EVERY_BAR and broker.pending() == ()


def test_a_muted_contests_waiting_candidates_leave_at_once() -> None:
    broker = PromotionBroker[str]()
    broker.offer(_major("homer", 0.9), _m(0))
    broker.offer(_major("other", 0.9, contest=FAVORITE), _m(0))
    assert [c.card for c in broker.withdraw(ELSEWHERE)] == ["homer"]
    assert [c.card for c in broker.pending()] == ["other"]


# --- Snapshots and reminders ---------------------------------------------------------------------------------------


def test_a_snapshot_never_plays_into_action_and_waits_for_room_in_a_break() -> None:
    broker = PromotionBroker[str]()
    broker.offer(_unscored("close game"), _m(0))
    assert broker.hold(_m(0)) is None  # an unscored card holds nothing
    assert broker.decide(IN_ACTION, _m(1)).reason is PromotionRefusal.BREAK_ONLY
    tight = PromotionMoment((FavoriteMoment(activity=FavoriteActivity.MICRO_BREAK, room=7 * S),))
    assert broker.decide(tight, _m(2)).reason is PromotionRefusal.NO_ROOM
    roomy = PromotionMoment((FavoriteMoment(activity=FavoriteActivity.MICRO_BREAK, room=8 * S),))
    decision = broker.decide(roomy, _m(3))
    assert decision.start is not None and decision.start.card == "close game"


def test_a_reminder_must_start_within_60_s() -> None:
    broker = PromotionBroker[str]()
    reminder = _unscored("bid", PromotionKind.REMINDER)
    broker.offer(reminder, _m(0))
    assert broker.policy.latest_start(reminder) == _m(60)
    assert broker.policy.airtime(PromotionKind.REMINDER) == Airtime(8 * S, 6 * S)
    assert [c.card for c in broker.decide(IN_ACTION, _m(61)).retired] == ["bid"]


def test_scored_events_go_before_unscored_cards_then_by_expiry_age_and_id() -> None:
    broker = PromotionBroker[str]()
    for candidate in (
        _unscored("snapshot"),
        _major("later", 0.9, eligible=5, action=False),
        _major("b", 0.9, action=False),
        _major("a", 0.9, action=False),
        _major("best", 1.0, eligible=6, action=False),
    ):
        broker.offer(candidate, _m(6))
    assert [c.card for c in broker.pending()] == ["best", "a", "b", "later", "snapshot"]


# --- Moments -------------------------------------------------------------------------------------------------------


def test_one_favorite_in_action_leaves_no_room_whatever_the_others_break() -> None:
    moment = PromotionMoment(
        (
            FavoriteMoment(activity=FavoriteActivity.MICRO_BREAK, room=30 * S),
            FavoriteMoment(activity=FavoriteActivity.IN_ACTION),
        )
    )
    assert moment.context is BarContext.ACTION and moment.room == timedelta(0) and not moment.ideal_for(8 * S)


def test_with_two_favorites_in_breaks_the_shorter_opportunity_limits_a_cut_in() -> None:
    moment = PromotionMoment(
        (
            FavoriteMoment(activity=FavoriteActivity.ORDINARY_BREAK, room=90 * S),
            FavoriteMoment(activity=FavoriteActivity.MICRO_BREAK, room=10 * S),
            FavoriteMoment(activity=FavoriteActivity.ORDINARY_BREAK, room=None),
        )
    )
    assert moment.context is BarContext.MICRO_BREAK and moment.room == 10 * S
    assert not moment.ideal_for(12 * S) and moment.ideal_for(8 * S)
    weather = PromotionMoment((FavoriteMoment(activity=FavoriteActivity.ORDINARY_BREAK, room=None),))
    assert weather.room is None and weather.ideal_for(timedelta(minutes=5))


def test_in_breaks_without_room_a_break_only_event_waits_and_an_action_qualified_one_cuts_in_after_the_hold() -> None:
    broker = PromotionBroker[str]()
    broker.offer(_major("homer", 0.9, action=False), _m(0))
    broker.offer(_major("winner", 1.2), _m(0))
    closing = PromotionMoment((FavoriteMoment(activity=FavoriteActivity.ORDINARY_BREAK, room=4 * S),))
    early = broker.decide(closing, _m(10))
    assert early.start is None and [r.reason for r in early.refusals] == [
        PromotionRefusal.HOLDING,
        PromotionRefusal.NO_ROOM,
    ]
    late = broker.decide(closing, _m(24))
    assert late.start is not None and late.start.card == "winner"


def test_a_proxy_event_above_the_action_bar_still_cant_cut_into_action() -> None:
    broker = PromotionBroker[str]()
    broker.offer(_major("buzzer", 1.3, action=False), _m(0))
    assert broker.decide(IN_ACTION, _m(30)).reason is PromotionRefusal.BREAK_ONLY


def test_below_the_bar_an_event_waits_for_a_lower_one() -> None:
    broker = PromotionBroker[str]()
    broker.offer(_major("walk-off", 0.99), _m(0))
    assert broker.decide(IN_ACTION, _m(30)).reason is PromotionRefusal.BELOW_BAR
    micro = PromotionMoment((FavoriteMoment(activity=FavoriteActivity.MICRO_BREAK, room=30 * S),))
    decision = broker.decide(micro, _m(31))
    assert decision.start is not None and decision.reason is None


# --- The cooldown --------------------------------------------------------------------------------------------------


def test_the_cooldown_survives_a_config_reload_as_a_value() -> None:
    first = PromotionBroker[str]()
    first.offer(_major("a", 0.9, action=False), _m(0))
    decision = first.decide(OPEN_ROTATION, _m(10))
    assert decision.start is not None and first.started(decision.start, OPEN_ROTATION, _m(10)).confirmed
    reloaded = PromotionBroker[str](PromotionPolicy(), cooldown=first.cooldown)
    reloaded.offer(_major("b", 0.9, action=False, eligible=20), _m(20))
    assert reloaded.decide(OPEN_ROTATION, _m(20)).reason is PromotionRefusal.COOLDOWN
    assert reloaded.next_deadline(_m(20)) == _m(70)
    assert reloaded.decide(OPEN_ROTATION, _m(70)).start is not None


def test_an_empty_broker_waits_on_nothing() -> None:
    broker = PromotionBroker[str]()
    decision: PromotionDecision[str] = broker.decide(OPEN_ROTATION, _m(0))
    assert decision.reason is PromotionRefusal.NOTHING_PENDING and decision.start is None
    assert broker.next_deadline(_m(0)) is None
    assert broker.started(_major("ghost", 1.0), OPEN_ROTATION, _m(0)).refusal is PromotionRefusal.NOT_PENDING
    assert PromotionCooldown().ready_at(60 * S) is None and not PromotionCooldown().blocks(_m(0), 60 * S)


def test_the_next_deadline_is_the_latest_start_once_hold_and_cooldown_have_passed() -> None:
    broker = PromotionBroker[str]()
    broker.offer(_major("winner", 0.9, action=False), _m(0))
    assert broker.next_deadline(_m(30)) == _m(108)
    assert broker.next_deadline(_m(108)) is None


@pytest.mark.parametrize(
    "build, message",
    [
        (lambda: PromotionPolicy(cooldown=timedelta(0)), "positive"),
        (lambda: PromotionPolicy(remember_for=-S), "positive"),
        (lambda: PromotionPolicy(major=Airtime(200 * S, 8 * S)), "fit its own lifetime"),
        (lambda: PromotionPolicy(snapshot=Airtime(200 * S, 8 * S)), "fit its own lifetime"),
        (lambda: Airtime(8 * S, 12 * S), "minimum"),
        (lambda: Airtime(8 * S, timedelta(0)), "minimum"),
        (lambda: PromotionKey(ELSEWHERE, ""), "lineage key"),
        (
            lambda: PromotionCandidate(
                key=PromotionKey(ELSEWHERE, "p"), kind=PromotionKind.MAJOR, eligible=_m(0), card=""
            ),
            "interrupt value",
        ),
        (
            lambda: PromotionCandidate(
                key=PromotionKey(ELSEWHERE, "p"),
                kind=PromotionKind.SNAPSHOT,
                eligible=_m(0),
                card="",
                value=_major("y", 1.0).value,
            ),
            "interrupt value",
        ),
        (lambda: _major("x", 1.0, revision=-1), "negative"),
    ],
)
def test_promotion_values_refuse_what_cant_be(build: Callable[[], object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        build()


def test_a_promotion_key_reads_as_its_contest_and_play() -> None:
    assert str(PromotionKey(ELSEWHERE, "play 41")) == "mlb:mlb_statsapi:747 play 41"
