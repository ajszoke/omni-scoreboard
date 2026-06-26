"""The win-probability gauge: a per-team bar beside the logo, in the freed colour."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from omni.core.enum import PanelProfile
from omni.domain.baseball import WinProbability
from omni.providers.mlb_palette import LOGO_ALT_COLOR, LOGO_BASE_COLOR
from omni.providers.mlb_teams import MlbTeamRegistry
from omni.renderers.canvas import RecordingCanvas
from omni.renderers.context import RenderContext
from omni.renderers.win_meter import _meter_width, draw_win_meter

NOW = datetime(2026, 6, 17, 23, 30, tzinfo=timezone.utc)
REG = MlbTeamRegistry.from_color_file()
COL, LAD = REG.resolve(115), REG.resolve(119)  # black-base vs Dodger-blue-base: no clash
NYY, DET = REG.resolve(147), REG.resolve(116)  # identical cap navy: a clash


def _ctx(profile: PanelProfile) -> RenderContext:
    return RenderContext(profile=profile, now=NOW, logos=None)


def test_quad_draws_a_gauge_beside_each_logo_in_the_freed_colour() -> None:
    canvas = RecordingCanvas(128, 64)
    draw_win_meter(
        canvas, _ctx(PanelProfile.QUAD_128X64), COL, LAD, WinProbability(home=26.0, away=74.0), away_top=0, home_top=32
    )
    away, home = sorted(canvas.rects(), key=lambda r: r.y)  # away gauge sits higher on the panel
    # Neither side clashed, so each shows its base tile -> the gauge uses its freed (alt) colour.
    assert (away.x, away.w, away.h) == (22, 2, 15)  # round(74% * 20px), filled from the bottom
    assert away.color == LOGO_ALT_COLOR[115].value_lifted()
    assert (home.x, home.w, home.h) == (22, 2, 5)  # round(26% * 20px)
    assert home.color == LOGO_ALT_COLOR[119].value_lifted()


def test_a_clashing_side_uses_its_base_colour_freed_to_the_meter() -> None:
    # DET flips to its alt tile, so its freed colour is its BASE (navy); NYY keeps base, freeing its alt.
    canvas = RecordingCanvas(128, 64)
    draw_win_meter(
        canvas, _ctx(PanelProfile.QUAD_128X64), NYY, DET, WinProbability(home=40.0, away=60.0), away_top=0, home_top=32
    )
    away, home = sorted(canvas.rects(), key=lambda r: r.y)
    assert away.color == LOGO_ALT_COLOR[147].value_lifted()  # NYY shows base -> freed = alt (silver)
    assert home.color == LOGO_BASE_COLOR[116].value_lifted()  # DET shows alt -> freed = base (navy)


def test_a_zero_percent_side_draws_no_bar() -> None:
    canvas = RecordingCanvas(128, 64)
    draw_win_meter(
        canvas, _ctx(PanelProfile.QUAD_128X64), COL, LAD, WinProbability(home=100.0, away=0.0), away_top=0, home_top=32
    )
    rects = canvas.rects()
    assert len(rects) == 1 and rects[0].h == 20  # only the home (100%) gauge, full height


def test_single_draws_a_narrow_gauge_beside_the_colour_bar() -> None:
    canvas = RecordingCanvas(64, 32)
    draw_win_meter(
        canvas, _ctx(PanelProfile.SINGLE_64X32), COL, LAD, WinProbability(home=50.0, away=50.0), away_top=0, home_top=16
    )
    away, home = sorted(canvas.rects(), key=lambda r: r.y)
    # The gauge sizes itself from the single's mark: x=2 (right of the 2px bar), as tall as the 16px row.
    assert (away.x, away.w, away.h) == (2, 2, 8)  # round(50% * 16px), filled from the bottom
    assert (home.x, home.w, home.h) == (2, 2, 8)


@pytest.mark.parametrize("gap, width", [(0, 1), (1, 1), (2, 2), (5, 2)])
def test_meter_width_fills_the_gap_clamped_to_one_or_two(gap: int, width: int) -> None:
    # Open-ended per layout: the gauge takes the whole gap before the label, but never wider than
    # 2px (would crowd the score) nor below 1px (would vanish).
    assert _meter_width(gap) == width
