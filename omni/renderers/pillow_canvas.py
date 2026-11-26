"""A Pillow-backed `Canvas` for headless preview and golden-image snapshot tests."""

from __future__ import annotations

from PIL import Image

from omni.core.colors import RGBColor
from omni.renderers.font import text_mask
from omni.renderers.image import LogoImage

__all__ = ["PillowCanvas"]

# Each tile as a Pillow image, built once: a board pastes the same few tiles every frame. Keyed by identity and
# holding the tile itself, so an id can't be reused while its entry stands; a slate's tiles number in the dozens.
_TILES: dict[int, tuple[LogoImage, Image.Image]] = {}


def _as_pillow(image: LogoImage) -> Image.Image:
    held = _TILES.get(id(image))
    if held is None:
        tile = Image.new("RGB", (image.width, image.height))
        tile.putdata([(c.r, c.g, c.b) for c in image.pixels])
        held = _TILES[id(image)] = (image, tile)
    return held[1]


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
        left, top, right, bottom = max(0, x), max(0, y), min(self._w, x + w), min(self._h, y + h)
        if left < right and top < bottom:
            self._img.paste((color.r, color.g, color.b), (left, top, right, bottom))

    def text(self, x: int, y: int, s: str, color: RGBColor, *, font: str = "4x6") -> None:
        # Pillow clips a paste that runs off the canvas.
        self._img.paste((color.r, color.g, color.b), (x, y), text_mask(font, s))

    def draw_image(self, x: int, y: int, image: LogoImage) -> None:
        self._img.paste(_as_pillow(image), (x, y))

    def image(self) -> Image.Image:
        return self._img
