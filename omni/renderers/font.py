"""BDF font rasterization for the Pillow canvas.

Uses the repo's committed patched BDF fonts via `bdfparser`, so glyph rendering is
deterministic and golden images are stable. Only fixed-width fonts are registered,
so character advance is a constant cell width.
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


FONTS: dict[str, FontSpec] = {
    "4x6": FontSpec("4x6", 4, 6),
    "6x10": FontSpec("6x10", 6, 10),
}


@lru_cache(maxsize=None)
def _load(name: str) -> Any:  # Any: bdfparser ships no type stubs
    spec = FONTS[name]
    return bdfparser.Font(str(_FONT_DIR / f"{spec.name}.bdf"))


def char_size(name: str) -> tuple[int, int]:
    """The fixed (width, height) cell of a registered font, in pixels."""
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
