"""Text lines about a player, shared by the cards that name one.

A long name never runs off the board: each line draws through a `TextLane`, which holds a string
that fits and marquees one that doesn't. What leads the name — a lineup spot, a W/L/S — is drawn apart
from it and stays put while the name scrolls, so it never leaves the board along with the name's start.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from omni.core.colors import RGBColor
from omni.renderers.canvas import Canvas
from omni.renderers.text import text_width
from omni.renderers.text_lane import TextLane, draw_text_lane

__all__ = ["PANEL_X", "PanelEntry", "draw_name_with_stats", "draw_panel_entry"]

# The quad's right-hand panel: a column of name-over-stats entries beside the line score.
PANEL_X = 72  # clear of the line score's widest columns
_PANEL_WIDTH = 128 - PANEL_X
_PANEL_NAME_FONT = "5x8"
_PANEL_STATS_FONT = "4x6"
_PANEL_STATS_DROP = 8  # a stat line's cell top below its name's

_STATS_GAP = 3  # the gap between a name and the stat line that trails it
_STATS_DROP = 3  # lines a 4x6 stat line up on the baseline of the 6x10 name beside it


@dataclass(frozen=True, slots=True, kw_only=True)
class PanelEntry:
    """One player in the right-hand panel: a name line, and a stat line beneath it if there is one."""

    name: str
    name_color: RGBColor
    leader: str | None = None
    stats: str | None = None
    stats_color: RGBColor | None = None


def _draw_leader(canvas: Canvas, leader: str | None, *, x: int, y: int, color: RGBColor, font: str) -> int:
    """Draw what leads a name, if anything; return the x where the name starts."""
    if leader is None:
        return x
    canvas.text(x, y, leader, color, font=font)
    return x + text_width(f"{leader} ", font)


def draw_panel_entry(canvas: Canvas, entry: PanelEntry, *, y: int, now: datetime) -> None:
    """Draw one panel entry with its name's cell top at ``y``."""
    name_x = _draw_leader(canvas, entry.leader, x=PANEL_X, y=y, color=entry.name_color, font=_PANEL_NAME_FONT)
    name_lane = TextLane(x=name_x, y=y, width=PANEL_X + _PANEL_WIDTH - name_x, font=_PANEL_NAME_FONT)
    draw_text_lane(canvas, name_lane, entry.name, entry.name_color, now=now)
    if entry.stats is not None and entry.stats_color is not None:
        stats_lane = TextLane(x=PANEL_X, y=y + _PANEL_STATS_DROP, width=_PANEL_WIDTH, font=_PANEL_STATS_FONT)
        draw_text_lane(canvas, stats_lane, entry.stats, entry.stats_color, now=now)


def draw_name_with_stats(
    canvas: Canvas,
    name: str,
    stats: str,
    *,
    x: int,
    y: int,
    right: int,
    name_color: RGBColor,
    stats_color: RGBColor,
    now: datetime,
    leader: str | None = None,
    name_font: str = "6x10",
    stats_font: str = "4x6",
) -> None:
    """A single line: ``name``, then ``stats`` a size smaller beside it.

    When the name leaves room, it is drawn where it sits — always readable — and the stats take a
    lane from just past it to ``right``, marqueeing there when they outgrow the gap. A name long
    enough to crowd out its own stats instead claims the whole line as its lane and marquees, so it
    can never overrun ``right``. Either way nothing draws beyond the line's bounds.
    """
    name_x = _draw_leader(canvas, leader, x=x, y=y, color=name_color, font=name_font)
    stats_x = name_x + text_width(name, name_font) + _STATS_GAP
    if stats_x < right:  # the name leaves room for a stat line beside it
        canvas.text(name_x, y, name, name_color, font=name_font)
        lane = TextLane(x=stats_x, y=y + _STATS_DROP, width=right - stats_x, font=stats_font)
        draw_text_lane(canvas, lane, stats, stats_color, now=now)
    else:  # an overflowing name takes the whole line and marquees; no room left for stats
        lane = TextLane(x=name_x, y=y, width=right - name_x, font=name_font)
        draw_text_lane(canvas, lane, name, name_color, now=now)
