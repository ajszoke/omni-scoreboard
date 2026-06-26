"""draw_win_meter: fills each side's gauge from the resolved treatment, bottom-up."""

from __future__ import annotations

from omni.core.colors import RGBColor
from omni.domain.baseball import WinProbability
from omni.domain.logos import LogoVariant
from omni.renderers.canvas import RecordingCanvas
from omni.renderers.visual_treatment import MarkTreatment, MatchupVisualTreatment, Rect
from omni.renderers.win_meter import draw_win_meter

AWAY_C, HOME_C = RGBColor(255, 0, 0), RGBColor(0, 0, 255)


def _treatment(away_meter: Rect, home_meter: Rect) -> MatchupVisualTreatment:
    """A treatment carrying just the two meter bounds + colors the gauge drawer needs."""

    def side(meter: Rect, color: RGBColor) -> MarkTreatment:
        return MarkTreatment(
            variant=LogoVariant.BASE,
            is_tile=True,
            mark=Rect(2, meter.y, 20, meter.height),
            meter=meter,
            meter_color=color,
        )

    return MatchupVisualTreatment(away=side(away_meter, AWAY_C), home=side(home_meter, HOME_C))


def test_each_gauge_fills_from_the_bottom_to_its_percentage() -> None:
    canvas = RecordingCanvas(128, 64)
    draw_win_meter(canvas, _treatment(Rect(22, 0, 2, 20), Rect(22, 20, 2, 20)), WinProbability(home=26.0, away=74.0))
    away, home = sorted(canvas.rects(), key=lambda r: r.y)
    assert (away.x, away.y, away.w, away.h) == (22, 5, 2, 15)  # round(74% * 20)=15, bottom-aligned within y=0..20
    assert away.color == AWAY_C
    assert (home.x, home.y, home.w, home.h) == (22, 35, 2, 5)  # round(26% * 20)=5, bottom of the y=20..40 mark
    assert home.color == HOME_C


def test_a_zero_percent_side_draws_no_bar() -> None:
    canvas = RecordingCanvas(128, 64)
    draw_win_meter(canvas, _treatment(Rect(22, 0, 2, 20), Rect(22, 20, 2, 20)), WinProbability(home=100.0, away=0.0))
    rects = canvas.rects()
    assert len(rects) == 1 and (rects[0].y, rects[0].h) == (20, 20)  # only the full home gauge, aligned to its mark
