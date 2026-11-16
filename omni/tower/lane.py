"""Lane: one surface's airtime. Segments air one at a time, in the order they were offered.

Each frame of the segment on air gets its dwell's minimum, then keeps going to its maximum while
nothing is pending; after that comes its next frame, or the next segment. Something is *pending* when:
- another segment is waiting and available;
- what's on air no longer describes what's around it (`invalidate`);
- the surface is closing out (`close_out`), when every frame gets just its minimum and no hold applies.
A held segment stays past its maximum until something is pending.

The display says what it actually showed. `cue` names the frame that should be on air, and `committed`
is the display's word that it drew that frame. A frame's dwell counts from its first commit, so a
frame that never drew spends none of its time.

Availability, latest start and expiry are UTC instants. Dwell runs on the monotonic clock.

Edits follow the presentation contract. A newer revision of a waiting segment takes its place in line.
A newer revision of the segment on air swaps its frames in place, keeping its dwell (a cosmetic edit).
An edit to one that has already aired doesn't bring it back: a material correction comes as a segment
of its own.

`changes` reports, for the trace, what happened since it was last asked: a segment up (its first
frame committed), a later frame up, a segment down and why, a segment dropped unseen and why.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import timedelta
from enum import Enum
from typing import Generic

from omni.core.clock import ClockReading, MonotonicTime
from omni.core.enum import StrEnumMixin
from omni.tower.segment import Cue, PayloadT, Segment, SegmentKey, Surface

__all__ = ["ChangeKind", "EndReason", "Lane", "LaneChange", "earliest"]


class EndReason(StrEnumMixin, str, Enum):
    """Why a segment came down, or never went up."""

    RAN_ITS_TIME = "ran its time"  # its last frame had its maximum
    NEXT_WAITING = "next waiting"  # it had its minimum, with another segment waiting
    INVALIDATED = "no longer current"  # it stopped describing what's around it, and nothing replaced it
    CLOSING = "closing out"  # the surface is closing out: each frame gets its minimum
    CLEARED = "cleared"  # the surface was cleared
    TOO_LATE = "too late to start"  # its latest start passed while it waited
    EXPIRED = "expired"  # its hard expiry passed
    UNDRAWN = "failed to draw"  # the display couldn't draw it


class ChangeKind(StrEnumMixin, str, Enum):
    UP = "up"  # its first frame was committed
    FRAME = "frame"  # a later frame was committed
    DOWN = "down"  # it came down, having aired
    DROPPED = "dropped"  # it came down, or was passed over, without airing


@dataclass(frozen=True, slots=True)
class LaneChange(Generic[PayloadT]):
    """One thing that happened on a lane, for the trace."""

    kind: ChangeKind
    segment: Segment[PayloadT]
    at: ClockReading
    frame: int = 0
    reason: EndReason | None = None
    aired: timedelta | None = None  # for a segment coming down: how long its last frame was up


class Lane(Generic[PayloadT]):
    """One surface's schedule: what's on air, what waits, and when each frame goes."""

    def __init__(self, surface: Surface) -> None:
        self._surface = surface
        self._known: dict[SegmentKey, int] = {}  # every key offered, with its newest revision
        self._waiting: deque[Segment[PayloadT]] = deque()
        self._current: Segment[PayloadT] | None = None
        self._frame = 0
        self._since: MonotonicTime | None = None  # when the frame on air was first committed
        self._aired = False  # whether any frame of the segment on air has been committed
        self._stale = False
        self._brief = False
        self._changes: list[LaneChange[PayloadT]] = []

    @property
    def idle(self) -> bool:
        """Nothing on air and nothing waiting."""
        return self._current is None and not self._waiting

    def offer(self, segments: Iterable[Segment[PayloadT]]) -> None:
        """Queue segments behind any waiting. One already offered changes only by a newer revision."""
        for segment in segments:
            if segment.key.surface is not self._surface:
                raise ValueError(f"a {segment.key.surface.value} segment offered to the {self._surface.value} lane")
            known = self._known.get(segment.key)
            if known is not None and segment.revision <= known:
                continue
            self._known[segment.key] = segment.revision
            if known is None:
                self._waiting.append(segment)
            elif self._current is not None and self._current.key == segment.key:
                self._current = segment  # edited on air: the new frames, the same dwell
                self._frame = min(self._frame, len(segment.frames) - 1)
            else:
                for index, waiting in enumerate(self._waiting):
                    if waiting.key == segment.key:
                        self._waiting[index] = segment  # a newer revision waits in its place
                        break

    def invalidate(self) -> None:
        """What's on air no longer describes what's around it: it goes once its frame has had its minimum."""
        self._stale = True

    def close_out(self) -> None:
        """What's on air and what's waiting get one frame each, at its minimum, with no holds."""
        self._stale = self._brief = True

    def resume(self) -> None:
        """Frames get their full time again."""
        self._brief = False

    def clear(self, at: ClockReading) -> list[Segment[PayloadT]]:
        """Empty the lane; returns what goes unseen (everything waiting, and anything on air not yet drawn)."""
        dropped = list(self._waiting)
        self._waiting.clear()
        if self._current is not None:
            if self._aired:
                self._down(EndReason.CLEARED, at)
            else:
                dropped.insert(0, self._current)
                self._current = None
        return dropped

    def cue(self, at: ClockReading) -> Cue[PayloadT] | None:
        """The frame that should be on air at ``at``, once the rules have run: the one up, its next, the next
        segment's first, or nothing."""
        current = self._current
        if current is not None:
            if current.expires_at is not None and at.utc >= current.expires_at:
                self._down(EndReason.EXPIRED, at)
            elif self._since is not None:
                on_air = at.monotonic.since(self._since)
                pending = self._pending(at)
                if pending is not None and on_air >= current.dwell.minimum:
                    self._down(pending, at)
                elif on_air >= current.dwell.maximum and (current.hold is None or self._brief):
                    if self._frame + 1 < len(current.frames):
                        self._frame, self._since = self._frame + 1, None
                    else:
                        self._down(EndReason.RAN_ITS_TIME, at)
        while self._current is None and self._waiting and self._waiting[0].available_at <= at.utc:
            segment = self._waiting.popleft()
            late = segment.latest_start is not None and at.utc > segment.latest_start
            if late or (segment.expires_at is not None and at.utc >= segment.expires_at):
                reason = EndReason.TOO_LATE if late else EndReason.EXPIRED
                self._changes.append(LaneChange(ChangeKind.DROPPED, segment, at, reason=reason))
                continue
            self._current, self._frame, self._since, self._aired, self._stale = segment, 0, None, False, False
        return Cue(self._current, self._frame) if self._current is not None else None

    def committed(self, cue: Cue[PayloadT], at: ClockReading) -> None:
        """The display drew ``cue`` at ``at``. The first commit of a frame starts its dwell."""
        current = self._current
        if current is None or cue.segment.key != current.key or cue.frame != self._frame or self._since is not None:
            return  # no longer on air, or its dwell already runs
        self._since = at.monotonic
        kind = ChangeKind.FRAME if self._aired else ChangeKind.UP
        self._aired = True
        self._changes.append(LaneChange(kind, current, at, frame=self._frame))

    def next_deadline(self, at: ClockReading) -> MonotonicTime | None:
        """When the lane will next change by itself, as of ``at``.

        None means only an offer, a commit or a call like `invalidate` can change it.
        """
        current = self._current
        if current is None:
            if not self._waiting:
                return None
            return at.monotonic + max(timedelta(0), self._waiting[0].available_at - at.utc)
        deadlines: list[MonotonicTime | None] = []
        if current.expires_at is not None:
            deadlines.append(at.monotonic + (current.expires_at - at.utc))
        if self._since is not None:
            minimum_ends = self._since + current.dwell.minimum
            if self._pending(at) is not None:
                deadlines.append(minimum_ends)
            else:
                if current.hold is None:
                    deadlines.append(self._since + current.dwell.maximum)
                if self._waiting:  # the next isn't available yet; once it is, it's pending
                    available = at.monotonic + (self._waiting[0].available_at - at.utc)
                    deadlines.append(max(available, minimum_ends))
        return earliest(deadlines)

    def changes(self) -> list[LaneChange[PayloadT]]:
        """What happened since the last ask, oldest first."""
        changes, self._changes = self._changes, []
        return changes

    def _pending(self, at: ClockReading) -> EndReason | None:
        """Why the segment on air should go once its frame has had its minimum, if it should."""
        if self._brief:
            return EndReason.CLOSING
        if self._waiting and self._waiting[0].available_at <= at.utc:
            return EndReason.NEXT_WAITING  # what's next takes over, whether or not this one still holds
        if self._stale:
            return EndReason.INVALIDATED  # nothing takes over: the surface goes blank
        return None

    def _down(self, reason: EndReason, at: ClockReading) -> None:
        current = self._current
        assert current is not None
        kind = ChangeKind.DOWN if self._aired else ChangeKind.DROPPED
        aired = at.monotonic.since(self._since) if self._since is not None else None
        self._changes.append(LaneChange(kind, current, at, frame=self._frame, reason=reason, aired=aired))
        self._current = None


def earliest(deadlines: Iterable[MonotonicTime | None]) -> MonotonicTime | None:
    """The soonest of some deadlines, ignoring the ones that aren't set."""
    return min((deadline for deadline in deadlines if deadline is not None), default=None)
