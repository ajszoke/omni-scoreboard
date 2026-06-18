"""Tests for the Canvas protocol implementations (recording + Pillow)."""

from __future__ import annotations

from omni.core.colors import RGBColor
from omni.renderers.canvas import Canvas, RecordingCanvas
from omni.renderers.pillow_canvas import PillowCanvas


def test_recording_canvas_records_ops_and_satisfies_protocol() -> None:
    canvas = RecordingCanvas(128, 64)
    assert isinstance(canvas, Canvas)
    canvas.fill(RGBColor(0, 0, 0))
    canvas.fill_rect(1, 2, 3, 4, RGBColor(10, 20, 30))
    canvas.text(5, 6, "HI", RGBColor(255, 255, 255), font="4x6")
    assert [op.op for op in canvas.ops] == ["fill", "fill_rect", "text"]
    rect = canvas.rects()[0]
    assert (rect.x, rect.y, rect.w, rect.h) == (1, 2, 3, 4)
    assert canvas.texts()[0].text == "HI"


def test_pillow_canvas_draws_pixels_and_satisfies_protocol() -> None:
    canvas = PillowCanvas(8, 4)
    assert isinstance(canvas, Canvas)
    canvas.fill(RGBColor(0, 0, 0))
    canvas.fill_rect(2, 1, 3, 2, RGBColor(255, 0, 0))
    image = canvas.image()
    assert image.size == (8, 4)
    assert image.getpixel((2, 1)) == (255, 0, 0)  # inside the rect
    assert image.getpixel((0, 0)) == (0, 0, 0)  # outside the rect
    canvas.set_pixel(7, 3, RGBColor(0, 255, 0))
    assert image.getpixel((7, 3)) == (0, 255, 0)
    canvas.set_pixel(99, 99, RGBColor(0, 0, 255))  # out of bounds: clipped, no error
