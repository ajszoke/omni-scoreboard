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

import io
import os
import sys
from dataclasses import dataclass

import cairosvg
import numpy as np
from PIL import Image
from scipy import ndimage

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root, for `omni`

from omni.core.colors import RGBColor  # noqa: E402
from omni.providers.mlb_palette import LOGO_ALT_COLOR  # noqa: E402
from omni.providers.mlb_teams import _TEAM_ID_INFO  # noqa: E402

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_RAW = os.path.join(_ROOT, "assets", "raw", "mlb")
_OUT = os.path.join(_ROOT, "assets", "logos", "mlb")
_RENDER_PX = 240
_SIZE = 20
_PAD = 2
_DARK_LUMA = 0.45  # below this a background takes the light (cap-on-dark) mark


@dataclass(frozen=True, slots=True)
class Treatment:
    """How one tile variant is built: which source, on what background, adjusted how."""

    bg: RGBColor
    source: str | None = None  # source stem; None -> cap polarity chosen by bg luminance
    recolors: tuple[tuple[RGBColor, RGBColor, int], ...] = ()  # (from, to, tolerance)
    knockout: RGBColor | None = None  # make this colour transparent before cropping
    dx: int = 0  # nudge the centred mark horizontally
    pad: int = _PAD  # margin inside the tile (smaller -> larger mark)


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
    stem = treatment.source or _surface(treatment.bg)
    folder = os.path.join(_RAW, str(team_id))
    png_path = os.path.join(folder, f"{stem}.png")
    if os.path.exists(png_path):
        return np.array(Image.open(png_path).convert("RGBA"))
    png = cairosvg.svg2png(url=os.path.join(folder, f"{stem}.svg"), output_width=_RENDER_PX, output_height=_RENDER_PX)
    return np.array(Image.open(io.BytesIO(png)).convert("RGBA"))


def _knockout(rgba: np.ndarray, color: RGBColor, tol: int = 45) -> np.ndarray:
    """Make a baked-in background colour transparent (so the crop is the mark, not a square)."""
    rgb = rgba[:, :, :3].astype(int)
    near = np.sqrt(((rgb - [color.r, color.g, color.b]) ** 2).sum(2)) <= tol
    out = rgba.copy()
    out[near, 3] = 0
    return out


def _detrademark(rgba: np.ndarray, min_frac: float = 0.04) -> np.ndarray:
    """Drop a tiny isolated component only in the bottom-right (the (TM)/(R) zone).

    Position-gating keeps legitimate small body parts — the apostrophe of "A's" sits
    mid-mark, so it survives while the corner trademark glyph is removed.
    """
    height, width = rgba.shape[:2]
    solid = rgba[:, :, 3] > 30
    labels, count = ndimage.label(solid)
    if count <= 1:
        return rgba
    areas = ndimage.sum(solid, labels, range(1, count + 1))
    centroids = ndimage.center_of_mass(solid, labels, range(1, count + 1))
    out = rgba.copy()
    for index, (area, (cy, cx)) in enumerate(zip(areas, centroids), start=1):
        in_corner = cy > 0.62 * height and cx > 0.55 * width
        if area < min_frac * areas.max() and in_corner:
            out[labels == index, 3] = 0
    return out


def _recolor(rgba: np.ndarray, recolors: tuple[tuple[RGBColor, RGBColor, int], ...]) -> np.ndarray:
    # Masks are computed against the ORIGINAL so rules can't chain — a two-colour swap
    # (NYM orange<->white) works because each side keys off the unmodified image.
    out = rgba.copy()
    rgb = rgba[:, :, :3].astype(int)
    opaque = rgba[:, :, 3] > 30
    for frm, to, tol in recolors:
        near = np.sqrt(((rgb - [frm.r, frm.g, frm.b]) ** 2).sum(2)) <= tol
        out[opaque & near, :3] = [to.r, to.g, to.b]
    return out


def _compose(rgba: np.ndarray, bg: RGBColor, dx: int, pad: int) -> Image.Image:
    alpha = rgba[:, :, 3]
    ys, xs = np.where(alpha > 30)
    mark = Image.fromarray(rgba[ys.min() : ys.max() + 1, xs.min() : xs.max() + 1])
    mark.thumbnail((_SIZE - 2 * pad, _SIZE - 2 * pad), Image.Resampling.LANCZOS)
    tile = Image.new("RGBA", (_SIZE, _SIZE), (bg.r, bg.g, bg.b, 255))
    tile.alpha_composite(mark, ((_SIZE - mark.width) // 2 + dx, (_SIZE - mark.height) // 2))
    return tile.convert("RGB")


def build(team_id: int, abbr: str) -> None:
    for slot, suffix in (("base", ""), ("alt", "-alt")):
        t = _treatment(team_id, slot)
        rgba = _render(team_id, t)
        if t.knockout is not None:
            rgba = _knockout(rgba, t.knockout)
        rgba = _recolor(_detrademark(rgba), t.recolors)
        _compose(rgba, t.bg, t.dx, t.pad).save(os.path.join(_OUT, f"{abbr}{suffix}.png"))


def main() -> None:
    for team_id, (abbr, _) in _TEAM_ID_INFO.items():
        build(team_id, abbr.lower())
    print(f"rendered base + alt tiles for {len(_TEAM_ID_INFO)} teams")


if __name__ == "__main__":
    main()
