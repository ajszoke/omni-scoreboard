"""Overflow-safe text lanes: anchored when the text fits, a clock-driven marquee when it doesn't.

A *lane* is a fixed horizontal slot — an x, a width, a baseline-cell top, and a font — that a
string is drawn into. When the string fits, it is anchored (left, center, or right) and held
still. When it is wider than the lane, it scrolls horizontally so the whole value can be read
over time, and only the glyphs that fall wholly inside the lane are drawn — so the scroll
never spills onto whatever sits beside the lane.

The scroll position is a pure function of the render clock (`RenderContext.now`), not a counter
the renderer bumps once per frame. A frame-counted scroller advances by however many times the
render loop happened to fire, so its speed drifts with frame timing and it can jump or stutter;
deriving the offset from wall-clock time instead means the same instant always yields the same
frame and the text glides at a fixed pixels-per-second no matter how the loop is paced. One
cycle dwells at the head (so the start is readable), slides left at a constant rate, dwells at
the tail (so the end is readable), then repeats — all of it determined by the clock, so nothing
has to be reset when a card appears or the loop hitches.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from omni.core.colors import RGBColor
from omni.renderers.canvas import Canvas
from omni.renderers.font import advance
from omni.renderers.text import draw_centered, draw_right_aligned, text_width

__all__ = [
    "LaneAnchor",
    "MarqueeStyle",
    "MarqueePlan",
    "TextLane",
    "DEFAULT_MARQUEE",
    "draw_text_lane",
]


class LaneAnchor(Enum):
    """How a string that *fits* its lane is positioned within it (ignored once it marquees)."""

    LEFT = "left"
    CENTER = "center"
    RIGHT = "right"


@dataclass(frozen=True, slots=True, kw_only=True)
class MarqueeStyle:
    """The tunable feel of a marquee scroll: how fast it slides and how long it pauses at each end.

    Speed is in pixels per second, so the scroll reads at the same rate on any render loop. The
    dwells give the eye time to catch the start and the end before the cycle repeats.
    """

    speed_px_per_s: float = 14.0  # a comfortable small-panel read speed
    start_dwell_s: float = 1.5  # hold at the head before sliding, so the start can be read
    end_dwell_s: float = 1.5  # hold at the tail before repeating, so the end can be read

    def __post_init__(self) -> None:
        if self.speed_px_per_s <= 0:
            raise ValueError("speed_px_per_s must be positive")
        if self.start_dwell_s < 0 or self.end_dwell_s < 0:
            raise ValueError("dwell seconds cannot be negative")


DEFAULT_MARQUEE = MarqueeStyle()


@dataclass(frozen=True, slots=True, kw_only=True)
class MarqueePlan:
    """The resolved scroll for one overflowing string: its travel distance and its timing.

    ``offset_at`` maps an absolute clock reading to how far left the text is shifted, in pixels —
    0 at the head, ``distance`` at the tail. It is cyclic and depends only on its argument, so a
    given instant always produces the same offset (no hidden per-frame state to drift or reset).
    """

    distance: int  # pixels the text must travel for its tail to reach the lane's right edge
    style: MarqueeStyle = DEFAULT_MARQUEE

    def __post_init__(self) -> None:
        if self.distance <= 0:
            raise ValueError("a marquee plan needs a positive travel distance (the text must overflow)")

    @property
    def scroll_s(self) -> float:
        """Seconds the slide itself takes at the style's speed."""
        return self.distance / self.style.speed_px_per_s

    @property
    def period_s(self) -> float:
        """One full cycle: the head dwell, the slide, then the tail dwell."""
        return self.style.start_dwell_s + self.scroll_s + self.style.end_dwell_s

    def offset_at(self, clock_s: float) -> int:
        """The leftward pixel shift at absolute time ``clock_s`` (seconds), cycling every period."""
        t = clock_s % self.period_s
        if t < self.style.start_dwell_s:
            return 0  # dwelling at the head — the start is held still and readable
        t -= self.style.start_dwell_s
        if t >= self.scroll_s:
            return self.distance  # dwelling at the tail — the end is held still and readable
        return min(self.distance, int(t * self.style.speed_px_per_s))  # sliding at a constant rate


@dataclass(frozen=True, slots=True, kw_only=True)
class TextLane:
    """A fixed text slot: a string is drawn into ``[x, x + width)`` with its cell top at ``y``."""

    x: int
    y: int
    width: int
    font: str
    anchor: LaneAnchor = LaneAnchor.LEFT
    style: MarqueeStyle = DEFAULT_MARQUEE

    def __post_init__(self) -> None:
        if self.width <= 0:
            raise ValueError("a text lane needs a positive width")

    @property
    def right(self) -> int:
        """The exclusive right edge of the lane (one past its last pixel)."""
        return self.x + self.width


def draw_text_lane(canvas: Canvas, lane: TextLane, text: str, color: RGBColor, *, now: datetime) -> None:
    """Draw ``text`` into ``lane``: anchored if it fits, else a clock-driven marquee clipped to the lane."""
    width = text_width(text, lane.font)
    if width <= lane.width:
        _draw_anchored(canvas, lane, text, color)
        return
    plan = MarqueePlan(distance=width - lane.width, style=lane.style)
    visible, vx = _clip_to_lane(text, lane, plan.offset_at(now.timestamp()))
    if visible:
        canvas.text(vx, lane.y, visible, color, font=lane.font)


def _draw_anchored(canvas: Canvas, lane: TextLane, text: str, color: RGBColor) -> None:
    """Place a string that fits its lane at the lane's anchor."""
    if lane.anchor is LaneAnchor.LEFT:
        canvas.text(lane.x, lane.y, text, color, font=lane.font)
    elif lane.anchor is LaneAnchor.RIGHT:
        draw_right_aligned(canvas, lane.right, lane.y, text, color, lane.font)
    else:  # LaneAnchor.CENTER
        draw_centered(canvas, lane.x, lane.right, lane.y, text, color, lane.font)


def _clip_to_lane(text: str, lane: TextLane, offset: int) -> tuple[str, int]:
    """The substring whose glyphs fall wholly inside the lane at ``offset``, and its left x.

    Walks glyph advances from the text's shifted left edge and keeps a glyph only if its whole
    cell lies within ``[lane.x, lane.right)``, so nothing draws past either edge. The kept glyphs
    are a contiguous run; clipping a contiguous string from both ends can only yield a substring.
    A lane narrower than its font's glyphs keeps nothing and draws blank rather than spilling.
    """
    x = lane.x - offset  # the shifted left edge of the whole string
    chars: list[str] = []
    vx = lane.x
    for ch in text:
        adv = advance(lane.font, ch)
        if x >= lane.x and x + adv <= lane.right:
            if not chars:
                vx = x  # the first wholly-visible glyph fixes where the run is drawn
            chars.append(ch)
        elif chars:
            break  # the run has passed the lane's right edge; everything after is clipped
        x += adv
    return "".join(chars), vx
