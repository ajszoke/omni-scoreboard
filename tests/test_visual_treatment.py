"""The unified matchup visual treatment + the hardware-tunable contrast policy."""

from __future__ import annotations

import dataclasses
from itertools import permutations

import pytest

from omni.core.colors import RGBColor
from omni.core.enum import PanelProfile
from omni.domain.logos import LogoVariant
from omni.providers.mlb_palette import LOGO_ALT_COLOR, LOGO_BASE_COLOR
from omni.providers.mlb_teams import _TEAM_ID_INFO, MlbTeamRegistry
from omni.renderers.image import LogoStore
from omni.renderers.logo_clash import logo_background
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
BLACK, WHITE = RGBColor(0, 0, 0), RGBColor(255, 255, 255)


def _treat(away, home, profile, *, logos, away_top, home_top, policy=DEFAULT_CONTRAST_POLICY):
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
    "profile, tile_x, meter_x", [(PanelProfile.QUAD_128X64, 0, 20), (PanelProfile.STACK_64X64, 1, 21)]
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


def test_a_clash_switches_the_away_side_and_frees_its_base_color() -> None:
    # NYY/LAA aren't the pair: use a club with no meter color of its own. TEX's navy-blue base and CHC's
    # share a clash, so the visiting TEX shows its alt, and its freed color is its BASE, lifted.
    tex, chc = REG.resolve(140), REG.resolve(112)
    t = _treat(tex, chc, PanelProfile.QUAD_128X64, logos=LOGOS, away_top=0, home_top=20)
    assert (t.away.variant, t.home.variant) == (LogoVariant.ALT, LogoVariant.BASE)
    assert t.away.meter_color == LOGO_BASE_COLOR[140].value_lifted()
    assert t.home.meter_color == LOGO_ALT_COLOR[112].value_lifted()


@pytest.mark.parametrize("away_id, home_id", [(147, 116), (116, 147), (111, 116)])
def test_the_clubs_sharing_a_cap_navy_meter_white_when_they_switch(away_id: int, home_id: int) -> None:
    # BOS, DET and NYY all wear the same navy, and lifted it's a royal blue none of them wear.
    t = _treat(
        REG.resolve(away_id), REG.resolve(home_id), PanelProfile.STACK_64X64, logos=LOGOS, away_top=0, home_top=22
    )
    assert t.away.variant is LogoVariant.ALT and t.away.meter_color == WHITE
    assert t.home.variant is LogoVariant.BASE and t.home.meter_color == LOGO_ALT_COLOR[home_id].value_lifted()


def test_a_bar_paints_the_palette_lifted_and_meters_in_what_that_leaves() -> None:
    t = _treat(NYY, COL, PanelProfile.SINGLE_64X32, logos=LOGOS, away_top=0, home_top=16)
    assert (t.away.bar_color, t.away.meter_color) == (LOGO_BASE_COLOR[147].value_lifted(), LOGO_ALT_COLOR[147])
    t = _treat(COL, LAD, PanelProfile.SINGLE_64X32, logos=LOGOS, away_top=0, home_top=16)
    assert (t.home.bar_color, t.home.meter_color) == (LOGO_BASE_COLOR[119].value_lifted(), WHITE)


def test_a_black_base_cannot_name_a_club_on_a_bar_so_its_second_color_does() -> None:
    # COL's tile is black, but a black bar is an unlit LED: the purple paints the bar, leaving nothing
    # for the meter but white. With two black bases, no flip is even needed to tell them apart.
    pit = REG.resolve(134)
    t = _treat(COL, pit, PanelProfile.SINGLE_64X32, logos=LOGOS, away_top=0, home_top=16)
    assert (t.away.bar_color, t.away.meter_color) == (LOGO_ALT_COLOR[115].value_lifted(), WHITE)
    assert (t.home.bar_color, t.home.meter_color) == (LOGO_ALT_COLOR[134], WHITE)


def test_a_black_spare_color_meters_white_rather_than_a_lifted_grey() -> None:
    bal = REG.resolve(110)  # orange tile, black alt
    t = _treat(bal, LAD, PanelProfile.QUAD_128X64, logos=LOGOS, away_top=0, home_top=20)
    assert t.away.is_tile and t.away.meter_color == WHITE


def test_two_bars_that_only_merge_once_lifted_still_separate() -> None:
    # BOS navy and LAD blue are distinct tiles, but lifting the navy lands it almost on the blue.
    bos = REG.resolve(111)
    assert logo_background(bos, LogoVariant.BASE).delta_e(logo_background(LAD, LogoVariant.BASE)) >= 25.0  # type: ignore[union-attr, arg-type]
    t = _treat(bos, LAD, PanelProfile.SINGLE_64X32, logos=LOGOS, away_top=0, home_top=16)
    assert t.away.bar_color.delta_e(t.home.bar_color) >= 25.0


def test_an_away_bar_with_no_second_color_switches_to_white_and_meters_its_first() -> None:
    # CIN's alt is black, so it has one lit color — red, a clash with the Angels' red. Home keeps it.
    cin, laa = REG.resolve(113), REG.resolve(108)
    t = _treat(cin, laa, PanelProfile.SINGLE_64X32, logos=LOGOS, away_top=0, home_top=16)
    assert (t.away.bar_color, t.away.meter_color) == (WHITE, LOGO_BASE_COLOR[113].value_lifted())
    assert t.home.bar_color == LOGO_BASE_COLOR[108].value_lifted()


def test_boston_meters_white_rather_than_blue_when_it_shows_red() -> None:
    bos = REG.resolve(111)
    tile = _treat(bos, DET, PanelProfile.STACK_64X64, logos=LOGOS, away_top=0, home_top=22).away  # navy clash
    assert tile.variant is LogoVariant.ALT and tile.meter_color == WHITE
    bar = _treat(bos, LAD, PanelProfile.SINGLE_64X32, logos=LOGOS, away_top=0, home_top=16).away  # lifted clash
    assert bar.bar_color == LOGO_ALT_COLOR[111] and bar.meter_color == WHITE


def test_boston_keeps_its_red_meter_beside_its_navy() -> None:
    bos = REG.resolve(111)
    tile = _treat(bos, LAD, PanelProfile.STACK_64X64, logos=LOGOS, away_top=0, home_top=22).away
    assert tile.variant is LogoVariant.BASE and tile.meter_color == LOGO_ALT_COLOR[111]
    home = _treat(LAD, bos, PanelProfile.SINGLE_64X32, logos=LOGOS, away_top=0, home_top=16).home
    assert home.meter_color == LOGO_ALT_COLOR[111]


def test_a_club_with_no_palette_paints_its_primary_and_meters_white() -> None:
    # An uncataloged club has only the one color, so a meter in it would vanish against its own mark.
    stub = dataclasses.replace(LAD, logo=dataclasses.replace(LAD.logo, preferred_background=None), logo_alt=None)
    for profile, logos in ((PanelProfile.QUAD_128X64, LOGOS), (PanelProfile.SINGLE_64X32, None)):
        t = _treat(COL, stub, profile, logos=logos, away_top=0, home_top=16)
        assert t.home.meter_color == WHITE
    assert t.home.bar_color == LAD.primary_color.value_lifted()


ALL_TEAMS = [REG.resolve(team_id) for team_id in _TEAM_ID_INFO]
SWEEP = [
    (PanelProfile.QUAD_128X64, 20),
    (PanelProfile.STACK_64X64, 22),
    (PanelProfile.SINGLE_64X32, 16),
]
# Beside their silver these two meter in white, however close the two read: CWS's only spare is black,
# and NYY's navy lifts to a blue the club doesn't wear.
METER_LAST_RESORT = {"CWS", "NYY"}


def _first_lit_color(team):
    base = logo_background(team, LogoVariant.BASE)
    return (base if base != BLACK else logo_background(team, LogoVariant.ALT)).value_lifted()


def _shown_color(team, side):
    return logo_background(team, side.variant) if side.is_tile else side.bar_color


@pytest.mark.parametrize("profile, home_top", SWEEP)
def test_every_matchup_keeps_its_marks_and_meters_apart(profile: PanelProfile, home_top: int) -> None:
    floor = DEFAULT_CONTRAST_POLICY.clash_delta_e
    for away, home in permutations(ALL_TEAMS, 2):
        t = _treat(away, home, profile, logos=LOGOS, away_top=0, home_top=home_top)
        pair = (away.abbreviation, home.abbreviation)
        assert _shown_color(away, t.away).delta_e(_shown_color(home, t.home)) >= floor, pair
        assert t.home.variant is LogoVariant.BASE, pair  # home always shows its primary tile...
        assert t.home.bar_color == _first_lit_color(home), pair  # ...and paints its first color as a bar
        for team, side in ((away, t.away), (home, t.home)):
            assert side.meter_color.contrast_ratio(BLACK) >= DEFAULT_CONTRAST_POLICY.min_contrast, team.abbreviation
            assert side.is_tile or side.bar_color.contrast_ratio(BLACK) >= DEFAULT_CONTRAST_POLICY.min_contrast
            if team.abbreviation not in METER_LAST_RESORT:
                assert side.meter_color.delta_e(_shown_color(team, side)) >= floor, team.abbreviation


@pytest.mark.parametrize("gap, width", [(0, 1), (1, 1), (2, 2), (5, 2)])
def test_meter_width_fills_the_gap_clamped_to_one_or_two(gap: int, width: int) -> None:
    assert _meter_width(gap) == width
