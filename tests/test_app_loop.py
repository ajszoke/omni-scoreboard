"""Integration tests for AppLoop.run_once: the whole spine, one deterministic tick."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from omni.app.contest_store import ContestStore
from omni.app.display import RecordingDisplaySink
from omni.app.loop import AppLoop
from omni.app.pipeline import LiveBaseballPipeline
from omni.app.supervisor import ProviderStatus, ProviderSupervisor
from omni.cards.base import ScoreboardCard
from omni.cards.factory import CardFactory
from omni.core.enum import GameStatus, League, PanelProfile
from omni.core.ids import LeagueScopedId, SourceRef
from omni.core.time import DurationSeconds
from omni.domain.baseball import BaseballBaseState, BaseballCount, BaseballGameState, InningPhase
from omni.domain.contest import TeamGame
from omni.events.baseball import LiveBaseballFeed
from omni.providers.base import ProviderError, ProviderUpdate
from omni.providers.mlb_teams import MlbTeamRegistry
from omni.queue.delay_policy import DelayPolicy
from omni.queue.priority import PriorityScorer
from omni.queue.scheduler import InterleavedCardQueue
from omni.renderers.canvas import Canvas
from omni.renderers.context import RenderContext
from omni.renderers.image import LogoStore
from omni.renderers.registry import RendererRegistry, default_registry

_REG = MlbTeamRegistry.from_color_file()
SOURCE = SourceRef("mlb_statsapi", "https://statsapi.mlb.com")
T = datetime(2026, 6, 17, 23, 0, 0, tzinfo=timezone.utc)
QUAD = PanelProfile.QUAD_128X64


def _game(raw: str = "g1", status: GameStatus = GameStatus.LIVE) -> TeamGame:
    return TeamGame(
        id=LeagueScopedId(League.MLB, SOURCE, raw),
        league=League.MLB,
        status=status,
        scheduled_start=T,
        away=_REG.resolve(115),
        home=_REG.resolve(119),
    )


def _state(away: int = 1, home: int = 2) -> BaseballGameState:
    return BaseballGameState(
        away_score=away,
        home_score=home,
        inning=5,
        phase=InningPhase.TOP,
        count=BaseballCount(balls=0, strikes=0, outs=0),
        bases=BaseballBaseState(),
    )


class _Provider:
    """A schedule provider that can be told to fail."""

    source = SOURCE
    league = League.MLB

    def __init__(self, *contests: TeamGame) -> None:
        self._contests = contests
        self.fail = False

    def refresh(self, now: datetime) -> ProviderUpdate:
        if self.fail:
            raise ProviderError("schedule fetch down")
        return ProviderUpdate(source=SOURCE, observed_at=now, contests=self._contests)


def _fetch_feed(game: TeamGame, now: datetime) -> LiveBaseballFeed:
    return LiveBaseballFeed(state=_state())


class _CapturingRegistry(RendererRegistry):
    """Wraps a real registry, recording the RenderContext the loop hands each render."""

    def __init__(self, inner: RendererRegistry) -> None:
        super().__init__()
        self._inner = inner
        self.last_context: RenderContext | None = None

    def render(self, card: ScoreboardCard[Any], context: RenderContext, canvas: Canvas) -> None:
        self.last_context = context
        self._inner.render(card, context, canvas)


def _build(
    *,
    lag: int = 0,
    registry: RendererRegistry | None = None,
    provider: _Provider | None = None,
    logos: LogoStore | None = None,
) -> tuple[AppLoop, RecordingDisplaySink, _Provider]:
    queue = InterleavedCardQueue()
    pipeline = LiveBaseballPipeline(
        scorer=PriorityScorer(),
        delay_policy=DelayPolicy(broadcast_lag=DurationSeconds(lag)),
        factory=CardFactory(),
        queue=queue,
    )
    provider = provider if provider is not None else _Provider(_game())
    sink = RecordingDisplaySink(QUAD)
    loop = AppLoop(
        supervisor=ProviderSupervisor(provider, max_age=DurationSeconds(120)),
        store=ContestStore(),
        pipeline=pipeline,
        queue=queue,
        registry=registry if registry is not None else default_registry(),
        sink=sink,
        fetch_feed=_fetch_feed,
        logos=logos,
    )
    return loop, sink, provider


def test_run_once_shows_a_live_card_end_to_end() -> None:
    loop, sink, _ = _build(lag=0)
    res = loop.run_once(T)
    assert res.provider_status is ProviderStatus.FRESH
    assert res.reconciliation is not None and res.reconciliation.added == (_game().id,)
    assert res.shown is not None and res.render_error is None
    assert sink.committed == 1


def test_run_once_buffers_within_delay_then_surfaces() -> None:
    loop, sink, _ = _build(lag=30)
    first = loop.run_once(T)
    assert first.shown is None and sink.committed == 0  # still inside the TV delay
    second = loop.run_once(T + timedelta(seconds=30))
    assert second.shown is not None and sink.committed == 1


def test_run_once_survives_provider_outage_from_the_start() -> None:
    provider = _Provider(_game())
    provider.fail = True
    loop, sink, _ = _build(lag=0, provider=provider)
    res = loop.run_once(T)
    assert res.provider_status is ProviderStatus.NEVER_LOADED
    assert res.reconciliation is None  # nothing to apply
    assert res.shown is None and res.render_error is None  # no crash, nothing to show
    assert sink.committed == 0


def test_run_once_keeps_serving_last_known_good_when_provider_drops() -> None:
    provider = _Provider(_game())
    loop, sink, _ = _build(lag=0, provider=provider)
    loop.run_once(T)  # success — card shown
    assert sink.committed == 1
    provider.fail = True
    res = loop.run_once(T + timedelta(seconds=10))  # provider down, schedule cached
    assert res.shown is not None  # still showing the game from last-known-good
    assert sink.committed == 2


def test_run_once_isolates_a_render_failure() -> None:
    loop, sink, _ = _build(lag=0, registry=RendererRegistry())  # empty — no renderer registered
    res = loop.run_once(T)
    assert res.render_error is not None and "no renderer" in res.render_error
    assert res.shown is None
    assert sink.committed == 0  # nothing committed on failure
    assert res.pipeline.ingested  # but the pipeline still built + ingested the card


def test_run_once_threads_the_logo_store_into_the_render_context() -> None:
    # The running app's logo store must reach the renderer as ambient RenderContext.
    capturing = _CapturingRegistry(default_registry())
    store = LogoStore()
    loop, _, _ = _build(lag=0, registry=capturing, logos=store)
    loop.run_once(T)
    assert capturing.last_context is not None
    assert capturing.last_context.logos is store


def test_run_once_defaults_to_no_logo_store() -> None:
    # A loop built without logos (a test/replay) renders with the colour-bar fallback.
    capturing = _CapturingRegistry(default_registry())
    loop, _, _ = _build(lag=0, registry=capturing)
    loop.run_once(T)
    assert capturing.last_context is not None
    assert capturing.last_context.logos is None
