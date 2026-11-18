"""The contest store, scope by scope: membership by outcome, order by receipt, the union, retirement, health."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from omni.app.contest_store import FINAL_HOLD, ContestStore, ProviderStatus, ScopeHealth
from omni.core.clock import ClockReading, MonotonicTime
from omni.core.enum import GameStatus, League
from omni.core.ids import LeagueScopedId, SourceRef
from omni.domain.contest import Contest
from omni.providers.base import (
    ScopeSpan,
    LocalDate,
    PollScope,
    ProviderUpdate,
    ScopeObservation,
    ScopeOutcome,
    ScopeReport,
)

SOURCE = SourceRef("mlb_statsapi")
T = datetime(2026, 6, 17, 23, 0, 0, tzinfo=timezone.utc)
DAY = date(2026, 6, 17)
MAX_AGE = timedelta(minutes=2)
S = timedelta(seconds=1)


def _at(seconds: float) -> ClockReading:
    return ClockReading(T + timedelta(seconds=seconds), MonotonicTime(seconds))


def _scope(day: date = DAY, league: League = League.MLB) -> PollScope:
    return PollScope(SOURCE, league, LocalDate(day))


def _id(raw: str, league: League = League.MLB) -> LeagueScopedId:
    return LeagueScopedId(league, SOURCE, raw)


def _contest(raw: str, status: GameStatus = GameStatus.SCHEDULED, league: League = League.MLB) -> Contest:
    return Contest(id=_id(raw, league), league=league, status=status, scheduled_start=T)


def _answer(scope: PollScope, *contests: Contest, at: float, skipped: tuple[str, ...] = ()) -> ScopeObservation:
    update = ProviderUpdate(source=SOURCE, observed_at=T, contests=contests, warnings=skipped)
    return ScopeObservation.answer(scope, update, _at(at))


def _failure(scope: PollScope, *, at: float, error: str = "ProviderError: down") -> ScopeObservation:
    return ScopeObservation.failure(scope, error, _at(at))


def _cover(store: ContestStore, day: date = DAY, *, league: League = League.MLB, window: int = 1) -> None:
    """``day`` as the current scope of ``league``, with the ``window - 1`` days before it."""
    earlier = tuple(_scope(day - timedelta(days=back), league) for back in range(1, window))
    store.cover(ScopeSpan(current=_scope(day, league), earlier=earlier, max_age=MAX_AGE))


def _span() -> ScopeSpan:
    return ScopeSpan(current=_scope(), max_age=MAX_AGE)


def _raws(store: ContestStore) -> list[str]:
    return [contest.id.raw for contest in store.contests]


def _status(store: ContestStore, raw: str) -> GameStatus:
    contest = store.get(_id(raw))
    assert contest is not None
    return contest.status


def _league(store: ContestStore, at: float, league: League = League.MLB) -> ScopeHealth:
    health = store.league_health(league, MonotonicTime(at))
    assert health is not None
    return health


def _own(store: ContestStore, scope: PollScope, at: float) -> ScopeHealth:
    health = store.health(scope, MonotonicTime(at))
    assert health is not None
    return health


# --- one scope: complete, partial, failed, in receipt order -------------------------------------------


def test_an_empty_store_holds_nothing() -> None:
    store = ContestStore()
    assert len(store) == 0 and store.contests == () and store.scopes == ()
    assert store.get(_id("g1")) is None and _id("g1") not in store
    assert store.membership(_scope()) == () and store.health(_scope(), MonotonicTime(0)) is None
    assert store.league_health(League.MLB, MonotonicTime(0)) is None and store.league_healths(MonotonicTime(0)) == ()


def test_a_complete_answer_is_its_scope_s_membership_and_the_union_names_what_changed() -> None:
    store = ContestStore()
    _cover(store)
    assert store.apply(_answer(_scope(), _contest("g1"), _contest("g2"), at=0))
    first = store.reconcile(MonotonicTime(0))
    assert first.added == (_id("g1"), _id("g2")) and first.changed
    assert store.get(_id("g1")) is not None and _id("g2") in store and len(store) == 2
    store.apply(_answer(_scope(), _contest("g1", GameStatus.LIVE), _contest("g3"), at=30))
    second = store.reconcile(MonotonicTime(30))
    assert (second.added, second.updated, second.removed) == ((_id("g3"),), (_id("g1"),), (_id("g2"),))
    assert _raws(store) == ["g1", "g3"] and _status(store, "g1") is GameStatus.LIVE


def test_an_answer_handed_again_changes_nothing_and_the_changes_come_sorted() -> None:
    store = ContestStore()
    _cover(store)
    answer = _answer(_scope(), _contest("g3"), _contest("g1"), _contest("g2"), at=0)
    store.apply(answer)
    assert store.reconcile(MonotonicTime(0)).added == (_id("g1"), _id("g2"), _id("g3"))
    assert store.apply(answer)  # the same answer, handed again on the next pass
    assert not store.reconcile(MonotonicTime(1)).changed
    assert store.membership(_scope()) == (_contest("g3"), _contest("g1"), _contest("g2"))  # as the answer listed


def test_an_observation_older_than_its_scope_s_last_is_ignored_for_that_scope_alone() -> None:
    store = ContestStore()
    _cover(store)
    _cover(store, league=League.NBA)
    store.apply(_answer(_scope(), _contest("g1"), at=10))
    assert not store.apply(_answer(_scope(), _contest("g2"), at=5))  # it arrived before the one applied
    assert not store.apply(_failure(_scope(), at=5))
    assert store.apply(_answer(_scope(league=League.NBA), _contest("n1", league=League.NBA), at=1))  # its own order
    store.reconcile(MonotonicTime(10))
    assert _raws(store) == ["g1", "n1"]


def test_an_older_partial_applied_after_a_newer_complete_is_ignored_and_a_newer_partial_keeps_what_it_omits() -> None:
    store = ContestStore()
    _cover(store)
    store.apply(_answer(_scope(), _contest("g1"), _contest("g2"), at=10))
    late = _answer(_scope(), _contest("g1", GameStatus.LIVE), _contest("g3"), at=5, skipped=("a row",))
    assert store.take(ScopeReport(span=_span(), observations=(late,))) == (late,)  # ignored
    store.reconcile(MonotonicTime(10))
    assert _raws(store) == ["g1", "g2"] and _status(store, "g1") is GameStatus.SCHEDULED
    store.apply(_answer(_scope(), _contest("g2", GameStatus.LIVE), at=20, skipped=("g1's row",)))
    change = store.reconcile(MonotonicTime(20))
    assert change.updated == (_id("g2"),) and change.removed == ()
    assert _raws(store) == ["g1", "g2"]  # g1's row was skipped, not refuted: it stays
    assert _own(store, _scope(), 20).outcome is ScopeOutcome.PARTIAL


def test_an_empty_complete_answer_removes_only_its_own_scope_s_members() -> None:
    store = ContestStore()
    _cover(store, window=2)
    _cover(store, league=League.NBA)
    store.apply(_answer(_scope(), _contest("g1"), at=0))
    store.apply(_answer(_scope(DAY - timedelta(days=1)), _contest("late", GameStatus.LIVE), at=0))
    store.apply(_answer(_scope(league=League.NBA), _contest("n1", league=League.NBA), at=0))
    store.reconcile(MonotonicTime(0))
    store.apply(_answer(_scope(league=League.NBA), at=30))  # a confirmed day without games
    assert store.reconcile(MonotonicTime(30)).removed == (_id("n1", League.NBA),)
    assert _raws(store) == ["g1", "late"]
    nba = _league(store, 30, League.NBA)
    assert nba.status is ProviderStatus.FRESH and nba.complete == _at(30)


def test_a_failed_answer_keeps_the_membership_and_counts_against_its_health_until_an_answer() -> None:
    store = ContestStore()
    _cover(store)
    store.apply(_answer(_scope(), _contest("g1"), at=0))
    store.reconcile(MonotonicTime(0))
    for at in (10, 20, 20):  # the second failure is handed twice: it counts once
        store.apply(_failure(_scope(), at=at))
    assert not store.reconcile(MonotonicTime(20)).changed and _raws(store) == ["g1"]
    failing = _own(store, _scope(), 20)
    assert (failing.outcome, failing.failures, failing.error) == (ScopeOutcome.FAILED, 2, "ProviderError: down")
    assert failing.answered == failing.complete == failing.fresh_from == _at(0) and failing.age == 20 * S
    store.apply(_answer(_scope(), _contest("g1"), at=30))
    recovered = _own(store, _scope(), 30)
    assert (recovered.failures, recovered.error) == (0, None)


# --- across scopes ----------------------------------------------------------------------------------


def test_a_contest_two_scopes_list_is_there_once_as_the_scope_that_heard_last_has_it() -> None:
    store = ContestStore()
    _cover(store, window=2)
    yesterday = _scope(DAY - timedelta(days=1))
    store.apply(_answer(_scope(), _contest("tonight"), _contest("resumed", GameStatus.LIVE), at=5))
    store.apply(_answer(yesterday, _contest("resumed", GameStatus.SUSPENDED), at=5))
    store.reconcile(MonotonicTime(5))
    assert _raws(store) == ["tonight", "resumed"]  # a tie goes to the later window
    assert _status(store, "resumed") is GameStatus.LIVE
    store.apply(_answer(yesterday, _contest("resumed", GameStatus.FINAL), at=10))  # yesterday's heard later
    assert store.reconcile(MonotonicTime(10)).updated == (_id("resumed"),)
    assert _raws(store) == ["tonight", "resumed"] and _status(store, "resumed") is GameStatus.FINAL


def test_leagues_come_in_the_order_first_covered_each_its_current_scope_first_then_the_earlier_ones() -> None:
    store = ContestStore()
    _cover(store, league=League.NBA)
    _cover(store, window=2)
    store.apply(_answer(_scope(DAY - timedelta(days=1)), _contest("late", GameStatus.LIVE), at=0))
    store.apply(_answer(_scope(), _contest("tonight"), at=0))
    store.apply(_answer(_scope(league=League.NBA), _contest("n1", league=League.NBA), at=0))
    store.reconcile(MonotonicTime(0))
    assert _raws(store) == ["n1", "tonight", "late"]


# --- rolling dates ------------------------------------------------------------------------------------


def test_on_a_fresh_start_yesterday_s_finals_stay_away_and_a_game_the_board_had_stays_through_its_final() -> None:
    store = ContestStore()
    _cover(store, window=2)
    yesterday = _scope(DAY - timedelta(days=1))
    store.apply(_answer(yesterday, _contest("late", GameStatus.LIVE), _contest("done", GameStatus.FINAL), at=0))
    store.reconcile(MonotonicTime(0))
    assert _raws(store) == ["late"]
    store.apply(_answer(yesterday, _contest("late", GameStatus.FINAL), _contest("done", GameStatus.FINAL), at=60))
    store.reconcile(MonotonicTime(60))
    assert _raws(store) == ["late"]


def test_a_retired_scope_keeps_a_game_in_play_then_its_final_for_the_hold_then_is_dropped() -> None:
    store = ContestStore()
    yesterday = _scope(DAY - timedelta(days=1))
    _cover(store, window=2)
    games = (_contest("marathon", GameStatus.LIVE), _contest("rained", GameStatus.POSTPONED))
    store.apply(_answer(yesterday, *games, at=0))
    store.reconcile(MonotonicTime(0))
    assert _raws(store) == ["marathon", "rained"]
    _cover(store, DAY + timedelta(days=1), window=2)  # midnight: the 16th rolls off
    store.apply(_answer(yesterday, *games, _contest("unseen", GameStatus.LIVE), at=10))  # one the board never had
    store.reconcile(MonotonicTime(10))
    assert _raws(store) == ["marathon"] and yesterday in store.scopes  # the postponed game retires
    store.apply(_answer(yesterday, _contest("marathon", GameStatus.FINAL), at=20))
    store.reconcile(MonotonicTime(20))  # first handed out final now
    store.reconcile(MonotonicTime(20) + (FINAL_HOLD - S))
    assert _raws(store) == ["marathon"]
    store.reconcile(MonotonicTime(20) + FINAL_HOLD)
    assert _raws(store) == [] and yesterday not in store.scopes  # nothing left: it's no longer asked
    assert not store.apply(_answer(yesterday, at=4000))  # a scope no source covers takes nothing


def test_a_league_s_current_scope_cant_retire_and_retiring_one_the_store_doesnt_hold_does_nothing() -> None:
    store = ContestStore()
    _cover(store)
    with pytest.raises(ValueError, match="can't retire"):
        store.retire(_scope())
    store.retire(_scope(DAY - timedelta(days=9)))
    assert store.scopes == (_scope(),)


# --- health ---------------------------------------------------------------------------------------------


def test_a_scope_is_fresh_within_its_max_age_on_the_monotonic_clock_then_stale() -> None:
    store = ContestStore()
    _cover(store)
    assert _league(store, 0).status is ProviderStatus.NEVER_LOADED
    received = ClockReading(T - timedelta(minutes=10), MonotonicTime(0))  # the wall clock had stepped back
    store.apply(ScopeObservation.answer(_scope(), ProviderUpdate(source=SOURCE, observed_at=T), received))
    fresh = _league(store, 120.9)
    assert fresh.status is ProviderStatus.FRESH and fresh.age == 120 * S
    stale = _league(store, 121)
    assert stale.status is ProviderStatus.STALE and stale.max_age == MAX_AGE


def test_a_failed_scope_goes_stale_on_its_own_clock_while_another_league_stays_fresh() -> None:
    store = ContestStore()
    _cover(store)
    _cover(store, league=League.NBA)
    store.apply(_answer(_scope(league=League.NBA), at=0))
    for at in (0, 60, 120):
        store.apply(_answer(_scope(), _contest("g1"), at=at))
        store.apply(_failure(_scope(league=League.NBA), at=at + 1))
    mlb, nba = store.league_healths(MonotonicTime(122))
    assert (mlb.scope.league, mlb.status) == (League.MLB, ProviderStatus.FRESH)
    assert (nba.scope.league, nba.status, nba.failures) == (League.NBA, ProviderStatus.STALE, 3)


def test_a_new_current_scope_stands_on_its_predecessor_s_last_answer_until_its_own() -> None:
    store = ContestStore()
    today, tomorrow = _scope(), _scope(DAY + timedelta(days=1))
    _cover(store, window=2)
    store.apply(_answer(today, at=0))
    _cover(store, DAY + timedelta(days=1), window=2)  # midnight
    store.apply(_answer(today, at=90))  # the day before, still asked: it no longer counts for the league
    carried = _league(store, 120)
    assert carried.scope == tomorrow and carried.status is ProviderStatus.FRESH
    assert carried.fresh_from == _at(0) and carried.answered is None
    assert _league(store, 121).status is ProviderStatus.STALE
    store.apply(_failure(tomorrow, at=125))
    failing = _league(store, 125)
    assert failing.error == "ProviderError: down" and failing.fresh_from == _at(0)
    store.apply(_answer(tomorrow, at=130))
    assert _league(store, 130).status is ProviderStatus.FRESH
    assert _own(store, today, 130).fresh_from == _at(90)  # its own


def test_a_new_current_scope_after_one_that_never_answered_is_never_loaded() -> None:
    store = ContestStore()
    _cover(store)
    _cover(store, DAY + timedelta(days=1))
    health = _league(store, 5)
    assert health.status is ProviderStatus.NEVER_LOADED and health.age is None
