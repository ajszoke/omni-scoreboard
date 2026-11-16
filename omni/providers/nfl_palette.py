"""Authoritative per-club logo backgrounds for the NFL: the color each tile renders on.

Every club has two committed tiles: a base on its first color and an alt on a distinct second one.
When two clubs in a matchup share a near-identical base, the away club renders its alt so the two stay
apart on a small panel; home always keeps its primary. These maps are the runtime record of that
decision, and they mirror the committed tiles exactly: `tests/test_nfl_palette.py` asserts each
tile's corner pixel equals its entry here, so the two can't drift.

The colors start from nflverse's team table (CC BY 4.0), first color for the base and second for the
alt, with per-club changes noted inline. The tiles themselves are built by
`tools/render_nfl_logo_variants.py`. Keyed by the canonical abbreviation in `nfl_teams`.
"""

from __future__ import annotations

from omni.core.colors import RGBColor

__all__ = ["FREED_BASE_METER_COLOR", "LOGO_ALT_COLOR", "LOGO_BASE_COLOR"]

_BLACK = RGBColor(0, 0, 0)
_WHITE = RGBColor(255, 255, 255)

# abbreviation -> the base tile background (the club's primary color).
LOGO_BASE_COLOR: dict[str, RGBColor] = {
    "ARI": RGBColor(151, 35, 63),
    "ATL": RGBColor(167, 25, 48),
    "BAL": RGBColor(36, 23, 115),
    "BUF": RGBColor(0, 51, 141),
    "CAR": RGBColor(0, 133, 202),
    "CHI": RGBColor(11, 22, 42),
    "CIN": RGBColor(251, 79, 20),
    "CLE": RGBColor(49, 29, 0),  # brown: the orange helmet as the club shows it; nflverse lists orange first
    "DAL": RGBColor(0, 34, 68),
    "DEN": RGBColor(251, 79, 20),  # orange, the Broncos' signature, rather than nflverse's navy
    "DET": RGBColor(0, 118, 182),
    "GB": RGBColor(32, 55, 49),
    "HOU": RGBColor(3, 32, 47),
    "IND": RGBColor(0, 44, 95),
    "JAX": RGBColor(0, 103, 120),
    "KC": RGBColor(227, 24, 55),
    "LAC": RGBColor(0, 123, 199),
    "LAR": RGBColor(0, 53, 148),
    "LV": _BLACK,
    "MIA": RGBColor(0, 142, 151),
    "MIN": RGBColor(79, 38, 131),
    "NE": RGBColor(0, 34, 68),
    "NO": RGBColor(211, 188, 141),
    "NYG": RGBColor(11, 34, 101),
    "NYJ": RGBColor(0, 63, 45),
    "PHI": RGBColor(0, 76, 84),
    "PIT": _BLACK,
    "SEA": RGBColor(0, 34, 68),
    "SF": RGBColor(170, 0, 0),
    "TB": RGBColor(167, 25, 48),
    "TEN": RGBColor(68, 149, 210),
    "WAS": RGBColor(90, 20, 20),
}

# abbreviation -> the alt tile background (a distinct second color).
LOGO_ALT_COLOR: dict[str, RGBColor] = {
    "ARI": _BLACK,
    "ATL": _BLACK,
    "BAL": RGBColor(158, 124, 12),
    "BUF": RGBColor(198, 12, 48),
    "CAR": RGBColor(191, 192, 191),  # silver, the third color: the black panther vanishes on black
    "CHI": RGBColor(230, 65, 0),
    "CIN": _BLACK,
    "CLE": RGBColor(255, 60, 0),  # orange, with the helmet stroked brown so it doesn't vanish
    "DAL": RGBColor(176, 183, 188),
    "DEN": RGBColor(0, 34, 68),
    "DET": RGBColor(176, 183, 188),
    "GB": RGBColor(255, 182, 18),
    "HOU": RGBColor(167, 25, 48),
    "IND": RGBColor(165, 172, 175),
    "JAX": _BLACK,
    "KC": RGBColor(255, 182, 18),
    "LAC": RGBColor(255, 194, 14),
    "LAR": RGBColor(255, 209, 0),
    "LV": RGBColor(165, 172, 175),
    "MIA": RGBColor(245, 130, 32),
    "MIN": RGBColor(255, 198, 47),
    "NE": RGBColor(198, 12, 48),
    "NO": _BLACK,
    "NYG": RGBColor(167, 25, 48),
    "NYJ": _BLACK,
    "PHI": RGBColor(165, 172, 175),
    "PIT": RGBColor(255, 182, 18),
    "SEA": RGBColor(105, 190, 40),
    "SF": RGBColor(179, 153, 93),
    "TB": RGBColor(50, 47, 43),
    "TEN": RGBColor(213, 10, 10),
    "WAS": _BLACK,  # the third color: the logo's thin W blurs on gold at this size
}

# abbreviation -> the meter color when the club shows its alt, where its base won't do. These navy
# bases lift to a royal blue none of these clubs wear, so the meter goes white beside the alt instead.
FREED_BASE_METER_COLOR: dict[str, RGBColor] = {
    "CHI": _WHITE,
    "DAL": _WHITE,
    "HOU": _WHITE,
    "NE": _WHITE,
    "SEA": _WHITE,
}
