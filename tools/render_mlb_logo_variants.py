"""Render cached MLB logo SVGs into the 20x20 base + alt logo tiles.

The default treatment recolours nothing: the official cap mark ships in two
polarities, so a dark background takes ``cap_on_dark`` (a light mark) and a light
background takes ``cap_on_light`` (a dark mark) — NYY's navy primary yields a white
"NY", its silver alt the official navy "NY". A per-team treatment layer overrides
that where a club reads better from its primary logo (strokes/shadows), needs an
element recoloured (CIN's drop shadow, TEX's), or a different source entirely (HOU's
bare star-H cap, which only Wikimedia carries — every mlbstatic variant is a roundel).

Pipeline per variant: render the chosen SVG to a transparent canvas (cairosvg),
de-trademark (drop the tiny isolated mark in the bottom-right — never the body),
optionally recolour elements, crop to the mark, and centre it on the background.

A one-off; needs ``cairosvg``, ``scipy``, ``Pillow``, ``numpy`` (tool-time only — the
runtime never renders SVG). Run ``fetch_mlb_logos`` first, then this, from the repo root.
"""

from __future__ import annotations

import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root, for `omni`

from omni.core.colors import RGBColor  # noqa: E402
from omni.providers.mlb_palette import LOGO_ALT_COLOR  # noqa: E402
from omni.providers.mlb_teams import _TEAM_ID_INFO  # noqa: E402
from tools.logo_tiles import Treatment, rasterize, tile  # noqa: E402

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_RAW = os.path.join(_ROOT, "assets", "raw", "mlb")
_OUT = os.path.join(_ROOT, "assets", "logos", "mlb")
_DARK_LUMA = 0.45  # below this a background takes the light (cap-on-dark) mark


# The primary background each base tile renders on (the liked set + TEX/PIT/CWS fixes).
_BASE_BG: dict[int, RGBColor] = {
    108: RGBColor(186, 0, 33), 109: RGBColor(167, 25, 48), 110: RGBColor(223, 70, 1),
    111: RGBColor(189, 48, 57), 112: RGBColor(14, 51, 134), 113: RGBColor(198, 1, 31),
    114: RGBColor(0, 43, 92), 115: RGBColor(51, 0, 111), 116: RGBColor(12, 35, 64),
    117: RGBColor(0, 45, 98), 118: RGBColor(1, 71, 135), 119: RGBColor(0, 90, 156),
    120: RGBColor(171, 0, 3), 121: RGBColor(0, 45, 114), 133: RGBColor(0, 56, 49),
    134: RGBColor(0, 0, 0), 135: RGBColor(47, 36, 29), 136: RGBColor(12, 44, 86),
    137: RGBColor(0, 0, 0), 138: RGBColor(196, 30, 58), 139: RGBColor(11, 46, 93),
    140: RGBColor(0, 50, 120), 141: RGBColor(19, 74, 142), 142: RGBColor(0, 43, 92),
    143: RGBColor(232, 24, 40), 144: RGBColor(19, 39, 79), 145: RGBColor(0, 0, 0),
    146: RGBColor(0, 0, 0), 147: RGBColor(12, 35, 64), 158: RGBColor(19, 41, 75),
}  # fmt: skip

_BLACK, _WHITE = RGBColor(0, 0, 0), RGBColor(255, 255, 255)

# Per-team overrides (else: official cap by bg luminance, no recolour).
_NYM_ORANGE = RGBColor(255, 89, 16)
_BASE_TREAT: dict[int, Treatment] = {
    117: Treatment(bg=RGBColor(0, 45, 98), source="cap_wikimedia", pad=0),  # HOU: ringless star-H, enlarged
    115: Treatment(bg=_BLACK, source="cap_on_dark"),  # COL: the crisp silver+purple CR is now the primary, on black
    121: Treatment(
        bg=RGBColor(0, 45, 114), source="insignia", knockout=RGBColor(0, 45, 114)
    ),  # NYM: orange NY on blue, unmodified
    138: Treatment(
        bg=RGBColor(190, 10, 20), source="insignia", knockout=RGBColor(190, 10, 20)
    ),  # STL: white StL, navy stroke, on red
    # ATL base: red A, navy stroke -> recolour the stroke white (red-on-blue + white stroke).
    144: Treatment(bg=RGBColor(19, 39, 79), source="primary", recolors=((RGBColor(19, 39, 79), _WHITE, 32),)),
    # ATH base: cap-on-dark is a white A's (apostrophe intact) -> gold on green.
    133: Treatment(bg=RGBColor(0, 56, 49), source="cap_on_dark", recolors=((_WHITE, RGBColor(255, 184, 28), 40),)),
    111: Treatment(bg=RGBColor(12, 35, 64)),  # BOS: the cap B on navy (the polarity that read well)
}
_ALT_TREAT: dict[int, Treatment] = {
    113: Treatment(bg=_BLACK, source="primary", recolors=((_BLACK, _WHITE, 70),)),  # CIN: white drop shadow
    118: Treatment(bg=RGBColor(255, 199, 44), source="primary"),  # KC: blue KC on gold
    134: Treatment(
        bg=RGBColor(255, 205, 0), source="primary", recolors=((RGBColor(253, 184, 39), _BLACK, 45),)
    ),  # PIT: plain black P on yellow
    140: Treatment(
        bg=RGBColor(192, 17, 31), source="cap_on_dark", recolors=((RGBColor(192, 17, 31), RGBColor(0, 50, 120), 45),)
    ),  # TEX: blue shadow
    114: Treatment(
        bg=RGBColor(227, 25, 55),
        source="primary",
        recolors=((RGBColor(227, 25, 55), RGBColor(0, 90, 156), 45), (RGBColor(0, 43, 92), _WHITE, 45)),
    ),  # CLE: blue C, white stroke, on red
    115: Treatment(bg=RGBColor(51, 0, 111), source="cap_on_dark"),  # COL: swapped — the purple-bg CR is the alt
    # NYM: the insignia with its colours inverted (blue NY on orange) for a consistent secondary.
    121: Treatment(
        bg=_NYM_ORANGE,
        source="insignia",
        knockout=RGBColor(0, 45, 114),
        recolors=((_NYM_ORANGE, RGBColor(0, 45, 114), 60),),
    ),
    143: Treatment(bg=RGBColor(0, 48, 135), source="primary"),  # PHI: white-stroke red P on blue
    # ATL alt: recolour the red fill white (white-on-red + the navy stroke reads as blue).
    144: Treatment(bg=RGBColor(206, 17, 65), source="primary", recolors=((RGBColor(206, 17, 65), _WHITE, 42),)),
    111: Treatment(bg=RGBColor(189, 48, 57), source="socks"),  # BOS: the hanging socks, original colours, on red
}


def _surface(bg: RGBColor) -> str:
    return "cap_on_dark" if bg.relative_luminance() < _DARK_LUMA else "cap_on_light"


def _treatment(team_id: int, slot: str) -> Treatment:
    if slot == "base":
        return _BASE_TREAT.get(team_id, Treatment(bg=_BASE_BG[team_id]))
    return _ALT_TREAT.get(team_id, Treatment(bg=LOGO_ALT_COLOR[team_id]))


def _render(team_id: int, treatment: Treatment) -> np.ndarray:
    """Rasterize the source — an SVG via cairosvg, or a pre-rendered Wikimedia PNG."""
    return rasterize(os.path.join(_RAW, str(team_id)), treatment.source or _surface(treatment.bg))


def build(team_id: int, abbr: str) -> None:
    for slot, suffix in (("base", ""), ("alt", "-alt")):
        t = _treatment(team_id, slot)
        tile(_render(team_id, t), t).save(os.path.join(_OUT, f"{abbr}{suffix}.png"))


def main() -> None:
    for team_id, (abbr, _) in _TEAM_ID_INFO.items():
        build(team_id, abbr.lower())
    print(f"rendered base + alt tiles for {len(_TEAM_ID_INFO)} teams")


if __name__ == "__main__":
    main()
