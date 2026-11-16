"""Integration tests for the AppLoop: the whole spine, refreshed and painted deterministically."""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone
from typing import Any

from omni.app.contest_store import ContestStore
from omni.app.display import RecordingDisplaySink
from omni.app.loop import AppLoop, _treatment
from omni.app.pipeline import LiveBaseballPipeline
from omni.app.supervisor import ProviderStatus, ProviderSupervisor
from omni.core.clock import ClockReading, MonotonicTime
from omni.cards.base import ScoreboardCard
from omni.cards.factory import CardFactory
from omni.core.enum import GameStatus, League, PanelProfile
from omni.core.ids import LeagueScopedId, SourceRef
from omni.core.time import DurationSeconds
from omni.domain.baseball import BaseballBaseState, BaseballCount, BaseballGameState, InningPhase
from omni.domain.contest import GolfTournament, TeamGame
from omni.events.baseball import LiveBaseballFeed
from omni.providers.base import ProviderError, ProviderUpdate
from omni.providers.mlb_teams import MlbTeamRegistry
from omni.queue.delay_policy import DelayPolicy
from omni.queue.priority import PriorityScorer
from omni.queue.scheduler import InterleavedCardQueue, SelectionReason
from omni.renderers.canvas import Canvas, RecordingCanvas
from omni.renderers.context import RenderContext
from omni.renderers.image import LogoStore
from omni.renderers.registry import RendererRegistry, default_registry
from omni.tower.board import FrameKind, SourceHealth
from omni.tower.lane import ChangeKind, EndReason

_REG = MlbTeamRegistry.from_color_file()
SOURCE = SourceRef("mlb_statsapi", "https://statsapi.mlb.com")
T = datetime(2026, 6, 17, 23, 0, 0, tzinfo=timezone.utc)
QUAD = PanelProfile.QUAD_128X64


def _at(moment: datetime) -> ClockReading:
    """``moment`` read both ways, the monotonic clock running alongside UTC."""
    return ClockReading(moment, MonotonicTime((moment - T).total_seconds()))


def _later(seconds: float) -> ClockReading:
    return _at(T + timedelta(seconds=seconds))


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
    return LiveBaseballFeed(state=_state(), received_at=T)


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
    res = loop.run_once(_at(T))
    assert res.provider_status is ProviderStatus.FRESH
    assert res.reconciliation is not None and res.reconciliation.added == (_game().id,)
    assert res.shown is not None and res.frame is None and res.render_error is None
    assert sink.committed == 1


def test_run_once_holds_the_board_within_the_delay_then_surfaces() -> None:
    loop, sink, _ = _build(lag=30)
    first = loop.run_once(_at(T))
    assert first.shown is None and first.frame is FrameKind.HELD  # the game's card is still inside the TV delay
    assert loop.refresh(T).health.held_until == T + timedelta(seconds=30)  # when it clears: the bar's end
    second = loop.run_once(_later(30))
    assert second.shown is not None and sink.committed == 2


def test_run_once_survives_a_provider_outage_from_the_start() -> None:
    provider = _Provider(_game())
    provider.fail = True
    loop, sink, _ = _build(lag=0, provider=provider)
    res = loop.run_once(_at(T))
    assert res.provider_status is ProviderStatus.NEVER_LOADED
    assert res.reconciliation is None  # nothing to apply
    assert res.shown is None and res.frame is FrameKind.OFFLINE and res.render_error is None
    assert sink.committed == 1  # the offline frame, drawn on purpose


def test_run_once_keeps_serving_last_known_good_when_provider_drops() -> None:
    provider = _Provider(_game())
    loop, sink, _ = _build(lag=0, provider=provider)
    first = loop.run_once(_at(T))
    provider.fail = True
    res = loop.run_once(_later(10))  # provider down, schedule cached
    assert res.shown == first.shown  # still showing the game from last-known-good
    assert sink.committed == 2


def test_a_stale_source_dims_the_card_and_an_expired_one_takes_it_down() -> None:
    provider = _Provider(_game())
    loop, sink, _ = _build(lag=0, provider=provider)
    fresh = loop.paint(_at(T)) if loop.refresh(T) else None
    assert fresh is not None and not fresh.stale
    provider.fail = True
    stale = loop.refresh(T + timedelta(seconds=121))
    assert (
        stale.health.source is SourceHealth.STALE
        and stale.health.reason == "no schedule for over 2m: ProviderError: schedule fetch down"
    )
    painted = loop.paint(_later(121))
    assert painted.stale and painted.shown is not None
    assert _colors(sink.frames[-1]) < _colors(sink.frames[0])  # the same card, dimmer
    gone = loop.refresh(T + timedelta(seconds=601))
    assert gone.health.source is SourceHealth.OFFLINE
    assert gone.health.reason == "no schedule for over 10m: ProviderError: schedule fetch down"
    assert loop.paint(_later(601)).frame is FrameKind.OFFLINE


def test_the_refresh_deadline_names_the_source_s_next_change_and_the_delay_s_next_release() -> None:
    loop, _, provider = _build(lag=30)
    loop.refresh(T)
    assert loop.refresh_deadline(_at(T)) == MonotonicTime(30.0)  # the state clears the delay
    loop.refresh(T + timedelta(seconds=30))
    assert loop.refresh_deadline(_later(30)) == MonotonicTime(151.0)  # the schedule goes stale
    provider.fail = True
    loop.refresh(T + timedelta(seconds=151))
    assert loop.refresh_deadline(_later(151)) == MonotonicTime(631.0)  # and then offline
    loop.refresh(T + timedelta(seconds=631))
    assert loop.refresh_deadline(_later(631)) is None
    assert loop.next_deadline(_later(631)) == MonotonicTime(631.0)  # nothing cued yet: the board is due now
    assert loop.paint(_later(631)).frame is FrameKind.OFFLINE
    assert loop.next_deadline(_later(631)) is None  # the offline frame waits for the source


def test_run_once_isolates_a_render_failure() -> None:
    loop, sink, _ = _build(lag=0, registry=RendererRegistry())  # empty — no renderer registered
    res = loop.run_once(_at(T))
    assert res.render_error is not None and "no renderer" in res.render_error
    assert res.shown is None
    assert sink.committed == 0  # nothing committed on failure
    assert res.pipeline.ingested  # but the pipeline still built + ingested the card


def test_run_once_threads_the_logo_store_into_the_render_context() -> None:
    # The running app's logo store must reach the renderer as ambient RenderContext.
    capturing = _CapturingRegistry(default_registry())
    store = LogoStore()
    loop, _, _ = _build(lag=0, registry=capturing, logos=store)
    loop.run_once(_at(T))
    assert capturing.last_context is not None
    assert capturing.last_context.logos is store


def test_run_once_defaults_to_no_logo_store() -> None:
    # A loop built without logos (a test/replay) renders with the color-bar fallback.
    capturing = _CapturingRegistry(default_registry())
    loop, _, _ = _build(lag=0, registry=capturing)
    loop.run_once(_at(T))
    assert capturing.last_context is not None
    assert capturing.last_context.logos is None


def test_painting_draws_what_the_board_has_up_and_presents_a_card_once() -> None:
    loop, sink, _ = _build(lag=0)
    before = loop.paint(_at(T))
    assert before.shown is None and before.frame is FrameKind.IDLE  # nothing refreshed yet
    loop.refresh(T)
    first = loop.paint(_at(T))
    assert first.shown is not None and first.reason is SelectionReason.ROTATION
    assert first.presented is not None and first.presented.card == first.shown
    treatment = first.presented.treatment
    assert treatment is not None and not treatment.away.is_tile  # no logo store: color bars
    assert [change.kind for change in first.changes] == [ChangeKind.DOWN, ChangeKind.UP]  # the idle frame, then it
    later = loop.paint(_later(0.1))  # frames between refreshes
    assert later.shown == first.shown and later.presented is None and later.changes == ()
    loop.refresh(T + timedelta(seconds=12))
    again = loop.paint(_later(12))
    assert again.shown == first.shown and again.presented is None  # a lone card stays up: no new presentation
    assert sink.committed == 4


def test_a_card_that_cant_be_drawn_is_set_aside_and_spends_no_dwell() -> None:
    loop, sink, _ = _build(lag=0, registry=RendererRegistry())  # no renderers: every card fails to draw
    loop.refresh(T)
    painted = loop.paint(_at(T))
    assert painted.render_error is not None and painted.shown is None and painted.presented is None
    assert sink.committed == 0
    [dropped] = painted.changes
    assert dropped.kind is ChangeKind.DROPPED and dropped.reason is EndReason.UNDRAWN
    after = loop.paint(_later(0.1))  # the board moves on rather than retrying the card every frame
    assert after.frame is FrameKind.IDLE and sink.committed == 1
    assert loop.next_deadline(_later(0.1)) == MonotonicTime(60.0)  # when it may be tried again
    assert loop.paint(_later(60)).render_error is not None  # tried again, and set aside again


class _FailingSink(RecordingDisplaySink):
    """A display that fails to commit until told otherwise."""

    failing = True

    def commit(self, frame: Canvas) -> None:
        if self.failing:
            raise OSError("display gone")
        super().commit(frame)


def test_a_display_that_fails_spends_no_airtime_and_keeps_the_card_cued() -> None:
    loop, _, _ = _build(lag=0)
    sink = _FailingSink(QUAD)
    loop._sink = sink
    loop.refresh(T)
    failed = loop.paint(_at(T))
    assert failed.render_error == "OSError: display gone" and failed.changes == ()
    sink.failing = False
    drawn = loop.paint(_later(5))
    assert drawn.presented is not None and drawn.presented.at == T + timedelta(seconds=5)  # its dwell starts here


class _NoFrames(RecordingDisplaySink):
    def new_frame(self) -> Canvas:
        raise OSError("no buffer")


def test_a_display_with_no_frame_to_draw_on_spends_nothing() -> None:
    loop, _, _ = _build(lag=0)
    loop._sink = _NoFrames(QUAD)
    loop.refresh(T)
    painted = loop.paint(_at(T))
    assert painted.render_error == "OSError: no buffer" and painted.changes == ()


def test_a_card_that_leaves_comes_down_and_the_board_says_why() -> None:
    provider = _Provider(_game())
    loop, _, _ = _build(lag=0, provider=provider)
    loop.run_once(_at(T))
    provider._contests = ()  # the game leaves the schedule
    painted = loop.paint(_later(1)) if loop.refresh(T + timedelta(seconds=1)) else None
    assert painted is not None and painted.frame is FrameKind.IDLE
    down = [change for change in painted.changes if change.kind is ChangeKind.DOWN]
    assert down[0].reason is EndReason.INVALIDATED


def test_a_card_without_two_team_rows_has_no_matchup_treatment() -> None:
    golf = GolfTournament(
        id=LeagueScopedId(League.PGA, SourceRef("x"), "t"),
        league=League.PGA,
        status=GameStatus.LIVE,
        scheduled_start=T,
        tournament_name="Open",
    )
    loop, _, _ = _build(lag=0)
    loop.refresh(T)
    card = loop._queue.card(f"{_game().id.raw}:live")
    assert card is not None
    golfing = dataclasses.replace(card, contest=golf)
    assert _treatment(golfing, RenderContext(profile=QUAD, now=T)) is None


def _colors(frame: Canvas) -> int:
    """The sum of every channel drawn into a recorded frame: dimmer frames sum lower."""
    assert isinstance(frame, RecordingCanvas)
    return sum(op.color.r + op.color.g + op.color.b for op in frame.ops if op.color is not None)
