"""draw_win_meter: faint team-color stripes that brighten from the division toward the winner."""

from __future__ import annotations

from omni.core.colors import RGBColor
from omni.domain.baseball import WinProbability
from omni.domain.logos import LogoVariant
from omni.renderers.canvas import RecordingCanvas
from omni.renderers.visual_treatment import MarkTreatment, MatchupVisualTreatment, Rect
from omni.renderers.win_meter import draw_win_meter

AWAY_C, HOME_C = RGBColor(200, 0, 0), RGBColor(0, 0, 200)
FLOOR = 0.2


def _treatment() -> MatchupVisualTreatment:
    # Quad-like: away tile on top (meter y=0..20), home below (y=20..40); both meters 2px wide at x=22.
    def side(meter: Rect, color: RGBColor) -> MarkTreatment:
        return MarkTreatment(
            variant=LogoVariant.BASE,
            is_tile=True,
            mark=Rect(2, meter.y, 20, meter.height),
            meter=meter,
            meter_color=color,
        )

    return MatchupVisualTreatment(away=side(Rect(22, 0, 2, 20), AWAY_C), home=side(Rect(22, 20, 2, 20), HOME_C))


def _scaled(c: RGBColor, f: float) -> RGBColor:
    return RGBColor(round(c.r * f), round(c.g * f), round(c.b * f))


def test_favored_side_brightens_from_the_division_over_a_faint_floor() -> None:
    canvas = RecordingCanvas(128, 64)
    draw_win_meter(canvas, _treatment(), WinProbability(home=77.0, away=23.0))
    home = sorted([r for r in canvas.rects() if r.y >= 20], key=lambda r: r.y)
    assert len(home) == 20 and all((r.x, r.w, r.h) == (22, 2, 1) for r in home)  # the whole tile is a stripe
    # (77-50)/50 * 20 = 10.8 -> rows 0..9 fully won, row 10 the leading transition, 11..19 the faint floor.
    assert all(r.color == HOME_C for r in home[:10])  # won rows at full brightness
    assert home[10].color == _scaled(HOME_C, FLOOR + (1 - FLOOR) * 0.8)  # leading row: floor + 0.8 of the way up
    assert all(r.color == _scaled(HOME_C, FLOOR) for r in home[11:])  # the faint floor beyond the lead


def test_trailing_side_stays_a_faint_floor_stripe() -> None:
    canvas = RecordingCanvas(128, 64)
    draw_win_meter(canvas, _treatment(), WinProbability(home=77.0, away=23.0))
    away = [r for r in canvas.rects() if r.y < 20]
    assert len(away) == 20  # the away tile still shows its stripe...
    assert {r.color for r in away} == {_scaled(AWAY_C, FLOOR)}  # ...uniformly at the faint floor (it is behind)


def test_a_runaway_lights_the_winning_stripe_whole() -> None:
    canvas = RecordingCanvas(128, 64)
    draw_win_meter(canvas, _treatment(), WinProbability(home=100.0, away=0.0))
    home = [r for r in canvas.rects() if r.y >= 20]
    assert {r.color for r in home} == {HOME_C}  # 100% -> every row of the tile is full bright


def test_a_dead_heat_is_two_faint_stripes() -> None:
    canvas = RecordingCanvas(128, 64)
    draw_win_meter(canvas, _treatment(), WinProbability(home=50.0, away=50.0))
    rects = canvas.rects()
    assert len(rects) == 40  # both stripes present, none brighter than the floor
    assert {r.color for r in rects} == {_scaled(HOME_C, FLOOR), _scaled(AWAY_C, FLOOR)}
