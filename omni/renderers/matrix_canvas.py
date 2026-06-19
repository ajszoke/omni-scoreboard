"""A `Canvas` that drives a real LED matrix surface (emulator or hardware).

Bridges omni's hardware-agnostic `Canvas` onto any object exposing the
rgbmatrix/RGBMatrixEmulator `FrameCanvas` pixel API (`SetPixel`). The rasterizing
logic mirrors `PillowCanvas` exactly, so what the emulator (or a Pi panel) shows
is pixel-identical to the golden-image snapshots.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from omni.core.colors import RGBColor
from omni.renderers.font import rasterize

__all__ = ["MatrixCanvas", "MatrixSurface"]


@runtime_checkable
class MatrixSurface(Protocol):
    """The slice of the rgbmatrix/emulator canvas API this adapter needs.

    Method name follows the external library (`SetPixel`), not PEP 8.
    """

    def SetPixel(self, x: int, y: int, r: int, g: int, b: int) -> None: ...


class MatrixCanvas:
    """Draws onto an LED matrix frame canvas; out-of-bounds writes are clipped."""

    def __init__(self, surface: MatrixSurface, width: int, height: int) -> None:
        self._surface = surface
        self._w = width
        self._h = height

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
            self._surface.SetPixel(x, y, color.r, color.g, color.b)

    def fill_rect(self, x: int, y: int, w: int, h: int, color: RGBColor) -> None:
        for yy in range(max(0, y), min(self._h, y + h)):
            for xx in range(max(0, x), min(self._w, x + w)):
                self._surface.SetPixel(xx, yy, color.r, color.g, color.b)

    def text(self, x: int, y: int, s: str, color: RGBColor, *, font: str = "4x6") -> None:
        for ry, row in enumerate(rasterize(font, s)):
            for rx, on in enumerate(row):
                if on:
                    self.set_pixel(x + rx, y + ry, color)
