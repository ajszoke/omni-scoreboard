"""Break leases: anchoring, what can't open or extend one, revocation, and guests (`BOARD_POLICY.md` §5)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from types import MappingProxyType

import pytest

from omni.core.clock import ClockReading, MonotonicTime
from omni.core.enum import League
from omni.core.ids import LeagueScopedId, SourceRef
from omni.domain.breaks import BreakConfidence, BreakEvidence, BreakId, BreakKind
from omni.policy.interrupts import FavoriteActivity
from omni.policy.leases import (
    CEILINGS,
    BreakScale,
    Ceiling,
    FavoriteMoment,
    LeaseBook,
    LeasePolicy,
    LeaseRefusal,
    LeaseState,
    LeaseVerdict,
)

T0 = datetime(2026, 11, 22, 18, 0, tzinfo=timezone.utc)
S = timedelta(seconds=1)
MLB_GAME = LeagueScopedId(League.MLB, SourceRef("mlb_statsapi"), "746")
NFL_GAME = LeagueScopedId(League.NFL, SourceRef("nflmeta"), "401")
NBA_GAME = LeagueScopedId(League.NBA, SourceRef("balldontlie"), "18")
POLL = 15 * S
CARD = 12 * S


def _m(seconds: float) -> MonotonicTime:
    return MonotonicTime(float(seconds))


def _contest(kind: BreakKind) -> LeagueScopedId:
    return {League.MLB: MLB_GAME, League.NFL: NFL_GAME, League.NBA: NBA_GAME}[kind.league]


def _evidence(
    kind: BreakKind,
    marker: str = "m1",
    *,
    at: float = 0.0,
    age: float | None = 0.0,
    stated: float | None = None,
    ended: bool = False,
    revision: int = 0,
) -> BreakEvidence:
    received = ClockReading(T0 + at * S, _m(at))
    return BreakEvidence(
        id=BreakId(_contest(kind), kind, marker),
        received=received,
        confidence=BreakConfidence.EXPLICIT if age is not None else BreakConfidence.INFERRED,
        onset=None if age is None else received.utc - age * S,
        stated=None if stated is None else stated * S,
        ended=ended,
        revision=revision,
    )


def _observe(
    book: LeaseBook,
    evidence: BreakEvidence,
    *,
    released: float | None = None,
    delay: timedelta = timedelta(0),
    national: bool = False,
) -> LeaseVerdict:
    when = evidence.received.monotonic if released is None else _m(released)
    return book.observe(evidence, released=when, poll_interval=POLL, delay=delay, national=national)


def _guests(
    book: LeaseBook, contest: LeagueScopedId, start: float, *, card: timedelta = CARD
) -> list[tuple[float, float]]:
    """Back-to-back guests from ``start`` while the break may host one."""
    shows: list[tuple[float, float]] = []
    t = start
    while book.may_host(contest, card, _m(t)):
        book.hosted(contest, _m(t))
        shows.append((t, t + card.total_seconds()))
        t += card.total_seconds()
    return shows


# --- Reference numbers ---------------------------------------------------------------------------------------------


def test_a_generic_official_timeout_gets_one_12_s_guest() -> None:
    book = LeaseBook()
    _observe(book, _evidence(BreakKind.NFL_OFFICIAL_TIMEOUT))
    assert _guests(book, NFL_GAME, 0) == [(0, 12)]
    # Another 12 s card would still fit what's left, but the break hosts one guest.
    assert book.remaining(NFL_GAME, _m(12)) == 18 * S
    assert book.guest_refusal(NFL_GAME, CARD, _m(12)) is LeaseRefusal.TURNS_SPENT
    assert book.favorite(NFL_GAME, _m(12)) == FavoriteMoment(activity=FavoriteActivity.MICRO_BREAK, room=timedelta(0))


def test_a_positively_identified_tv_timeout_with_10_s_detection_gets_eight_12_s_guests() -> None:
    book = LeaseBook()
    _observe(book, _evidence(BreakKind.NFL_TV_TIMEOUT, age=10))
    shows = _guests(book, NFL_GAME, 0)
    assert shows == [(12.0 * n, 12.0 * n + 12) for n in range(8)]
    assert book.guest_refusal(NFL_GAME, CARD, _m(96)) is LeaseRefusal.NO_ROOM


@pytest.mark.parametrize("stated, airtime", [(120, 96), (145, 120), (175, 144)])
def test_mlb_inning_breaks_give_guest_airtime_by_their_stated_length(stated: float, airtime: float) -> None:
    book = LeaseBook()
    _observe(book, _evidence(BreakKind.MLB_INNING_BREAK, stated=stated))
    shows = _guests(book, MLB_GAME, 0)
    assert sum(end - start for start, end in shows) == airtime


def test_an_nba_decrement_with_15_s_detection_allowance_gives_three_turns_at_most() -> None:
    book = LeaseBook()
    verdict = _observe(book, _evidence(BreakKind.NBA_TEAM_TIMEOUT, age=None))
    assert verdict.lease is not None and verdict.lease.provisional
    # 60 s from the inferred onset, less one poll interval and the return margin: 39 s of budget.
    assert book.favorite(NBA_GAME, _m(0)) == FavoriteMoment(activity=FavoriteActivity.MICRO_BREAK, room=39 * S)
    assert _guests(book, NBA_GAME, 0) == [(0, 12), (12, 24), (24, 36)]


def test_resumed_action_after_an_nba_decrement_revokes_further_grants() -> None:
    book = LeaseBook()
    _observe(book, _evidence(BreakKind.NBA_TEAM_TIMEOUT, age=None))
    book.hosted(NBA_GAME, _m(0))
    book.hosted(NBA_GAME, _m(12))
    revoked = book.action(NBA_GAME, _m(20))
    assert revoked is not None and revoked.state(_m(20)) is LeaseState.REVOKED
    assert book.guest_refusal(NBA_GAME, CARD, _m(24)) is LeaseRefusal.RESUMED
    assert book.favorite(NBA_GAME, _m(24)).activity is FavoriteActivity.IN_ACTION
    assert not book.in_break(NBA_GAME, _m(24))


# --- MLB inning breaks ---------------------------------------------------------------------------------------------


def test_a_10_s_late_inning_break_detection_reduces_the_allowance_before_admission() -> None:
    book = LeaseBook()
    _observe(book, _evidence(BreakKind.MLB_INNING_BREAK, age=10, stated=120))
    assert book.remaining(MLB_GAME, _m(0)) == 95 * S
    assert sum(end - start for start, end in _guests(book, MLB_GAME, 0)) == 84


def test_a_repeated_same_half_marker_extends_nothing() -> None:
    book = LeaseBook()
    first = _observe(book, _evidence(BreakKind.MLB_INNING_BREAK, "top 5 middle", stated=120))
    again = _observe(book, _evidence(BreakKind.MLB_INNING_BREAK, "top 5 middle", at=50, stated=145, revision=1))
    assert again.refusal is LeaseRefusal.DUPLICATE and again.lease is None
    lease = book.lease(MLB_GAME)
    assert first.lease is not None and lease is not None and lease.ends == first.lease.ends == _m(105)


def test_a_backfilled_start_and_end_pair_opens_nothing() -> None:
    book = LeaseBook()
    pair = _observe(book, _evidence(BreakKind.MLB_INNING_BREAK, "top 5 middle", stated=120, ended=True))
    assert pair.refusal is LeaseRefusal.ENDED_ON_ARRIVAL and book.lease(MLB_GAME) is None
    start = _observe(book, _evidence(BreakKind.MLB_INNING_BREAK, "top 5 middle", at=5, stated=120))
    assert start.refusal is LeaseRefusal.DUPLICATE
    assert book.guest_refusal(MLB_GAME, CARD, _m(5)) is LeaseRefusal.NO_BREAK


def test_a_pitching_change_inside_an_inning_break_doesnt_extend_it_nor_open_one_after() -> None:
    book = LeaseBook()
    _observe(book, _evidence(BreakKind.MLB_INNING_BREAK, "top 5 middle", stated=120))
    change = _evidence(BreakKind.MLB_PITCHING_CHANGE, "ab 41", at=90)
    assert _observe(book, change).refusal is LeaseRefusal.INSIDE_OPEN_BREAK
    lease = book.lease(MLB_GAME)
    assert lease is not None and lease.ends == _m(105)
    # The next half's action cancels the inning break; the change, read again, still opens nothing.
    book.action(MLB_GAME, _m(100))
    assert _observe(book, change, released=101).refusal is LeaseRefusal.DUPLICATE
    assert not book.in_break(MLB_GAME, _m(101))


@pytest.mark.parametrize("age, refusal", [(19, LeaseRefusal.BACKFILL), (36, LeaseRefusal.SPENT)])
def test_an_old_substitution_gets_no_lease(age: float, refusal: LeaseRefusal) -> None:
    book = LeaseBook()
    assert _observe(book, _evidence(BreakKind.MLB_PITCHING_CHANGE, age=age)).refusal is refusal
    assert book.lease(MLB_GAME) is None


def test_a_stated_inning_break_shorter_than_the_trim_is_already_spent() -> None:
    verdict = _observe(LeaseBook(), _evidence(BreakKind.MLB_INNING_BREAK, stated=10))
    assert verdict.refusal is LeaseRefusal.SPENT


def test_an_inning_break_of_unknown_length_gets_60_s() -> None:
    verdict = _observe(LeaseBook(), _evidence(BreakKind.MLB_INNING_BREAK))
    assert verdict.lease is not None and verdict.lease.ends == _m(60)


# --- What opens, ends and revokes a lease --------------------------------------------------------------------------


def test_a_completed_ruling_grants_nothing() -> None:
    book = LeaseBook()
    assert _observe(book, _evidence(BreakKind.NFL_REVIEW, ended=True)).refusal is LeaseRefusal.ENDED_ON_ARRIVAL
    assert book.favorite(NFL_GAME, _m(0)).activity is FavoriteActivity.IN_ACTION


def test_the_feeds_end_marker_closes_an_open_lease_once() -> None:
    book = LeaseBook()
    _observe(book, _evidence(BreakKind.NFL_TEAM_TIMEOUT, "play 88"))
    ended = _observe(book, _evidence(BreakKind.NFL_TEAM_TIMEOUT, "play 88", at=10, ended=True))
    assert ended.refusal is None and ended.lease is not None
    assert ended.lease.state(_m(9)) is LeaseState.OPEN  # read before the end, it was open
    assert ended.lease.state(_m(10)) is LeaseState.ENDED
    assert book.guest_refusal(NFL_GAME, CARD, _m(11)) is LeaseRefusal.ENDED
    again = _observe(book, _evidence(BreakKind.NFL_TEAM_TIMEOUT, "play 88", at=12, ended=True))
    assert again.refusal is LeaseRefusal.DUPLICATE


def test_an_end_marker_for_a_break_that_isnt_open_changes_nothing() -> None:
    book = LeaseBook()
    _observe(book, _evidence(BreakKind.NFL_TEAM_TIMEOUT, "play 88"))
    # It lapsed at 30, unended; its end arriving after renews and closes nothing.
    assert _observe(book, _evidence(BreakKind.NFL_TEAM_TIMEOUT, "play 88", at=35, ended=True)).refusal is (
        LeaseRefusal.DUPLICATE
    )
    _observe(book, _evidence(BreakKind.NFL_INJURY_TIMEOUT, "play 90", at=40))
    late_end = _evidence(BreakKind.NFL_TEAM_TIMEOUT, "play 88", at=45, ended=True)
    assert _observe(book, late_end).refusal is LeaseRefusal.DUPLICATE
    assert book.in_break(NFL_GAME, _m(45))
    # A break that never got a lease, then its end.
    fresh = LeaseBook()
    _observe(fresh, _evidence(BreakKind.NFL_TEAM_TIMEOUT, "play 70", age=29))
    assert _observe(fresh, _evidence(BreakKind.NFL_TEAM_TIMEOUT, "play 70", at=1, ended=True)).refusal is (
        LeaseRefusal.DUPLICATE
    )


def test_a_lease_that_runs_out_returns_the_favorite_to_weighted_eligibility_without_claiming_play_resumed() -> None:
    book = LeaseBook()
    _observe(book, _evidence(BreakKind.NFL_TEAM_TIMEOUT))
    lease = book.lease(NFL_GAME)
    assert lease is not None and lease.state(_m(30)) is LeaseState.LAPSED
    assert book.favorite(NFL_GAME, _m(30)).activity is FavoriteActivity.IN_ACTION
    assert book.guest_refusal(NFL_GAME, CARD, _m(30)) is LeaseRefusal.LAPSED
    assert book.remaining(NFL_GAME, _m(30)) == timedelta(0)
    assert book.hosted(NFL_GAME, _m(30)) is None
    # Action released later still records the resumption.
    revoked = book.action(NFL_GAME, _m(40))
    assert revoked is not None and revoked.state(_m(40)) is LeaseState.REVOKED


def test_only_action_released_after_a_lease_opened_revokes_it() -> None:
    book = LeaseBook()
    _observe(book, _evidence(BreakKind.NFL_QUARTER_BREAK, at=10))
    assert book.action(NFL_GAME, _m(5)) is None
    assert book.action(NFL_GAME, _m(10)) is None
    assert book.action(MLB_GAME, _m(20)) is None
    assert book.action(NFL_GAME, _m(20)) is not None
    assert book.action(NFL_GAME, _m(25)) is None  # already revoked


def test_a_lease_is_anchored_on_the_viewers_delayed_timeline() -> None:
    book = LeaseBook()
    # Received 5 s after it began, shown 30 s behind live: on the viewer's timeline it began at 25.
    evidence = _evidence(BreakKind.MLB_MOUND_VISIT, age=5)
    verdict = _observe(book, evidence, released=25, delay=30 * S)
    assert verdict.lease is not None and verdict.lease.anchor == _m(25) and verdict.lease.ends == _m(61)
    assert book.remaining(MLB_GAME, _m(25)) == 36 * S


def test_a_lease_never_begins_after_its_release() -> None:
    book = LeaseBook()
    verdict = _observe(book, _evidence(BreakKind.MLB_MOUND_VISIT), released=10, delay=30 * S)
    assert verdict.lease is not None and verdict.lease.anchor == _m(10) and verdict.lease.ends == _m(46)


def test_an_onset_after_its_arrival_counts_as_fresh() -> None:
    verdict = _observe(LeaseBook(), _evidence(BreakKind.MLB_MOUND_VISIT, age=-3))
    assert verdict.lease is not None and verdict.lease.ends == _m(36)


@pytest.mark.parametrize(
    "kind, national, length",
    [
        (BreakKind.NBA_QUARTER_BREAK, False, 135),
        (BreakKind.NBA_QUARTER_BREAK, True, 195),
        (BreakKind.NBA_TV_TIMEOUT, True, 165),
        (BreakKind.NBA_HALFTIME, True, 840),
        (BreakKind.NFL_HALFTIME, False, 900),
        (BreakKind.MLB_REVIEW, False, 48),
    ],
)
def test_ceilings_follow_the_table_and_national_tv(kind: BreakKind, national: bool, length: float) -> None:
    verdict = _observe(LeaseBook(), _evidence(kind), national=national)
    assert verdict.lease is not None and verdict.lease.ends == _m(length)


def test_an_mlb_review_hosts_two_guests() -> None:
    book = LeaseBook()
    _observe(book, _evidence(BreakKind.MLB_REVIEW))
    assert _guests(book, MLB_GAME, 0) == [(0, 12), (12, 24)]


def test_a_weather_stoppage_stays_open_while_its_status_stands() -> None:
    book = LeaseBook()
    _observe(book, _evidence(BreakKind.MLB_WEATHER, age=600))
    assert book.favorite(MLB_GAME, _m(3600)) == FavoriteMoment(activity=FavoriteActivity.ORDINARY_BREAK, room=None)
    assert book.remaining(MLB_GAME, _m(3600)) is None
    assert book.may_host(MLB_GAME, timedelta(minutes=10), _m(3600))
    assert book.next_deadline(_m(0)) is None


def test_the_next_deadline_is_the_first_open_lease_to_run_out() -> None:
    book = LeaseBook()
    _observe(book, _evidence(BreakKind.NFL_TEAM_TIMEOUT))
    _observe(book, _evidence(BreakKind.MLB_INNING_BREAK, stated=120))
    assert book.next_deadline(_m(0)) == _m(30)
    assert book.next_deadline(_m(30)) == _m(105)
    book.action(MLB_GAME, _m(40))
    assert book.next_deadline(_m(40)) is None


def test_a_guest_counted_outside_an_open_lease_counts_nowhere() -> None:
    book = LeaseBook()
    assert book.hosted(NFL_GAME, _m(0)) is None
    assert book.remaining(NFL_GAME, _m(0)) == timedelta(0)


def test_forgetting_a_game_drops_its_leases_and_the_breaks_it_saw() -> None:
    book = LeaseBook()
    evidence = _evidence(BreakKind.NFL_QUARTER_BREAK)
    _observe(book, evidence)
    book.forget(NFL_GAME)
    assert book.lease(NFL_GAME) is None
    assert _observe(book, evidence, released=1).lease is not None


def test_an_ordinary_break_reads_as_one_with_its_room_net_of_the_margin() -> None:
    book = LeaseBook(LeasePolicy(return_margin=10 * S))
    assert book.policy.return_margin == 10 * S
    _observe(book, _evidence(BreakKind.NFL_QUARTER_BREAK))
    assert book.favorite(NFL_GAME, _m(20)) == FavoriteMoment(activity=FavoriteActivity.ORDINARY_BREAK, room=60 * S)
    assert BreakScale.MICRO.activity is FavoriteActivity.MICRO_BREAK


@pytest.mark.parametrize(
    "build, message",
    [
        (lambda: LeasePolicy(ceilings=MappingProxyType({})), "every kind"),
        (lambda: LeasePolicy(return_margin=-S), "can't be negative"),
        (lambda: LeasePolicy(severe_backfill=0.0), "share"),
        (lambda: LeasePolicy(severe_backfill=1.5), "share"),
        (lambda: Ceiling(length=timedelta(0), scale=BreakScale.MICRO), "positive"),
        (lambda: Ceiling(length=S, scale=BreakScale.MICRO, national=-S), "positive"),
        (lambda: Ceiling(length=S, scale=BreakScale.MICRO, turns=0), "at least one"),
        (lambda: FavoriteMoment(activity=FavoriteActivity.MICRO_BREAK, room=-S), "negative"),
        (lambda: FavoriteMoment(activity=FavoriteActivity.IN_ACTION, room=S), "no room"),
        (lambda: FavoriteMoment(activity=FavoriteActivity.IN_ACTION, room=None), "no room"),
    ],
)
def test_lease_values_refuse_what_cant_be(build: Callable[[], object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        build()


def test_the_default_table_covers_every_kind_of_break() -> None:
    assert set(CEILINGS) == set(BreakKind)
