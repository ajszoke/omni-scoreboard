"""Tests for the live-baseball renderer: per-profile layouts + golden snapshots.

Regenerate goldens intentionally with: OMNI_REGEN_GOLDEN=1 pytest -k golden
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
from omni.cards.baseball import LiveBaseballCardPayload
from omni.core.colors import RGBColor
from omni.core.enum import DisplayPriority, GameStatus, League, PanelProfile
from omni.core.ids import LeagueScopedId, SourceRef
from omni.core.time import DurationSeconds
from omni.domain.base import LogoAsset
from omni.domain.contest import Contest, TeamGame
from omni.domain.teams import Team
from omni.domain.baseball import BaseballBaseState, BaseballCount, InningPhase
from omni.panels.geometry import geometry_for
from omni.providers.mlb_teams import MlbTeamRegistry
from omni.renderers.base import Renderer
from omni.renderers.canvas import RecordingCanvas
from omni.renderers.context import RenderContext
from omni.renderers.image import LogoStore
from omni.renderers.live_baseball import LiveBaseballRenderer
from omni.renderers.pillow_canvas import PillowCanvas

GOLDEN_DIR = Path(__file__).resolve().parent / "golden"
LOGOS = LogoStore()  # resolves the committed COL/LAD tiles for the logo + golden renders
T = datetime(2026, 6, 17, 19, 5, tzinfo=timezone.utc)
SOURCE = SourceRef("mlb_statsapi")
ALL_PROFILES = (PanelProfile.QUAD_128X64, PanelProfile.STACK_64X64, PanelProfile.SINGLE_64X32)
AWAY_COLOR = RGBColor(51, 0, 111)
HOME_COLOR = RGBColor(0, 90, 156)
WHITE = RGBColor(255, 255, 255)


def _team(team_id: str, name: str, abbr: str, color: RGBColor) -> Team:
    return Team(
        id=LeagueScopedId(League.MLB, SOURCE, team_id),
        league=League.MLB,
        display_name=name,
        short_name=name.split()[-1],
        abbreviation=abbr,
        primary_color=color,
        secondary_color=RGBColor(196, 206, 212),
        logo=LogoAsset(key=abbr.lower(), path=f"assets/logos/mlb/{abbr.lower()}.png"),
    )


def _game() -> TeamGame:
    return TeamGame(
        id=LeagueScopedId(League.MLB, SOURCE, "g1"),
        league=League.MLB,
        status=GameStatus.LIVE,
        scheduled_start=T,
        away=_team("115", "Colorado Rockies", "COL", AWAY_COLOR),
        home=_team("119", "Los Angeles Dodgers", "LAD", HOME_COLOR),
    )


def make_card(
    *,
    phase: InningPhase = InningPhase.TOP,
    away_score: int = 3,
    home_score: int = 5,
    inning: int = 7,
    bases: BaseballBaseState = BaseballBaseState(first=True),
) -> ScoreboardCard[LiveBaseballCardPayload]:
    payload = LiveBaseballCardPayload(
        away_score=away_score,
        home_score=home_score,
        inning=inning,
        phase=phase,
        count=BaseballCount(balls=2, strikes=1, outs=2),
        bases=bases,
    )
    return ScoreboardCard(
        id=CardId("g1:live"),
        kind=CardKind.LIVE_GAME,
        contest=_game(),
        timing=DisplayTiming(available_at=T, min_display=DurationSeconds(5), max_display=DurationSeconds(30)),
        priority=CardPriority(band=DisplayPriority.FAVORITE, score=50.0),
        layout_support=LayoutSupport(profiles=frozenset(ALL_PROFILES)),
        dedupe_key=DedupeKey("g1:live"),
        payload=payload,
    )


REGISTRY = MlbTeamRegistry.from_color_file()  # real teams carry the base/alt backgrounds the clash reads


def _clash_card() -> ScoreboardCard[LiveBaseballCardPayload]:
    # NYY @ DET: both render on an identical cap navy, so the home side must flip to its alt.
    game = TeamGame(
        id=LeagueScopedId(League.MLB, SOURCE, "g2"),
        league=League.MLB,
        status=GameStatus.LIVE,
        scheduled_start=T,
        away=REGISTRY.resolve(147),  # Yankees
        home=REGISTRY.resolve(116),  # Tigers
    )
    return dataclasses.replace(make_card(away_score=4, home_score=2, inning=6), contest=game)


def _render(
    card: ScoreboardCard[LiveBaseballCardPayload], profile: PanelProfile, *, logos: LogoStore | None = None
) -> RecordingCanvas:
    width, height = geometry_for(profile).size
    canvas = RecordingCanvas(width, height)
    LiveBaseballRenderer().render(card, RenderContext(profile=profile, now=T, logos=logos), canvas)
    return canvas


def test_renderer_supports_all_three_profiles() -> None:
    renderer: Renderer[LiveBaseballCardPayload] = LiveBaseballRenderer()
    assert renderer.supported_profiles == frozenset(ALL_PROFILES)


def test_renderer_rejects_non_teamgame_contest() -> None:
    contest = Contest(
        id=LeagueScopedId(League.MLB, SOURCE, "g1"),
        league=League.MLB,
        status=GameStatus.LIVE,
        scheduled_start=T,
    )
    card = dataclasses.replace(make_card(), contest=contest)
    with pytest.raises(TypeError):
        LiveBaseballRenderer().render(
            card, RenderContext(profile=PanelProfile.QUAD_128X64, now=T), RecordingCanvas(128, 64)
        )


def test_draw_op_quad_128x64() -> None:
    canvas = _render(make_card(), PanelProfile.QUAD_128X64)
    assert canvas.ops[0].op == "fill" and canvas.ops[0].color == RGBColor(0, 0, 0)
    rects = canvas.rects()
    assert any((r.x, r.y, r.w, r.h) == (0, 0, 4, 32) and r.color == AWAY_COLOR for r in rects)
    assert any((r.x, r.y, r.w, r.h) == (0, 32, 4, 32) and r.color == HOME_COLOR for r in rects)
    assert any((r.x, r.y, r.w, r.h) == (108, 16, 6, 6) and r.color == WHITE for r in rects)  # 1B filled
    texts = {(t.x, t.y, t.text) for t in canvas.texts()}
    assert {(8, 11, "COL"), (8, 43, "LAD")} <= texts
    assert {(52, 11, "3"), (52, 43, "5")} <= texts
    assert {(68, 6, "T7"), (68, 14, "2-1"), (68, 22, "2 OUT")} <= texts


def test_draw_op_stack_64x64_keeps_full_status() -> None:
    canvas = _render(make_card(), PanelProfile.STACK_64X64)
    rects = canvas.rects()
    assert any((r.x, r.y, r.w, r.h) == (0, 0, 3, 20) and r.color == AWAY_COLOR for r in rects)
    assert any((r.x, r.y, r.w, r.h) == (0, 22, 3, 20) and r.color == HOME_COLOR for r in rects)
    assert any((r.x, r.y, r.w, r.h) == (55, 52, 5, 5) and r.color == WHITE for r in rects)  # 1B filled
    texts = {(t.x, t.y, t.text) for t in canvas.texts()}
    assert {(5, 6, "COL"), (5, 28, "LAD")} <= texts
    assert {(56, 6, "3"), (56, 28, "5")} <= texts  # right-aligned at 62 - 6
    assert {(3, 46, "T7"), (20, 46, "2-1"), (3, 55, "2 OUT")} <= texts  # full status retained


def test_draw_op_single_64x32_is_an_explicit_compromise() -> None:
    canvas = _render(make_card(), PanelProfile.SINGLE_64X32)
    texts = {(t.x, t.y, t.text) for t in canvas.texts()}
    # Essentials shown: abbreviations, scores, inning phase.
    assert {(4, 5, "COL"), (4, 21, "LAD")} <= texts
    assert {(36, 3, "3"), (36, 19, "5")} <= texts
    assert (46, 13, "T7") in texts
    # Compromise: count, outs, and the bases diamond are omitted at 64x32.
    joined = " ".join(t.text for t in canvas.texts())
    assert "OUT" not in joined and "-" not in joined
    # Only the two team stripes are drawn (no base markers).
    assert {(r.x, r.y, r.w, r.h) for r in canvas.rects()} == {(0, 0, 2, 16), (0, 16, 2, 16)}


def test_draw_op_bottom_inning_shows_b_label() -> None:
    # The InningPhase.BOTTOM arm is a ternary (a coverage blind spot), so assert it.
    canvas = _render(make_card(phase=InningPhase.BOTTOM, inning=9), PanelProfile.QUAD_128X64)
    assert (68, 6, "B9") in {(t.x, t.y, t.text) for t in canvas.texts()}


def test_draw_op_middle_break_shows_label_and_suppresses_at_bat() -> None:
    # Between halves there is no active at-bat: the larger panels show the phase
    # label alone, not a stale "2-1 / 2 OUT" or a bases diamond.
    for profile in (PanelProfile.QUAD_128X64, PanelProfile.STACK_64X64):
        canvas = _render(make_card(phase=InningPhase.MIDDLE, inning=7), profile)
        texts = [t.text for t in canvas.texts()]
        assert "MID7" in texts
        assert "2-1" not in texts and not any("OUT" in t for t in texts)  # at-bat suppressed
        assert not any(r.color == WHITE and r.w == r.h for r in canvas.rects())  # no bases drawn


def test_draw_op_end_break_shows_end_label() -> None:
    canvas = _render(make_card(phase=InningPhase.END, inning=8), PanelProfile.QUAD_128X64)
    assert "END8" in {t.text for t in canvas.texts()}


def test_draw_op_single_profile_shows_break_label() -> None:
    # The 64x32 compromise never showed an at-bat anyway — it just swaps the label.
    canvas = _render(make_card(phase=InningPhase.MIDDLE, inning=7), PanelProfile.SINGLE_64X32)
    assert (46, 13, "MID7") in {(t.x, t.y, t.text) for t in canvas.texts()}


def test_draw_op_two_digit_score_right_aligns() -> None:
    # "10" is two 6px glyphs, so its left edge is 58 - 12 = 46 on quad.
    canvas = _render(make_card(away_score=10), PanelProfile.QUAD_128X64)
    assert (46, 11, "10") in {(t.x, t.y, t.text) for t in canvas.texts()}


def test_draw_op_empty_bases_draw_dim_outlines() -> None:
    rects = _render(make_card(bases=BaseballBaseState()), PanelProfile.QUAD_128X64).rects()
    dim = RGBColor(60, 60, 60)
    assert any((r.x, r.y, r.w, r.h) == (108, 16, 6, 1) and r.color == dim for r in rects)  # 1B top edge
    assert not any(r.w == 6 and r.h == 6 and r.color == WHITE for r in rects)  # nothing filled white


def test_logos_replace_the_team_bar_on_quad_and_stack() -> None:
    quad = _render(make_card(), PanelProfile.QUAD_128X64, logos=LOGOS)
    assert {i.key for i in quad.images()} == {"col", "lad"}  # both tiles blitted
    bars = {(r.x, r.y, r.w, r.h) for r in quad.rects()}
    assert (0, 0, 4, 32) not in bars and (0, 32, 4, 32) not in bars  # the tile replaces the colour bar
    texts = {(t.x, t.y, t.text) for t in quad.texts()}
    assert {(24, 11, "COL"), (24, 43, "LAD")} <= texts  # abbreviations shift right to clear the tile

    stack = _render(make_card(), PanelProfile.STACK_64X64, logos=LOGOS)
    assert {i.key for i in stack.images()} == {"col", "lad"}
    assert {(23, 6, "COL"), (23, 28, "LAD")} <= {(t.x, t.y, t.text) for t in stack.texts()}


def test_single_profile_drops_the_logo_even_with_a_store() -> None:
    # An explicit compromise: a 20px tile does not fit a 64x32 row, so the bar identifies the team.
    canvas = _render(make_card(), PanelProfile.SINGLE_64X32, logos=LOGOS)
    assert canvas.images() == []
    assert {(0, 0, 2, 16), (0, 16, 2, 16)} <= {(r.x, r.y, r.w, r.h) for r in canvas.rects()}


def _assert_matches_golden(image: Image.Image, name: str) -> None:
    path = GOLDEN_DIR / name
    if os.environ.get("OMNI_REGEN_GOLDEN"):
        path.parent.mkdir(parents=True, exist_ok=True)
        image.save(path)
        return
    assert path.exists(), f"missing golden {name}; regenerate with OMNI_REGEN_GOLDEN=1"
    expected = Image.open(path).convert("RGB")
    assert image.convert("RGB").tobytes() == expected.tobytes(), f"render differs from golden {name}"


@pytest.mark.parametrize("profile", ALL_PROFILES)
def test_golden_image_per_profile(profile: PanelProfile) -> None:
    width, height = geometry_for(profile).size
    canvas = PillowCanvas(width, height)
    LiveBaseballRenderer().render(make_card(), RenderContext(profile=profile, now=T, logos=LOGOS), canvas)
    _assert_matches_golden(canvas.image(), f"live_baseball_{profile.to_json_value()}.png")


def test_clash_blits_the_home_alt_tile() -> None:
    # The renderer routes both marks through the clash resolver: NYY keeps its base tile,
    # DET flips to its alt so the two navy caps don't merge on the panel.
    canvas = _render(_clash_card(), PanelProfile.QUAD_128X64, logos=LOGOS)
    assert {i.key for i in canvas.images()} == {"nyy", "det-alt"}


def test_clash_golden_image_quad() -> None:
    width, height = geometry_for(PanelProfile.QUAD_128X64).size
    canvas = PillowCanvas(width, height)
    LiveBaseballRenderer().render(
        _clash_card(), RenderContext(profile=PanelProfile.QUAD_128X64, now=T, logos=LOGOS), canvas
    )
    _assert_matches_golden(canvas.image(), "live_baseball_clash_quad_128x64.png")


@pytest.mark.parametrize("profile", ALL_PROFILES)
def test_break_golden_image_per_profile(profile: PanelProfile) -> None:
    # The between-halves (MIDDLE) layout: matchup + scores + phase label, no at-bat.
    width, height = geometry_for(profile).size
    canvas = PillowCanvas(width, height)
    card = make_card(phase=InningPhase.MIDDLE, inning=7)
    LiveBaseballRenderer().render(card, RenderContext(profile=profile, now=T, logos=LOGOS), canvas)
    _assert_matches_golden(canvas.image(), f"live_baseball_break_{profile.to_json_value()}.png")
