"""The per-team win-probability gauge: a thin vertical bar beside each team's mark.

A live game's win probability is shown the way the reference board shows it — a slim gauge
next to each club's tile, filled from the bottom in proportion to that side's chance, in the
team's *freed* colour (value-lifted so even a dim navy reads on the black panel). The gauge's
bounds **and** colour come from the matchup's :class:`MatchupVisualTreatment`, which derives
the meter from the actual mark — so the gauge can never drift from the mark it hugs (the bug a
standalone meter geometry once caused, a full home gauge spilling into the strip below).
"""

from __future__ import annotations

from omni.core.colors import RGBColor
from omni.domain.baseball import WinProbability
from omni.renderers.canvas import Canvas
from omni.renderers.visual_treatment import MatchupVisualTreatment, Rect

__all__ = ["draw_win_meter"]


def draw_win_meter(canvas: Canvas, treatment: MatchupVisualTreatment, win_probability: WinProbability) -> None:
    """Draw both teams' win-probability gauges from the resolved matchup treatment.

    Each gauge fills from the bottom to its side's win percentage, in that side's treatment
    colour, within the meter bounds the treatment derived from the mark — so a full gauge spans
    exactly its mark's height and never spills past it.
    """
    _gauge(canvas, treatment.away.meter, win_probability.away, treatment.away.meter_colour)
    _gauge(canvas, treatment.home.meter, win_probability.home, treatment.home.meter_colour)


def _gauge(canvas: Canvas, meter: Rect, percent: float, colour: RGBColor) -> None:
    fill = round(percent / 100.0 * meter.height)
    if fill <= 0:
        return  # a 0% side shows no bar
    top = meter.y + (meter.height - fill)  # fill from the bottom up
    canvas.fill_rect(meter.x, top, meter.width, fill, colour)
