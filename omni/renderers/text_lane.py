"""Overflow-safe text lanes: anchored when the text fits, a clock-driven marquee when it doesn't.

A *lane* is a fixed horizontal slot — an x, a width, a baseline-cell top, and a font — that a
string is drawn into. When the string fits, it is anchored (left, center, or right) and held
still. When it is wider than the lane, it scrolls horizontally so the whole value can be read
over time, and only the glyphs that fall wholly inside the lane are drawn — so the scroll
never spills onto whatever sits beside the lane.

The scroll position is a pure function of time, not a counter the renderer bumps once per frame. A
frame-counted scroller advances by however many times the render loop happened to fire, so its speed
drifts with frame timing and it can jump or stutter; deriving the offset from time instead means the
same instant always yields the same frame and the text moves at the same pace no matter how the loop
is paced. Each line's cycle starts when that line went up, holding at its head.

A board keeps a `LaneClock`: when each line went up, on the monotonic clock, by *presentation* and
lane. A presentation is what the lines belong to: one card's time on the board, or one frame of the
play strip. Each frame drawn gets a `LaneFrame` view of the clock to draw through. A line new to its
lane starts with that frame; a lane whose text changes starts over, and a scrolling line that's
replaced leaves the lane blank for a beat first, so the change reads as a new line rather than the old
one jerking back. The starts a frame chooses stand only once the display took it (`LaneFrame.commit`),
so a frame that failed to draw starts nothing; and a new presentation starts every line at its head,
even one whose text and place match a line before it. Without a clock (a still preview, a golden image),
the cycle runs off the render clock itself.

The clock also keeps when its presentation went up, the moment its first frame was taken, so a card that changes
what it shows as it stays up (a live basketball card's two 6 s halves) can ask a frame how long that has been
(`LaneFrame.up_for`), on the same terms as its lines.

Every marquee moves the same way, whatever its length. It holds at the head, speeds up along one
fixed ramp to a cruising speed, cruises, slows along the same ramp mirrored, holds at the tail, then
comes back the same way. Only the cruise stretches with the text; a trip too short to reach cruising
speed rides the same ramps and turns around in the middle.
"""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Hashable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

from omni.core.clock import MonotonicTime
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
    "LaneFrame",
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

_LaneKey = tuple[int, int, int, str]  # a lane within its presentation: where it sits, and its font


@dataclass(frozen=True, slots=True)
class _Line:
    text: str
    since: MonotonicTime  # when its marquee cycle starts
    overflows: bool


class LaneClock:
    """A board's memory of when each line went up, by presentation and then by lane.

    A board keeps one for as long as it runs. For each frame it draws, it asks `frame` for the view its
    renderer draws through, naming the presentation the frame belongs to and the frame's monotonic
    moment. Only one presentation is remembered: a new one starts every line afresh.
    """

    def __init__(self) -> None:
        self._presentation: Hashable | None = None
        self._lines: dict[_LaneKey, _Line] = {}
        self._since: MonotonicTime | None = None  # when the presentation's first frame was taken

    def frame(self, presentation: Hashable, at: MonotonicTime) -> LaneFrame:
        """The view a frame of ``presentation``, drawn at ``at``, reads its lines' starts through."""
        lines = self._lines if presentation == self._presentation else {}
        return LaneFrame(self, presentation, at, lines)

    def _up_since(self, presentation: Hashable, at: MonotonicTime) -> MonotonicTime:
        """When ``presentation`` went up: the moment it was first taken, or ``at`` for one not yet taken."""
        if presentation == self._presentation and self._since is not None:
            return self._since
        return at

    def _commit(self, presentation: Hashable, lines: dict[_LaneKey, _Line], since: MonotonicTime) -> None:
        self._presentation, self._lines, self._since = presentation, lines, since


class LaneFrame:
    """One frame's view of a `LaneClock`: how long each of its lines has been up, as of the frame's moment.

    A line new to its lane, or changed, starts with this frame; the starts stand once `commit` says the
    display took the frame.
    """

    def __init__(self, clock: LaneClock, presentation: Hashable, at: MonotonicTime, lines: dict[_LaneKey, _Line]):
        self._clock = clock
        self._presentation = presentation
        self._at = at
        self._lines = dict(lines)  # this frame's own: the clock keeps its lines until the commit
        self._since = clock._up_since(presentation, at)

    def elapsed(self, lane: TextLane, text: str, *, overflows: bool) -> float | None:
        """Seconds since ``text`` went up in ``lane``, or None during the beat before it shows."""
        key = (lane.x, lane.y, lane.width, lane.font)
        line = self._lines.get(key)
        if line is None or line.text != text:
            gap = _SWAP_GAP if line is not None and line.overflows else timedelta(0)
            line = _Line(text=text, since=self._at + gap, overflows=overflows)
            self._lines[key] = line
        seconds = self._at.since(line.since).total_seconds()
        return seconds if seconds >= 0 else None

    def up_for(self) -> float:
        """Seconds since this frame's presentation went up: 0 for its first frame, counted from when the display
        took that one."""
        return self._at.since(self._since).total_seconds()

    def commit(self) -> None:
        """The display took this frame: the starts it chose stand."""
        self._clock._commit(self._presentation, self._lines, self._since)


@dataclass(frozen=True, slots=True)
class Marquee:
    """A board's scrolling: how its lines move, and the frame's view of when each went up."""

    style: MarqueeStyle = DEFAULT_MARQUEE
    clock: LaneFrame | None = None


def draw_text_lane(
    canvas: Canvas,
    lane: TextLane,
    text: str,
    color: RGBColor,
    *,
    now: datetime,
    clock: LaneFrame | None = None,
) -> None:
    """Draw ``text`` into ``lane``: anchored if it fits, else a clock-driven marquee clipped to the lane.

    With a `LaneFrame` the marquee starts at its head when the text went up, and a lane between a
    scrolling line and its replacement draws nothing; without one, the cycle runs off ``now``.
    """
    width = text_width(text, lane.font)
    overflows = width > lane.width
    clock_s = clock.elapsed(lane, text, overflows=overflows) if clock is not None else now.timestamp()
    if clock_s is None:
        return  # the beat between lines
    if not overflows:
        _draw_anchored(canvas, lane, text, color)
        return
    plan = MarqueePlan(distance=width - lane.width, style=lane.style)
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
