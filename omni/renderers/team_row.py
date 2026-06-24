"""The team mark at the left of a scoreboard row: a logo tile, or a colour bar.

Every baseball card draws two stacked team rows whose left edge identifies the club.
When the render context carries a logo store the mark is the 20x20 cap-insignia tile
(its baked-in background already conveys the team colour); without one — a small panel
that can't fit a tile, or a unit test that doesn't supply a store — it falls back to
the thin primary-colour bar the cards drew before logos existed.

`draw_team_mark` owns that choice for the two profiles a 20px tile fits (quad and
stack) and returns the x where the abbreviation should start, which differs between
the two paths (a tile pushes the label further right than a bar). The single 64x32
profile never fits a tile and keeps the colour bar — an explicit, tested compromise.
"""

from __future__ import annotations

from dataclasses import dataclass

from omni.core.enum import PanelProfile
from omni.domain.teams import Team
from omni.renderers.canvas import Canvas
from omni.renderers.context import RenderContext

__all__ = ["LOGO_SIZE", "draw_team_mark"]

LOGO_SIZE = 20  # the committed tiles are 20x20


@dataclass(frozen=True, slots=True)
class _RowGeom:
    """Per-profile placement of the team mark and the label that follows it."""

    bar_w: int
    bar_h: int
    bar_label_x: int  # abbreviation x when a colour bar is drawn
    logo_x: int
    logo_inset_y: int  # logo y = row_top + this (centres the tile in the row)
    logo_label_x: int  # abbreviation x when a logo tile is drawn


# Quad rows are 32px tall (tile centred with a 6px inset); stack rows are 20px (tile flush).
_GEOM: dict[PanelProfile, _RowGeom] = {
    PanelProfile.QUAD_128X64: _RowGeom(bar_w=4, bar_h=32, bar_label_x=8, logo_x=2, logo_inset_y=6, logo_label_x=24),
    PanelProfile.STACK_64X64: _RowGeom(bar_w=3, bar_h=20, bar_label_x=5, logo_x=1, logo_inset_y=0, logo_label_x=23),
}


def draw_team_mark(canvas: Canvas, context: RenderContext, team: Team, *, row_top: int) -> int:
    """Draw the team's left mark for `context.profile`; return the abbreviation's x.

    Blits the logo tile when the store resolves one, else draws the colour bar. Only
    quad and stack are supported here (a 20px tile does not fit the single profile).
    """
    geom = _GEOM[context.profile]
    logo = context.logos.resolve(team.logo) if context.logos is not None else None
    if logo is not None:
        canvas.draw_image(geom.logo_x, row_top + geom.logo_inset_y, logo)
        return geom.logo_label_x
    canvas.fill_rect(0, row_top, geom.bar_w, geom.bar_h, team.primary_color)
    return geom.bar_label_x
