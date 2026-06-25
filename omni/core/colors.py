"""RGB color value object with WCAG luminance/contrast and CIELab difference.

Used to pick legible text colors on team-colored backgrounds across the three
panel profiles without hand-tuning per team, and to measure how *distinct* two
team colors are (`delta_e`) so clashing matchups can be told apart.
"""

from __future__ import annotations

import colorsys
import math
from dataclasses import dataclass

__all__ = ["RGBColor"]

# WCAG 3:1 — the contrast floor for graphical objects and large text, which is what a
# colour meter bar is. Brand colours that fall under it wash out on the black panel.
_LEGIBLE_CONTRAST = 3.0


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

    def value_lifted(self, *, on: RGBColor | None = None, min_contrast: float = _LEGIBLE_CONTRAST) -> RGBColor:
        """Brighten this colour just enough to read against a dark background.

        Many brand colours — navies, deep reds — sit close to black and wash out on the
        black panel/meter. This lightens the colour until it clears `min_contrast` WCAG
        contrast against `on` (default black): first by raising its HSV *value* (hue and
        saturation kept, so it stays recognisably the team's colour), then — only if full
        brightness still falls short (a deep blue) — by shedding saturation toward white.
        Returns ``self`` unchanged when it already clears the floor. Intended for a dark
        `on`: it only ever lightens (best effort — returns white if even white can't reach
        the floor against `on`).
        """
        background = on if on is not None else RGBColor(0, 0, 0)
        if self.contrast_ratio(background) >= min_contrast:
            return self
        hue, sat, value = colorsys.rgb_to_hsv(self.r / 255, self.g / 255, self.b / 255)

        def at(v: float, s: float) -> RGBColor:
            r, g, b = colorsys.hsv_to_rgb(hue, s, v)
            return RGBColor(round(r * 255), round(g * 255), round(b * 255))

        if at(1.0, sat).contrast_ratio(background) >= min_contrast:
            # Minimal brightness raise — hue and saturation preserved (contrast rises with value).
            lo, hi = value, 1.0
            for _ in range(24):
                mid = (lo + hi) / 2
                if at(mid, sat).contrast_ratio(background) >= min_contrast:
                    hi = mid
                else:
                    lo = mid
            lifted = at(hi, sat)
        else:
            # Full brightness is still too dim: hold value at max and keep the most
            # saturation that still reads, shedding the rest toward white.
            lo, hi = 0.0, sat
            for _ in range(24):
                mid = (lo + hi) / 2
                if at(1.0, mid).contrast_ratio(background) >= min_contrast:
                    lo = mid
                else:
                    hi = mid
            lifted = at(1.0, lo)
        # The search tests rounded colours, so `lifted` already clears the floor when one
        # is reachable; an unreachable floor (e.g. above 21:1 on black) settles at white.
        return lifted


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
