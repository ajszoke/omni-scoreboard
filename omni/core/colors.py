"""RGB color value object with WCAG luminance/contrast and CIELab difference.

Used to pick legible text colors on team-colored backgrounds across the three
panel profiles without hand-tuning per team, and to measure how *distinct* two
team colors are (`delta_e`) so clashing matchups can be told apart.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

__all__ = ["RGBColor"]


@dataclass(frozen=True, slots=True)
class RGBColor:
    """An 8-bit-per-channel RGB color (each component 0..255)."""

    r: int
    g: int
    b: int

    def __post_init__(self) -> None:
        for component in (self.r, self.g, self.b):
            if not 0 <= component <= 255:
                raise ValueError("RGB components must be 0..255")

    def relative_luminance(self) -> float:
        """WCAG relative luminance, 0.0 (black) .. 1.0 (white)."""

        def convert(channel: int) -> float:
            c = channel / 255
            return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

        return 0.2126 * convert(self.r) + 0.7152 * convert(self.g) + 0.0722 * convert(self.b)

    def contrast_ratio(self, other: RGBColor) -> float:
        """WCAG contrast ratio between two colors, 1.0 .. 21.0 (symmetric)."""
        lighter = max(self.relative_luminance(), other.relative_luminance())
        darker = min(self.relative_luminance(), other.relative_luminance())
        return (lighter + 0.05) / (darker + 0.05)

    def delta_e(self, other: RGBColor) -> float:
        """CIE76 color difference in L*a*b* (symmetric, 0.0 for identical colors).

        Measures *perceived* distance, not raw RGB distance: ~2.3 is the just-
        noticeable difference, and two team backgrounds within a small threshold
        (~20) read as "the same color" on a small panel and need disambiguating.
        """
        l1 = _to_lab(self)
        l2 = _to_lab(other)
        return math.sqrt(sum((a - b) ** 2 for a, b in zip(l1, l2)))


def _to_lab(color: RGBColor) -> tuple[float, float, float]:
    """sRGB → CIE L*a*b* (D65) — the perceptually-uniform space `delta_e` measures in.

    The XYZ luminance row carries the same 0.2126/0.7152/0.0722 human-perception
    weighting as `relative_luminance`, so L* is a perceptual lightness (green reads
    brighter than red brighter than blue at equal code value).
    """

    def linear(channel: int) -> float:
        c = channel / 255
        return ((c + 0.055) / 1.055) ** 2.4 if c > 0.04045 else c / 12.92

    r, g, b = (linear(channel) * 100 for channel in (color.r, color.g, color.b))
    # sRGB → XYZ (D65), then normalize by the reference white.
    x = (r * 0.4124 + g * 0.3576 + b * 0.1805) / 95.047
    y = (r * 0.2126 + g * 0.7152 + b * 0.0722) / 100.0
    z = (r * 0.0193 + g * 0.1192 + b * 0.9504) / 108.883

    def pivot(t: float) -> float:
        return t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116

    fx, fy, fz = pivot(x), pivot(y), pivot(z)
    return (116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz))
