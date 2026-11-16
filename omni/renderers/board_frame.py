"""Renderer for the board's explicit frames: what the panel shows when no card should be up.

Each is the board clock, in the board's zone, dim enough to sit quietly in a room:
- **idle** (nothing to show): the clock alone;
- **held** (games are on, but nothing has cleared the TV delay yet): the clock over an amber
  ``Syncing...`` and a bar filling toward when the first held card clears (on the single, a thin one
  along the bottom edge);
- **offline** (the source has gone quiet past its limit, or never answered): the clock over a red
  ``OFFLINE``.

Every profile shows the whole frame; only the fonts and the bar's size change with the panel.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, tzinfo

from omni.core.colors import RGBColor
from omni.core.enum import PanelProfile
from omni.renderers.canvas import Canvas
from omni.renderers.context import RenderContext
from omni.renderers.text import draw_centered
from omni.tower.board import BoardFrame, FrameKind

__all__ = ["BoardFrameRenderer", "clock_label"]

_BLACK = RGBColor(0, 0, 0)
_CLOCK = RGBColor(110, 110, 110)  # dim: a clock in a quiet room, not a card
_AMBER = RGBColor(255, 176, 0)  # like a paused game's banner
_TRACK = RGBColor(70, 48, 0)  # the bar's unfilled part: amber, dimmed
_LABELS: dict[FrameKind, tuple[str, RGBColor] | None] = {
    FrameKind.IDLE: None,
    FrameKind.HELD: ("Syncing...", _AMBER),
    FrameKind.OFFLINE: ("OFFLINE", RGBColor(200, 40, 40)),
}


@dataclass(frozen=True, slots=True)
class _Bar:
    y: int
    width: int
    height: int


@dataclass(frozen=True, slots=True)
class _Layout:
    clock_font: str
    clock_y: int  # the clock's cell top with no label under it
    labeled_clock_y: int  # the clock's cell top with a label under it
    label_font: str
    label_y: int
    bar: _Bar | None = None  # under a held frame's label
    barred_clock_y: int = 0  # the clock's cell top over a label and a bar
    barred_label_y: int = 0  # the label's cell top over a bar
    barred_label_font: str = ""  # the label's font over a bar (one whose periods are dots, not crosses)


_LAYOUTS: dict[PanelProfile, _Layout] = {
    PanelProfile.QUAD_128X64: _Layout(
        "10x20",
        22,
        14,
        "6x10",
        40,
        bar=_Bar(y=47, width=72, height=4),
        barred_clock_y=6,
        barred_label_y=31,
        barred_label_font="6x12",
    ),
    PanelProfile.STACK_64X64: _Layout(
        "6x12",
        26,
        19,
        "4x6",
        37,
        bar=_Bar(y=42, width=44, height=3),
        barred_clock_y=15,
        barred_label_y=32,
        barred_label_font="4x6",
    ),
    PanelProfile.SINGLE_64X32: _Layout(
        "6x10",
        11,
        6,
        "4x6",
        20,
        bar=_Bar(y=30, width=64, height=2),  # along the bottom edge, under the two lines
        barred_clock_y=6,
        barred_label_y=20,
        barred_label_font="4x6",
    ),
}


def clock_label(now: datetime, zone: tzinfo | None) -> str:
    """The time of day as the board's clock reads it, like ``7:42 PM``, in ``zone`` (None: ``now``'s own)."""
    local = now.astimezone(zone) if zone is not None else now
    return f"{local.hour % 12 or 12}:{local.minute:02d} {'AM' if local.hour < 12 else 'PM'}"


class BoardFrameRenderer:
    """Draws an explicit board frame onto a `Canvas` for any profile."""

    supported_profiles = frozenset(_LAYOUTS)

    def render(self, frame: BoardFrame, context: RenderContext, canvas: Canvas) -> None:
        layout = _LAYOUTS[context.profile]
        canvas.fill(_BLACK)
        label = _LABELS[frame.kind]
        bar = layout.bar if frame.kind is FrameKind.HELD else None
        label_font = layout.label_font
        if bar is not None:
            clock_y, label_y, label_font = layout.barred_clock_y, layout.barred_label_y, layout.barred_label_font
        elif label is not None:
            clock_y, label_y = layout.labeled_clock_y, layout.label_y
        else:
            clock_y, label_y = layout.clock_y, 0
        clock = clock_label(context.now, context.timezone or context.now.tzinfo)
        draw_centered(canvas, 0, canvas.width, clock_y, clock, _CLOCK, layout.clock_font)
        if label is not None:
            text, color = label
            draw_centered(canvas, 0, canvas.width, label_y, text, color, label_font)
        if bar is not None:
            _draw_bar(canvas, bar, frame.progress(context.now))


def _draw_bar(canvas: Canvas, bar: _Bar, progress: float | None) -> None:
    """A bar centered across the panel, filled left to right to ``progress`` (an empty track if unknown)."""
    x = (canvas.width - bar.width) // 2
    canvas.fill_rect(x, bar.y, bar.width, bar.height, _TRACK)
    filled = math.floor(bar.width * progress) if progress is not None else 0
    if filled:
        canvas.fill_rect(x, bar.y, filled, bar.height, _AMBER)
