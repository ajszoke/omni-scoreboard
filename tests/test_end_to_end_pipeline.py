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
    assert card.payload.away_line.runs == 3 and card.payload.home_line.runs == 5
    assert card.payload.bases.first and card.payload.bases.third


def test_pipeline_renders_fixture_state_on_quad() -> None:
    card = _assembled_card()
    width, height = geometry_for(PanelProfile.QUAD_128X64).size
    canvas = RecordingCanvas(width, height)
    LiveBaseballRenderer().render(card, RenderContext(profile=PanelProfile.QUAD_128X64, now=NOW), canvas)

    texts = {(t.x, t.y, t.text) for t in canvas.texts()}
    assert {(5, 5, "COL"), (5, 25, "LAD")} <= texts  # abbr names the team when only the 4px bar drew
    assert {(25, 1, "3"), (39, 1, "7"), (53, 1, "0")} <= texts  # big R H E columns (away runs/hits/errors)
    assert {(25, 21, "5"), (39, 21, "9"), (53, 21, "1")} <= texts  # home
    assert {(64, 2, "▲7"), (64, 28, "2-1")} <= texts  # inning (filled triangle) + count, big font
    assert (98, 44, "87 SWPR") in texts  # the current-play sweeper -> the live pitch token's reserved pitcher-row lane

    # First (center 108,20) and third (96,20) occupied -> filled white diamonds; second (102,9) empty.
    assert any(o.op == "fill_rect" and o.color == WHITE and o.y == 20 and o.x <= 108 <= o.x + o.w for o in canvas.ops)
    assert any(o.op == "fill_rect" and o.color == WHITE and o.y == 20 and o.x <= 96 <= o.x + o.w for o in canvas.ops)
    assert not any(
        o.op == "fill_rect" and o.color == WHITE and o.y == 9 and o.x <= 102 <= o.x + o.w for o in canvas.ops
    )


def test_pipeline_renders_on_all_three_profiles() -> None:
    card = _assembled_card()
    for profile in PanelProfile:
        width, height = geometry_for(profile).size
        canvas = RecordingCanvas(width, height)
        LiveBaseballRenderer().render(card, RenderContext(profile=profile, now=NOW), canvas)
        joined = " ".join(t.text for t in canvas.texts())
        # Team abbreviations and the inning label appear on every profile; the quad's 6x10 inning
        # uses a filled triangle, the 4x6 profiles an arrow (no 4x6 triangle glyph).
        inning = "▲7" if profile is PanelProfile.QUAD_128X64 else "↑7"
        assert "COL" in joined and "LAD" in joined and inning in joined
