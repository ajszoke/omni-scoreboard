"""What `AppLoop.refresh` tells the board's policy.

Both leagues' pipelines run over fake slates and feeds on a fake clock. The board's input is read off a recording
`BoardSession`.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest

from omni.app import loop as loop_module
from omni.app.contest_store import ContestStore
from omni.app.display import RecordingDisplaySink
from omni.app.loop import AppLoop
from omni.app.nfl_pipeline import LiveFootballPipeline
from omni.app.pipeline import LiveBaseballPipeline
from omni.cards.factory import CardFactory
from omni.core.clock import ClockReading, MonotonicTime
from omni.core.enum import DisplayPriority, League, PanelProfile, UpdateUrgency
from omni.core.ids import LeagueScopedId
from omni.core.time import DurationSeconds
from omni.domain.baseball import BaseballBaseState, BaseballCount, BaseballGameState, BaseballScoringImpact, InningPhase
from omni.domain.contest import TeamGame
from omni.events.base import EventImportance
from omni.events.baseball import BaseballGameEvent, BaseballGameEventType, BaseballPlayPayload, LiveBaseballFeed
from omni.policy.promotion import PromotionKind
from omni.policy.view import PolicyView
from omni.queue.delay_policy import DelayPolicy
from omni.queue.priority import PriorityScorer
from omni.queue.scheduler import InterleavedCardQueue
from omni.renderers.registry import default_registry
from omni.tower.board import BoardHealth, BoardSession
from tests.test_app_loop_nfl import MLB_SOURCE, FakeSlate, _mlb_game, nfl_slate
from tests.test_app_nfl_pipeline import T, FakeNflFeeds, drive, nfl_game

S = timedelta(seconds=1)
GAME = nfl_game()
QUAD = PanelProfile.QUAD_128X64


class _RecordingBoard(BoardSession):
    """The board session, keeping each view it was published."""

    published: list[PolicyView] = []

    def publish(self, health: BoardHealth, policy: PolicyView = PolicyView()) -> None:
        _RecordingBoard.published.append(policy)
        super().publish(health, policy)


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


def _loop(*slates: Any, feed: Any, feeds: FakeNflFeeds, mlb_lag: int = 0) -> tuple[AppLoop, InterleavedCardQueue]:
    queue = InterleavedCardQueue()
    loop = AppLoop(
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
