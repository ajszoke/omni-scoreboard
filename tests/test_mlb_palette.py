"""The logo palette: complete, matches the committed tiles, and clears the clash floor."""

from __future__ import annotations

import pytest

from omni.providers.mlb_palette import LOGO_ALT_COLOR, LOGO_BASE_COLOR
from omni.providers.mlb_teams import _TEAM_ID_INFO
from omni.renderers.image import LogoImage

_FLOOR = 25.0  # delta_e below which two backgrounds blur together on a small panel


def _corner(abbr: str, suffix: str = "") -> "LogoImage":
    return LogoImage.from_png(f"assets/logos/mlb/{abbr.lower()}{suffix}.png", key=abbr.lower())


def test_both_palettes_cover_every_team() -> None:
    assert set(LOGO_BASE_COLOR) == set(_TEAM_ID_INFO)
    assert set(LOGO_ALT_COLOR) == set(_TEAM_ID_INFO)


@pytest.mark.parametrize("team_id, info", sorted(_TEAM_ID_INFO.items()))
def test_palette_matches_the_committed_tiles(team_id: int, info: tuple[str, str]) -> None:
    # The colour a club's tile actually renders on must equal its palette entry — the
    # guard that keeps the runtime maps and the committed art from drifting apart.
    abbr = info[0]
    assert _corner(abbr).pixel(0, 0).delta_e(LOGO_BASE_COLOR[team_id]) < 5
    assert _corner(abbr, "-alt").pixel(0, 0).delta_e(LOGO_ALT_COLOR[team_id]) < 5


@pytest.mark.parametrize("team_id, info", sorted(_TEAM_ID_INFO.items()))
def test_base_and_alt_are_distinct(team_id: int, info: tuple[str, str]) -> None:
    distance = LOGO_BASE_COLOR[team_id].delta_e(LOGO_ALT_COLOR[team_id])
    assert distance >= _FLOOR, f"{info[0]}: base/alt only {distance:.0f} apart"


def test_every_team_has_20x20_base_and_alt_tiles() -> None:
    for _, (abbr, _nick) in _TEAM_ID_INFO.items():
        for suffix in ("", "-alt"):
            tile = _corner(abbr, suffix)
            assert (tile.width, tile.height) == (20, 20)


def test_detrademark_kept_the_twins_c_and_the_athletics_apostrophe() -> None:
    # The Twins must keep their red "C" (a red-alt would have erased it)...
    reds = sum(1 for p in _corner("MIN").pixels if p.r > 120 and p.g < 90 and p.b < 90)
    assert reds >= 8
    # ...and the A's its gold mark including the apostrophe the stripper used to eat.
    golds = sum(1 for p in _corner("ATH").pixels if p.r > 180 and p.g > 120 and p.b < 90)
    assert golds >= 8
