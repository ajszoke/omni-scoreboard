"""Tests for the Canvas protocol implementations (recording + Pillow)."""

from __future__ import annotations

from omni.core.colors import RGBColor
from omni.renderers.canvas import Canvas, RecordingCanvas
from omni.renderers.image import LogoImage
from omni.renderers.pillow_canvas import PillowCanvas

# A 2x2 tile with four distinct corner colours, row-major.
_TILE = LogoImage(
    key="t",
    width=2,
    height=2,
    pixels=(RGBColor(10, 0, 0), RGBColor(0, 20, 0), RGBColor(0, 0, 30), RGBColor(40, 40, 40)),
)


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


def test_recording_canvas_records_image_blit_with_key_and_extent() -> None:
    canvas = RecordingCanvas(128, 64)
    canvas.draw_image(8, 16, _TILE)
    image = canvas.images()[0]
    assert image.op == "image"
    assert (image.x, image.y, image.w, image.h) == (8, 16, 2, 2)
    assert image.key == "t"  # records WHICH logo was blitted, for layout assertions


def test_pillow_canvas_blits_image_pixels_and_clips() -> None:
    canvas = PillowCanvas(8, 4)
    canvas.draw_image(2, 1, _TILE)
    image = canvas.image()
    assert image.getpixel((2, 1)) == (10, 0, 0)  # tile (0,0)
    assert image.getpixel((3, 1)) == (0, 20, 0)  # tile (1,0)
    assert image.getpixel((2, 2)) == (0, 0, 30)  # tile (0,1)
    assert image.getpixel((3, 2)) == (40, 40, 40)  # tile (1,1)
    assert image.getpixel((0, 0)) == (0, 0, 0)  # untouched
    canvas.draw_image(7, 3, _TILE)  # only the top-left pixel lands in bounds; rest clipped, no error
    assert image.getpixel((7, 3)) == (10, 0, 0)


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


def test_recording_canvas_dimensions_and_set_pixel() -> None:
    canvas = RecordingCanvas(64, 32)
    assert (canvas.width, canvas.height) == (64, 32)
    canvas.set_pixel(3, 4, RGBColor(1, 2, 3))
    last = canvas.ops[-1]
    assert last.op == "set_pixel" and (last.x, last.y) == (3, 4)


def test_pillow_canvas_text_lights_glyph_pixels() -> None:
    canvas = PillowCanvas(16, 8)
    canvas.text(1, 1, "1", RGBColor(255, 255, 255), font="4x6")
    image = canvas.image()
    lit = sum(1 for x in range(16) for y in range(8) if image.getpixel((x, y)) == (255, 255, 255))
    assert lit > 0  # the font path actually rasterized pixels (not via RecordingCanvas)
    assert image.getpixel((15, 7)) == (0, 0, 0)  # untouched corner stays background


def test_pillow_canvas_clips_out_of_bounds_draws() -> None:
    canvas = PillowCanvas(8, 4)
    canvas.fill_rect(-3, -3, 100, 100, RGBColor(255, 0, 0))  # oversize rect: fills frame, no error
    image = canvas.image()
    assert image.getpixel((0, 0)) == (255, 0, 0)
    assert image.getpixel((7, 3)) == (255, 0, 0)
    canvas.text(-50, -50, "1", RGBColor(0, 255, 0), font="4x6")  # fully off-screen: no error/no change
    assert image.getpixel((0, 0)) == (255, 0, 0)
