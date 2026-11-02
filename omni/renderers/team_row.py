"""The team mark at the left of a scoreboard row: a logo tile, or a color bar.

Every baseball card draws two stacked team rows whose left edge identifies the club.
When the render context carries a logo store the mark is the 20x20 cap-insignia tile
(its baked-in background already conveys the team color); without one — a small panel
that can't fit a tile, or a unit test that doesn't supply a store — it falls back to
a thin bar, painted from the same palette as the tiles and clash-resolved the same way.

`draw_team_mark` owns that choice for the two profiles a 20px tile fits (quad and
stack) and returns the x where the abbreviation should start, which differs between
the two paths (a tile pushes the label further right than a bar). The single 64x32
profile never fits a tile and keeps the color bar — an explicit, tested compromise.

A team carries two tiles (a base and a distinct alt); which one a side shows depends
on the *other* side, so `draw_matchup_marks` resolves the pair together — flipping one
club to its alt when their backgrounds would otherwise blur — and is what the cards
call. `draw_team_mark` stays the per-side primitive it delegates to.
"""

from __future__ import annotations

from dataclasses import dataclass

from omni.core.colors import RGBColor
from omni.core.enum import PanelProfile
from omni.domain.logos import LogoVariant
from omni.domain.teams import Team
from omni.renderers.canvas import Canvas
from omni.renderers.context import RenderContext
from omni.renderers.logo_clash import resolve_logo_variants
from omni.renderers.visual_treatment import MarkTreatment, resolve_bar_colors

__all__ = ["LOGO_SIZE", "draw_team_mark", "draw_matchup_marks", "draw_single_bars", "draw_treated_mark"]

LOGO_SIZE = 20  # the committed tiles are 20x20

_SINGLE_BAR_WIDTH = 2
_SINGLE_ROW_HEIGHT = 16


@dataclass(frozen=True, slots=True)
class _RowGeom:
    """Per-profile placement of the team mark and the label that follows it."""

    bar_w: int
    bar_h: int
    bar_label_x: int  # abbreviation x when a color bar is drawn
    logo_x: int
    logo_inset_y: int  # logo y = row_top + this (centers the tile in the row)
    logo_label_x: int  # abbreviation x when a logo tile is drawn


# Quad rows are 32px tall (tile centered with a 6px inset); stack rows are 20px (tile flush).
_GEOM: dict[PanelProfile, _RowGeom] = {
    PanelProfile.QUAD_128X64: _RowGeom(bar_w=4, bar_h=32, bar_label_x=8, logo_x=2, logo_inset_y=6, logo_label_x=24),
    PanelProfile.STACK_64X64: _RowGeom(bar_w=3, bar_h=20, bar_label_x=5, logo_x=1, logo_inset_y=0, logo_label_x=23),
}


def draw_team_mark(
    canvas: Canvas,
    context: RenderContext,
    team: Team,
    *,
    row_top: int,
    bar_color: RGBColor,
    variant: LogoVariant = LogoVariant.BASE,
) -> int:
    """Draw the team's left mark for `context.profile`; return the abbreviation's x.

    Blits the logo tile when the store resolves one, else draws the color bar. `variant`
    selects the base or the alt tile; an alt request with no alt tile falls back to the
    base. Only quad and stack are supported here (a 20px tile does not fit the single
    profile).
    """
    geom = _GEOM[context.profile]
    asset = team.logo_alt if variant is LogoVariant.ALT and team.logo_alt is not None else team.logo
    logo = context.logos.resolve(asset) if context.logos is not None else None
    if logo is not None:
        canvas.draw_image(geom.logo_x, row_top + geom.logo_inset_y, logo)
        return geom.logo_label_x
    canvas.fill_rect(0, row_top, geom.bar_w, geom.bar_h, bar_color)
    return geom.bar_label_x


def draw_treated_mark(canvas: Canvas, context: RenderContext, team: Team, side: MarkTreatment) -> None:
    """Draw a live-geometry mark from its resolved treatment: the logo tile, or the color-bar fallback.

    The bounds come from the treatment — the same source the win meter derives from — so the tile
    and its gauge are guaranteed to agree.
    """
    logo = None
    if side.is_tile and context.logos is not None:
        asset = team.logo_alt if side.variant is LogoVariant.ALT and team.logo_alt is not None else team.logo
        logo = context.logos.resolve(asset)
    if logo is not None:
        canvas.draw_image(side.mark.x, side.mark.y, logo)
    else:
        canvas.fill_rect(side.mark.x, side.mark.y, side.mark.width, side.mark.height, side.bar_color)


def draw_matchup_marks(
    canvas: Canvas, context: RenderContext, away: Team, home: Team, *, away_top: int, home_top: int
) -> tuple[int, int]:
    """Draw both teams' marks, flipping one to its alt tile when their tiles would clash.

    Resolves the variant pair once (so the two sides never disagree on who flipped) and
    returns the two abbreviation x-positions. The color-bar fallback resolves its own clash on
    the colors a bar actually paints, since black can't name a club there and lifting can merge two.
    """
    variants = resolve_logo_variants(away, home)
    away_bar, home_bar = resolve_bar_colors(away, home, policy=context.contrast)
    away_x = draw_team_mark(canvas, context, away, row_top=away_top, bar_color=away_bar, variant=variants.away)
    home_x = draw_team_mark(canvas, context, home, row_top=home_top, bar_color=home_bar, variant=variants.home)
    return away_x, home_x


def draw_single_bars(canvas: Canvas, context: RenderContext, away: Team, home: Team) -> None:
    """Draw the single profile's two team bars, one per 16px row (a 20px tile never fits there)."""
    away_bar, home_bar = resolve_bar_colors(away, home, policy=context.contrast)
    canvas.fill_rect(0, 0, _SINGLE_BAR_WIDTH, _SINGLE_ROW_HEIGHT, away_bar)
    canvas.fill_rect(0, _SINGLE_ROW_HEIGHT, _SINGLE_BAR_WIDTH, _SINGLE_ROW_HEIGHT, home_bar)
