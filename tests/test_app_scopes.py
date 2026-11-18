"""The board's slates as scopes, through the production path: `build_planned_loop` (or the `AppLoop` it builds,
with a second league beside it), `PlannedMlb` and its `PollPlan` and `Poller`, refreshed on a fake clock.

Each scope keeps its own membership in the order its answers arrived on the monotonic clock, so a wall-clock step
can't freeze or age the slate; a scope that fails keeps what it had and goes stale on its own; and a second
league's outage leaves MLB's cards and health alone.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Collection, Sequence
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from omni.app.contest_store import ContestStore, ProviderStatus
from omni.app.display import RecordingDisplaySink
from omni.app.loop import AppLoop, LeagueHealth, Refresh
from omni.app.pipeline import LiveBaseballPipeline
from omni.app.planned_mlb import MLB_POLICIES, PlannedMlb
from omni.app.poll_plan import PollKey, PollPlan, PollPriority, PollSource
from omni.app.poller import Poller
from omni.app.runner import build_loop, build_planned_loop, warm_up
from omni.cards.factory import CardFactory
from omni.core.clock import ClockReading, FakeClock, MonotonicTime
from omni.core.enum import GameStatus, League
from omni.core.ids import LeagueScopedId, SourceRef
from omni.core.time import DurationSeconds, local_date
from omni.domain.contest import Contest
from omni.providers.base import (
    ScopeSpan,
    CurrentSlate,
    LocalDate,
    PollScope,
    ProviderError,
    ProviderUpdate,
    ScopeObservation,
    ScopeReport,
    ScopeSource,
)
from omni.queue.delay_policy import DelayPolicy
from omni.queue.priority import PriorityScorer
from omni.queue.scheduler import InterleavedCardQueue
from omni.renderers.registry import default_registry
from omni.tower.board import SourceHealth
from tests.test_app_loop import SOURCE, _fetch_feed, _game
from tests.test_planned_mlb import AFTER_MIDNIGHT, QUAD, TODAY, YESTERDAY, S, _FakeMlb

NBA = SourceRef("balldontlie")
MINUTE = 60 * S


class _Board:
    """The running MLB board over a fake StatsAPI, with any other leagues' schedules beside it on one plan."""

    def __init__(self, *others: ScopeSource, plan: PollPlan | None = None, start: datetime = AFTER_MIDNIGHT) -> None:
        self.clock = FakeClock(start)
        self.plan = plan if plan is not None else PollPlan(MLB_POLICIES)
        self.provider = _FakeMlb(self.clock)
        self.planned = PlannedMlb(self.plan, self.clock, self.provider)
        self.poller = Poller(self.plan, self.clock)
        if others:
            self.loop = _loop(self.planned, *others)
        else:
            self.loop = build_planned_loop(self.planned, RecordingDisplaySink(QUAD), broadcast_lag=DurationSeconds(0))

    @property
    def store(self) -> ContestStore:
        return self.loop._store

    def ids(self) -> list[str]:
        return [contest.id.raw for contest in self.store.contests]

    def step(self, by: timedelta) -> Refresh:
        """Let ``by`` pass, ask what's due, and refresh."""
        self.clock.advance(by)
        self.poller.run_due()
        return self.loop.refresh(self.clock.read())


def _loop(planned: PlannedMlb, *others: ScopeSource) -> AppLoop:
    """The running board's spine with no delay, its schedule MLB's and the others' beside it."""
    queue = InterleavedCardQueue()
    pipeline = LiveBaseballPipeline(
        scorer=PriorityScorer(),
        delay_policy=DelayPolicy(broadcast_lag=DurationSeconds(0)),
        factory=CardFactory(),
        queue=queue,
    )
    return AppLoop(
        schedules=(planned, *others),
        store=ContestStore(),
        pipeline=pipeline,
        queue=queue,
        registry=default_registry(),
        sink=RecordingDisplaySink(QUAD),
        fetch_feed=planned.fetch_feed,
        fetch_pregame_details=planned.fetch_pregame_details,
    )


class _OtherLeague:
    """A second league's slate, one local date at a time, asked through the board's plan like MLB's. Its games
    aren't baseball, so the baseball pipeline leaves them be."""

    zone = ZoneInfo("America/Denver")

    def __init__(self, plan: PollPlan) -> None:
        self.plan = plan
        self.games: tuple[Contest, ...] = ()
        self.failing = False

    def scope(self, at: datetime) -> PollScope:
        return PollScope(NBA, League.NBA, LocalDate(local_date(at, self.zone)))

    def game(self, raw: str) -> Contest:
        return Contest(
            id=LeagueScopedId(League.NBA, NBA, raw),
            league=League.NBA,
            status=GameStatus.LIVE,
            scheduled_start=AFTER_MIDNIGHT,
        )

    def observe(self, at: ClockReading, held: Collection[PollScope]) -> ScopeReport:
        scope = self.scope(at.utc)
        key: PollKey[ProviderUpdate] = PollKey(PollSource.SLATE, scope)
        self.plan.want(key, self._fetch, priority=PollPriority.NORMAL, at=at)
        observed: list[ScopeObservation] = []
        answer = self.plan.latest(key, at)
        if answer is not None:
            observed.append(ScopeObservation.answer(scope, answer.value, answer.received))
        failure = self.plan.failure(key)
        if failure is not None:
            observed.append(ScopeObservation.failure(scope, failure.error, failure.at))
        return ScopeReport(span=ScopeSpan(current=scope, max_age=2 * MINUTE), observations=tuple(observed))

    def _fetch(self, asked: datetime) -> ProviderUpdate:
        if self.failing:
            raise ProviderError("NBA slate down")
        return ProviderUpdate(source=NBA, observed_at=asked, contests=self.games)


def _mlb(day: date) -> PollScope:
    """MLB's slate for ``day``, as the fake StatsAPI answers it."""
    return PollScope(SOURCE, League.MLB, LocalDate(day))


def _league(refresh: Refresh, league: League) -> LeagueHealth:
    [health] = [health for health in refresh.leagues if health.league is league]
    return health


def _first(timeline: Sequence[tuple[float, SourceHealth]], source: SourceHealth) -> float:
    return next(at for at, seen in timeline if seen is source)


# --- the wall clock ---------------------------------------------------------------------------------


def test_after_the_wall_clock_steps_back_ten_minutes_a_fresh_slate_replaces_the_old_one_at_once() -> None:
    board = _Board()
    old, new = _game("old", GameStatus.SCHEDULED), _game("new", GameStatus.SCHEDULED)
    board.provider.slates = {TODAY: (old,)}
    warm_up(board.loop, board.poller, board.clock)
    assert board.ids() == ["old"]
    board.clock.jump(-10 * MINUTE)  # its answers now arrive dated ten minutes before the last
    board.provider.slates[TODAY] = (new,)
    refresh = board.step(15 * S)  # today's slate is due again, and answers with only the new game
    assert (refresh.reconciliation.added, refresh.reconciliation.removed) == ((new.id,), (old.id,))
    assert board.ids() == ["new"]
    assert refresh.health.source is SourceHealth.FRESH and refresh.provider_status is ProviderStatus.FRESH


def test_a_forward_wall_clock_jump_ages_nothing_the_slate_goes_stale_by_the_monotonic_clock_alone() -> None:
    board = _Board()
    board.provider.slates = {TODAY: (_game("g1", GameStatus.SCHEDULED),)}
    warm_up(board.loop, board.poller, board.clock)  # today's slate answered at 0
    board.provider.failing = {TODAY}  # and never again
    board.clock.jump(10 * MINUTE)  # the wall clock leaps ahead: the answer looks ten minutes old
    assert board.loop.refresh(board.clock.read()).health.source is SourceHealth.FRESH
    assert board.loop.refresh_deadline(board.clock.read()) == MonotonicTime(121)  # when it goes stale
    timeline = [(second, board.step(S).health.source) for second in range(1, 131)]
    assert _first(timeline, SourceHealth.STALE) == 121  # its 2 minutes, measured since it arrived
    assert {source for second, source in timeline if second < 121} == {SourceHealth.FRESH}


def test_at_local_midnight_the_board_stays_fresh_while_the_new_date_is_first_asked() -> None:
    board = _Board(start=AFTER_MIDNIGHT - 30 * MINUTE - 10 * S)  # 23:59:50 in Denver, on the 17th
    late, tonight = _game("late"), _game("tonight", GameStatus.SCHEDULED)
    board.provider.slates = {YESTERDAY: (late,), TODAY: (tonight,)}
    warm_up(board.loop, board.poller, board.clock)
    assert board.ids() == ["late"]
    board.clock.advance(15 * S)  # past midnight: the 18th is today, and hasn't been asked yet
    refresh = board.loop.refresh(board.clock.read())
    assert refresh.health.source is SourceHealth.FRESH  # it stands on the 17th's answer, 15 s old
    assert _league(refresh, League.MLB).scope.scope == _mlb(TODAY) and board.ids() == ["late"]
    board.poller.run_due()
    assert board.loop.refresh(board.clock.read()).health.source is SourceHealth.FRESH
    assert board.ids() == ["tonight", "late"]


# --- what each scope's answers do ---------------------------------------------------------------------


class _Scripted:
    """A schedule source that hands each pass the observations queued for it, of one scope."""

    scope = PollScope(SOURCE, League.MLB, CurrentSlate())

    def __init__(self) -> None:
        self.queued: list[ScopeObservation] = []

    def hand(self, *games: Contest, received: float, skipped: tuple[str, ...] = ()) -> None:
        update = ProviderUpdate(source=SOURCE, observed_at=AFTER_MIDNIGHT, contests=games, warnings=skipped)
        moment = ClockReading(AFTER_MIDNIGHT + timedelta(seconds=received), MonotonicTime(received))
        self.queued.append(ScopeObservation.answer(self.scope, update, moment))

    def observe(self, at: ClockReading, held: Collection[PollScope]) -> ScopeReport:
        handed, self.queued = tuple(self.queued), []
        return ScopeReport(span=ScopeSpan(current=self.scope, max_age=2 * MINUTE), observations=handed)


def test_an_older_partial_landing_after_a_newer_complete_answer_is_ignored_and_a_newer_one_keeps_what_it_omits() -> (
    None
):
    source = _Scripted()
    clock = FakeClock(AFTER_MIDNIGHT)
    loop = build_loop(source, _fetch_feed, RecordingDisplaySink(QUAD))
    first, second, third = (_game(raw, GameStatus.SCHEDULED) for raw in ("g1", "g2", "g3"))
    source.hand(first, second, received=10)
    clock.advance(10 * S)
    assert loop.refresh(clock.read()).reconciliation.added == (first.id, second.id)
    source.hand(dataclasses.replace(first, status=GameStatus.PREGAME), third, received=5, skipped=("a row",))
    clock.advance(S)
    assert not loop.refresh(clock.read()).reconciliation.changed  # it arrived before the complete answer
    source.hand(dataclasses.replace(second, status=GameStatus.PREGAME), received=20, skipped=("g1's row",))
    clock.advance(9 * S)
    refresh = loop.refresh(clock.read())
    assert refresh.reconciliation.updated == (second.id,) and refresh.reconciliation.removed == ()
    assert [contest.id.raw for contest in loop._store.contests] == ["g1", "g2"]  # g1 skipped, not refuted


def test_yesterday_s_empty_complete_slate_removes_its_own_games_and_leaves_today_s() -> None:
    board = _Board()
    late, tonight = _game("late"), _game("tonight", GameStatus.SCHEDULED)
    board.provider.slates = {YESTERDAY: (late,), TODAY: (tonight,)}
    warm_up(board.loop, board.poller, board.clock)
    assert board.ids() == ["tonight", "late"]
    board.provider.slates[YESTERDAY] = ()  # the late game is gone from its date
    refresh = board.step(MINUTE)  # yesterday's slate is asked once a minute
    assert refresh.reconciliation.removed == (late.id,) and board.ids() == ["tonight"]


def test_around_midnight_a_game_both_dates_list_is_on_the_board_once_as_last_heard() -> None:
    board = _Board()
    resumed = _game("resumed", GameStatus.SUSPENDED)
    board.provider.slates = {YESTERDAY: (resumed,), TODAY: (resumed,)}
    warm_up(board.loop, board.poller, board.clock)
    board.provider.slates[TODAY] = (dataclasses.replace(resumed, status=GameStatus.LIVE),)
    board.step(15 * S)  # today's slate hears it resume; yesterday's last answer still says suspended
    assert [(contest.id.raw, contest.status) for contest in board.store.contests] == [("resumed", GameStatus.LIVE)]
    assert [contest.status for contest in board.store.membership(_mlb(YESTERDAY))] == [GameStatus.SUSPENDED]


# --- a second league -----------------------------------------------------------------------------------


def test_two_leagues_slates_for_one_date_are_two_poll_keys_and_two_memberships() -> None:
    plan = PollPlan(MLB_POLICIES)
    nba = _OtherLeague(plan)
    board = _Board(nba, plan=plan)
    board.provider.slates = {TODAY: (_game("g1", GameStatus.SCHEDULED),)}
    nba.games = (nba.game("n1"),)
    warm_up(board.loop, board.poller, board.clock)
    mlb_scope, nba_scope = _mlb(TODAY), nba.scope(board.clock.now())
    assert mlb_scope.window == nba_scope.window == LocalDate(TODAY)
    mlb_key: PollKey[ProviderUpdate] = PollKey(PollSource.SLATE, mlb_scope)
    nba_key: PollKey[ProviderUpdate] = PollKey(PollSource.SLATE, nba_scope)
    mlb_answer, nba_answer = plan.latest(mlb_key, board.clock.read()), plan.latest(nba_key, board.clock.read())
    assert mlb_answer is not None and nba_answer is not None and mlb_answer.value != nba_answer.value
    assert [game.id.raw for game in board.store.membership(mlb_scope)] == ["g1"]
    assert [game.id.raw for game in board.store.membership(nba_scope)] == ["n1"]
    assert board.ids() == ["g1", "n1"]


def test_one_league_offline_leaves_the_other_s_cards_and_health_alone() -> None:
    plan = PollPlan(MLB_POLICIES)
    nba = _OtherLeague(plan)
    board = _Board(nba, plan=plan)
    board.provider.slates = {TODAY: (_game("g1"),)}
    nba.games = (nba.game("n1"),)
    warm_up(board.loop, board.poller, board.clock)  # both slates answered at 0
    nba.failing = True
    nba_timeline: list[tuple[float, SourceHealth]] = []
    for second in range(5, 650, 5):
        refresh = board.step(5 * S)
        painted = board.loop.paint(board.clock.read())
        assert refresh.health.source is SourceHealth.FRESH  # the board's health is MLB's
        assert _league(refresh, League.MLB).source is SourceHealth.FRESH
        assert painted.shown is not None and painted.shown.raw == "mlb:mlb_statsapi:g1:live" and not painted.stale
        nba_timeline.append((second, _league(refresh, League.NBA).source))
    assert _first(nba_timeline, SourceHealth.STALE) == 125  # over its 2 minutes since its last answer
    assert _first(nba_timeline, SourceHealth.OFFLINE) == 605  # over 10
    nba_health = _league(refresh, League.NBA)
    assert nba_health.reason == "no schedule for over 10m: ProviderError: NBA slate down"
    assert nba_health.scope.status is ProviderStatus.STALE and nba_health.scope.failures > 1
    assert board.ids() == ["g1", "n1"]  # the failing scope keeps what it had


def test_a_league_that_never_answers_is_never_loaded_while_the_other_shows_its_cards() -> None:
    plan = PollPlan(MLB_POLICIES)
    nba = _OtherLeague(plan)
    nba.failing = True
    board = _Board(nba, plan=plan)
    board.provider.slates = {TODAY: (_game("g1"),)}
    warm_up(board.loop, board.poller, board.clock)
    refresh = board.step(S)
    nba_health = _league(refresh, League.NBA)
    assert nba_health.scope.status is ProviderStatus.NEVER_LOADED and nba_health.source is SourceHealth.OFFLINE
    assert nba_health.reason == "no schedule yet: ProviderError: NBA slate down"
    assert refresh.health.source is SourceHealth.FRESH and refresh.provider_status is ProviderStatus.FRESH
    assert board.loop.paint(board.clock.read()).shown is not None


def test_a_board_needs_a_schedule() -> None:
    queue = InterleavedCardQueue()
    pipeline = LiveBaseballPipeline(
        scorer=PriorityScorer(),
        delay_policy=DelayPolicy(broadcast_lag=DurationSeconds(0)),
        factory=CardFactory(),
        queue=queue,
    )
    with pytest.raises(ValueError, match="at least one schedule"):
        AppLoop(
            schedules=(),
            store=ContestStore(),
            pipeline=pipeline,
            queue=queue,
            registry=default_registry(),
            sink=RecordingDisplaySink(QUAD),
            fetch_feed=_fetch_feed,
        )
