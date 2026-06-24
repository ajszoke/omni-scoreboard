"""The authoritative logo alt palette: every team has a distinct, legible alt color."""

from __future__ import annotations

import pytest

from omni.core.colors import RGBColor
from omni.providers.mlb_palette import LOGO_ALT_COLOR, LOGO_BASE_FIX
from omni.providers.mlb_teams import _TEAM_ID_INFO
from omni.renderers.image import LogoImage

# Below this delta_e a mark blurs into its background on a 64px panel; the alt must
# clear it versus the base the tile actually renders on, or switching to it is futile.
_FLOOR = 25.0


def _base_bg(team_id: int, abbr: str) -> RGBColor:
    """The colour the base tile renders on — a `LOGO_BASE_FIX` overrides the committed tile."""
    if team_id in LOGO_BASE_FIX:
        return LOGO_BASE_FIX[team_id]
    return LogoImage.from_png(f"assets/logos/mlb/{abbr.lower()}.png", key=abbr.lower()).pixel(0, 0)


def test_every_team_has_exactly_one_alt_color() -> None:
    assert set(LOGO_ALT_COLOR) == set(_TEAM_ID_INFO)  # all 30, no strays


@pytest.mark.parametrize("team_id, info", sorted(_TEAM_ID_INFO.items()))
def test_alt_clears_the_legibility_floor(team_id: int, info: tuple[str, str]) -> None:
    abbr = info[0]
    distance = _base_bg(team_id, abbr).delta_e(LOGO_ALT_COLOR[team_id])
    assert distance >= _FLOOR, f"{abbr}: alt only {distance:.0f} from its base (floor {_FLOOR:.0f})"


def test_base_fixes_are_real_corrections() -> None:
    for team_id, fixed in LOGO_BASE_FIX.items():
        abbr = _TEAM_ID_INFO[team_id][0]
        committed = LogoImage.from_png(f"assets/logos/mlb/{abbr.lower()}.png", key=abbr.lower()).pixel(0, 0)
        assert committed.delta_e(fixed) >= 10  # the override actually changes the tile, not a no-op
