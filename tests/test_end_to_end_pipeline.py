"""End-to-end: raw StatsAPI fixtures -> provider -> CardFactory -> renderer.

Proves the whole typed pipeline closes — a schedule row becomes a `TeamGame`, a
game feed becomes a `BaseballGameState`, the factory assembles a card, and the
renderer draws the fixture's actual state (notably runners on first AND third,
which a first-base-only golden does not cover).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from omni.cards.factory import CardFactory
from omni.core.colors import RGBColor
from omni.core.enum import GameStatus, League, PanelProfile
from omni.domain.contest import TeamGame
from omni.panels.geometry import geometry_for
from omni.providers.mlb_statsapi import MlbStatsApiProvider
from omni.providers.mlb_teams import MlbTeamRegistry
from omni.renderers.canvas import RecordingCanvas
from omni.renderers.context import RenderContext
from omni.renderers.live_baseball import LiveBaseballRenderer

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "providers"
NOW = datetime(2026, 6, 17, 23, 30, tzinfo=timezone.utc)
WHITE = RGBColor(255, 255, 255)
DIM = RGBColor(60, 60, 60)


def _provider() -> MlbStatsApiProvider:
    schedule = json.loads((FIXTURES / "mlb_schedule.json").read_text())
    game: dict[str, Any] = json.loads((FIXTURES / "mlb_game_live.json").read_text())
    return MlbStatsApiProvider(
        MlbTeamRegistry.from_color_file(),
        fetch_schedule=lambda d, s: schedule,
        fetch_game=lambda pk: game,
    )


def _assembled_card() -> Any:
    provider = _provider()
    update = provider.refresh(NOW)
    game = next(c for c in update.contests if c.id.raw == "700001")
    assert isinstance(game, TeamGame) and game.status is GameStatus.LIVE
    state = provider.fetch_live_feed(game, now=NOW).state
    return CardFactory().live_baseball(game, state, now=NOW)


def test_pipeline_produces_a_renderable_mlb_card() -> None:
    card = _assembled_card()
    assert card.league is League.MLB
    assert card.contest.away.abbreviation == "COL"
    assert card.contest.home.abbreviation == "LAD"
    assert card.payload.away_score == 3 and card.payload.home_score == 5
    assert card.payload.bases.first and card.payload.bases.third


def test_pipeline_renders_fixture_state_on_quad() -> None:
    card = _assembled_card()
    width, height = geometry_for(PanelProfile.QUAD_128X64).size
    canvas = RecordingCanvas(width, height)
    LiveBaseballRenderer().render(card, RenderContext(profile=PanelProfile.QUAD_128X64, now=NOW), canvas)

    texts = {(t.x, t.y, t.text) for t in canvas.texts()}
    assert {(8, 11, "COL"), (8, 43, "LAD")} <= texts
    assert {(52, 11, "3"), (52, 43, "5")} <= texts
    assert {(68, 6, "T7"), (68, 14, "2-1"), (68, 22, "2 OUT")} <= texts

    rects = canvas.rects()
    # First and third occupied -> solid white; second empty -> dim outline, not filled.
    assert any((r.x, r.y, r.w, r.h) == (108, 16, 6, 6) and r.color == WHITE for r in rects)
    assert any((r.x, r.y, r.w, r.h) == (92, 16, 6, 6) and r.color == WHITE for r in rects)
    assert not any((r.x, r.y, r.w, r.h) == (100, 6, 6, 6) and r.color == WHITE for r in rects)
    assert any((r.x, r.y, r.w, r.h) == (100, 6, 6, 1) and r.color == DIM for r in rects)


def test_pipeline_renders_on_all_three_profiles() -> None:
    card = _assembled_card()
    for profile in PanelProfile:
        width, height = geometry_for(profile).size
        canvas = RecordingCanvas(width, height)
        LiveBaseballRenderer().render(card, RenderContext(profile=profile, now=NOW), canvas)
        joined = " ".join(t.text for t in canvas.texts())
        # Team abbreviations and the inning label appear on every profile.
        assert "COL" in joined and "LAD" in joined and "T7" in joined
