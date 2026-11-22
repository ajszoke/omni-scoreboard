"""Render the cached NBA logo marks into the 20x20 base + alt logo tiles.

Each club's mark comes in two surface variants, as the MLB caps do: one drawn for light backgrounds
and one for dark ones. A tile takes the variant that suits its background's luminance, which on its own
keeps most marks from vanishing. A hand-picked alternate mark comes as one raster for both tiles. Otherwise the default treatment recolors nothing: the mark, as large
as the tile allows with a pixel of margin all round, laid on its palette background and shrunk
bicubically (see ``logo_tiles.compose_on_background``). A per-club treatment layer overrides that where
a mark reads poorly on its background at this size, the same way the MLB and NFL tools do.

Shares its pipeline with the MLB and NFL tools (``tools/logo_tiles.py``). Needs ``cairosvg``,
``scipy``, ``Pillow`` and ``numpy`` (tool-time only; the runtime never renders SVG). Run
``fetch_nba_logos`` first, then this, from the repo root.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root, for `omni`

from omni.core.colors import RGBColor  # noqa: E402
from omni.domain.logos import LogoSurface  # noqa: E402
from omni.providers.nba_palette import LOGO_ALT_COLOR, LOGO_BASE_COLOR  # noqa: E402
from omni.providers.nba_teams import NBA_TEAMS  # noqa: E402
from tools.logo_tiles import Treatment, rasterize, tile  # noqa: E402

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_RAW = os.path.join(_ROOT, "assets", "raw", "nba")
_OUT = os.path.join(_ROOT, "assets", "logos", "nba")

_BLACK = RGBColor(0, 0, 0)
_WHITE = RGBColor(255, 255, 255)
_RENDER_PX = 720  # rendered large, so the one shrink to the tile has detail to work from
_PAD = 1  # a pixel of margin all round: the mark as big as the tile allows
_DARK_LUMA = 0.45  # below this a background takes the mark drawn for dark backgrounds


def _source(surface: LogoSurface) -> str:
    """The cached file stem for a surface variant of a club's mark."""
    return f"primary_{surface.value}"


def _surface(bg: RGBColor) -> LogoSurface:
    return LogoSurface.ON_DARK if bg.relative_luminance() < _DARK_LUMA else LogoSurface.ON_LIGHT


# The part of the mark both of a club's tiles show, where the whole mark is too wide to read at this
# size: a box of the mark (left, top, right, bottom, as fractions of its extent). Else: the whole mark.
_EMBLEM: dict[str, dict[str, object]] = {
    "NOP": {"crop": (0.25, 0.0, 0.75, 1.0)},  # the pelican, wings trimmed so it isn't a thin strip
    "OKC": {"crop": (0.15, 0.0, 0.85, 1.0)},  # the shield, its trailing streaks trimmed for the same reason
}


# Clubs whose tiles both show an alternate mark the user picked, cached as one raster for any surface,
# with any settings that mark needs.
_ALTERNATE: dict[str, dict[str, object]] = {
    # The hornet in side profile, without the primary mark's "CHARLOTTE HORNETS" bar. Its cut-out left
    # a few stray pixels along the top edge, which would otherwise widen the crop.
    "CHA": {"despeck": True},
}


def _on(abbreviation: str, slot: str, **changes: object) -> Treatment:
    """The club's mark on its palette background for ``slot``, with ``changes`` applied."""
    bg = LOGO_BASE_COLOR[abbreviation] if slot == "base" else LOGO_ALT_COLOR[abbreviation]
    settings: dict[str, object] = {
        "source": "alternate" if abbreviation in _ALTERNATE else _source(_surface(bg)),
        "pad": _PAD,
        "on_background": True,
        **_ALTERNATE.get(abbreviation, {}),
        **_EMBLEM.get(abbreviation, {}),
        **changes,
    }
    return Treatment(bg=bg, **settings)  # type: ignore[arg-type]


# Per-club overrides (else: the mark for the tile's surface, as drawn, on its palette background). Each
# fixes a mark drawn in its own background color, which would vanish into the tile.
_BASE_TREAT: dict[str, Treatment] = {
    "CHI": _on("CHI", "base", outline=_BLACK),  # the bull stroked black: its red melts into the red tile
    # Pistons: the ball and its white ring alone, without the logo's own blue rim and silver edge, so the ball fills
    # the blue tile. The white ring starts about 8.5% in from the logo's edge.
    "DET": _on("DET", "base", crop=(0.08, 0.08, 0.92, 0.92), crop_round=True),
    "HOU": _on("HOU", "base", recolors=((RGBColor(206, 14, 45), _WHITE, 60),)),  # white R on red
    "IND": _on("IND", "base", recolors=((RGBColor(12, 35, 64), _WHITE, 40),)),  # white P on navy
    # Bucks: the green buck and its cream markings trade colors, so the buck shows on green.
    "MIL": _on(
        "MIL",
        "base",
        recolors=(
            (RGBColor(0, 71, 26), RGBColor(237, 224, 197), 40),
            (RGBColor(237, 224, 197), RGBColor(0, 71, 26), 40),
        ),
    ),
    "ORL": _on("ORL", "base", recolors=((RGBColor(0, 80, 181), RGBColor(6, 25, 34), 30),)),  # the mark's navy ball
    "TOR": _on("TOR", "base", recolors=((RGBColor(188, 33, 38), _WHITE, 60),)),  # white claws on red
}
_ALT_TREAT: dict[str, Treatment] = {
    "PHI": _on("PHI", "alt", recolors=((RGBColor(200, 16, 46), _WHITE, 40),)),  # an all-white 76 on red
    "PHX": _on("PHX", "alt", outline=LOGO_BASE_COLOR["PHX"]),  # the sun-ball on white, stroked in the club's purple
    "UTA": _on("UTA", "alt", source=_source(LogoSurface.ON_LIGHT)),  # the purple note: a white one blurs on sky blue
}


def _treatment(abbreviation: str, slot: str) -> Treatment:
    table = _BASE_TREAT if slot == "base" else _ALT_TREAT
    return table.get(abbreviation, _on(abbreviation, slot))


def build(abbreviation: str) -> None:
    key = abbreviation.lower()
    for slot, suffix in (("base", ""), ("alt", "-alt")):
        treatment = _treatment(abbreviation, slot)
        rgba = rasterize(os.path.join(_RAW, key), treatment.source or _source(_surface(treatment.bg)), px=_RENDER_PX)
        tile(rgba, treatment).save(os.path.join(_OUT, f"{key}{suffix}.png"))


def main() -> None:
    os.makedirs(_OUT, exist_ok=True)
    for abbreviation in NBA_TEAMS:
        build(abbreviation)
    print(f"rendered base + alt tiles for {len(NBA_TEAMS)} teams")


if __name__ == "__main__":
    main()
