"""Renderer for the board's explicit frames: what the panel shows when no card should be up.

Each is the board clock, in the board's zone, dim enough to sit quietly in a room:
- **idle** (nothing to show): the clock alone;
- **held** (games are on, but nothing has cleared the TV delay yet): the clock over an amber ``TV DELAY``;
- **offline** (the source has gone quiet past its limit, or never answered): the clock over a red
  ``OFFLINE``.

Every profile shows the whole frame; only the fonts change with the panel.
"""

from __future__ import annotations

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
_LABELS: dict[FrameKind, tuple[str, RGBColor] | None] = {
    FrameKind.IDLE: None,
    FrameKind.HELD: ("TV DELAY", RGBColor(255, 176, 0)),  # amber, like a paused game's banner
    FrameKind.OFFLINE: ("OFFLINE", RGBColor(200, 40, 40)),
}


@dataclass(frozen=True, slots=True)
class _Layout:
    clock_font: str
    clock_y: int  # the clock's cell top with no label under it
    labeled_clock_y: int  # the clock's cell top with a label under it
    label_font: str
    label_y: int


_LAYOUTS: dict[PanelProfile, _Layout] = {
    PanelProfile.QUAD_128X64: _Layout("10x20", 22, 14, "6x10", 40),
    PanelProfile.STACK_64X64: _Layout("6x12", 26, 19, "4x6", 37),
    PanelProfile.SINGLE_64X32: _Layout("6x10", 11, 6, "4x6", 20),
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
        clock = clock_label(context.now, context.timezone or context.now.tzinfo)
        clock_y = layout.labeled_clock_y if label is not None else layout.clock_y
        draw_centered(canvas, 0, canvas.width, clock_y, clock, _CLOCK, layout.clock_font)
        if label is not None:
            text, color = label
            draw_centered(canvas, 0, canvas.width, layout.label_y, text, color, layout.label_font)
