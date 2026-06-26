"""Tests for the shared anchored text-layout primitives."""

from __future__ import annotations

from omni.core.colors import RGBColor
from omni.renderers.canvas import RecordingCanvas
from omni.renderers.text import draw_centered, draw_right_aligned, text_width

WHITE = RGBColor(255, 255, 255)


def test_text_width_sums_per_glyph_advances() -> None:
    assert text_width("ABC", "4x6") == 12  # 3 ordinary glyphs * 4px
    assert text_width("10", "6x10") == 12  # 2 ordinary glyphs * 6px
    # the thin space advances only 2px, so width is not simply cell * len.
    assert text_width("A B", "4x6") == 10  # 4 + 2 + 4


def test_draw_right_aligned_anchors_the_right_edge() -> None:
    canvas = RecordingCanvas(64, 32)
    draw_right_aligned(canvas, 60, 5, "10", WHITE, "6x10")
    op = canvas.texts()[0]
    assert op.text == "10" and op.x == 60 - 12  # right edge at 60


def test_right_aligned_edge_is_stable_as_the_value_widens() -> None:
    # The reason anchoring exists: a wider value still ends at the same right edge.
    narrow, wide = RecordingCanvas(64, 32), RecordingCanvas(64, 32)
    draw_right_aligned(narrow, 60, 5, "9", WHITE, "6x10")
    draw_right_aligned(wide, 60, 5, "10", WHITE, "6x10")
    assert narrow.texts()[0].x + text_width("9", "6x10") == 60
    assert wide.texts()[0].x + text_width("10", "6x10") == 60


def test_draw_centered_centers_within_bounds() -> None:
    canvas = RecordingCanvas(128, 64)
    draw_centered(canvas, 64, 128, 30, "MID7", WHITE, "4x6")  # 4 glyphs * 4 = 16px
    assert canvas.texts()[0].x == 64 + (64 - 16) // 2  # == 88
