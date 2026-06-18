"""Tests for the live-baseball renderer: draw-op layout + golden-image snapshot.

Regenerate the golden image intentionally with: OMNI_REGEN_GOLDEN=1 pytest -k golden
"""

from __future__ import annotations

import dataclasses
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
from PIL import Image

from omni.cards.base import (
    CardId,
    CardKind,
    CardPriority,
    DedupeKey,
    DisplayTiming,
    LayoutSupport,
    ScoreboardCard,
)
from omni.cards.baseball import BaseballBaseState, LiveBaseballCardPayload
from omni.core.colors import RGBColor
from omni.core.enum import DisplayPriority, GameStatus, League, PanelProfile
from omni.core.ids import LeagueScopedId, SourceRef
from omni.core.time import DurationSeconds
from omni.domain.base import LogoAsset
from omni.domain.contest import Contest, TeamGame
from omni.domain.teams import Team
from omni.events.baseball import BaseballCount, HalfInning
from omni.renderers.base import Renderer
from omni.renderers.canvas import RecordingCanvas
from omni.renderers.live_baseball import LiveBaseballRenderer
from omni.renderers.pillow_canvas import PillowCanvas

GOLDEN_DIR = Path(__file__).resolve().parent / "golden"
T = datetime(2026, 6, 17, 19, 5, tzinfo=timezone.utc)
SOURCE = SourceRef("mlb_statsapi")


def _team(team_id: str, name: str, abbr: str, color: RGBColor) -> Team:
    return Team(
        id=LeagueScopedId(League.MLB, SOURCE, team_id),
        league=League.MLB,
        display_name=name,
        short_name=name.split()[-1],
        abbreviation=abbr,
        primary_color=color,
        secondary_color=RGBColor(196, 206, 212),
        logo=LogoAsset(key=abbr.lower(), path=f"assets/{abbr.lower()}.png"),
    )


def _game() -> TeamGame:
    return TeamGame(
        id=LeagueScopedId(League.MLB, SOURCE, "g1"),
        league=League.MLB,
        status=GameStatus.LIVE,
        scheduled_start=T,
        away=_team("115", "Colorado Rockies", "COL", RGBColor(51, 0, 111)),
        home=_team("119", "Los Angeles Dodgers", "LAD", RGBColor(0, 90, 156)),
    )


def make_card(
    *,
    half: HalfInning = HalfInning.TOP,
    away_score: int = 3,
    home_score: int = 5,
    inning: int = 7,
    bases: BaseballBaseState = BaseballBaseState(first=True),
) -> ScoreboardCard[LiveBaseballCardPayload]:
    payload = LiveBaseballCardPayload(
        away_score=away_score,
        home_score=home_score,
        inning=inning,
        half=half,
        count=BaseballCount(balls=2, strikes=1, outs=2),
        bases=bases,
    )
    return ScoreboardCard(
        id=CardId("g1:live"),
        kind=CardKind.LIVE_GAME,
        contest=_game(),
        timing=DisplayTiming(available_at=T, min_display=DurationSeconds(5), max_display=DurationSeconds(30)),
        priority=CardPriority(band=DisplayPriority.FAVORITE, score=50.0),
        layout_support=LayoutSupport(profiles=frozenset({PanelProfile.QUAD_128X64})),
        dedupe_key=DedupeKey("g1:live"),
        payload=payload,
    )


def _render(card: ScoreboardCard[LiveBaseballCardPayload]) -> RecordingCanvas:
    canvas = RecordingCanvas(128, 64)
    LiveBaseballRenderer().render(card, PanelProfile.QUAD_128X64, canvas)
    return canvas


def test_renderer_conforms_to_protocol_and_supports_quad() -> None:
    renderer: Renderer[LiveBaseballCardPayload] = LiveBaseballRenderer()
    assert renderer.supported_profiles == frozenset({PanelProfile.QUAD_128X64})


def test_renderer_rejects_unsupported_profile() -> None:
    with pytest.raises(NotImplementedError):
        LiveBaseballRenderer().render(make_card(), PanelProfile.SINGLE_64X32, RecordingCanvas(64, 32))


def test_renderer_rejects_non_teamgame_contest() -> None:
    contest = Contest(
        id=LeagueScopedId(League.MLB, SOURCE, "g1"),
        league=League.MLB,
        status=GameStatus.LIVE,
        scheduled_start=T,
    )
    card = dataclasses.replace(make_card(), contest=contest)
    with pytest.raises(TypeError):
        LiveBaseballRenderer().render(card, PanelProfile.QUAD_128X64, RecordingCanvas(128, 64))


def test_draw_op_layout() -> None:
    canvas = _render(make_card())

    # Background cleared to black first.
    assert canvas.ops[0].op == "fill" and canvas.ops[0].color == RGBColor(0, 0, 0)

    rects = canvas.rects()
    # Team color stripes: away (top) purple, home (bottom) blue.
    assert any((r.x, r.y, r.w, r.h) == (0, 0, 4, 32) and r.color == RGBColor(51, 0, 111) for r in rects)
    assert any((r.x, r.y, r.w, r.h) == (0, 32, 4, 32) and r.color == RGBColor(0, 90, 156) for r in rects)
    # Occupied first base -> a filled 6x6 white square at (108, 16).
    assert any((r.x, r.y, r.w, r.h) == (108, 16, 6, 6) and r.color == RGBColor(255, 255, 255) for r in rects)

    texts = {(t.x, t.y, t.text) for t in canvas.texts()}
    assert {(8, 11, "COL"), (8, 43, "LAD")} <= texts  # abbreviations
    assert {(52, 11, "3"), (52, 43, "5")} <= texts  # right-aligned single-digit scores
    assert {(68, 6, "T7"), (68, 14, "2-1"), (68, 22, "2 OUT")} <= texts  # status panel


def test_draw_op_bottom_inning_shows_b_label() -> None:
    texts = {(t.x, t.y, t.text) for t in _render(make_card(half=HalfInning.BOTTOM, inning=9)).texts()}
    assert (68, 6, "B9") in texts


def test_draw_op_two_digit_score_right_aligns() -> None:
    # "10" is two 6px-wide glyphs, so its left edge is 58 - 12 = 46.
    texts = {(t.x, t.y, t.text) for t in _render(make_card(away_score=10)).texts()}
    assert (46, 11, "10") in texts


def test_draw_op_empty_bases_draw_dim_outlines() -> None:
    rects = _render(make_card(bases=BaseballBaseState())).rects()
    dim = RGBColor(60, 60, 60)
    # Each empty base is drawn as four 1px DIM edges; no base is filled white 6x6.
    assert any((r.x, r.y, r.w, r.h) == (108, 16, 6, 1) and r.color == dim for r in rects)  # 1B top edge
    assert any((r.x, r.y, r.w, r.h) == (100, 6, 6, 1) and r.color == dim for r in rects)  # 2B top edge
    assert not any(r.w == 6 and r.h == 6 and r.color == RGBColor(255, 255, 255) for r in rects)


def _assert_matches_golden(image: Image.Image, name: str) -> None:
    path = GOLDEN_DIR / name
    if os.environ.get("OMNI_REGEN_GOLDEN"):
        path.parent.mkdir(parents=True, exist_ok=True)
        image.save(path)
        return
    assert path.exists(), f"missing golden {name}; regenerate with OMNI_REGEN_GOLDEN=1"
    expected = Image.open(path).convert("RGB")
    assert image.convert("RGB").tobytes() == expected.tobytes(), f"render differs from golden {name}"


def test_golden_image_quad_128x64() -> None:
    canvas = PillowCanvas(128, 64)
    LiveBaseballRenderer().render(make_card(), PanelProfile.QUAD_128X64, canvas)
    _assert_matches_golden(canvas.image(), "live_baseball_quad_128x64.png")
