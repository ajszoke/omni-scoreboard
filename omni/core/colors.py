"""RGB color value object with WCAG luminance/contrast and CIELab difference.

Used to pick legible text colors on team-colored backgrounds across the three
panel profiles without hand-tuning per team, and to measure how *distinct* two
team colors are (`delta_e`, `delta_e_2000`) so clashing matchups can be told apart.
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

        Measures *perceived* distance, not raw RGB distance: ~2.3 is the just-noticeable
        difference, and two team backgrounds within a threshold read as "the same color" on a
        small panel. It overstates how far apart two saturated colors of nearly the same hue
        look, so the clash check asks `delta_e_2000` too (`renderers.logo_clash.clash_metric`).
        """
        l1 = _to_lab(self)
        l2 = _to_lab(other)
        return math.sqrt(sum((a - b) ** 2 for a, b in zip(l1, l2)))

    def delta_e_2000(self, other: RGBColor) -> float:
        """CIEDE2000 color difference (symmetric, 0.0 for identical colors).

        CIE76 is a straight line through L*a*b*, which overstates how far apart two saturated
        colors of nearly the same hue look: a deep red and a bright red score as distinct there,
        yet read as one red on a lit panel. CIEDE2000 weights lightness, chroma and hue the way the
        eye does, so it catches the pairs CIE76 calls apart that still read as one color.
        """
        return _ciede2000(_to_lab(self), _to_lab(other))

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
    """sRGB → CIE L*a*b* (D65) — the space `delta_e` and `delta_e_2000` measure in.

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


def _ciede2000(lab1: tuple[float, float, float], lab2: tuple[float, float, float]) -> float:
    """The CIEDE2000 difference between two L*a*b* colors (Sharma, Wu and Dalal 2005), with unit
    weights kL = kC = kH = 1.

    Neutral colors pull a* out a little (G) so greys sit apart from faintly tinted ones; the
    lightness, chroma and hue differences are then each scaled by how sensitive the eye is to that
    difference at the pair's mean lightness, chroma and hue (S_L, S_C, S_H), and the rotation term
    R_T corrects the blue region, where hue and chroma differences interact.
    """
    l1, a1, b1 = lab1
    l2, a2, b2 = lab2
    chroma_mean = (math.hypot(a1, b1) + math.hypot(a2, b2)) / 2
    g = 0.5 * (1 - _chroma_weight(chroma_mean))
    a1p, a2p = (1 + g) * a1, (1 + g) * a2
    c1p, c2p = math.hypot(a1p, b1), math.hypot(a2p, b2)
    h1p = math.degrees(math.atan2(b1, a1p)) % 360
    h2p = math.degrees(math.atan2(b2, a2p)) % 360

    # Hue difference and mean hue go the short way round the circle; an achromatic color has no
    # hue, so a pair with one contributes no hue difference and its mean hue is the other's.
    if c1p * c2p == 0:
        dhp = 0.0
        hp_mean = h1p + h2p
    else:
        dhp = h2p - h1p
        hp_mean = (h1p + h2p) / 2
        if abs(dhp) > 180:
            dhp -= math.copysign(360, dhp)
            hp_mean += 180 if hp_mean < 180 else -180

    dlp = l2 - l1
    dcp = c2p - c1p
    dhp_big = 2 * math.sqrt(c1p * c2p) * math.sin(math.radians(dhp / 2))

    lp_mean = (l1 + l2) / 2
    cp_mean = (c1p + c2p) / 2
    t = (
        1
        - 0.17 * math.cos(math.radians(hp_mean - 30))
        + 0.24 * math.cos(math.radians(2 * hp_mean))
        + 0.32 * math.cos(math.radians(3 * hp_mean + 6))
        - 0.20 * math.cos(math.radians(4 * hp_mean - 63))
    )
    s_l = 1 + 0.015 * (lp_mean - 50) ** 2 / math.sqrt(20 + (lp_mean - 50) ** 2)
    s_c = 1 + 0.045 * cp_mean
    s_h = 1 + 0.015 * cp_mean * t
    rotation = 30 * math.exp(-(((hp_mean - 275) / 25) ** 2))
    r_t = -2 * _chroma_weight(cp_mean) * math.sin(math.radians(2 * rotation))

    lightness, chroma, hue = dlp / s_l, dcp / s_c, dhp_big / s_h
    return math.sqrt(lightness**2 + chroma**2 + hue**2 + r_t * chroma * hue)


def _chroma_weight(chroma: float) -> float:
    """sqrt(C^7 / (C^7 + 25^7)): near 0 for neutral colors, near 1 for saturated ones."""
    c7 = chroma**7
    return math.sqrt(c7 / (c7 + 25**7))
