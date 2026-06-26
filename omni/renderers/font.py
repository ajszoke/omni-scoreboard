"""BDF font rasterization for the Pillow canvas.

Uses the repo's committed patched BDF fonts via `bdfparser`, so glyph rendering is
deterministic and golden images are stable. The whole committed set is registered (so
nothing we ship is forgotten); each entry's width/height is its BDF bounding box. Almost
all are fixed-width — `advance` reads each glyph's real DWIDTH, so even the one proportional
face (helvR12) measures correctly, while the fixed faces advance by their constant cell.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import bdfparser

__all__ = ["FontSpec", "FONTS", "char_size", "advance", "rasterize"]

_FONT_DIR = Path(__file__).resolve().parents[2] / "assets" / "fonts" / "patched"


@dataclass(frozen=True, slots=True)
class FontSpec:
    name: str
    width: int
    height: int


# Every committed BDF, registered by its filename stem so it can be named from a renderer.
# Standard X11 faces are named WxH (with -B bold / -O oblique siblings at the same cell); the
# rest carry their own names. helvR12 is proportional (the dims are its bounding box).
FONTS: dict[str, FontSpec] = {
    "tom-thumb": FontSpec("tom-thumb", 3, 6),
    "4x6": FontSpec("4x6", 4, 6),
    "4x6-legacy": FontSpec("4x6-legacy", 4, 6),
    "5x7": FontSpec("5x7", 5, 7),
    "5x8": FontSpec("5x8", 5, 8),
    "6x9": FontSpec("6x9", 6, 9),
    "6x10": FontSpec("6x10", 6, 10),
    "6x12": FontSpec("6x12", 6, 12),
    "clR6x12": FontSpec("clR6x12", 6, 12),
    "6x13": FontSpec("6x13", 6, 13),
    "6x13B": FontSpec("6x13B", 6, 13),
    "6x13O": FontSpec("6x13O", 6, 13),
    "7x13": FontSpec("7x13", 7, 13),
    "7x13B": FontSpec("7x13B", 7, 13),
    "7x13O": FontSpec("7x13O", 7, 13),
    "7x14": FontSpec("7x14", 7, 14),
    "7x14B": FontSpec("7x14B", 7, 14),
    "8x13": FontSpec("8x13", 8, 13),
    "8x13B": FontSpec("8x13B", 8, 13),
    "8x13O": FontSpec("8x13O", 8, 13),
    "helvR12": FontSpec("helvR12", 14, 15),
    "9x15": FontSpec("9x15", 9, 15),
    "9x15B": FontSpec("9x15B", 9, 15),
    "9x18": FontSpec("9x18", 9, 18),
    "9x18B": FontSpec("9x18B", 9, 18),
    "10x20": FontSpec("10x20", 10, 20),
}


@lru_cache(maxsize=None)
def _load(name: str) -> Any:  # Any: bdfparser ships no type stubs
    spec = FONTS[name]
    return bdfparser.Font(str(_FONT_DIR / f"{spec.name}.bdf"))


def char_size(name: str) -> tuple[int, int]:
    """The (width, height) bounding-box cell of a registered font, in pixels."""
    spec = FONTS[name]
    return spec.width, spec.height


@lru_cache(maxsize=None)
def _advances(name: str) -> dict[int, int]:
    """Map of codepoint -> horizontal advance (DWIDTH) for every glyph in `name`."""
    font = _load(name)
    return {cp: font.glyphbycp(cp).meta["dwx0"] for cp in font.glyphs}


def advance(name: str, char: str) -> int:
    """The horizontal advance of `char` in `name`, in pixels.

    Almost every glyph in these fixed-width fonts advances by the full cell, but a
    few are deliberately narrower — the thin space U+2009 advances 2px in 4x6 — so
    anchored layout must sum real advances rather than assume a constant cell. A
    char with no glyph falls back to the cell width.
    """
    width, _ = char_size(name)
    return _advances(name).get(ord(char), width)


def rasterize(name: str, text: str) -> list[list[int]]:
    """Render `text` to a row-major grid of 0/1 pixels using font `name`."""
    grid: list[list[int]] = _load(name).draw(text).todata(2)
    return grid
