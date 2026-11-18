"""The board with both leagues: `AppLoop.refresh` and `paint` with baseball's pipeline and NFL's into one queue, and the
builders that put NFL on the board (`build_loop`, `build_planned_loop`).

NFL's slate is a fake scope source and its answers a fake `NflFeeds` (`tests.test_app_nfl_pipeline`), on a fake clock.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable
from datetime import datetime, timedelta
from typing import Any

import pytest

from omni.app.contest_store import ContestStore
from omni.app.display import RecordingDisplaySink
from omni.app.loop import AppLoop
from omni.app.nfl_pipeline import LiveFootballPipeline
from omni.app.pipeline import LiveBaseballPipeline, PipelineResult
from omni.app.planned_mlb import MLB_POLICIES, PlannedMlb
from omni.app.poll_plan import PollPlan
from omni.app.runner import NflSource, build_loop, build_planned_loop
from omni.cards.base import CardId
from omni.cards.factory import CardFactory
from omni.core.clock import ClockReading, FakeClock, MonotonicTime
from omni.core.enum import GameStatus, League, PanelProfile
from omni.core.ids import LeagueScopedId, SourceRef
from omni.core.time import DurationSeconds
from omni.domain.baseball import BaseballBaseState, BaseballCount, BaseballGameState, InningPhase
from omni.domain.contest import Contest, TeamGame
from omni.domain.football import FootballInterval
from omni.events.baseball import LiveBaseballFeed
from omni.providers.base import (
    CurrentSlate,
    PollScope,
    ProviderUpdate,
    ScopeObservation,
    ScopeReport,
    ScopeSpan,
)
from omni.providers.mlb_teams import MlbTeamRegistry
from omni.providers.nflmeta import NFLMETA_SOURCE, NflPhase
from omni.queue.delay_policy import DelayPolicy
from omni.queue.priority import PriorityScorer
from omni.queue.scheduler import InterleavedCardQueue
from omni.renderers.registry import default_registry
from tests.test_app_nfl_pipeline import KICKOFF, LIVE_KEY, T, FakeNflFeeds, drive, nfl_game
from tests.test_planned_mlb import _FakeMlb

MLB_SOURCE = SourceRef("mlb_statsapi", "https://statsapi.mlb.com")
_MLB = MlbTeamRegistry.from_color_file()
GAME = nfl_game()
FINAL_KEY = "nfl:nflmeta:401:final"


def _mlb_game(raw: str = "401") -> TeamGame:
    return TeamGame(
        id=LeagueScopedId(League.MLB, MLB_SOURCE, raw),
        league=League.MLB,
        status=GameStatus.LIVE,
        scheduled_start=T,
        away=_MLB.resolve(115),
        home=_MLB.resolve(119),
    )


def _mlb_feed(game: TeamGame, now: datetime) -> LiveBaseballFeed:
    return LiveBaseballFeed(
        state=BaseballGameState(
            away_score=1,
            home_score=0,
            inning=4,
            phase=InningPhase.TOP,
            count=BaseballCount(balls=0, strikes=0, outs=1),
            bases=BaseballBaseState(),
            away_hits=3,
            home_hits=2,
        )
    )


class FakeSlate:
    """One league's slate as a single current scope, answered at once with the games it's given."""

    def __init__(self, league: League, source: SourceRef, *games: Contest) -> None:
        self.scope = PollScope(source, league, CurrentSlate())
        self.games = games

    def observe(self, at: ClockReading, held: Collection[PollScope]) -> ScopeReport:
        update = ProviderUpdate(source=self.scope.source, observed_at=at.utc, contests=self.games)
        span = ScopeSpan(current=self.scope, max_age=timedelta(minutes=2))
        return ScopeReport(span=span, observations=(ScopeObservation.answer(self.scope, update, at),))


def nfl_slate(*games: Contest) -> FakeSlate:
    return FakeSlate(League.NFL, NFLMETA_SOURCE, *games)


class _Board:
    """Both leagues' pipelines into one queue and one board, on a fake clock from ``T``."""

    def __init__(self, *slates: FakeSlate, profile: PanelProfile = PanelProfile.QUAD_128X64, nfl_lag: int = 0) -> None:
        self.clock = FakeClock(T)
        self.feeds = FakeNflFeeds()
        self.queue = InterleavedCardQueue()
        self.baseball = _SpyBaseball(
            scorer=PriorityScorer(),
            delay_policy=DelayPolicy(broadcast_lag=DurationSeconds(0)),
            factory=CardFactory(),
            queue=self.queue,
        )
        self.football = _SpyFootball(
            feeds=self.feeds, delay_policy=DelayPolicy(broadcast_lag=DurationSeconds(nfl_lag)), queue=self.queue
        )
        self.sink = RecordingDisplaySink(profile)
        self.loop = AppLoop(
            schedules=slates,
            store=ContestStore(),
            pipeline=self.baseball,
            queue=self.queue,
            registry=default_registry(),
            sink=self.sink,
            fetch_feed=_mlb_feed,
            football=self.football,
        )

    def to(self, seconds: float) -> ClockReading:
        self.clock.advance(T + timedelta(seconds=seconds) - self.clock.now())
        return self.clock.read()


class _SpyBaseball(LiveBaseballPipeline):
    """Baseball's pipeline, noting the games each pass hands it."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.given: list[set[LeagueScopedId]] = []

    def refresh(self, games: Iterable[TeamGame], **kwargs: Any) -> PipelineResult:
        games = list(games)
        self.given.append({game.id for game in games})
        return super().refresh(games, **kwargs)


class _SpyFootball(LiveFootballPipeline):
    """NFL's pipeline, noting the games each pass hands it."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.given: list[set[LeagueScopedId]] = []

    def refresh(self, games: Iterable[TeamGame], *, at: ClockReading) -> PipelineResult:
        games = list(games)
        self.given.append({game.id for game in games})
        return super().refresh(games, at=at)


# --- AppLoop ------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("profile", list(PanelProfile))
def test_an_nfl_game_s_live_card_and_then_its_final_are_drawn_on_every_profile(profile: PanelProfile) -> None:
    board = _Board(nfl_slate(GAME), profile=profile)
    at = board.to(0)
    board.feeds.answer(GAME, KICKOFF, at)
    refresh = board.loop.refresh(at)
    assert refresh.pipeline.ingested == (CardId(LIVE_KEY),)
    painted = board.loop.paint(at)
    assert painted.shown == CardId(LIVE_KEY) and painted.render_error is None and board.sink.committed == 1
    over = drive(17, 10, period=4, interval=FootballInterval.FINAL)
    at = board.to(1)
    board.feeds.answer(GAME, over, at, phase=NflPhase.POST)
    assert board.loop.refresh(at).pipeline.finals == (CardId(FINAL_KEY),)
    board.loop.paint(at)
    final = board.loop.paint(board.to(10))  # the live card has had its minimum, and hands off
    assert final.shown == CardId(FINAL_KEY) and final.render_error is None and board.sink.committed == 3


def test_a_baseball_game_never_reaches_the_football_pipeline_and_no_other_league_reaches_either() -> None:
    mlb = _mlb_game()
    ncaa = TeamGame(
        id=LeagueScopedId(League.NCAAF, SourceRef("x"), "401"),
        league=League.NCAAF,
        status=GameStatus.LIVE,
        scheduled_start=T,
        away=nfl_game().away,
        home=nfl_game().home,
    )
    board = _Board(
        FakeSlate(League.MLB, MLB_SOURCE, mlb), nfl_slate(GAME), FakeSlate(League.NCAAF, SourceRef("x"), ncaa)
    )
    at = board.to(0)
    board.feeds.answer(GAME, KICKOFF, at)
    refresh = board.loop.refresh(at)
    assert board.baseball.given == [{mlb.id}] and board.football.given == [{GAME.id}]
    assert set(refresh.pipeline.ingested) == {CardId(f"{mlb.id}:live"), CardId(LIVE_KEY)}  # both, as one result


def test_both_leagues_share_the_board_and_a_game_in_nfl_s_delay_holds_nothing_up() -> None:
    mlb = _mlb_game()
    board = _Board(FakeSlate(League.MLB, MLB_SOURCE, mlb), nfl_slate(GAME), nfl_lag=30)
    at = board.to(0)
    board.feeds.answer(GAME, KICKOFF, at)
    first = board.loop.refresh(at)
    assert first.pipeline.held == (GAME.id,) and first.health.held == 1
    assert first.health.held_until == T + timedelta(seconds=30)
    assert board.loop.refresh_deadline(at) == MonotonicTime(30)  # NFL's state clears its own delay
    assert board.loop.paint(at).shown == CardId(f"{mlb.id}:live")  # MLB's card airs meanwhile
    shown = set()
    for second in range(30, 80):
        at = board.to(second)
        board.loop.refresh(at)
        painted = board.loop.paint(at)
        if painted.shown is not None:
            shown.add(painted.shown)
    assert shown == {CardId(f"{mlb.id}:live"), CardId(LIVE_KEY)}  # they take turns


# --- the builders -------------------------------------------------------------------------------------------------


class _MlbSlate:
    """MLB's schedule as a plain provider, with one live game."""

    source = MLB_SOURCE
    league = League.MLB

    def refresh(self, now: datetime) -> ProviderUpdate:
        return ProviderUpdate(source=MLB_SOURCE, observed_at=now, contests=(_mlb_game("g1"),))


def _reading(seconds: float) -> ClockReading:
    return ClockReading(T + timedelta(seconds=seconds), MonotonicTime(seconds))


def test_build_loop_puts_nfl_on_the_board_held_for_its_own_delay() -> None:
    feeds = FakeNflFeeds()
    sink = RecordingDisplaySink(PanelProfile.QUAD_128X64)
    loop = build_loop(
        _MlbSlate(),
        _mlb_feed,
        sink,
        broadcast_lag=DurationSeconds(0),
        nfl=NflSource(nfl_slate(GAME), feeds),
        nfl_delay=DurationSeconds(30),
    )
    feeds.answer(GAME, KICKOFF, _reading(0))
    first = loop.refresh(_reading(0))
    assert [league.league for league in first.leagues] == [League.MLB, League.NFL]
    assert first.pipeline.held == (GAME.id,) and loop._queue.card(LIVE_KEY) is None
    assert first.leagues[1].reason == ""  # no quota holds it back
    assert loop.refresh(_reading(30)).pipeline.ingested[-1] == CardId(LIVE_KEY)


def test_a_quota_holding_one_league_back_says_so_in_that_league_s_health() -> None:
    def mlb_quota(scope: PollScope) -> str | None:
        return "quota: StatsAPI slowed" if scope.league is League.MLB else None

    def nfl_quota(scope: PollScope) -> str | None:
        return "quota: NFLMeta rows 8% left" if scope.league is League.NFL else None

    loop = build_loop(
        _MlbSlate(),
        _mlb_feed,
        RecordingDisplaySink(PanelProfile.QUAD_128X64),
        quota=mlb_quota,
        nfl=NflSource(nfl_slate(GAME), FakeNflFeeds(), held_back=nfl_quota),
    )
    mlb, nfl = loop.refresh(_reading(0)).leagues
    assert (mlb.reason, nfl.reason) == ("quota: StatsAPI slowed", "quota: NFLMeta rows 8% left")


def test_the_running_board_takes_nfl_beside_its_planned_mlb() -> None:
    clock = FakeClock(T)
    planned = PlannedMlb(PollPlan(MLB_POLICIES), clock, _FakeMlb(clock))
    feeds = FakeNflFeeds()
    loop = build_planned_loop(
        planned,
        RecordingDisplaySink(PanelProfile.QUAD_128X64),
        nfl=NflSource(nfl_slate(GAME), feeds),
        nfl_delay=DurationSeconds(20),
    )
    feeds.answer(GAME, KICKOFF, clock.read())
    refresh = loop.refresh(clock.read())
    assert League.NFL in {league.league for league in refresh.leagues}
    assert refresh.pipeline.releases == ((GAME.id, T + timedelta(seconds=20)),)
