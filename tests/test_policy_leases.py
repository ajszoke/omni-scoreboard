"""Break leases: anchoring, what can't open or extend one, supersession, revocation, and guests (`BOARD_POLICY.md`
§5).

Most tests hold a break's evidence to an answer that stood when it arrived and released at once (no viewer delay), so
its age is its onset's; those about the anchor say otherwise. A board's lease book takes only the kinds a detector
finds today; the reference numbers for kinds still to come use a book that takes every kind."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from types import MappingProxyType

import pytest

from omni.core.clock import ClockReading, MonotonicTime
from omni.core.enum import League
from omni.core.ids import LeagueScopedId, SourceRef
from omni.domain.breaks import AgeBasis, BreakAge, BreakConfidence, BreakEvidence, BreakId, BreakKind, OnsetBasis
from omni.events.break_detection import detection_age
from omni.policy.interrupts import FavoriteActivity
from omni.policy.leases import (
    CEILINGS,
    BreakScale,
    Ceiling,
    FavoriteMoment,
    LeaseBook,
    LeaseGrant,
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
EVERY_KIND = LeasePolicy(supported=frozenset(BreakKind))  # a book for kinds a future detector would bring


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
    released: float | None = None,
) -> BreakEvidence:
    """Evidence received at ``at`` in an answer that stood then, ``age`` seconds into its break (None: unknown),
    released at ``released`` (default: as it arrived)."""
    received = ClockReading(T0 + at * S, _m(at))
    return BreakEvidence(
        id=BreakId(_contest(kind), kind, marker),
        received=received,
        confidence=BreakConfidence.EXPLICIT if age is not None else BreakConfidence.INFERRED,
        onset=None if age is None else received.utc - age * S,
        stated=None if stated is None else stated * S,
        ended=ended,
        revision=revision,
        as_of=received.utc,
        released=_m(at if released is None else released),
    )


def _observe(
    book: LeaseBook, evidence: BreakEvidence, *, decided: float | None = None, national: bool = False
) -> LeaseVerdict:
    """The board takes ``evidence`` in at ``decided`` (default: its release)."""
    assert evidence.released is not None
    when = evidence.released if decided is None else _m(decided)
    return book.observe(evidence, at=when, poll_interval=POLL, national=national)


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
    book = LeaseBook(EVERY_KIND)
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
    book = LeaseBook(EVERY_KIND)
    verdict = _observe(book, _evidence(BreakKind.NBA_TEAM_TIMEOUT, age=None))
    assert verdict.lease is not None and verdict.lease.provisional
    # 60 s from the inferred onset, less one poll interval and the return margin: 39 s of budget.
    assert book.favorite(NBA_GAME, _m(0)) == FavoriteMoment(activity=FavoriteActivity.MICRO_BREAK, room=39 * S)
    assert _guests(book, NBA_GAME, 0) == [(0, 12), (12, 24), (24, 36)]


def test_resumed_action_after_an_nba_decrement_revokes_further_grants() -> None:
    book = LeaseBook(EVERY_KIND)
    decrement = _evidence(BreakKind.NBA_TEAM_TIMEOUT, age=None)
    _observe(book, decrement)
    book.hosted(NBA_GAME, _m(0))
    book.hosted(NBA_GAME, _m(12))
    revoked = book.resumed(NBA_GAME, {decrement.id}, _m(20))
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
    book.resumed(MLB_GAME, {BreakId(MLB_GAME, BreakKind.MLB_INNING_BREAK, "top 5 middle")}, _m(100))
    assert _observe(book, change, decided=101).refusal is LeaseRefusal.DUPLICATE
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
    _observe(book, _evidence(BreakKind.NFL_OFFICIAL_TIMEOUT, "play 90", at=40))
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
    # Action released later after the break began still records the resumption.
    assert lease is not None
    revoked = book.resumed(NFL_GAME, {lease.id}, _m(40))
    assert revoked is not None and revoked.state(_m(40)) is LeaseState.REVOKED


def test_released_action_revokes_only_the_breaks_it_names() -> None:
    book = LeaseBook()
    quarter = _evidence(BreakKind.NFL_QUARTER_BREAK, at=10)
    _observe(book, quarter)
    assert book.resumed(NFL_GAME, set(), _m(20)) is None  # a newer marker that came before the break: not closed
    assert book.resumed(NFL_GAME, {BreakId(NFL_GAME, BreakKind.NFL_TEAM_TIMEOUT, "play 9")}, _m(20)) is None
    assert book.resumed(MLB_GAME, {quarter.id}, _m(20)) is None  # another game
    revoked = book.resumed(NFL_GAME, {quarter.id}, _m(20))
    assert revoked is not None and revoked.state(_m(20)) is LeaseState.REVOKED
    assert book.resumed(NFL_GAME, {quarter.id}, _m(25)) is None  # already revoked


def test_a_lease_is_anchored_at_its_pinned_release_less_its_age_however_late_it_is_read() -> None:
    """Received 5 s after it began, released 30 s later on the viewer's delayed timeline: it began there at 25 and
    ends at 61, whether the board reads it at its release or 10 s after."""
    evidence = _evidence(BreakKind.MLB_MOUND_VISIT, age=5, released=30)
    for decided in (30.0, 40.0):
        verdict = _observe(LeaseBook(), evidence, decided=decided)
        assert verdict.lease is not None and (verdict.lease.anchor, verdict.lease.ends) == (_m(25), _m(61))
        assert verdict.lease.remaining(_m(decided)) == (61 - decided) * S
        assert verdict.age == BreakAge(age=5 * S, basis=AgeBasis.SAME_SOURCE)
        assert verdict.elapsed == (decided - 25) * S


def test_evidence_with_no_pinned_release_is_anchored_from_its_receipt() -> None:
    received = _evidence(BreakKind.MLB_MOUND_VISIT, at=10)
    unreleased = BreakEvidence(id=received.id, received=received.received, confidence=received.confidence)
    verdict = LeaseBook().observe(unreleased, at=_m(12), poll_interval=POLL)
    assert verdict.lease is not None and verdict.lease.ends == _m(10 - 15 + 36)  # a poll interval came off, too
    assert verdict.lease.provisional


def test_a_lease_never_begins_after_the_decision_that_reads_it() -> None:
    verdict = _observe(LeaseBook(), _evidence(BreakKind.MLB_MOUND_VISIT, released=30), decided=10)
    assert verdict.lease is not None and verdict.lease.anchor == _m(10) and verdict.lease.ends == _m(46)


def test_an_onset_after_its_answer_within_the_seconds_rounding_counts_as_fresh() -> None:
    verdict = _observe(LeaseBook(), _evidence(BreakKind.MLB_MOUND_VISIT, age=-3))
    assert verdict.lease is not None and verdict.lease.ends == _m(36)


@pytest.mark.parametrize(
    "kind, national, length",
    [
        (BreakKind.NBA_QUARTER_BREAK, False, 135),
        (BreakKind.NBA_QUARTER_BREAK, True, 195),
        (BreakKind.NBA_TV_TIMEOUT, False, 165),
        (BreakKind.NBA_TV_TIMEOUT, True, 165),
        (BreakKind.NBA_HALFTIME, True, 840),
        (BreakKind.NFL_HALFTIME, False, 900),
        (BreakKind.MLB_REVIEW, False, 48),
    ],
)
def test_ceilings_follow_the_table_and_national_tv(kind: BreakKind, national: bool, length: float) -> None:
    verdict = _observe(LeaseBook(EVERY_KIND), _evidence(kind), national=national)
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
    inning = _evidence(BreakKind.MLB_INNING_BREAK, stated=120)
    _observe(book, inning)
    assert book.next_deadline(_m(0)) == _m(30)
    assert book.next_deadline(_m(30)) == _m(105)
    book.resumed(MLB_GAME, {inning.id}, _m(40))
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
    assert _observe(book, evidence, decided=1).lease is not None


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


# --- Severe backfill and evidence refused at once ------------------------------------------------------------------


@pytest.mark.parametrize(
    "decided, refusal, left",
    [
        (6, None, 18),  # 18 of its 36 s gone: half, not more; the 12 s card and its 6 s margin just fit
        (7, LeaseRefusal.BACKFILL, None),  # 19 s gone
        (24, LeaseRefusal.SPENT, None),  # all of it
    ],
)
def test_more_than_half_a_ceiling_gone_at_the_real_decision_refuses_the_lease(
    decided: float, refusal: LeaseRefusal | None, left: float | None
) -> None:
    """A mound visit 12 s old when its answer arrived, released at 0: by the decision it has used 12 s plus the time
    since its release. The refusal says the age, and how much of the ceiling had gone."""
    book = LeaseBook()
    verdict = _observe(book, _evidence(BreakKind.MLB_MOUND_VISIT, age=12), decided=decided)
    assert verdict.refusal is refusal
    assert verdict.age == BreakAge(age=12 * S, basis=AgeBasis.SAME_SOURCE)
    assert verdict.elapsed == (12 + decided) * S
    if left is not None:
        assert book.remaining(MLB_GAME, _m(decided)) == left * S
        assert book.may_host(MLB_GAME, CARD, _m(decided))
        assert not book.may_host(MLB_GAME, CARD + S, _m(decided))


def test_evidence_from_a_stale_game_or_of_a_kind_no_detector_finds_is_refused_at_once_and_never_again() -> None:
    book = LeaseBook()
    stale = _evidence(BreakKind.MLB_MOUND_VISIT)
    assert book.observe(stale, at=_m(0), poll_interval=POLL, fresh=False) == LeaseVerdict(
        lease=None, refusal=LeaseRefusal.STALE
    )
    assert _observe(book, stale).refusal is LeaseRefusal.DUPLICATE  # fresh again, it renews nothing
    tv = _evidence(BreakKind.NFL_TV_TIMEOUT)
    assert _observe(book, tv).refusal is LeaseRefusal.UNSUPPORTED
    assert LeaseBook(EVERY_KIND).policy.supported == frozenset(BreakKind)


# --- Nested intervals ---------------------------------------------------------------------------------------------


def test_a_rain_delay_supersedes_an_open_mound_visit_its_budget_discarded() -> None:
    book = LeaseBook()
    visit = _evidence(BreakKind.MLB_MOUND_VISIT, "at-bat 41 event 5", age=3)
    _observe(book, visit)
    rain = _observe(book, _evidence(BreakKind.MLB_WEATHER, "delayed top 5", at=5, age=None))
    assert rain.lease is not None and rain.lease.ends is None and rain.lease.id.kind is BreakKind.MLB_WEATHER
    assert rain.superseded is not None and rain.superseded.id == visit.id
    assert rain.superseded.state(_m(5)) is LeaseState.SUPERSEDED and rain.superseded.state(_m(4)) is LeaseState.OPEN
    assert book.favorite(MLB_GAME, _m(100)) == FavoriteMoment(activity=FavoriteActivity.ORDINARY_BREAK, room=None)


def test_an_ordinary_break_supersedes_a_micro_one_and_a_break_of_the_same_rank_is_merely_nested() -> None:
    book = LeaseBook()
    _observe(book, _evidence(BreakKind.MLB_MOUND_VISIT, "at-bat 41 event 5"))
    nested = _observe(book, _evidence(BreakKind.MLB_REVIEW, "at-bat 41 event 6", at=2))
    assert nested.refusal is LeaseRefusal.INSIDE_OPEN_BREAK and nested.superseded is None
    timeout = LeaseBook()
    _observe(timeout, _evidence(BreakKind.NFL_OFFICIAL_TIMEOUT, "play 88"))
    quarter = _observe(timeout, _evidence(BreakKind.NFL_QUARTER_BREAK, "end of period 1", at=4, age=None))
    assert quarter.lease is not None and quarter.lease.id.kind is BreakKind.NFL_QUARTER_BREAK
    assert quarter.superseded is not None and quarter.superseded.id.kind is BreakKind.NFL_OFFICIAL_TIMEOUT
    assert LeasePolicy().supersedes(BreakKind.MLB_WEATHER, BreakKind.MLB_INNING_BREAK)
    assert not LeasePolicy().supersedes(BreakKind.MLB_INNING_BREAK, BreakKind.MLB_WEATHER)


def test_a_superseding_interval_too_old_for_a_lease_still_closes_the_short_one() -> None:
    book = LeaseBook()
    _observe(book, _evidence(BreakKind.NFL_TEAM_TIMEOUT, "play 88"))
    warning = _observe(book, _evidence(BreakKind.NFL_TWO_MINUTE_WARNING, "play 90", at=3, age=60))
    assert warning.refusal is LeaseRefusal.BACKFILL and warning.superseded is not None
    assert book.guest_refusal(NFL_GAME, CARD, _m(3)) is LeaseRefusal.CHANGED
    assert book.favorite(NFL_GAME, _m(3)).activity is FavoriteActivity.IN_ACTION


# --- A guest's permission -----------------------------------------------------------------------------------------


def test_a_guest_s_permission_names_its_lease_and_holds_while_it_still_fits() -> None:
    book = LeaseBook()
    visit = _evidence(BreakKind.MLB_MOUND_VISIT, age=12)  # 24 s left at 0
    _observe(book, visit)
    grant = book.grant(MLB_GAME, CARD)
    assert grant == LeaseGrant(contest=MLB_GAME, id=visit.id, latest_start=_m(6), expires=_m(24))
    assert book.holds(grant, CARD, _m(6)) is None
    assert book.holds(grant, CARD, _m(16)) is LeaseRefusal.NO_ROOM  # 8 s left; it needs 18
    book.hosted(MLB_GAME, _m(0), visit.id)
    assert book.holds(grant, CARD, _m(1)) is LeaseRefusal.TURNS_SPENT
    assert book.closed(grant) is None
    book.resumed(MLB_GAME, {visit.id}, _m(10))
    assert book.holds(grant, CARD, _m(11)) is LeaseRefusal.RESUMED and book.closed(grant) == _m(10)


def test_a_guest_s_permission_in_a_replaced_lease_no_longer_holds_and_says_when_it_closed() -> None:
    book = LeaseBook()
    visit = _evidence(BreakKind.MLB_MOUND_VISIT, "at-bat 41 event 5")
    _observe(book, visit)
    grant = book.grant(MLB_GAME, CARD)
    _observe(book, _evidence(BreakKind.MLB_WEATHER, "delayed top 5", at=5))
    assert book.holds(grant, CARD, _m(5)) is LeaseRefusal.CHANGED
    assert book.closed(grant) == _m(5)
    assert book.hosted(MLB_GAME, _m(6), visit.id) is None  # a guest counts only in the lease it was granted
    weather = book.grant(MLB_GAME, CARD)
    assert (weather.latest_start, weather.expires) == (None, None)
    book.forget(MLB_GAME)
    assert book.holds(grant, CARD, _m(7)) is LeaseRefusal.NO_BREAK and book.closed(grant) is None


def test_a_lapsed_lease_that_play_then_resumes_from_closes_after_its_ceiling() -> None:
    book = LeaseBook()
    timeout = _evidence(BreakKind.NFL_TEAM_TIMEOUT, "play 88")
    _observe(book, timeout)
    grant = book.grant(NFL_GAME, CARD)
    assert book.holds(grant, CARD, _m(30)) is LeaseRefusal.LAPSED
    book.resumed(NFL_GAME, {timeout.id}, _m(40))
    assert book.closed(grant) == _m(40)


# --- A break's age ------------------------------------------------------------------------------------------------


def _aged(*, onset: float | None, as_of: float | None, received: float = 0.0) -> BreakEvidence:
    """A mound visit whose answer stood at ``as_of`` and arrived at ``received``, begun at ``onset`` (seconds)."""
    reading = ClockReading(T0 + received * S, _m(received))
    return BreakEvidence(
        id=BreakId(MLB_GAME, BreakKind.MLB_MOUND_VISIT, "m"),
        received=reading,
        confidence=BreakConfidence.EXPLICIT,
        onset=None if onset is None else T0 + onset * S,
        as_of=None if as_of is None else T0 + as_of * S,
    )


@pytest.mark.parametrize(
    "onset, as_of, received, age, basis, uncertainty",
    [
        (-10, -2, 0, 10, AgeBasis.SAME_SOURCE, 0),  # 8 s on the source's clock, 2 s in transport
        (-10, 2, 0, 12, AgeBasis.SAME_SOURCE, 0),  # the source's clock a little ahead: no transport
        (-10, 0, 150, 10 + 15, AgeBasis.SAME_SOURCE, 15),  # a transport longer than plausible: a poll interval
        (590, 600, 0, 10 + 15, AgeBasis.SAME_SOURCE, 15),  # the source's clock ten minutes ahead of ours
        (10, 0, 0, 15, AgeBasis.PROVISIONAL, 15),  # dated after its own answer: incoherent
        (None, 0, 0, 15, AgeBasis.PROVISIONAL, 15),
        (-10, None, 0, 15, AgeBasis.PROVISIONAL, 15),
    ],
)
def test_a_break_s_age_is_told_on_the_source_s_clock_with_a_measured_transport_or_a_marked_poll_interval(
    onset: float | None, as_of: float | None, received: float, age: float, basis: AgeBasis, uncertainty: float
) -> None:
    told = detection_age(_aged(onset=onset, as_of=as_of, received=received), poll_interval=POLL)
    assert told == BreakAge(age=age * S, basis=basis, uncertainty=uncertainty * S)
    assert told.provisional is (uncertainty > 0)


def test_a_local_hold_is_aged_on_this_board_s_clock_and_anchored_from_its_pinned_release() -> None:
    reading = ClockReading(T0 + 100 * S, _m(100))
    halftime = BreakEvidence(
        id=BreakId(NFL_GAME, BreakKind.NFL_HALFTIME, "halftime"),
        received=reading,
        confidence=BreakConfidence.EXPLICIT,
        onset=reading.utc - 40 * S,  # the hold began 40 s before, by our clock
        as_of=reading.utc + 600 * S,  # the scoreboard's clock, well off ours, isn't read
        onset_basis=OnsetBasis.LOCAL_HOLD,
        released=_m(145),
    )
    verdict = LeaseBook().observe(halftime, at=_m(145), poll_interval=POLL)
    assert verdict.age == BreakAge(age=40 * S, basis=AgeBasis.LOCAL_HOLD)
    assert verdict.lease is not None and verdict.lease.ends == _m(145 - 40 + 900)


def test_a_break_s_age_can_t_be_negative() -> None:
    with pytest.raises(ValueError, match="can't be negative"):
        BreakAge(age=-S, basis=AgeBasis.SAME_SOURCE)
    with pytest.raises(ValueError, match="can't be negative"):
        BreakAge(age=S, basis=AgeBasis.PROVISIONAL, uncertainty=-S)
