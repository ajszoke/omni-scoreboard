"""Shared steps for turning cached logo art into the board's 20x20 tiles, for any league.

Each league's render tool picks a source and a background per tile (its treatment table); these
steps do the rest: rasterize the source, make a baked-in background transparent, drop stray specks,
cut an emblem out of a fuller logo, drop the tiny trademark glyph in the corner, recolor marked
elements, crop to the mark, and center it on the tile.

A tool-time module; needs ``cairosvg``, ``scipy``, ``Pillow`` and ``numpy`` (see
``tools/requirements-logos.txt``). The runtime never renders SVG.
"""

from __future__ import annotations

import io
import os
from dataclasses import dataclass

import cairosvg
import numpy as np
from PIL import Image
from scipy import ndimage

from omni.core.colors import RGBColor

__all__ = [
    "PAD",
    "SIZE",
    "Treatment",
    "clip",
    "compose",
    "compose_on_background",
    "despeck",
    "detrademark",
    "knockout",
    "rasterize",
    "recolor",
    "tile",
]

RENDER_PX = 240
SIZE = 20
PAD = 2


@dataclass(frozen=True, slots=True)
class Treatment:
    """How one tile variant is built: which source, on what background, adjusted how."""

    bg: RGBColor
    source: str | None = None  # source stem; None -> cap polarity chosen by bg luminance
    recolors: tuple[tuple[RGBColor, RGBColor, int], ...] = ()  # (from, to, tolerance)
    knockout: RGBColor | None = None  # make this colour transparent before cropping
    dx: int = 0  # nudge the centred mark horizontally
    pad: int = PAD  # margin inside the tile (smaller -> larger mark)
    on_background: bool = False  # lay the mark on its tile color at full size, then shrink bicubically
    outline: RGBColor | None = None  # a one-pixel stroke around the mark, to set it off its own background color
    crop: tuple[float, float, float, float] | None = None  # keep this box of the mark, e.g. without its wordmark
    crop_round: bool = False  # keep the ellipse inscribed in the crop box instead, e.g. a roundel's inner disc
    despeck: bool = False  # drop stray specks anywhere in the source, e.g. a cut-out's leftover pixels


def rasterize(folder: str, stem: str, *, px: int = RENDER_PX) -> np.ndarray:
    """Rasterize a cached source: a pre-rendered PNG when there is one, else the SVG via cairosvg."""
    png_path = os.path.join(folder, f"{stem}.png")
    if os.path.exists(png_path):
        return np.array(Image.open(png_path).convert("RGBA"))
    png = cairosvg.svg2png(url=os.path.join(folder, f"{stem}.svg"), output_width=px, output_height=px)
    return np.array(Image.open(io.BytesIO(png)).convert("RGBA"))


def knockout(rgba: np.ndarray, color: RGBColor, tol: int = 45) -> np.ndarray:
    """Make a baked-in background colour transparent (so the crop is the mark, not a square)."""
    rgb = rgba[:, :, :3].astype(int)
    near = np.sqrt(((rgb - [color.r, color.g, color.b]) ** 2).sum(2)) <= tol
    out = rgba.copy()
    out[near, 3] = 0
    return out


def clip(rgba: np.ndarray, box: tuple[float, float, float, float], *, round_: bool = False) -> np.ndarray:
    """Keep only part of the mark: ``box`` is (left, top, right, bottom) as fractions of the mark's extent.

    A wordmark or a ring of lettering turns to noise at tile size, so a club's emblem can be cut out of
    its full logo. With ``round_``, only the ellipse inscribed in the box is kept, for the disc inside a
    roundel. Returns just the kept region, so later steps see it as the whole source.
    """
    ys, xs = np.where(rgba[:, :, 3] > 30)
    top, height = ys.min(), ys.max() + 1 - ys.min()
    left, width = xs.min(), xs.max() + 1 - xs.min()
    x0, x1 = left + round(box[0] * width), left + round(box[2] * width)
    y0, y1 = top + round(box[1] * height), top + round(box[3] * height)
    out = rgba[y0:y1, x0:x1].copy()
    if round_:
        yy, xx = np.mgrid[0 : y1 - y0, 0 : x1 - x0]
        cy, cx = (y1 - y0 - 1) / 2, (x1 - x0 - 1) / 2
        outside = ((yy - cy) / ((y1 - y0) / 2)) ** 2 + ((xx - cx) / ((x1 - x0) / 2)) ** 2 > 1
        out[outside, 3] = 0
    return out


def despeck(rgba: np.ndarray, min_frac: float = 0.001) -> np.ndarray:
    """Drop every isolated speck, wherever it sits, smaller than ``min_frac`` of the mark's largest part.

    A raster cut out of its background can keep a few stray pixels along an edge; left in, they would
    widen the crop to the mark or land on the tile.
    """
    solid = rgba[:, :, 3] > 30
    labels, count = ndimage.label(solid)
    if count <= 1:
        return rgba
    areas = ndimage.sum(solid, labels, range(1, count + 1))
    out = rgba.copy()
    out[np.isin(labels, np.flatnonzero(areas < min_frac * areas.max()) + 1), 3] = 0
    return out


def detrademark(rgba: np.ndarray, min_frac: float = 0.04) -> np.ndarray:
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


def recolor(rgba: np.ndarray, recolors: tuple[tuple[RGBColor, RGBColor, int], ...]) -> np.ndarray:
    # Masks are computed against the ORIGINAL so rules can't chain — a two-colour swap
    # (NYM orange<->white) works because each side keys off the unmodified image.
    out = rgba.copy()
    rgb = rgba[:, :, :3].astype(int)
    opaque = rgba[:, :, 3] > 30
    for frm, to, tol in recolors:
        near = np.sqrt(((rgb - [frm.r, frm.g, frm.b]) ** 2).sum(2)) <= tol
        out[opaque & near, :3] = [to.r, to.g, to.b]
    return out


def compose(rgba: np.ndarray, bg: RGBColor, dx: int, pad: int) -> Image.Image:
    alpha = rgba[:, :, 3]
    ys, xs = np.where(alpha > 30)
    mark = Image.fromarray(rgba[ys.min() : ys.max() + 1, xs.min() : xs.max() + 1])
    mark.thumbnail((SIZE - 2 * pad, SIZE - 2 * pad), Image.Resampling.LANCZOS)
    tile = Image.new("RGBA", (SIZE, SIZE), (bg.r, bg.g, bg.b, 255))
    tile.alpha_composite(mark, ((SIZE - mark.width) // 2 + dx, (SIZE - mark.height) // 2))
    return tile.convert("RGB")


def compose_on_background(rgba: np.ndarray, treatment: Treatment) -> Image.Image:
    """Center the mark on the tile, shrunk from full size with the tile color already behind it.

    Laying the mark on its background first means its edges blend toward the tile rather than toward
    transparent black, and the one bicubic shrink that follows is the only resampling the mark sees.
    Side by side with Lanczos, area averaging in linear light, pulling toward the logo's own colors and
    drawing the vector at tile size, this read best on the NFL marks at 18px.
    """
    mark = _crop(rgba)
    if treatment.outline is not None:
        mark = _stroke(mark, treatment.outline, box=SIZE - 2 * treatment.pad)
    box = SIZE - 2 * treatment.pad
    scale = min(box / mark.shape[1], box / mark.shape[0])
    width, height = max(1, round(mark.shape[1] * scale)), max(1, round(mark.shape[0] * scale))
    bg = treatment.bg
    full = Image.new("RGBA", (mark.shape[1], mark.shape[0]), (bg.r, bg.g, bg.b, 255))
    full.alpha_composite(Image.fromarray(mark))
    small = full.convert("RGB").resize((width, height), Image.Resampling.BICUBIC)
    tile = Image.new("RGB", (SIZE, SIZE), (bg.r, bg.g, bg.b))
    tile.paste(small, ((SIZE - width) // 2 + treatment.dx, (SIZE - height) // 2))
    return tile


def _crop(rgba: np.ndarray) -> np.ndarray:
    ys, xs = np.where(rgba[:, :, 3] > 30)
    return rgba[ys.min() : ys.max() + 1, xs.min() : xs.max() + 1]


def _stroke(mark: np.ndarray, color: RGBColor, *, box: int) -> np.ndarray:
    """The mark with a stroke about one tile pixel wide around it, under the mark itself."""
    radius = max(1, round(max(mark.shape[:2]) / box))
    padded = np.pad(mark, ((radius, radius), (radius, radius), (0, 0)))
    solid = padded[:, :, 3] > 30
    yy, xx = np.mgrid[-radius : radius + 1, -radius : radius + 1]
    grown = ndimage.binary_dilation(solid, structure=(xx**2 + yy**2) <= radius**2)
    stroked = np.zeros_like(padded)
    stroked[grown] = [color.r, color.g, color.b, 255]
    over = Image.fromarray(stroked)
    over.alpha_composite(Image.fromarray(padded))
    return np.array(over)


def tile(rgba: np.ndarray, treatment: Treatment) -> Image.Image:
    """Run a rasterized source through one treatment and return the finished tile.

    A treatment laid on its background (or stroked) takes that path; otherwise the original one,
    which the committed MLB tiles were rendered with.
    """
    if treatment.knockout is not None:
        rgba = knockout(rgba, treatment.knockout)
    if treatment.despeck:
        rgba = despeck(rgba)
    if treatment.crop is not None:
        rgba = clip(rgba, treatment.crop, round_=treatment.crop_round)
    rgba = recolor(detrademark(rgba), treatment.recolors)
    if treatment.on_background or treatment.outline is not None:
        return compose_on_background(rgba, treatment)
    return compose(rgba, treatment.bg, treatment.dx, treatment.pad)
