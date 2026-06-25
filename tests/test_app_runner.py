"""Tests for the app runner: build_loop wiring and the CLI arg parser."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from omni.app.__main__ import _parse_args
from omni.app.display import RecordingDisplaySink
from omni.app.runner import build_loop
from omni.core.enum import GameStatus, League, PanelProfile
from omni.core.ids import LeagueScopedId, SourceRef
from omni.core.time import DurationSeconds
from omni.domain.baseball import BaseballBaseState, BaseballCount, BaseballGameState, InningPhase
from omni.domain.contest import TeamGame
from omni.events.baseball import LiveBaseballFeed
from omni.providers.base import ProviderUpdate
from omni.providers.mlb_teams import MlbTeamRegistry
from omni.renderers.canvas import RecordingCanvas
from omni.renderers.image import LogoStore

_REG = MlbTeamRegistry.from_color_file()
SOURCE = SourceRef("mlb_statsapi", "https://statsapi.mlb.com")
T = datetime(2026, 6, 17, 23, 0, 0, tzinfo=timezone.utc)


def _game() -> TeamGame:
    return TeamGame(
        id=LeagueScopedId(League.MLB, SOURCE, "g1"),
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
    result = loop.run_once(T)
    assert result.shown is not None
    assert sink.committed == 1


def test_build_loop_wires_the_broadcast_delay() -> None:
    sink = RecordingDisplaySink(PanelProfile.QUAD_128X64)
    loop = build_loop(_Provider(), _fetch_feed, sink, broadcast_lag=DurationSeconds(30))
    assert loop.run_once(T).shown is None  # inside the delay — nothing shown yet
    assert loop.run_once(T + timedelta(seconds=30)).shown is not None
    assert sink.committed == 1


def test_build_loop_threads_logos_through_to_the_rendered_frame() -> None:
    # A LogoStore handed to build_loop must reach rendering: the quad live card blits team tiles.
    sink = RecordingDisplaySink(PanelProfile.QUAD_128X64)
    loop = build_loop(_Provider(), _fetch_feed, sink, broadcast_lag=DurationSeconds(0), logos=LogoStore())
    loop.run_once(T)
    frame = sink.frames[-1]
    assert isinstance(frame, RecordingCanvas)
    assert frame.images()  # tiles blitted — the store reached the renderer end to end


def test_build_loop_without_logos_falls_back_to_colour_bars() -> None:
    # No store wired (a test/replay) → the renderer draws colour bars, never a tile blit.
    sink = RecordingDisplaySink(PanelProfile.QUAD_128X64)
    loop = build_loop(_Provider(), _fetch_feed, sink, broadcast_lag=DurationSeconds(0))
    loop.run_once(T)
    frame = sink.frames[-1]
    assert isinstance(frame, RecordingCanvas)
    assert not frame.images()  # colour-bar fallback, no blits


def test_parse_args_defaults() -> None:
    ns = _parse_args(["--profile", "quad_128x64", "--emulated"])
    assert ns.profile == "quad_128x64"
    assert ns.emulated is True
    assert ns.favorite == [] and ns.delay == 45 and ns.tick == 12
    assert ns.timezone == "America/New_York"


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
            "--tick",
            "8",
            "--timezone",
            "America/Denver",
        ]
    )
    assert ns.favorite == ["COL", "LAD"]
    assert ns.delay == 30 and ns.tick == 8
    assert ns.timezone == "America/Denver"
