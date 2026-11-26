"""The NFL logo palette: complete, matches the committed tiles, and clears the clash floor."""

from __future__ import annotations

import pytest

from omni.core.colors import RGBColor
from omni.providers.nfl_palette import FREED_BASE_METER_COLOR, LOGO_ALT_COLOR, LOGO_BASE_COLOR
from omni.providers.nfl_teams import NFL_TEAMS
from omni.renderers.image import LogoImage
from omni.renderers.visual_treatment import DEFAULT_CONTRAST_POLICY

_FLOOR = 25.0  # delta_e below which two backgrounds blur together on a small panel
_FLOOR_DE2000 = 20.0  # delta_e_2000 below which two backgrounds blur together, whatever CIE76 says
_CLUBS = sorted(NFL_TEAMS)


def _tile(abbreviation: str, suffix: str = "") -> LogoImage:
    key = abbreviation.lower()
    return LogoImage.from_png(f"assets/logos/nfl/{key}{suffix}.png", key=f"nfl/{key}{suffix}")


def test_both_palettes_cover_every_club() -> None:
    assert set(LOGO_BASE_COLOR) == set(NFL_TEAMS)
    assert set(LOGO_ALT_COLOR) == set(NFL_TEAMS)
    assert set(FREED_BASE_METER_COLOR) <= set(NFL_TEAMS)


@pytest.mark.parametrize("abbreviation", _CLUBS)
def test_palette_matches_the_committed_tiles(abbreviation: str) -> None:
    # The color a club's tile actually renders on must equal its palette entry: the guard that
    # keeps the runtime maps and the committed art from drifting apart.
    assert _tile(abbreviation).pixel(0, 0).delta_e(LOGO_BASE_COLOR[abbreviation]) < 5
    assert _tile(abbreviation, "-alt").pixel(0, 0).delta_e(LOGO_ALT_COLOR[abbreviation]) < 5


@pytest.mark.parametrize("abbreviation", _CLUBS)
def test_base_and_alt_are_distinct(abbreviation: str) -> None:
    base, alt = LOGO_BASE_COLOR[abbreviation], LOGO_ALT_COLOR[abbreviation]
    assert base.delta_e(alt) >= _FLOOR, f"{abbreviation}: base/alt only {base.delta_e(alt):.0f} apart"
    assert base.delta_e_2000(alt) >= _FLOOR_DE2000, f"{abbreviation}: base/alt only {base.delta_e_2000(alt):.0f} apart"


@pytest.mark.parametrize("abbreviation", _CLUBS)
def test_every_club_has_20x20_base_and_alt_tiles(abbreviation: str) -> None:
    for suffix in ("", "-alt"):
        tile = _tile(abbreviation, suffix)
        assert (tile.width, tile.height) == (20, 20)


def test_every_meter_color_value_lifts_to_legibility_on_black() -> None:
    floor = DEFAULT_CONTRAST_POLICY.min_contrast
    black = RGBColor(0, 0, 0)
    for palette in (LOGO_BASE_COLOR, LOGO_ALT_COLOR, FREED_BASE_METER_COLOR):
        for abbreviation, color in palette.items():
            if color == black:
                continue  # a black background is never spent on a meter; the meter goes white instead
            lifted = DEFAULT_CONTRAST_POLICY.lift(color)
            assert lifted.contrast_ratio(black) >= floor, f"{abbreviation} {color} did not lift"


def test_a_logo_drawn_in_its_own_background_color_was_recolored_to_show() -> None:
    # Dallas's navy star would vanish on its navy tile; it's recolored white.
    whites = sum(1 for p in _tile("DAL").pixels if p.r > 220 and p.g > 220 and p.b > 220)
    assert whites >= 40
    # The Bears' orange C would vanish on the orange alt; it's recolored navy.
    darks = sum(1 for p in _tile("CHI", "-alt").pixels if p.r < 100)  # the orange tile's red channel is 230
    assert darks >= 30
