"""The per-team win-probability gauge: a thin vertical bar beside each team's logo.

A live game's win probability is shown the way the reference board shows it — a slim
gauge next to each club's tile, filled from the bottom in proportion to that side's
chance. The colour is the team's *freed* colour: whichever of its base/alt palette
colours is NOT the one its logo tile is currently using (the clash resolver decides
that), value-lifted so even a dim navy reads on the black panel. Only the profiles that
fit a logo carry the gauge; ``single_64x32`` (a colour bar, no tile) omits it.
"""

from __future__ import annotations

from dataclasses import dataclass

from omni.core.colors import RGBColor
from omni.core.enum import PanelProfile
from omni.domain.baseball import WinProbability
from omni.domain.logos import LogoVariant
from omni.domain.teams import Team
from omni.renderers.canvas import Canvas
from omni.renderers.context import RenderContext
from omni.renderers.logo_clash import resolve_logo_variants

__all__ = ["draw_win_meter"]


@dataclass(frozen=True, slots=True)
class _MeterGeom:
    """Where a team's gauge sits relative to its row top — hugging the logo's right edge."""

    x: int
    width: int
    inset_y: int  # gauge top = row_top + inset_y (aligned with the logo tile)
    height: int  # full gauge height (the logo height)


# The gauge sits in the 2px gap between the 20px logo and the abbreviation.
_GEOM: dict[PanelProfile, _MeterGeom] = {
    PanelProfile.QUAD_128X64: _MeterGeom(x=22, width=2, inset_y=6, height=20),
    PanelProfile.STACK_64X64: _MeterGeom(x=21, width=2, inset_y=0, height=20),
}


def _meter_colour(team: Team, shown: LogoVariant) -> RGBColor:
    """The value-lifted colour for `team`'s gauge: its *freed* colour (the palette colour
    of the logo variant it is NOT showing), or its primary when no freed colour is known."""
    freed = team.logo if shown is LogoVariant.ALT else team.logo_alt
    background = freed.preferred_background if freed is not None else None
    return (background if background is not None else team.primary_color).value_lifted()


def draw_win_meter(
    canvas: Canvas,
    context: RenderContext,
    away: Team,
    home: Team,
    win_probability: WinProbability,
    *,
    away_top: int,
    home_top: int,
) -> None:
    """Draw both teams' win-probability gauges for `context.profile`.

    A no-op on a profile that doesn't fit a logo (`single_64x32`). Each gauge fills from
    the bottom to its side's win percentage, in that team's freed colour; the variant
    decision matches the logo marks (the same pure resolver), so a team's gauge colour is
    always the colour its tile is *not* using.
    """
    geom = _GEOM.get(context.profile)
    if geom is None:
        return
    variants = resolve_logo_variants(away, home)
    _gauge(canvas, geom, away_top, win_probability.away, _meter_colour(away, variants.away))
    _gauge(canvas, geom, home_top, win_probability.home, _meter_colour(home, variants.home))


def _gauge(canvas: Canvas, geom: _MeterGeom, row_top: int, percent: float, colour: RGBColor) -> None:
    fill = round(percent / 100.0 * geom.height)
    if fill <= 0:
        return  # a 0% side shows no bar
    top = row_top + geom.inset_y + (geom.height - fill)  # fill from the bottom up
    canvas.fill_rect(geom.x, top, geom.width, fill, colour)
