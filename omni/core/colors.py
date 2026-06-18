"""RGB color value object with WCAG luminance/contrast helpers.

Used to pick legible text colors on team-colored backgrounds across the three
panel profiles without hand-tuning per team.
"""

from __future__ import annotations

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
