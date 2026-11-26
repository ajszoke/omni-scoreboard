"""Authoritative per-club logo backgrounds for the NBA: the color each tile renders on.

Every club has two committed tiles: a base on its first color and an alt on a distinct second one.
When two clubs in a matchup share a near-identical base, the away club renders its alt so the two stay
apart on a small panel; home always keeps its primary. These maps are the runtime record of that
decision, and they mirror the committed tiles exactly: `tests/test_nba_palette.py` asserts each
tile's corner pixel equals its entry here, so the two can't drift.

The colors start from Wikipedia's basketball color table, `Module:Sports color/basketball` (CC BY-SA
4.0, by Wikipedia's contributors): its first color for the base and its second for the alt (the table's
other two entries are text colors). Per-club changes are noted inline, drawing on the color lists in
the clubs' Wikipedia articles. `assets/raw/nba/manifest.json` pins the revision read. The tiles
themselves are built by `tools/render_nba_logo_variants.py`. Keyed by the canonical abbreviation in
`nba_teams`.
"""

from __future__ import annotations

from omni.core.colors import RGBColor

__all__ = ["FREED_BASE_METER_COLOR", "LOGO_ALT_COLOR", "LOGO_BASE_COLOR"]

_BLACK = RGBColor(0, 0, 0)
_WHITE = RGBColor(255, 255, 255)

# abbreviation -> the base tile background (the club's primary color).
LOGO_BASE_COLOR: dict[str, RGBColor] = {
    "ATL": RGBColor(200, 16, 46),
    "BOS": RGBColor(0, 131, 72),
    "BKN": _BLACK,
    "CHA": RGBColor(29, 17, 96),  # the user's pick, the table's second: a teal hornet on purple, beside a teal meter
    "CHI": RGBColor(206, 17, 65),
    "CLE": RGBColor(134, 0, 56),
    "DAL": RGBColor(0, 100, 177),
    "DEN": RGBColor(14, 34, 64),
    "DET": RGBColor(29, 66, 138),
    "GSW": RGBColor(29, 66, 138),
    "HOU": RGBColor(206, 14, 45),
    "IND": RGBColor(12, 35, 64),
    "LAC": RGBColor(18, 23, 63),
    "LAL": RGBColor(49, 0, 111),
    "MEM": RGBColor(93, 118, 169),
    "MIA": _BLACK,
    "MIL": RGBColor(0, 71, 27),
    "MIN": RGBColor(29, 66, 138),
    "NOP": RGBColor(12, 35, 64),
    "NYK": RGBColor(29, 66, 138),
    "OKC": RGBColor(0, 122, 193),
    "ORL": RGBColor(0, 80, 181),
    "PHI": RGBColor(29, 66, 138),
    "PHX": RGBColor(29, 17, 96),
    "POR": _BLACK,
    "SAC": _BLACK,
    "SAS": _BLACK,
    "TOR": RGBColor(206, 17, 65),
    "UTA": RGBColor(78, 0, 142),
    "WAS": RGBColor(0, 43, 92),
}

# abbreviation -> the alt tile background (a distinct second color).
LOGO_ALT_COLOR: dict[str, RGBColor] = {
    "ATL": RGBColor(253, 185, 39),
    "BOS": _WHITE,
    "BKN": RGBColor(111, 114, 113),  # gray, the third color: on white, its meter (white too) would vanish
    "CHA": RGBColor(0, 120, 140),  # the table's first, traded with the base
    "CHI": _BLACK,
    "CLE": _WHITE,  # the user's pick: the base's maroon C with its gold bevel, on white
    "DAL": RGBColor(187, 196, 202),
    "DEN": RGBColor(254, 197, 36),
    "DET": RGBColor(200, 16, 46),
    "GSW": RGBColor(253, 185, 39),
    "HOU": RGBColor(255, 213, 32),
    "IND": RGBColor(255, 213, 32),
    "LAC": RGBColor(200, 16, 46),
    "LAL": RGBColor(253, 185, 39),
    "MEM": RGBColor(18, 23, 63),
    "MIA": RGBColor(249, 160, 27),  # yellow, the third color: the red ball melts into the table's red
    "MIL": RGBColor(238, 225, 198),
    "MIN": RGBColor(0, 154, 68),
    "NOP": RGBColor(185, 151, 91),
    "NYK": RGBColor(245, 132, 38),
    "OKC": RGBColor(239, 59, 36),
    "ORL": _BLACK,
    "PHI": RGBColor(200, 16, 46),
    "PHX": _WHITE,  # the user's pick: the sun-ball reads on white once it is stroked in the club's purple
    "POR": RGBColor(224, 58, 62),
    "SAC": RGBColor(90, 45, 129),
    "SAS": RGBColor(196, 206, 212),
    "TOR": _BLACK,
    "UTA": RGBColor(121, 163, 220),
    "WAS": RGBColor(227, 24, 55),
}

# abbreviation -> the meter color when the club shows its alt, where its base won't do. These navy
# bases lift to a royal blue none of these clubs wear, so the meter goes white beside the alt instead.
# Denver's navy is left out: it lifts to the skyline blue the Nuggets do wear.
FREED_BASE_METER_COLOR: dict[str, RGBColor] = {
    "IND": _WHITE,
    "LAC": _WHITE,
    "NOP": _WHITE,
    "WAS": _WHITE,
}
