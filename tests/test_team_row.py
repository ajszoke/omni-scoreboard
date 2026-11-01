"""Tests for the shared team mark: a logo tile when a store is present, else a bar."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from omni.core.colors import RGBColor
from omni.core.enum import PanelProfile
from omni.domain.logos import LogoVariant
from omni.providers.mlb_palette import LOGO_ALT_COLOR, LOGO_BASE_COLOR
from omni.providers.mlb_teams import MlbTeamRegistry
from omni.renderers.canvas import RecordingCanvas
from omni.renderers.context import RenderContext
from omni.renderers.image import LogoStore
from omni.renderers.team_row import draw_matchup_marks, draw_single_bars, draw_team_mark

NOW = datetime(2026, 6, 17, 23, 30, tzinfo=timezone.utc)
REG = MlbTeamRegistry.from_color_file()
COL = REG.resolve(115)  # tile: assets/logos/mlb/col.png
BAR = RGBColor(10, 20, 30)


def _ctx(profile: PanelProfile, logos: LogoStore | None) -> RenderContext:
    return RenderContext(profile=profile, now=NOW, logos=logos)


@pytest.mark.parametrize(
    "profile, row_top, logo_xy, label_x",
    [
        (PanelProfile.QUAD_128X64, 0, (2, 6), 24),  # 20px tile centred in a 32px row
        (PanelProfile.QUAD_128X64, 32, (2, 38), 24),
        (PanelProfile.STACK_64X64, 0, (1, 0), 23),  # 20px tile flush in a 20px row
        (PanelProfile.STACK_64X64, 22, (1, 22), 23),
    ],
)
def test_logo_is_blitted_when_a_store_is_present(
    profile: PanelProfile, row_top: int, logo_xy: tuple[int, int], label_x: int
) -> None:
    canvas = RecordingCanvas(*_size(profile))
    returned = draw_team_mark(canvas, _ctx(profile, LogoStore()), COL, row_top=row_top, bar_color=BAR)
    assert returned == label_x  # the label starts after the tile
    blit = canvas.images()[0]
    assert (blit.x, blit.y) == logo_xy and blit.key == "col"
    assert canvas.rects() == []  # a tile, not a bar


@pytest.mark.parametrize(
    "profile, bar, label_x",
    [
        (PanelProfile.QUAD_128X64, (0, 0, 4, 32), 8),
        (PanelProfile.STACK_64X64, (0, 0, 3, 20), 5),
    ],
)
def test_colour_bar_fallback_without_a_store(
    profile: PanelProfile, bar: tuple[int, int, int, int], label_x: int
) -> None:
    canvas = RecordingCanvas(*_size(profile))
    returned = draw_team_mark(canvas, _ctx(profile, None), COL, row_top=0, bar_color=BAR)
    assert returned == label_x  # the bar pushes the label less far than a tile
    rect = canvas.rects()[0]
    assert (rect.x, rect.y, rect.w, rect.h) == bar
    assert rect.color == BAR
    assert canvas.images() == []


def test_missing_tile_falls_back_to_the_bar(tmp_path: Path) -> None:
    canvas = RecordingCanvas(128, 64)
    returned = draw_team_mark(
        canvas, _ctx(PanelProfile.QUAD_128X64, LogoStore(root=tmp_path)), COL, row_top=0, bar_color=BAR
    )
    assert returned == 8 and canvas.rects() and not canvas.images()


def test_alt_variant_blits_the_alt_tile() -> None:
    canvas = RecordingCanvas(128, 64)
    draw_team_mark(
        canvas, _ctx(PanelProfile.QUAD_128X64, LogoStore()), COL, row_top=0, bar_color=BAR, variant=LogoVariant.ALT
    )
    assert canvas.images()[0].key == "col-alt"  # the alt request resolves the -alt tile


def test_matchup_flips_the_clashing_side() -> None:
    # NYY and DET share an identical cap navy, so the visiting side renders its alt tile.
    canvas = RecordingCanvas(128, 64)
    nyy, det = REG.resolve(147), REG.resolve(116)
    away_x, home_x = draw_matchup_marks(
        canvas, _ctx(PanelProfile.QUAD_128X64, LogoStore()), nyy, det, away_top=0, home_top=32
    )
    assert {i.key for i in canvas.images()} == {"nyy-alt", "det"}  # away switched, home kept its primary
    assert away_x == home_x == 24  # both rows still place the label after a tile


def test_matchup_keeps_distinct_bases_on_their_base_tiles() -> None:
    canvas = RecordingCanvas(128, 64)
    col, lad = REG.resolve(115), REG.resolve(119)  # black vs Dodger blue: no clash
    draw_matchup_marks(canvas, _ctx(PanelProfile.QUAD_128X64, LogoStore()), col, lad, away_top=0, home_top=32)
    assert {i.key for i in canvas.images()} == {"col", "lad"}  # neither side flipped


def _size(profile: PanelProfile) -> tuple[int, int]:
    from omni.panels.geometry import geometry_for

    return geometry_for(profile).size


@pytest.mark.parametrize("away_id, home_id", [(134, 145), (145, 146), (146, 134), (115, 134)])
def test_single_bars_stay_lit_for_the_clubs_whose_base_is_black(away_id: int, home_id: int) -> None:
    # PIT, CWS, MIA and COL all have a black base, which on a bar is an unlit LED.
    canvas = RecordingCanvas(64, 32)
    draw_single_bars(canvas, _ctx(PanelProfile.SINGLE_64X32, None), REG.resolve(away_id), REG.resolve(home_id))
    rects = {(r.x, r.y, r.w, r.h): r.color for r in canvas.rects()}
    assert set(rects) == {(0, 0, 2, 16), (0, 16, 2, 16)}
    for color in rects.values():
        assert color is not None and color.contrast_ratio(RGBColor(0, 0, 0)) >= 3.0


def test_single_bars_keep_home_on_its_primary_and_switch_the_away_side_on_a_clash() -> None:
    # CIN and LAA both paint red: home keeps it, and CIN, with black its only other color, goes white.
    canvas = RecordingCanvas(64, 32)
    draw_single_bars(canvas, _ctx(PanelProfile.SINGLE_64X32, None), REG.resolve(113), REG.resolve(108))
    rects = {(r.x, r.y): r.color for r in canvas.rects()}
    assert rects[(0, 0)] == RGBColor(255, 255, 255)
    assert rects[(0, 16)] == LOGO_BASE_COLOR[108].value_lifted()


def test_a_missing_logo_falls_back_to_the_same_resolved_bar_on_the_big_boards() -> None:
    canvas = RecordingCanvas(128, 64)
    draw_matchup_marks(
        canvas, _ctx(PanelProfile.QUAD_128X64, None), REG.resolve(134), REG.resolve(119), away_top=0, home_top=32
    )
    rects = {(r.y): r.color for r in canvas.rects()}
    assert rects[0] == LOGO_ALT_COLOR[134]  # PIT: its yellow, not an unlit black bar
    assert rects[32] == LOGO_BASE_COLOR[119].value_lifted()
