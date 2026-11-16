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
frame and the text moves at the same pace no matter how the loop is paced. Each line's cycle starts
when that line went up, holding at its head. A board keeps a `LaneClock`, which remembers when each
lane's current text appeared; a lane whose text changes starts over, and a scrolling line that's
replaced leaves the lane blank for a beat first, so the change reads as a new line rather than the
old one jerking back. Without a clock (a still preview, a golden image), the cycle runs off the
render clock itself.

Every marquee moves the same way, whatever its length. It holds at the head, speeds up along one
fixed ramp to a cruising speed, cruises, slows along the same ramp mirrored, holds at the tail, then
comes back the same way. Only the cruise stretches with the text; a trip too short to reach cruising
speed rides the same ramps and turns around in the middle.
"""

from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

from omni.core.colors import RGBColor
from omni.core.enum import StrEnumMixin
from omni.renderers.canvas import Canvas
from omni.renderers.font import advance
from omni.renderers.text import draw_centered, draw_right_aligned, text_width

__all__ = [
    "LaneAnchor",
    "MarqueeStyle",
    "ScrollSpeed",
    "MarqueePlan",
    "TextLane",
    "DEFAULT_MARQUEE",
    "LaneClock",
    "Marquee",
    "draw_text_lane",
]


class LaneAnchor(Enum):
    """How a string that *fits* its lane is positioned within it (ignored once it marquees)."""

    LEFT = "left"
    CENTER = "center"
    RIGHT = "right"


class ScrollSpeed(StrEnumMixin, str, Enum):
    """How fast a board's marquees move at their peak: slow is the default, medium 1.25 times it, fast 1.5."""

    SLOW = "slow"
    MEDIUM = "medium"
    FAST = "fast"

    @property
    def factor(self) -> float:
        return _SPEED_FACTORS[self]


_SPEED_FACTORS = {ScrollSpeed.SLOW: 1.0, ScrollSpeed.MEDIUM: 1.25, ScrollSpeed.FAST: 1.5}


@dataclass(frozen=True, slots=True, kw_only=True)
class MarqueeStyle:
    """The feel of every marquee: one hold, one ramp, one cruising speed."""

    cruise_px_per_s: float = 10.0  # mid-slow: a name stays readable the whole way across
    ramp_s: float = 2.0  # from standing to cruising, and back down
    hold_s: float = 2.0  # at each end, so the start and the end can be read

    def __post_init__(self) -> None:
        if self.cruise_px_per_s <= 0:
            raise ValueError("cruise_px_per_s must be positive")
        if self.ramp_s <= 0:
            raise ValueError("ramp_s must be positive")
        if self.hold_s < 0:
            raise ValueError("hold_s cannot be negative")

    @property
    def acceleration(self) -> float:
        """Pixels per second, per second, along a ramp."""
        return self.cruise_px_per_s / self.ramp_s

    @property
    def ramp_px(self) -> float:
        """How far one full ramp carries the text."""
        return self.cruise_px_per_s * self.ramp_s / 2.0

    def at(self, speed: ScrollSpeed) -> MarqueeStyle:
        """This style with a higher top speed: ``factor`` times the cruise, at the same acceleration.

        The ramp keeps speeding up for ``factor`` times as long to get there, so a line too short to
        reach top speed moves the same at every setting, and the holds stay as they are.
        """
        factor = speed.factor
        return dataclasses.replace(self, cruise_px_per_s=self.cruise_px_per_s * factor, ramp_s=self.ramp_s * factor)


DEFAULT_MARQUEE = MarqueeStyle()


@dataclass(frozen=True, slots=True, kw_only=True)
class MarqueePlan:
    """The resolved scroll for one overflowing string: its travel distance and its timing.

    ``offset_at`` maps seconds into the cycle to how far left the text is shifted, in pixels — 0 at
    the head, ``distance`` at the tail. It is cyclic and depends only on its argument, so a given
    instant always produces the same offset (no hidden per-frame state to drift or reset).
    """

    distance: int  # pixels the text must travel for its tail to reach the lane's right edge
    style: MarqueeStyle = DEFAULT_MARQUEE

    def __post_init__(self) -> None:
        if self.distance <= 0:
            raise ValueError("a marquee plan needs a positive travel distance (the text must overflow)")

    @property
    def ramp_time_s(self) -> float:
        """How long each ramp runs: the full ramp, or less on a trip too short to reach cruising speed."""
        return min(self.style.ramp_s, math.sqrt(self.distance / self.style.acceleration))

    @property
    def travel_s(self) -> float:
        """Seconds one trip takes, in either direction."""
        ramps = 2.0 * self.ramp_time_s
        cruise_px = max(self.distance - 2.0 * self.style.ramp_px, 0.0)
        return ramps + cruise_px / self.style.cruise_px_per_s

    @property
    def period_s(self) -> float:
        """One full cycle: hold at the head, out, hold at the tail, back."""
        return 2.0 * (self.style.hold_s + self.travel_s)

    def offset_at(self, clock_s: float) -> int:
        """The leftward pixel shift ``clock_s`` seconds into the cycle, which repeats every period."""
        hold, travel = self.style.hold_s, self.travel_s
        t = clock_s % self.period_s
        if t < hold:
            return 0
        if t < hold + travel:
            return round(self._along(t - hold))
        if t < 2.0 * hold + travel:
            return self.distance
        return round(self.distance - self._along(t - 2.0 * hold - travel))

    def _along(self, t: float) -> float:
        """How far along a trip the text is, ``t`` seconds into it."""
        ramp, travel, accel = self.ramp_time_s, self.travel_s, self.style.acceleration
        if t < ramp:
            return accel * t * t / 2.0
        if t > travel - ramp:
            left = travel - t
            return self.distance - accel * left * left / 2.0
        # Cruising at the ramp's top speed. A trip too short to reach the style's cruise has no cruise to
        # speak of: this is then just its midpoint, so the ramp it reached is the one to measure from.
        return accel * ramp * ramp / 2.0 + accel * ramp * (t - ramp)


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


_SWAP_GAP = timedelta(seconds=0.25)  # the beat of nothing between a scrolling line and its replacement
_FORGET_AFTER = timedelta(seconds=5)  # a lane not drawn this long (longer than any loop stall) starts over


@dataclass(slots=True)
class _Line:
    text: str
    since: datetime  # when its marquee cycle starts
    drawn_at: datetime
    overflows: bool


class LaneClock:
    """When each lane's current text went up, so every line's marquee starts at its own head.

    A board keeps one for as long as it runs and hands it to every render. A lane is known by where it
    sits and its font. When its text changes, the new line starts its cycle over; if the old line was
    one that scrolled, the lane stays blank for a beat first.
    """

    def __init__(self) -> None:
        self._lines: dict[tuple[int, int, int, str], _Line] = {}

    def start(self, lane: TextLane, text: str, *, overflows: bool, now: datetime) -> datetime:
        """When ``text``'s cycle in ``lane`` starts; a moment after ``now`` while the lane is blank."""
        key = (lane.x, lane.y, lane.width, lane.font)
        line = self._lines.get(key)
        if line is not None and line.text == text and now - line.drawn_at <= _FORGET_AFTER:
            line.drawn_at = now
            return line.since
        replacing_a_scroll = line is not None and now - line.drawn_at <= _FORGET_AFTER and line.overflows
        since = now + _SWAP_GAP if replacing_a_scroll else now
        self._lines[key] = _Line(text=text, since=since, drawn_at=now, overflows=overflows)
        return since


@dataclass(frozen=True, slots=True)
class Marquee:
    """A board's scrolling: how its lines move, and its memory of when each went up."""

    style: MarqueeStyle = DEFAULT_MARQUEE
    clock: LaneClock | None = None


def draw_text_lane(
    canvas: Canvas,
    lane: TextLane,
    text: str,
    color: RGBColor,
    *,
    now: datetime,
    clock: LaneClock | None = None,
) -> None:
    """Draw ``text`` into ``lane``: anchored if it fits, else a clock-driven marquee clipped to the lane.

    With a `LaneClock` the marquee starts at its head when the text went up, and a lane between a
    scrolling line and its replacement draws nothing; without one, the cycle runs off ``now``.
    """
    width = text_width(text, lane.font)
    overflows = width > lane.width
    since = clock.start(lane, text, overflows=overflows, now=now) if clock is not None else None
    if since is not None and now < since:
        return  # the beat between lines
    if not overflows:
        _draw_anchored(canvas, lane, text, color)
        return
    plan = MarqueePlan(distance=width - lane.width, style=lane.style)
    clock_s = (now - since).total_seconds() if since is not None else now.timestamp()
    visible, vx = _clip_to_lane(text, lane, plan.offset_at(clock_s))
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
