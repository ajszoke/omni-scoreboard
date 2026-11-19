"""What `AppLoop.refresh` tells the board's policy, and when a quiet league is offline.

Both leagues' pipelines run over fake slates and feeds on a fake clock. The board's input is read off a recording
`BoardSession`.
"""

from __future__ import annotations

from collections.abc import Collection
from datetime import datetime, timedelta
from typing import Any

import pytest

from omni.app import loop as loop_module
from omni.app.contest_store import ContestStore
from omni.app.display import RecordingDisplaySink
from omni.app.trace import DecisionTracer
from omni.app.loop import AppLoop
from omni.app.nfl_pipeline import LiveFootballPipeline
from omni.app.pipeline import LiveBaseballPipeline
from omni.app.runner import NflSource, build_loop
from omni.cards.factory import CardFactory
from omni.core.clock import ClockReading, FakeClock, MonotonicTime
from omni.core.enum import DisplayPriority, League, PanelProfile, UpdateUrgency
from omni.core.ids import LeagueScopedId
from omni.core.time import DurationSeconds
from omni.domain.baseball import BaseballBaseState, BaseballCount, BaseballGameState, BaseballScoringImpact, InningPhase
from omni.domain.contest import TeamGame
from omni.events.base import EventImportance
from omni.events.baseball import BaseballGameEvent, BaseballGameEventType, BaseballPlayPayload, LiveBaseballFeed
from omni.policy.promotion import PromotionKind
from omni.policy.names import PolicyName
from omni.policy.settings import PolicySettings
from omni.policy.view import PolicyView
from omni.providers.base import CurrentSlate, PollScope, ProviderUpdate, ScopeObservation, ScopeReport, ScopeSpan
from omni.providers.nflmeta import NFLMETA_SOURCE
from omni.queue.delay_policy import DelayPolicy
from omni.queue.priority import PriorityScorer
from omni.queue.scheduler import InterleavedCardQueue
from omni.renderers.registry import default_registry
from omni.tower.board import BoardHealth, BoardSession, SourceHealth
from tests.test_app_loop_nfl import MLB_SOURCE, FakeSlate, _mlb_game, nfl_slate
from tests.test_app_nfl_pipeline import T, FakeNflFeeds, drive, nfl_game

S = timedelta(seconds=1)
GAME = nfl_game()
QUAD = PanelProfile.QUAD_128X64


class _RecordingBoard(BoardSession):
    """The board session, keeping each view it was published."""

    published: list[PolicyView] = []

    def publish(self, health: BoardHealth, policy: PolicyView = PolicyView(), *, after: int = 0) -> None:
        _RecordingBoard.published.append(policy)
        super().publish(health, policy, after=after)


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> list[PolicyView]:
    _RecordingBoard.published = []
    monkeypatch.setattr(loop_module, "BoardSession", _RecordingBoard)
    return _RecordingBoard.published


def _seconds(seconds: float) -> ClockReading:
    return ClockReading(T + timedelta(seconds=seconds), MonotonicTime(float(seconds)))


def _mlb_state(*, away_hits: int = 3, home: int = 0) -> BaseballGameState:
    return BaseballGameState(
        away_score=0,
        home_score=home,
        inning=7,
        phase=InningPhase.BOTTOM,
        count=BaseballCount(balls=0, strikes=0, outs=1),
        bases=BaseballBaseState(),
        away_hits=away_hits,
        home_hits=3,
    )


def _loop(
    *slates: Any, feed: Any, feeds: FakeNflFeeds, mlb_lag: int = 0, settings: PolicySettings | None = None
) -> tuple[AppLoop, InterleavedCardQueue]:
    queue = InterleavedCardQueue()
    loop = AppLoop(
        settings=settings,
        schedules=slates,
        store=ContestStore(),
        pipeline=LiveBaseballPipeline(
            scorer=PriorityScorer(),
            delay_policy=DelayPolicy(broadcast_lag=DurationSeconds(mlb_lag)),
            factory=CardFactory(),
            queue=queue,
        ),
        queue=queue,
        registry=default_registry(),
        sink=RecordingDisplaySink(QUAD),
        fetch_feed=feed,
        football=LiveFootballPipeline(
            feeds=feeds, delay_policy=DelayPolicy(broadcast_lag=DurationSeconds(0)), queue=queue
        ),
    )
    return loop, queue


def test_a_refresh_publishes_one_view_of_both_leagues_games_and_their_offers_to_the_board(
    recorded: list[PolicyView],
) -> None:
    mlb = _mlb_game()
    feeds = FakeNflFeeds()

    def bid(game: TeamGame, now: datetime) -> LiveBaseballFeed:
        return LiveBaseballFeed(state=_mlb_state(away_hits=0))  # a no-hit bid through 7

    loop, _ = _loop(FakeSlate(League.MLB, MLB_SOURCE, mlb), nfl_slate(GAME), feed=bid, feeds=feeds)
    feeds.answer(GAME, drive(), _seconds(0))
    refresh = loop.refresh(_seconds(0))
    [view] = recorded
    assert set(view.games) == {mlb.id, GAME.id} and refresh.policy == view
    assert [offer.kind for offer in view.offers] == [PromotionKind.REMINDER]
    assert view.offers[0].card.raw == f"{mlb.id}:nohitter"


def test_an_offer_whose_card_the_queue_lets_go_in_the_same_refresh_isn_t_published(recorded: list[PolicyView]) -> None:
    mlb = _mlb_game()
    answers = [LiveBaseballFeed(state=_mlb_state(), received=_seconds(0), as_of=T)]
    homer = BaseballGameEvent(
        id=LeagueScopedId(League.MLB, MLB_SOURCE, "e1"),
        contest=mlb,
        event_type=BaseballGameEventType.HOME_RUN,
        source=MLB_SOURCE,
        source_time=T - 108 * S,  # reported 118 s after it happened: its card clears the delay long before its state
        observed_at=T - 108 * S,
        importance=EventImportance(
            priority=DisplayPriority.ALERT, urgency=UpdateUrgency.HIGH, leverage=0.0, rarity=0.5, favorite_relevance=0.0
        ),
        payload=BaseballPlayPayload(
            inning=7,
            phase=InningPhase.BOTTOM,
            description="a homer",
            rbi=1,
            away_score=0,
            home_score=1,
            scoring_impact=BaseballScoringImpact(rbi=1, go_ahead=True),
        ),
    )
    answers.append(LiveBaseballFeed(state=_mlb_state(home=1), events=(homer,), received=_seconds(10), as_of=T + 10 * S))

    def feed(game: TeamGame, now: datetime) -> LiveBaseballFeed:
        return [answer for answer in answers if answer.received is not None and answer.received.utc <= now][-1]

    loop, queue = _loop(FakeSlate(League.MLB, MLB_SOURCE, mlb), feed=feed, feeds=FakeNflFeeds(), mlb_lag=120)
    for moment in (0, 10, 12):
        loop.refresh(_seconds(moment))
    card = f"{mlb.id}:bigplay:e1"
    assert queue.card(card) is not None and recorded[-1].offers == ()  # up; the state after it is still held
    refresh = loop.refresh(_seconds(130))  # the state clears, but the card has 2 s left, short of its minimum
    assert [offer.card.raw for offer in refresh.pipeline.policy.offers] == [card]  # the pipeline offered it
    assert [missed.card.dedupe_key.raw for missed in refresh.unseen] == [card] and queue.card(card) is None
    assert recorded[-1].offers == () and refresh.policy.offers == ()  # but the board is never told of it


# --- a quiet league's health ------------------------------------------------------------------------------------------


class _QuietSlate:
    """NFL's slate, answered once at the start, then quiet by plan: its answer counts as fresh for ``max_age``."""

    def __init__(self, max_age: timedelta) -> None:
        self.scope = PollScope(NFLMETA_SOURCE, League.NFL, CurrentSlate())
        self.max_age = max_age
        self.answered = False

    def observe(self, at: ClockReading, held: Collection[PollScope]) -> ScopeReport:
        observations: tuple[ScopeObservation, ...] = ()
        if not self.answered:
            self.answered = True
            update = ProviderUpdate(source=NFLMETA_SOURCE, observed_at=at.utc, contests=(GAME,))
            observations = (ScopeObservation.answer(self.scope, update, at),)
        return ScopeReport(span=ScopeSpan(current=self.scope, max_age=self.max_age), observations=observations)


def _quiet_board(max_age: timedelta) -> tuple[AppLoop, FakeClock]:
    clock = FakeClock(T)
    loop = build_loop(
        None,
        _no_feed,
        RecordingDisplaySink(QUAD),
        nfl=NflSource(schedule=_QuietSlate(max_age), feeds=FakeNflFeeds()),
    )
    return loop, clock


def _no_feed(game: TeamGame, now: datetime) -> LiveBaseballFeed:
    raise AssertionError("no baseball here")


def _nfl_health(loop: AppLoop, clock: FakeClock, at: timedelta) -> Any:
    clock.advance(T + at - clock.now())
    [league] = loop.refresh(clock.read()).leagues
    return league


def test_a_league_quiet_by_plan_is_never_offline_and_one_that_missed_its_due_read_is_offline_ten_minutes_after() -> (
    None
):
    kickoff_read = 3 * 60 * 60 * S  # nothing is due before kickoff, three hours out
    loop, clock = _quiet_board(kickoff_read + 2 * 60 * S)
    _nfl_health(loop, clock, timedelta(0))
    assert _nfl_health(loop, clock, 20 * 60 * S).source is SourceHealth.FRESH  # quiet for 20 minutes: fresh
    assert _nfl_health(loop, clock, kickoff_read + 2 * 60 * S).source is SourceHealth.FRESH
    missed = _nfl_health(loop, clock, kickoff_read + 2 * 60 * S + S)  # its due read hasn't answered for 2 minutes
    assert missed.source is SourceHealth.STALE
    assert missed.changes_at == MonotonicTime((kickoff_read + 10 * 60 * S + S).total_seconds())
    assert _nfl_health(loop, clock, kickoff_read + 10 * 60 * S).source is SourceHealth.STALE
    offline = _nfl_health(loop, clock, kickoff_read + 10 * 60 * S + S)  # ten minutes after its read was due
    assert offline.source is SourceHealth.OFFLINE and offline.reason == "no schedule for over 10m: no answer"


# --- settings ---------------------------------------------------------------------------------------------------------


def test_a_board_runs_with_the_settings_it_s_built_with_and_the_defaults_otherwise() -> None:
    feeds = FakeNflFeeds()
    muted = PolicySettings(promotions=False)
    built = build_loop(FakeSlate(League.MLB, MLB_SOURCE), _no_feed, RecordingDisplaySink(QUAD), settings=muted)
    assert built.settings is muted
    plain, _ = _loop(FakeSlate(League.MLB, MLB_SOURCE), feed=_no_feed, feeds=feeds)
    assert plain.settings == PolicySettings(policy=PolicyName.BOARD_V1)  # none given: the scheduler's bands


def test_under_policy_v2_the_board_decides_from_the_published_view_and_the_trace_writes_why() -> None:
    mlb = _mlb_game()
    feeds = FakeNflFeeds()

    def live(game: TeamGame, now: datetime) -> LiveBaseballFeed:
        return LiveBaseballFeed(state=_mlb_state())

    settings = PolicySettings(policy=PolicyName.BOARD_V2)
    loop, _ = _loop(FakeSlate(League.MLB, MLB_SOURCE, mlb), nfl_slate(GAME), feed=live, feeds=feeds, settings=settings)
    feeds.answer(GAME, drive(), _seconds(0))
    loop.refresh(_seconds(0))
    painted = loop.paint(_seconds(0))
    assert painted.shown is not None and painted.decisions
    lines: list[str] = []
    DecisionTracer(lines.append).painted(painted)
    assert any(line.startswith("base ") and "omni-board-policy/2" in line and "why:" in line for line in lines)
