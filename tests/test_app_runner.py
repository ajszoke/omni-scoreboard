"""Tests for the app runner: build_loop wiring and the CLI arg parser."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from omni.app.__main__ import _parse_args
from omni.app.display import RecordingDisplaySink
from omni.app.runner import build_loop
from omni.core.enum import GameStatus, League, PanelProfile
from omni.core.ids import LeagueScopedId, SourceRef
from omni.core.time import DurationSeconds
from omni.domain.baseball import BaseballBaseState, BaseballCount, BaseballGameState, HalfInning
from omni.domain.contest import TeamGame
from omni.providers.base import ProviderUpdate
from omni.providers.mlb_teams import MlbTeamRegistry

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


def _fetch_state(game: TeamGame) -> BaseballGameState:
    return BaseballGameState(
        away_score=1,
        home_score=2,
        inning=7,
        half=HalfInning.BOTTOM,
        count=BaseballCount(balls=2, strikes=1, outs=1),
        bases=BaseballBaseState(first=True),
    )


def test_build_loop_produces_a_working_loop() -> None:
    sink = RecordingDisplaySink(PanelProfile.QUAD_128X64)
    loop = build_loop(_Provider(), _fetch_state, sink, broadcast_lag=DurationSeconds(0))
    result = loop.run_once(T)
    assert result.shown is not None
    assert sink.committed == 1


def test_build_loop_wires_the_broadcast_delay() -> None:
    sink = RecordingDisplaySink(PanelProfile.QUAD_128X64)
    loop = build_loop(_Provider(), _fetch_state, sink, broadcast_lag=DurationSeconds(30))
    assert loop.run_once(T).shown is None  # inside the delay — nothing shown yet
    assert loop.run_once(T + timedelta(seconds=30)).shown is not None
    assert sink.committed == 1


def test_parse_args_defaults() -> None:
    ns = _parse_args(["--profile", "quad_128x64", "--emulated"])
    assert ns.profile == "quad_128x64"
    assert ns.emulated is True
    assert ns.favorite == [] and ns.delay == 45 and ns.tick == 12


def test_parse_args_collects_favorites_and_overrides() -> None:
    ns = _parse_args(
        ["--profile", "single_64x32", "--favorite", "COL", "--favorite", "LAD", "--delay", "30", "--tick", "8"]
    )
    assert ns.favorite == ["COL", "LAD"]
    assert ns.delay == 30 and ns.tick == 8
