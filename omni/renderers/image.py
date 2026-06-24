"""Logo tiles: an immutable pixel grid a `Canvas` can blit, and a caching loader.

A `LogoImage` is the rasterized, hardware-agnostic form of a `LogoAsset` reference —
a flat grid of `RGBColor` the renderer blits with `Canvas.draw_image`. The committed
team tiles are pre-composited (the cap insignia already sits on the club's flat
background), so loading flattens RGBA to RGB: the stored RGB *is* the intended look,
and the partial alpha left by de-trademarking is a build artifact, not transparency.

`LogoStore` turns a `LogoAsset` reference into a `LogoImage`, caching by key and
reading the PNG from disk. This is where the logo I/O lives — the renderer asks the
store (handed to it as ambient `RenderContext`), so a renderer still never fetches.
A missing tile resolves to `None` so the renderer can fall back to a plain colour bar.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from omni.core.colors import RGBColor
from omni.domain.base import LogoAsset

__all__ = ["LogoImage", "LogoStore"]

# omni/renderers/image.py -> repo root, where committed `assets/` lives.
_REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True, slots=True)
class LogoImage:
    """An immutable RGB pixel grid (row-major) tagged with its asset key."""

    key: str
    width: int
    height: int
    pixels: tuple[RGBColor, ...]

    def __post_init__(self) -> None:
        if len(self.pixels) != self.width * self.height:
            raise ValueError("pixel count does not match width*height")

    def pixel(self, x: int, y: int) -> RGBColor:
        """The colour at `(x, y)` (caller bounds-checks; the canvas clips on blit)."""
        return self.pixels[y * self.width + x]

    @classmethod
    def from_png(cls, path: str | Path, *, key: str) -> LogoImage:
        """Load a PNG into a `LogoImage`, flattening to RGB (drops the alpha artifact)."""
        with Image.open(path) as handle:
            rgb = handle.convert("RGB")
            width, height = rgb.size
            access = rgb.load()
            if access is None:  # pragma: no cover - an RGB image always provides pixel access
                raise RuntimeError("Pillow image provided no pixel access")
            pixels = tuple(RGBColor(*access[x, y]) for y in range(height) for x in range(width))
        return cls(key=key, width=width, height=height, pixels=pixels)


class LogoStore:
    """Resolves `LogoAsset` references to `LogoImage`s, cached by key.

    Build it once and hand it to renderers as ambient `RenderContext`. `root` defaults
    to the repo root (where committed `assets/` lives); tests can point it elsewhere.
    """

    def __init__(self, root: str | Path | None = None) -> None:
        self._root = Path(root) if root is not None else _REPO_ROOT
        self._cache: dict[str, LogoImage | None] = {}

    def resolve(self, asset: LogoAsset) -> LogoImage | None:
        """The tile for `asset`, or `None` if its file is absent (renderer falls back)."""
        if asset.key in self._cache:
            return self._cache[asset.key]
        path = self._root / asset.path
        image = LogoImage.from_png(path, key=asset.key) if path.is_file() else None
        self._cache[asset.key] = image
        return image
