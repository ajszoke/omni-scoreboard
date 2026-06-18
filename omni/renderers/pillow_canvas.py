"""A Pillow-backed `Canvas` for headless preview and golden-image snapshot tests."""

from __future__ import annotations

from PIL import Image

from omni.core.colors import RGBColor
from omni.renderers.font import rasterize

__all__ = ["PillowCanvas"]


class PillowCanvas:
    """Draws onto an in-memory RGB `PIL.Image`; out-of-bounds writes are clipped."""

    def __init__(self, width: int, height: int, background: RGBColor = RGBColor(0, 0, 0)) -> None:
        self._w = width
        self._h = height
        self._img = Image.new("RGB", (width, height), (background.r, background.g, background.b))
        pixels = self._img.load()
        if pixels is None:  # pragma: no cover - an RGB image always provides pixel access
            raise RuntimeError("Pillow image provided no pixel access")
        self._px = pixels

    @property
    def width(self) -> int:
        return self._w

    @property
    def height(self) -> int:
        return self._h

    def fill(self, color: RGBColor) -> None:
        self.fill_rect(0, 0, self._w, self._h, color)

    def set_pixel(self, x: int, y: int, color: RGBColor) -> None:
        if 0 <= x < self._w and 0 <= y < self._h:
            self._px[x, y] = (color.r, color.g, color.b)

    def fill_rect(self, x: int, y: int, w: int, h: int, color: RGBColor) -> None:
        rgb = (color.r, color.g, color.b)
        for yy in range(max(0, y), min(self._h, y + h)):
            for xx in range(max(0, x), min(self._w, x + w)):
                self._px[xx, yy] = rgb

    def text(self, x: int, y: int, s: str, color: RGBColor, *, font: str = "4x6") -> None:
        rgb = (color.r, color.g, color.b)
        for ry, row in enumerate(rasterize(font, s)):
            for rx, on in enumerate(row):
                if on:
                    px, py = x + rx, y + ry
                    if 0 <= px < self._w and 0 <= py < self._h:
                        self._px[px, py] = rgb

    def image(self) -> Image.Image:
        return self._img
