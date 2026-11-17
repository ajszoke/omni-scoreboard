"""The NBA logo palette: complete, matches the committed tiles, and keeps every matchup apart."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from itertools import permutations
from pathlib import Path

import pytest

from omni.core.colors import RGBColor
from omni.core.enum import PanelProfile
from omni.domain.logos import LogoSource, LogoSurface, LogoVariant
from omni.domain.teams import Team
from omni.providers.nba_palette import FREED_BASE_METER_COLOR, LOGO_ALT_COLOR, LOGO_BASE_COLOR
from omni.providers.nba_teams import NBA_TEAMS, NbaTeamRegistry
from omni.renderers.image import LogoImage, LogoStore
from omni.renderers.logo_clash import MatchupLogoVariants, logo_background, resolve_logo_variants
from omni.renderers.visual_treatment import DEFAULT_CONTRAST_POLICY, MarkTreatment, resolve_matchup_treatment

_FLOOR = 25.0  # delta_e below which two backgrounds blur together on a small panel
_CLUBS = sorted(NBA_TEAMS)
_REGISTRY = NbaTeamRegistry.default()
_BLACK = RGBColor(0, 0, 0)
_WHITE = RGBColor(255, 255, 255)
_RAW = Path(__file__).resolve().parents[1] / "assets" / "raw" / "nba"


def _tile(abbreviation: str, suffix: str = "") -> LogoImage:
    key = abbreviation.lower()
    return LogoImage.from_png(f"assets/logos/nba/{key}{suffix}.png", key=f"nba/{key}{suffix}")


def test_both_palettes_cover_every_club() -> None:
    assert set(LOGO_BASE_COLOR) == set(NBA_TEAMS)
    assert set(LOGO_ALT_COLOR) == set(NBA_TEAMS)
    assert set(FREED_BASE_METER_COLOR) <= set(NBA_TEAMS)


@pytest.mark.parametrize("abbreviation", _CLUBS)
def test_palette_matches_the_committed_tiles(abbreviation: str) -> None:
    # The color a club's tile actually renders on must equal its palette entry: the guard that
    # keeps the runtime maps and the committed art from drifting apart.
    assert _tile(abbreviation).pixel(0, 0).delta_e(LOGO_BASE_COLOR[abbreviation]) < 5
    assert _tile(abbreviation, "-alt").pixel(0, 0).delta_e(LOGO_ALT_COLOR[abbreviation]) < 5


@pytest.mark.parametrize("abbreviation", _CLUBS)
def test_base_and_alt_are_distinct(abbreviation: str) -> None:
    distance = LOGO_BASE_COLOR[abbreviation].delta_e(LOGO_ALT_COLOR[abbreviation])
    assert distance >= _FLOOR, f"{abbreviation}: base/alt only {distance:.0f} apart"


@pytest.mark.parametrize("abbreviation", _CLUBS)
def test_every_club_has_20x20_base_and_alt_tiles(abbreviation: str) -> None:
    for suffix in ("", "-alt"):
        tile = _tile(abbreviation, suffix)
        assert (tile.width, tile.height) == (20, 20)


def test_every_meter_color_value_lifts_to_legibility_on_black() -> None:
    for palette in (LOGO_BASE_COLOR, LOGO_ALT_COLOR, FREED_BASE_METER_COLOR):
        for abbreviation, color in palette.items():
            if color == _BLACK:
                continue  # a black background is never spent on a meter; the meter goes white instead
            lifted = color.value_lifted()
            assert lifted.contrast_ratio(_BLACK) >= 3.0, f"{abbreviation} {color} did not lift"


def _count(tile: LogoImage, wanted: Callable[[RGBColor], bool]) -> int:
    return sum(1 for p in tile.pixels if wanted(p))


def _light(p: RGBColor) -> bool:
    return min(p.r, p.g, p.b) > 120


@pytest.mark.parametrize("abbreviation", ["HOU", "IND", "MIL", "TOR"])
def test_a_mark_drawn_in_its_own_background_color_was_recolored_to_show(abbreviation: str) -> None:
    # The Rockets' red R, the Pacers' navy P, the Bucks' green buck and the Raptors' red claws would
    # each vanish into the base tile they sit on; each is recolored light.
    assert _count(_tile(abbreviation), _light) >= 40


@pytest.mark.parametrize("abbreviation", ["PHI", "UTA"])
def test_a_dark_base_shows_the_mark_drawn_for_dark_backgrounds(abbreviation: str) -> None:
    # The 76ers' blue 6 and the Jazz's purple note vanish on their base; the dark-surface mark draws
    # them white.
    assert _count(_tile(abbreviation), _light) >= 40


def test_a_mark_drawn_in_its_alt_color_was_recolored_to_show() -> None:
    # The 76ers' red 7 goes white on the red alt, beside the white 6.
    assert _count(_tile("PHI", "-alt"), _light) >= 90
    # The Jazz keep their purple note on sky blue, where the white one would blur.
    assert _count(_tile("UTA", "-alt"), lambda p: p.b > p.g + 30 and p.r < 90 and p.b < 160) >= 30


def test_the_suns_alt_strokes_the_sun_ball_in_purple_on_white() -> None:
    tile = _tile("PHX", "-alt")
    assert _count(tile, lambda p: p.r > 200 and 60 < p.g < 160 and p.b < 80) >= 25  # the orange ball, as drawn
    assert _count(tile, lambda p: p.b > p.g + 20 and p.r < 170 and p.g < 150) >= 40  # the purple stroke


def test_the_bulls_base_strokes_the_bull_black() -> None:
    # The bull's red melts into the red tile; a black stroke keeps its silhouette.
    assert _count(_tile("CHI"), lambda p: max(p.r, p.g, p.b) < 50) >= 25


def test_the_cavaliers_alt_shows_the_bases_c_on_white() -> None:
    tile = _tile("CLE", "-alt")
    assert _count(tile, lambda p: 90 < p.r < 180 and p.g < 60 and p.b < 90) >= 35  # the maroon C
    assert _count(tile, lambda p: p.r > 150 and 110 < p.g < 190 and p.b < 130) >= 25  # its gold bevel


def test_the_magic_ball_shows_on_its_blue_base() -> None:
    # Its blue ball, the base's own blue, takes the mark's navy instead.
    assert _count(_tile("ORL"), lambda p: max(p.r, p.g, p.b) < 60) >= 12


def test_the_heat_ball_shows_on_its_alt() -> None:
    # The red ball would melt into the table's red; the alt is the club's yellow instead.
    assert _count(_tile("MIA", "-alt"), lambda p: p.r > 100 and p.g < 60) >= 40


_HAND_PICKED = {"CHA"}
_EXPECTED_SOURCE = {"BOS": LogoSource.ESPN, "CHA": LogoSource.CURATED}


def test_every_clubs_cached_marks_are_recorded_with_their_source() -> None:
    manifest = json.loads(_RAW.joinpath("manifest.json").read_text())
    table = manifest["_color_table"]
    assert "Module:Sports_color/basketball&oldid=" in table["url"] and table["license"].startswith("CC BY-SA 4.0")
    for abbreviation in _CLUBS:
        sources = manifest[abbreviation]["sources"]
        if abbreviation in _HAND_PICKED:
            # One alternate mark for both tiles, cached as a PNG whatever format it was fetched in.
            assert set(sources) == {"alternate.png"}, abbreviation
            assert _RAW.joinpath(abbreviation.lower(), "alternate.png").read_bytes().startswith(b"\x89PNG")
        else:
            stems = {name.rsplit(".", 1)[0] for name in sources}
            assert stems == {f"primary_{surface.value}" for surface in LogoSurface}, abbreviation
        for name, entry in sources.items():
            data = _RAW.joinpath(abbreviation.lower(), name).read_bytes()
            assert hashlib.sha256(data).hexdigest() == entry["sha256"], (abbreviation, name)
            assert LogoSource(entry["source"]) is _EXPECTED_SOURCE.get(abbreviation, LogoSource.NBA_CDN), abbreviation


def test_identical_bases_switch_the_away_side_and_leave_home_on_its_primary() -> None:
    # The 76ers and the Pistons share a royal blue: the visitor shows its red alt either way round.
    phi, det = _REGISTRY.resolve("PHI"), _REGISTRY.resolve("DET")
    assert resolve_logo_variants(phi, det) == MatchupLogoVariants(LogoVariant.ALT, LogoVariant.BASE)
    assert resolve_logo_variants(det, phi) == MatchupLogoVariants(LogoVariant.ALT, LogoVariant.BASE)
    # Two black bases clash the same way.
    mia, por = _REGISTRY.resolve("MIA"), _REGISTRY.resolve("POR")
    assert resolve_logo_variants(mia, por) == MatchupLogoVariants(LogoVariant.ALT, LogoVariant.BASE)


def test_distinct_bases_both_stay_on_base() -> None:
    lal, bos = _REGISTRY.resolve("LAL"), _REGISTRY.resolve("BOS")
    assert resolve_logo_variants(lal, bos) == MatchupLogoVariants(LogoVariant.BASE, LogoVariant.BASE)


_TEAMS = [_REGISTRY.resolve(abbreviation) for abbreviation in _CLUBS]
_LOGOS = LogoStore()
_SWEEP = [
    (PanelProfile.QUAD_128X64, 20),
    (PanelProfile.STACK_64X64, 22),
    (PanelProfile.SINGLE_64X32, 16),
]
# Beside its silver the Spurs meter in white, however close the two read: the club's only spare is black.
_METER_LAST_RESORT = {"SAS"}


def _shown_color(team: Team, side: MarkTreatment) -> RGBColor:
    shown = logo_background(team, side.variant) if side.is_tile else side.bar_color
    assert shown is not None
    return shown


@pytest.mark.parametrize("profile, home_top", _SWEEP)
def test_every_matchup_keeps_its_marks_and_meters_apart(profile: PanelProfile, home_top: int) -> None:
    for away, home in permutations(_TEAMS, 2):
        treatment = resolve_matchup_treatment(
            away, home, profile=profile, logos=_LOGOS, policy=DEFAULT_CONTRAST_POLICY, away_top=0, home_top=home_top
        )
        pair = (away.abbreviation, home.abbreviation)
        assert _shown_color(away, treatment.away).delta_e(_shown_color(home, treatment.home)) >= _FLOOR, pair
        assert treatment.home.variant is LogoVariant.BASE, pair  # home always keeps its primary
        for team, side in ((away, treatment.away), (home, treatment.home)):
            assert side.meter_color.contrast_ratio(_BLACK) >= DEFAULT_CONTRAST_POLICY.min_contrast, team.abbreviation
            if team.abbreviation not in _METER_LAST_RESORT:
                assert side.meter_color.delta_e(_shown_color(team, side)) >= _FLOOR, team.abbreviation


def test_a_navy_club_meters_white_when_it_switches_to_its_alt() -> None:
    # The Pacers and the Pelicans share a navy, which lifts to a royal blue neither wears.
    ind, nop = _REGISTRY.resolve("IND"), _REGISTRY.resolve("NOP")
    treatment = resolve_matchup_treatment(
        ind,
        nop,
        profile=PanelProfile.QUAD_128X64,
        logos=_LOGOS,
        policy=DEFAULT_CONTRAST_POLICY,
        away_top=0,
        home_top=20,
    )
    assert treatment.away.variant is LogoVariant.ALT and treatment.away.meter_color == _WHITE
    assert treatment.home.variant is LogoVariant.BASE
    assert treatment.home.meter_color == LOGO_ALT_COLOR["NOP"].value_lifted()
