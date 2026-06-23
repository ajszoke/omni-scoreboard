"""Tests for LiveBaseballPipeline: delay-safe live cards into the queue."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from omni.app.pipeline import LiveBaseballPipeline, PipelineResult
from omni.cards.factory import CardFactory
from omni.core.enum import GameStatus, League, PanelProfile
from omni.core.ids import LeagueScopedId, SourceRef
from omni.core.time import DurationSeconds
from omni.domain.baseball import BaseballBaseState, BaseballCount, BaseballGameState, HalfInning
from omni.domain.contest import TeamGame
from omni.providers.base import ProviderError
from omni.providers.mlb_teams import MlbTeamRegistry
from omni.queue.delay_policy import DelayPolicy
from omni.queue.priority import PriorityScorer
from omni.queue.scheduler import InterleavedCardQueue

_REG = MlbTeamRegistry.from_color_file()
SOURCE = SourceRef("mlb_statsapi", "https://statsapi.mlb.com")
T = datetime(2026, 6, 17, 23, 0, 0, tzinfo=timezone.utc)
QUAD = PanelProfile.QUAD_128X64


def _id(raw: str) -> LeagueScopedId:
    return LeagueScopedId(League.MLB, SOURCE, raw)


def _game(raw: str, status: GameStatus = GameStatus.LIVE) -> TeamGame:
    return TeamGame(
        id=_id(raw),
        league=League.MLB,
        status=status,
        scheduled_start=T,
        away=_REG.resolve(115),
        home=_REG.resolve(119),
    )


def _state(away: int = 0, home: int = 0, inning: int = 3) -> BaseballGameState:
    return BaseballGameState(
        away_score=away,
        home_score=home,
        inning=inning,
        half=HalfInning.TOP,
        count=BaseballCount(balls=0, strikes=0, outs=0),
        bases=BaseballBaseState(),
    )


class _Fetch:
    """A configurable game-state fetcher that can also fail on command."""

    def __init__(self) -> None:
        self.states: dict[str, BaseballGameState] = {}
        self.fail: set[str] = set()

    def set(self, raw: str, state: BaseballGameState) -> None:
        self.states[raw] = state

    def __call__(self, game: TeamGame) -> BaseballGameState:
        if game.id.raw in self.fail:
            raise ProviderError("game feed down")
        return self.states[game.id.raw]


def _setup(lag: int = 30) -> tuple[LiveBaseballPipeline, InterleavedCardQueue]:
    queue = InterleavedCardQueue()
    pipe = LiveBaseballPipeline(
        scorer=PriorityScorer(),
        delay_policy=DelayPolicy(broadcast_lag=DurationSeconds(lag)),
        factory=CardFactory(),
        queue=queue,
    )
    return pipe, queue


def test_holds_live_state_within_the_tv_delay() -> None:
    pipe, queue = _setup(lag=30)
    fetch = _Fetch()
    fetch.set("g1", _state())
    res = pipe.refresh([_game("g1")], now=T, fetch_state=fetch)
    assert res.held == (_id("g1"),)
    assert res.ingested == () and len(queue) == 0


def test_surfaces_a_card_once_the_delay_elapses() -> None:
    pipe, queue = _setup(lag=30)
    fetch = _Fetch()
    fetch.set("g1", _state())
    pipe.refresh([_game("g1")], now=T, fetch_state=fetch)  # buffered
    res = pipe.refresh([_game("g1")], now=T + timedelta(seconds=30), fetch_state=fetch)
    assert len(res.ingested) == 1 and len(queue) == 1


def test_shows_lag_old_state_not_the_current_spoiler() -> None:
    pipe, queue = _setup(lag=30)
    fetch = _Fetch()
    fetch.set("g1", _state(home=0))
    pipe.refresh([_game("g1")], now=T, fetch_state=fetch)  # observes 0-0
    fetch.set("g1", _state(home=1))  # a run scores...
    pipe.refresh([_game("g1")], now=T + timedelta(seconds=30), fetch_state=fetch)  # ...but it's fresh
    card = queue.next_card(T + timedelta(seconds=30), QUAD)
    assert card is not None
    assert card.payload.home_score == 0  # the delayed 0-0, never the un-aired 0-1


def test_ignores_non_live_games() -> None:
    pipe, queue = _setup()
    fetch = _Fetch()
    fetch.set("g1", _state())
    res = pipe.refresh([_game("g1", GameStatus.SCHEDULED), _game("g2", GameStatus.FINAL)], now=T, fetch_state=fetch)
    assert res == PipelineResult(ingested=(), held=(), removed=(), skipped=())
    assert len(queue) == 0


def test_isolates_per_game_fetch_failures() -> None:
    pipe, queue = _setup(lag=0)  # eligible immediately
    fetch = _Fetch()
    fetch.set("g2", _state(away=1, home=2))
    fetch.fail.add("g1")
    res = pipe.refresh([_game("g1"), _game("g2")], now=T, fetch_state=fetch)
    assert any("g1" in warning for warning in res.skipped)
    assert len(res.ingested) == 1 and len(queue) == 1  # g2 still made it through


def test_removes_card_when_a_game_leaves_the_live_set() -> None:
    pipe, queue = _setup(lag=0)
    fetch = _Fetch()
    fetch.set("g1", _state())
    pipe.refresh([_game("g1")], now=T, fetch_state=fetch)
    assert len(queue) == 1
    res = pipe.refresh([_game("g1", GameStatus.FINAL)], now=T + timedelta(seconds=10), fetch_state=fetch)
    assert res.removed == (_id("g1"),)
    assert len(queue) == 0


def test_processes_multiple_live_games() -> None:
    pipe, queue = _setup(lag=0)
    fetch = _Fetch()
    fetch.set("g1", _state())
    fetch.set("g2", _state(away=1, home=1))
    res = pipe.refresh([_game("g1"), _game("g2")], now=T, fetch_state=fetch)
    assert len(res.ingested) == 2 and len(queue) == 2


def test_drops_feed_when_a_game_leaves_before_ever_surfacing() -> None:
    pipe, queue = _setup(lag=30)
    fetch = _Fetch()
    fetch.set("g1", _state())
    res1 = pipe.refresh([_game("g1")], now=T, fetch_state=fetch)  # buffered, no card yet
    assert res1.held == (_id("g1"),) and len(queue) == 0
    res2 = pipe.refresh([_game("g1", GameStatus.FINAL)], now=T + timedelta(seconds=10), fetch_state=fetch)
    assert res2.removed == ()  # nothing was ever carded, so nothing to "remove"
    assert len(queue) == 0
    # The feed was dropped: going live again starts buffering afresh.
    res3 = pipe.refresh([_game("g1")], now=T + timedelta(seconds=20), fetch_state=fetch)
    assert res3.held == (_id("g1"),)
