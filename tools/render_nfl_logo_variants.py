"""Render the cached NFL logo SVGs into the 20x20 base + alt logo tiles.

The default treatment recolors nothing: the club's logo, as large as the tile allows with a pixel of
margin all round, laid on its palette background and shrunk bicubically (see
``logo_tiles.compose_on_background``). A per-club
treatment layer overrides that where a logo reads poorly on its background at this size, the same way
the MLB tool does.

Shares its pipeline with the MLB tool (``tools/logo_tiles.py``). Needs ``cairosvg``, ``scipy``,
``Pillow`` and ``numpy`` (tool-time only; the runtime never renders SVG). Run ``fetch_nfl_logos``
first, then this, from the repo root.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root, for `omni`

from omni.core.colors import RGBColor  # noqa: E402
from omni.providers.nfl_palette import LOGO_ALT_COLOR, LOGO_BASE_COLOR  # noqa: E402
from omni.providers.nfl_teams import NFL_TEAMS  # noqa: E402
from tools.logo_tiles import Treatment, rasterize, tile  # noqa: E402

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_RAW = os.path.join(_ROOT, "assets", "raw", "nfl")
_OUT = os.path.join(_ROOT, "assets", "logos", "nfl")

_WHITE = RGBColor(255, 255, 255)
_RENDER_PX = 720  # rendered large, so the one shrink to the tile has detail to work from
_PAD = 1  # a pixel of margin all round: the mark as big as the tile allows


def _on(abbreviation: str, slot: str, **changes: object) -> Treatment:
    """The club's logo on its palette background for ``slot``, with ``changes`` applied."""
    bg = LOGO_BASE_COLOR[abbreviation] if slot == "base" else LOGO_ALT_COLOR[abbreviation]
    settings: dict[str, object] = {"source": "primary", "pad": _PAD, "on_background": True, **changes}
    return Treatment(bg=bg, **settings)  # type: ignore[arg-type]


# Per-club overrides (else: the logo as drawn, on its palette background). Each fixes a logo drawn
# in its own background color, which would vanish into the tile.
_BASE_TREAT: dict[str, Treatment] = {
    "DAL": _on("DAL", "base", recolors=((RGBColor(0, 32, 64), _WHITE, 40),)),  # white star on navy
    "DET": _on("DET", "base", recolors=((RGBColor(0, 112, 176), _WHITE, 40),)),  # white lion on Honolulu blue
    "IND": _on("IND", "base", recolors=((RGBColor(0, 48, 104), _WHITE, 40),)),  # white horseshoe on blue
    "NYG": _on("NYG", "base", recolors=((RGBColor(8, 32, 96), _WHITE, 40),)),  # white "ny", red outline, on blue
}
_ALT_TREAT: dict[str, Treatment] = {
    "CLE": _on("CLE", "alt", outline=RGBColor(49, 29, 0)),  # the orange helmet, stroked brown, on orange
    "CHI": _on("CHI", "alt", recolors=((RGBColor(200, 56, 0), RGBColor(11, 22, 42), 50),)),  # navy C on orange
    "LAC": _on("LAC", "alt", recolors=((RGBColor(232, 168, 24), RGBColor(0, 123, 199), 45),)),  # blue bolt on gold
    # Rams: the horn goes blue and the letters white, so neither melts into the gold.
    "LAR": _on(
        "LAR", "alt", recolors=((RGBColor(248, 208, 0), RGBColor(0, 53, 148), 45), (RGBColor(0, 48, 144), _WHITE, 45))
    ),
    # Washington: drop the logo's own maroon square, leaving the gold W on black. A maroon W on gold
    # blurs to brown at this size; the W's strokes are only a pixel or so wide.
    "WAS": _on("WAS", "alt", knockout=RGBColor(88, 16, 16)),
}


def _treatment(abbreviation: str, slot: str) -> Treatment:
    table = _BASE_TREAT if slot == "base" else _ALT_TREAT
    return table.get(abbreviation, _on(abbreviation, slot))


def build(abbreviation: str) -> None:
    key = abbreviation.lower()
    for slot, suffix in (("base", ""), ("alt", "-alt")):
        treatment = _treatment(abbreviation, slot)
        rgba = rasterize(os.path.join(_RAW, key), treatment.source or "primary", px=_RENDER_PX)
        tile(rgba, treatment).save(os.path.join(_OUT, f"{key}{suffix}.png"))


def main() -> None:
    os.makedirs(_OUT, exist_ok=True)
    for abbreviation in NFL_TEAMS:
        build(abbreviation)
    print(f"rendered base + alt tiles for {len(NFL_TEAMS)} teams")


if __name__ == "__main__":
    main()
