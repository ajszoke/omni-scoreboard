"""Authoritative per-team logo palette: each club's alternate background colour.

A base logo tile composites the cap insignia on the team's primary colour. When two
clubs in a matchup have near-identical primaries (the navies NYY/DET, CLE/MIN) they
read as one indistinct block on a small panel, so one of them renders an *alt* tile
instead — the same mark on this distinct second brand colour. The freed primary then
becomes the team's win-probability meter colour (a later feature), which is why the
alt is a real team colour, not a generic contrasting fill.

Each alt is sourced from the club's official secondary; the value is chosen so the
mark stays legible against it (every entry clears a `delta_e` floor versus the base
the tile actually renders on — asserted in `tests/test_mlb_palette.py`). A few are
deliberate calls: AZ takes the throwback teal, LAD inverts to white (Dodgers carry no
third colour), MIN takes the 1961–75 vintage blue (a red alt would erase the red "C"
of the "TC").

`LOGO_BASE_FIX` records the two base tiles whose committed background is simply wrong
(TEX rendered red rather than the modern royal-blue cap; PIT a warm near-black rather
than solid black); the asset rebuild applies these. Keyed by StatsAPI team id to join
`mlb_teams._TEAM_ID_INFO`.
"""

from __future__ import annotations

from omni.core.colors import RGBColor

__all__ = ["LOGO_ALT_COLOR", "LOGO_BASE_FIX"]

# StatsAPI team id -> the alternate logo background (the distinct second brand colour).
LOGO_ALT_COLOR: dict[int, RGBColor] = {
    108: RGBColor(0, 50, 99),  # LAA Angels -> navy
    109: RGBColor(48, 206, 216),  # AZ D-backs -> teal (throwback)
    110: RGBColor(0, 0, 0),  # BAL Orioles -> black
    111: RGBColor(12, 35, 64),  # BOS Red Sox -> navy
    112: RGBColor(204, 52, 51),  # CHC Cubs -> red
    113: RGBColor(0, 0, 0),  # CIN Reds -> black
    114: RGBColor(227, 25, 55),  # CLE Guardians -> red
    115: RGBColor(196, 206, 212),  # COL Rockies -> silver
    116: RGBColor(250, 70, 22),  # DET Tigers -> orange
    117: RGBColor(235, 110, 31),  # HOU Astros -> orange
    118: RGBColor(189, 155, 96),  # KC Royals -> gold
    119: RGBColor(255, 255, 255),  # LAD Dodgers -> white (blue-on-white reverse; no third colour)
    120: RGBColor(20, 34, 90),  # WSH Nationals -> navy
    121: RGBColor(255, 89, 16),  # NYM Mets -> orange
    133: RGBColor(239, 178, 30),  # ATH Athletics -> gold
    134: RGBColor(253, 184, 39),  # PIT Pirates -> gold
    135: RGBColor(255, 196, 37),  # SD Padres -> gold
    136: RGBColor(0, 92, 92),  # SEA Mariners -> NW green
    137: RGBColor(239, 209, 159),  # SF Giants -> cream
    138: RGBColor(12, 35, 64),  # STL Cardinals -> navy
    139: RGBColor(143, 188, 230),  # TB Rays -> light blue
    140: RGBColor(192, 17, 31),  # TEX Rangers -> red
    141: RGBColor(108, 172, 228),  # TOR Blue Jays -> light blue
    142: RGBColor(0, 154, 188),  # MIN Twins -> vintage blue (1961-75); red "C"/white "T" stay
    143: RGBColor(0, 48, 135),  # PHI Phillies -> blue
    144: RGBColor(206, 17, 65),  # ATL Braves -> scarlet
    145: RGBColor(196, 206, 212),  # CWS White Sox -> silver
    146: RGBColor(0, 163, 224),  # MIA Marlins -> Miami blue
    147: RGBColor(196, 206, 211),  # NYY Yankees -> silver
    158: RGBColor(255, 199, 44),  # MIL Brewers -> gold
}

# Base-tile background corrections the asset rebuild applies (committed tile is wrong).
LOGO_BASE_FIX: dict[int, RGBColor] = {
    140: RGBColor(0, 50, 120),  # TEX -> royal blue (committed tile is red)
    134: RGBColor(0, 0, 0),  # PIT -> solid black (committed tile is warm #27251F, two-toned)
}
