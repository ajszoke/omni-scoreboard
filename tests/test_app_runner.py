"""Tests for the app runner: build_loop wiring and the CLI arg parser."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from omni.app.__main__ import _parse_args
from omni.app.display import RecordingDisplaySink
from omni.app.pipeline import MeterReading
from omni.app.runner import build_loop, paint_frame
from omni.cards.base import ScoreboardCard
from omni.core.clock import ClockReading, FakeClock, MonotonicTime
from omni.core.enum import GameStatus, League, PanelProfile
from omni.core.ids import LeagueScopedId, SourceRef
from omni.core.time import DurationSeconds
from omni.domain.baseball import BaseballBaseState, BaseballCount, BaseballGameState, InningPhase, WinProbability
from omni.domain.contest import TeamGame
from omni.events.baseball import LiveBaseballFeed
from omni.providers.base import ProviderUpdate
from omni.providers.mlb_teams import MlbTeamRegistry
from omni.queue.scheduler import Pick
from omni.renderers.canvas import Canvas, RecordingCanvas
from omni.renderers.context import RenderContext
from omni.renderers.image import LogoStore
from omni.renderers.registry import RendererRegistry, default_registry
from omni.renderers.text_lane import TextLane
from omni.tower.lane import ChangeKind

_REG = MlbTeamRegistry.from_color_file()
SOURCE = SourceRef("mlb_statsapi", "https://statsapi.mlb.com")
T = datetime(2026, 6, 17, 23, 0, 0, tzinfo=timezone.utc)


def _at(moment: datetime) -> ClockReading:
    return ClockReading(moment, MonotonicTime((moment - T).total_seconds()))


def _game(raw: str = "g1") -> TeamGame:
    return TeamGame(
        id=LeagueScopedId(League.MLB, SOURCE, raw),
        league=League.MLB,
        status=GameStatus.LIVE,
        scheduled_start=T,
        away=_REG.resolve(115),
        home=_REG.resolve(119),
    )


class _Provider:
    source = SOURCE
    league = League.MLB

    def refresh(self, now: datetime) -> ProviderUpdate:
        return ProviderUpdate(source=SOURCE, observed_at=now, contests=(_game(),))


def _fetch_feed(game: TeamGame, now: datetime) -> LiveBaseballFeed:
    return LiveBaseballFeed(
        state=BaseballGameState(
            away_score=1,
            home_score=2,
            inning=7,
            phase=InningPhase.BOTTOM,
            count=BaseballCount(balls=2, strikes=1, outs=1),
            bases=BaseballBaseState(first=True),
            away_hits=5,  # a normal mid-game state — hits present, so no no-hitter card pre-empts the live card
            home_hits=7,
        )
    )


def test_build_loop_produces_a_working_loop() -> None:
    sink = RecordingDisplaySink(PanelProfile.QUAD_128X64)
    loop = build_loop(_Provider(), _fetch_feed, sink, broadcast_lag=DurationSeconds(0))
    result = loop.run_once(_at(T))
    assert result.shown is not None
    assert sink.committed == 1


def test_build_loop_wires_the_broadcast_delay() -> None:
    sink = RecordingDisplaySink(PanelProfile.QUAD_128X64)
    loop = build_loop(_Provider(), _fetch_feed, sink, broadcast_lag=DurationSeconds(30))
    assert loop.run_once(_at(T)).shown is None  # inside the delay — the held frame instead
    assert loop.run_once(_at(T + timedelta(seconds=30))).shown is not None
    assert sink.committed == 2


def test_build_loop_threads_logos_through_to_the_rendered_frame() -> None:
    # A LogoStore handed to build_loop must reach rendering: the quad live card blits team tiles.
    sink = RecordingDisplaySink(PanelProfile.QUAD_128X64)
    loop = build_loop(_Provider(), _fetch_feed, sink, broadcast_lag=DurationSeconds(0), logos=LogoStore())
    loop.run_once(_at(T))
    frame = sink.frames[-1]
    assert isinstance(frame, RecordingCanvas)
    assert frame.images()  # tiles blitted — the store reached the renderer end to end


def test_build_loop_without_logos_falls_back_to_colour_bars() -> None:
    # No store wired (a test/replay) → the renderer draws colour bars, never a tile blit.
    sink = RecordingDisplaySink(PanelProfile.QUAD_128X64)
    loop = build_loop(_Provider(), _fetch_feed, sink, broadcast_lag=DurationSeconds(0))
    loop.run_once(_at(T))
    frame = sink.frames[-1]
    assert isinstance(frame, RecordingCanvas)
    assert not frame.images()  # colour-bar fallback, no blits


def test_build_loop_threads_the_win_probability_fetcher() -> None:
    # A win-prob fetcher handed to build_loop must reach the pipeline's live path.
    sink = RecordingDisplaySink(PanelProfile.QUAD_128X64)
    calls: list[str] = []

    def fetch_wp(game: TeamGame, as_of: ClockReading) -> MeterReading | None:
        calls.append(game.id.raw)
        return MeterReading(WinProbability(home=70.0, away=30.0), as_of)

    loop = build_loop(_Provider(), _fetch_feed, sink, broadcast_lag=DurationSeconds(0), fetch_win_probability=fetch_wp)
    loop.run_once(_at(T))
    assert calls == ["g1"]  # the pipeline fetched win-prob for the live game


class _TwoGames:
    source = SOURCE
    league = League.MLB

    def refresh(self, now: datetime) -> ProviderUpdate:
        return ProviderUpdate(source=SOURCE, observed_at=now, contests=(_game("g1"), _game("g2")))


class _SlowSink(RecordingDisplaySink):
    """A display whose swaps take time: each commit lets the clock run on by the next of ``delays``."""

    def __init__(self, clock: FakeClock, *delays: float) -> None:
        super().__init__(PanelProfile.QUAD_128X64)
        self._clock = clock
        self._delays = list(delays)

    def commit(self, frame: Canvas) -> None:
        self._clock.advance(timedelta(seconds=self._delays.pop(0) if self._delays else 0.0))
        super().commit(frame)


_LONG = TextLane(x=0, y=0, width=8, font="4x6")


class _Scrolling(RendererRegistry):
    """Today's renderers, plus one line too long for its lane in every frame: when it went up, as each frame
    reads it."""

    def __init__(self) -> None:
        super().__init__()
        self._real = default_registry()
        self.elapsed: list[float | None] = []

    def render(self, card: ScoreboardCard[Any], context: RenderContext, canvas: Canvas) -> None:
        self._real.render(card, context, canvas)
        assert context.marquee.clock is not None
        self.elapsed.append(context.marquee.clock.elapsed(_LONG, "a line too long for its lane", overflows=True))


def test_a_slow_swap_moves_the_dwell_the_booking_and_the_marquee_to_when_the_frame_was_shown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock(T)
    sink = _SlowSink(clock, 5.0)  # the first frame takes 5 s to reach the panel; the rest are instant
    scrolling = _Scrolling()
    loop = build_loop(_TwoGames(), _fetch_feed, sink, broadcast_lag=DurationSeconds(0), registry=scrolling)
    booked: list[ClockReading] = []
    book = loop._queue.book

    def spy(pick: Pick, at: ClockReading) -> bool:
        booked.append(at)
        return book(pick, at)

    monkeypatch.setattr(loop._queue, "book", spy)
    loop.refresh(clock.read())
    first = paint_frame(loop, clock)
    shown = T + timedelta(seconds=5)
    assert first.presented is not None and first.presented.at == shown and first.at == shown
    [up] = [change for change in first.changes if change.kind is ChangeKind.UP]
    assert up.at == ClockReading(shown, MonotonicTime(5.0))  # the dwell starts when the frame was shown
    assert booked == [up.at]  # and the turn is booked then
    assert loop.next_deadline(clock.read()) == MonotonicTime(13.0)  # its minimum, from delivery
    clock.advance(timedelta(seconds=0.1))
    assert paint_frame(loop, clock).shown == first.shown
    assert scrolling.elapsed == [0.0, pytest.approx(0.1)]  # the line started at delivery, not at drawing
    clock.advance(timedelta(seconds=11.8))  # 16.9 s: 11.9 s after delivery
    assert paint_frame(loop, clock).shown == first.shown
    clock.advance(timedelta(seconds=0.1))  # 12 s after delivery
    turned = paint_frame(loop, clock)
    assert turned.shown != first.shown
    [down] = [change for change in turned.changes if change.kind is ChangeKind.DOWN]
    assert down.aired == timedelta(seconds=12)


def test_a_paint_without_a_clock_credits_the_frame_when_it_was_drawn() -> None:
    clock = FakeClock(T)
    loop = build_loop(_TwoGames(), _fetch_feed, _SlowSink(clock, 5.0), broadcast_lag=DurationSeconds(0))
    loop.refresh(_at(T))
    painted = loop.paint(clock.read())
    assert painted.presented is not None and painted.presented.at == T  # a replay: no time passes in a paint


def test_parse_args_defaults() -> None:
    ns = _parse_args(["--profile", "quad_128x64", "--emulated"])
    assert ns.profile == "quad_128x64"
    assert ns.emulated is True
    assert ns.favorite == [] and ns.delay == 45 and ns.slot == 12
    assert ns.timezone == "America/New_York"
    assert ns.meter is False and ns.no_odds is False  # the live meter is opt-in; pregame odds are on


def test_the_live_meter_is_asked_for_by_flag() -> None:
    assert _parse_args(["--profile", "quad_128x64", "--meter"]).meter is True


def test_parse_args_collects_favorites_and_overrides() -> None:
    ns = _parse_args(
        [
            "--profile",
            "single_64x32",
            "--favorite",
            "COL",
            "--favorite",
            "LAD",
            "--delay",
            "30",
            "--slot",
            "8",
            "--timezone",
            "America/Denver",
        ]
    )
    assert ns.favorite == ["COL", "LAD"]
    assert ns.delay == 30 and ns.slot == 8
    assert _parse_args(["--profile", "quad_128x64", "--tick", "9"]).slot == 9  # the flag's old name still works
    assert ns.timezone == "America/Denver"
