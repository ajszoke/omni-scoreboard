"""Hardware-agnostic drawing surface and a recording test double."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from omni.core.colors import RGBColor
from omni.renderers.image import LogoImage

__all__ = ["Canvas", "DimmedCanvas", "DrawOp", "RecordingCanvas"]


@runtime_checkable
class Canvas(Protocol):
    """The minimal drawing surface a renderer needs.

    Coordinates are pixels with the origin at the top-left. `text` draws with the
    glyph cell's top-left at `(x, y)` using a named fixed-width font. `draw_image`
    blits a `LogoImage` with its top-left at `(x, y)`. All writes clip to bounds.
    """

    @property
    def width(self) -> int: ...

    @property
    def height(self) -> int: ...

    def fill(self, color: RGBColor) -> None: ...

    def set_pixel(self, x: int, y: int, color: RGBColor) -> None: ...

    def fill_rect(self, x: int, y: int, w: int, h: int, color: RGBColor) -> None: ...

    def text(self, x: int, y: int, s: str, color: RGBColor, *, font: str = "4x6") -> None: ...

    def draw_image(self, x: int, y: int, image: LogoImage) -> None: ...


@dataclass(frozen=True, slots=True)
class DrawOp:
    """One recorded drawing call (see `RecordingCanvas`)."""

    op: str
    x: int = 0
    y: int = 0
    w: int = 0
    h: int = 0
    color: RGBColor | None = None
    text: str = ""
    font: str = ""
    key: str = ""  # for "image" ops: the blitted logo's asset key


class RecordingCanvas:
    """A `Canvas` that records draw calls instead of rasterizing them.

    Lets tests assert layout (what got drawn where) independently of fonts/pixels.
    """

    def __init__(self, width: int, height: int) -> None:
        self._w = width
        self._h = height
        self.ops: list[DrawOp] = []

    @property
    def width(self) -> int:
        return self._w

    @property
    def height(self) -> int:
        return self._h

    def fill(self, color: RGBColor) -> None:
        self.ops.append(DrawOp("fill", color=color))

    def set_pixel(self, x: int, y: int, color: RGBColor) -> None:
        self.ops.append(DrawOp("set_pixel", x=x, y=y, color=color))

    def fill_rect(self, x: int, y: int, w: int, h: int, color: RGBColor) -> None:
        self.ops.append(DrawOp("fill_rect", x=x, y=y, w=w, h=h, color=color))

    def text(self, x: int, y: int, s: str, color: RGBColor, *, font: str = "4x6") -> None:
        self.ops.append(DrawOp("text", x=x, y=y, color=color, text=s, font=font))

    def draw_image(self, x: int, y: int, image: LogoImage) -> None:
        self.ops.append(DrawOp("image", x=x, y=y, w=image.width, h=image.height, key=image.key))

    def texts(self) -> list[DrawOp]:
        return [o for o in self.ops if o.op == "text"]

    def rects(self) -> list[DrawOp]:
        return [o for o in self.ops if o.op == "fill_rect"]

    def images(self) -> list[DrawOp]:
        return [o for o in self.ops if o.op == "image"]


class DimmedCanvas:
    """A `Canvas` that draws onto another at a fraction of every color's brightness.

    The board draws a card whose data is past its age through one, so the whole card reads as
    stale on every profile without any renderer making room for a marker.
    """

    def __init__(self, inner: Canvas, *, brightness: float = 0.4) -> None:
        if not 0.0 < brightness <= 1.0:
            raise ValueError("brightness must be in (0, 1]")
        self._inner = inner
        self._brightness = brightness

    @property
    def width(self) -> int:
        return self._inner.width

    @property
    def height(self) -> int:
        return self._inner.height

    def fill(self, color: RGBColor) -> None:
        self._inner.fill(self._dim(color))

    def set_pixel(self, x: int, y: int, color: RGBColor) -> None:
        self._inner.set_pixel(x, y, self._dim(color))

    def fill_rect(self, x: int, y: int, w: int, h: int, color: RGBColor) -> None:
        self._inner.fill_rect(x, y, w, h, self._dim(color))

    def text(self, x: int, y: int, s: str, color: RGBColor, *, font: str = "4x6") -> None:
        self._inner.text(x, y, s, self._dim(color), font=font)

    def draw_image(self, x: int, y: int, image: LogoImage) -> None:
        self._inner.draw_image(x, y, _dimmed_image(image, self._brightness))

    def _dim(self, color: RGBColor) -> RGBColor:
        return _dimmed(color, self._brightness)


def _dimmed(color: RGBColor, brightness: float) -> RGBColor:
    return RGBColor(round(color.r * brightness), round(color.g * brightness), round(color.b * brightness))


_DIMMED_TILES: dict[tuple[str, float], LogoImage] = {}  # a tile's key names one image: the store caches by it


def _dimmed_image(image: LogoImage, brightness: float) -> LogoImage:
    """A tile at a fraction of its brightness, kept so a stale card's frames don't redo the work."""
    dimmed = _DIMMED_TILES.get((image.key, brightness))
    if dimmed is None:
        pixels = tuple(_dimmed(pixel, brightness) for pixel in image.pixels)
        dimmed = LogoImage(key=f"{image.key}@{brightness:g}", width=image.width, height=image.height, pixels=pixels)
        _DIMMED_TILES[(image.key, brightness)] = dimmed
    return dimmed
