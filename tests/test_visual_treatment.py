"""The unified matchup visual treatment + the hardware-tunable contrast policy."""

from __future__ import annotations

import pytest

from omni.core.colors import RGBColor
from omni.core.enum import PanelProfile
from omni.domain.logos import LogoVariant
from omni.providers.mlb_palette import LOGO_ALT_COLOR, LOGO_BASE_COLOR
from omni.providers.mlb_teams import MlbTeamRegistry
from omni.renderers.image import LogoStore
from omni.renderers.visual_treatment import (
    DEFAULT_CONTRAST_POLICY,
    VisualContrastPolicy,
    _meter_width,
    resolve_matchup_treatment,
)

REG = MlbTeamRegistry.from_color_file()
COL, LAD = REG.resolve(115), REG.resolve(119)  # black-base vs Dodger-blue: no clash
NYY, DET = REG.resolve(147), REG.resolve(116)  # identical cap navy: a clash
LOGOS = LogoStore()


def _treat(away, home, profile, *, logos, away_top, home_top, policy=DEFAULT_CONTRAST_POLICY):  # type: ignore[no-untyped-def]
    return resolve_matchup_treatment(
        away, home, profile=profile, logos=logos, policy=policy, away_top=away_top, home_top=home_top
    )


def test_contrast_policy_lifts_to_its_floor_and_is_tunable() -> None:
    navy = RGBColor(0, 0, 128)
    assert DEFAULT_CONTRAST_POLICY.lift(navy) == navy.value_lifted(min_contrast=3.0)
    # A higher floor (e.g. a brighter diffuser) lifts further — the knob actually moves the output.
    hotter = VisualContrastPolicy(min_contrast=4.5).lift(navy)
    assert hotter.relative_luminance() > DEFAULT_CONTRAST_POLICY.lift(navy).relative_luminance()


def test_default_policy_reproduces_the_pre_policy_thresholds() -> None:
    assert (DEFAULT_CONTRAST_POLICY.min_contrast, DEFAULT_CONTRAST_POLICY.clash_delta_e) == (3.0, 25.0)


@pytest.mark.parametrize(
    "profile, tile_x, meter_x", [(PanelProfile.QUAD_128X64, 2, 22), (PanelProfile.STACK_64X64, 1, 21)]
)
def test_a_resolved_tile_puts_the_meter_at_the_tiles_right_edge(
    profile: PanelProfile, tile_x: int, meter_x: int
) -> None:
    t = _treat(COL, LAD, profile, logos=LOGOS, away_top=0, home_top=20)
    assert t.away.is_tile and (t.away.mark.x, t.away.mark.width) == (tile_x, 20)
    assert (t.away.meter.x, t.away.meter.width) == (meter_x, 2)  # the gauge hugs the tile's right edge


def test_no_store_falls_back_to_the_bar_and_the_meter_hugs_it() -> None:
    # Without a logo store the mark is the color bar, and the meter hugs the *bar* — not the old
    # hardcoded tile x=22 that left the gauge floating when only a bar drew.
    t = _treat(COL, LAD, PanelProfile.QUAD_128X64, logos=None, away_top=0, home_top=20)
    assert not t.away.is_tile and (t.away.mark.x, t.away.mark.width) == (0, 4)
    assert t.away.meter.x == 4


def test_single_never_tiles_even_with_a_store() -> None:
    t = _treat(COL, LAD, PanelProfile.SINGLE_64X32, logos=LOGOS, away_top=0, home_top=16)
    assert not t.away.is_tile and (t.away.meter.x, t.away.meter.width, t.away.meter.height) == (2, 2, 16)


def test_the_meter_stays_within_its_mark_so_a_full_gauge_cannot_clip_the_strip() -> None:
    # The round-3 geometry-drift fix: the home gauge is derived from the 20px tile at home_top=20,
    # so a full gauge spans exactly y=20..40 and never reaches the pitcher strip at y=41.
    t = _treat(COL, LAD, PanelProfile.QUAD_128X64, logos=LOGOS, away_top=0, home_top=20)
    assert (t.home.meter.y, t.home.meter.height) == (20, 20)
    assert t.home.meter.y + t.home.meter.height <= 41


def test_each_side_meters_in_its_freed_color_lifted() -> None:
    t = _treat(COL, LAD, PanelProfile.QUAD_128X64, logos=LOGOS, away_top=0, home_top=20)
    # Neither clashed -> each shows its base tile -> the meter uses its freed (alt) color, value-lifted.
    assert t.away.meter_color == LOGO_ALT_COLOR[115].value_lifted()
    assert t.home.meter_color == LOGO_ALT_COLOR[119].value_lifted()


def test_a_clash_flips_one_side_and_frees_its_base_color() -> None:
    # NYY/DET share a cap navy: DET flips to its alt, so its freed color is its BASE (navy);
    # NYY keeps base, freeing its alt (silver). The two sides agree (one resolve).
    t = _treat(NYY, DET, PanelProfile.QUAD_128X64, logos=LOGOS, away_top=0, home_top=20)
    assert (t.away.variant, t.home.variant) == (LogoVariant.BASE, LogoVariant.ALT)
    assert t.away.meter_color == LOGO_ALT_COLOR[147].value_lifted()
    assert t.home.meter_color == LOGO_BASE_COLOR[116].value_lifted()


@pytest.mark.parametrize("gap, width", [(0, 1), (1, 1), (2, 2), (5, 2)])
def test_meter_width_fills_the_gap_clamped_to_one_or_two(gap: int, width: int) -> None:
    assert _meter_width(gap) == width
