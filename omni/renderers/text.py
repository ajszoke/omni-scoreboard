"""Anchored text-layout primitives shared by the card renderers.

Anchoring a value to a right edge or a horizontal centre keeps it *stable* as its
width changes — a score going ``9`` -> ``10``, a countdown ``45m`` -> ``2h05m`` —
instead of drifting under a fixed left origin. These are the "anchored text / stable
alignment" primitives the build calls for (B1 P0/P1), factored out of the individual
renderers now that more than one needs them (the rule from the renderer-axis review:
shared profile-layout primitives only once at least two renderers use them).

Width sums each glyph's real advance: these fonts are fixed-width for ordinary glyphs
but carry a few deliberately narrower ones (the thin space U+2009 advances 2px in 4x6),
so a string is not simply ``cell_width * len``.
"""

from __future__ import annotations

from omni.core.colors import RGBColor
from omni.renderers.canvas import Canvas
from omni.renderers.font import advance

__all__ = ["text_width", "draw_right_aligned", "draw_centered"]


def text_width(s: str, font: str) -> int:
    """The pixel width of ``s`` in ``font``, summing each glyph's advance.

    Matches exactly what the rasterizer lays down (it advances by ``DWIDTH`` per
    glyph), so anchored layout stays in lockstep with the drawn pixels even when a
    string mixes full-cell glyphs with a narrow one like the thin space.
    """
    return sum(advance(font, ch) for ch in s)


def draw_right_aligned(canvas: Canvas, right_x: int, y: int, s: str, color: RGBColor, font: str) -> None:
    """Draw ``s`` so its right edge sits at ``right_x`` (stable as the string widens)."""
    canvas.text(right_x - text_width(s, font), y, s, color, font=font)


def draw_centered(canvas: Canvas, left: int, right: int, y: int, s: str, color: RGBColor, font: str) -> None:
    """Draw ``s`` horizontally centred within ``[left, right)``."""
    canvas.text(left + (right - left - text_width(s, font)) // 2, y, s, color, font=font)
