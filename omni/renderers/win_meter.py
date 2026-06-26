"""The per-team win-probability gauge: a thin vertical bar beside each team's logo.

A live game's win probability is shown the way the reference board shows it — a slim
gauge next to each club's tile, filled from the bottom in proportion to that side's
chance. The colour is the team's *freed* colour: whichever of its base/alt palette
colours is NOT the one its logo tile is currently using (the clash resolver decides
that), value-lifted so even a dim navy reads on the black panel. The gauge sizes itself from
the team mark, so every profile carries one — even ``single_64x32``, where it narrows
to fit beside the colour bar.
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


_IDEAL_WIDTH = 2  # the target gauge width; it narrows only when a layout is tight
_MIN_WIDTH = 1


@dataclass(frozen=True, slots=True)
class _MeterGeom:
    """Where a team's gauge sits relative to its row top — hugging the mark's right edge."""

    x: int
    width: int
    inset_y: int  # gauge top = row_top + inset_y (aligned with the mark)
    height: int  # full gauge height (the mark height)


@dataclass(frozen=True, slots=True)
class _MarkExtent:
    """The team-mark facts the gauge sizes itself from: the mark's right edge (where the gauge
    sits), the x where the label begins (the gauge fills the gap between), and the mark's
    height + inset below the row top."""

    right: int
    label_x: int
    height: int
    inset_y: int


# Each profile's actual mark — quad/stack hug the 20px tile, the single its 2px colour bar — so
# the gauge fills whatever pixels that layout leaves free before the label, down to 1px.
_MARKS: dict[PanelProfile, _MarkExtent] = {
    PanelProfile.QUAD_128X64: _MarkExtent(right=22, label_x=24, height=20, inset_y=6),
    PanelProfile.STACK_64X64: _MarkExtent(right=21, label_x=23, height=20, inset_y=0),
    PanelProfile.SINGLE_64X32: _MarkExtent(right=2, label_x=4, height=16, inset_y=0),
}


def _meter_width(gap: int) -> int:
    """The gauge width fitting a `gap`-pixel space before the label: the ideal 2px, but as
    little as 1px when the gap is tight — never wider, which would crowd the score."""
    return max(_MIN_WIDTH, min(_IDEAL_WIDTH, gap))


def _meter_geom(profile: PanelProfile) -> _MeterGeom:
    """Derive the gauge geometry from `profile`'s team mark — sized per layout, not hardcoded."""
    mark = _MARKS[profile]
    width = _meter_width(mark.label_x - mark.right)
    return _MeterGeom(x=mark.right, width=width, inset_y=mark.inset_y, height=mark.height)


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

    Each gauge fills from the bottom to its side's win percentage, in that team's freed
    colour; the variant decision matches the logo marks (the same pure resolver), so a
    team's gauge colour is always the colour its tile is *not* using. The gauge width is
    derived from the profile's mark, narrowing to 1px where a layout is tight.
    """
    geom = _meter_geom(context.profile)
    variants = resolve_logo_variants(away, home)
    _gauge(canvas, geom, away_top, win_probability.away, _meter_colour(away, variants.away))
    _gauge(canvas, geom, home_top, win_probability.home, _meter_colour(home, variants.home))


def _gauge(canvas: Canvas, geom: _MeterGeom, row_top: int, percent: float, colour: RGBColor) -> None:
    fill = round(percent / 100.0 * geom.height)
    if fill <= 0:
        return  # a 0% side shows no bar
    top = row_top + geom.inset_y + (geom.height - fill)  # fill from the bottom up
    canvas.fill_rect(geom.x, top, geom.width, fill, colour)
