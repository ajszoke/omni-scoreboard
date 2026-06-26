"""The win-probability meter: faint team-color stripes that brighten toward the winner.

The reference board reads win probability like a tug-of-war. Each team owns a thin stripe beside
its tile, lit at a faint floor so its color is always present; as a club pulls ahead, its stripe
fills to full brightness from the division between the tiles outward, the leading row caught
mid-transition so a stronger lean glows a touch farther. A pick-em leaves both stripes at the
faint floor; a runaway lights the winner's stripe whole.

Geometry and color come from the matchup's :class:`MatchupVisualTreatment`: each side's meter Rect
is the strip just right of its tile (so a stripe can't drift from the logos), painted in that
side's value-lifted color and scaled per row by how much of the tug-of-war that row has won.
"""

from __future__ import annotations

from omni.core.colors import RGBColor
from omni.domain.baseball import WinProbability
from omni.renderers.canvas import Canvas
from omni.renderers.visual_treatment import MarkTreatment, MatchupVisualTreatment, Rect

__all__ = ["draw_win_meter"]

_FLOOR = 0.2  # the minimum brightness every stripe row carries, so a team's color is always faintly lit


def draw_win_meter(canvas: Canvas, treatment: MatchupVisualTreatment, win_probability: WinProbability) -> None:
    """Draw both teams' win-probability stripes, each brightening from the division by its lead."""
    _stripe(canvas, treatment.away, win_probability.away, grows_down=False)  # away tile sits above the division
    _stripe(canvas, treatment.home, win_probability.home, grows_down=True)  # home tile below it


def _stripe(canvas: Canvas, side: MarkTreatment, pct: float, *, grows_down: bool) -> None:
    won = (pct - 50.0) / 50.0 * side.meter.height  # rows fully won, measured from the division; <= 0 when behind
    for offset in range(side.meter.height):
        lit = _clamp01(won - offset)  # 1 where won, the fraction at the leading row, 0 out beyond it
        _row(canvas, side.meter, offset, grows_down, _dim(side.meter_color, _FLOOR + (1.0 - _FLOOR) * lit))


def _row(canvas: Canvas, meter: Rect, offset: int, grows_down: bool, color: RGBColor) -> None:
    # Offset 0 sits on the division between the tiles; each further row steps into this side's tile.
    y = meter.y + offset if grows_down else meter.y + meter.height - 1 - offset
    canvas.fill_rect(meter.x, y, meter.width, 1, color)


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _dim(color: RGBColor, factor: float) -> RGBColor:
    """Scale a color toward black by ``factor`` (0..1) for a partially-lit stripe row."""
    return RGBColor(round(color.r * factor), round(color.g * factor), round(color.b * factor))
