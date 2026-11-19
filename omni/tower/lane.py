"""Lane: one surface's airtime. Segments air one at a time, in the order they were offered.

Each frame of the segment on air gets its dwell's minimum, then keeps going to its maximum while
nothing is pending; after that comes its next frame, or the next segment. Something is *pending* when:
- another segment is waiting and available;
- what's on air no longer describes what's around it (`invalidate`);
- the surface is closing out (`close_out`), when every frame gets just its minimum and no hold applies.
A held segment stays past its maximum until something is pending.

A close-out can have a bound: a moment by which the lane should be done. Nothing then starts that
can't have its minimum before the bound, keeping room for one segment named to go last (the one that
closes the story); what doesn't fit is dropped as having missed its air window. The last segment may
start until the bound, and keeps its minimum once it has started, even past the bound.

The display says what it actually showed. `cue` names the frame that should be on air, and `committed`
is the display's word that it drew that frame: that segment, at that revision, that frame. A frame's
dwell counts from its first commit, so a frame that never drew spends none of its time. When the display
loses what it showed (its panel went blank), `blanked` takes that credit back: the frame on air goes up
again from its next commit, with its whole dwell. A segment the display can't draw at all comes off the
lane (`failed`), so it never holds up what follows.

Being cued isn't being shown, so a segment's bounds hold while it waits on the display too: one cued
but never committed is dropped once its latest start passes, or at once if it's invalidated; and one on
air, committed or not (a stalled display, a blank panel waiting for a new one), goes at its expiry. A
held segment whose frame is on the panel is the exception: it outlasts its expiry while it's still
current, and goes as soon as something is pending.

A segment's availability, latest start and expiry are UTC instants. Offered with a clock reading, the
lane pins them to the monotonic clock as that reading places them, so a later wall-clock jump moves
none of them; offered without one, they're read against the wall clock. Dwell, and a close-out's bound,
run on the monotonic clock.

Edits follow the presentation contract. A newer revision of a waiting segment takes its place in line.
A newer revision of the segment on air swaps its frames in place, keeping its dwell (a cosmetic edit).
An edit to one that has already aired doesn't bring it back: a material correction comes as a segment
of its own.

A backlog can be bounded: `shed` keeps at most so many of the waiting segments a caller picks (say, the
routine ones), dropping the oldest unseen.

`changes` reports, for the trace, what happened since it was last asked: a segment up (its first
frame committed), a later frame up, a frame blanked and up again, a segment down and why, a segment
dropped unseen and why.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
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
    MISSED_WINDOW = "missed its air window"  # a bounded close-out had no room left for it
    SOURCE_STALE = "its source went stale"  # what it came from stopped answering
    ARRIVED_LATE = "arrived late"  # it reached the board long after its source wrote it
    BACKLOG = "backlog"  # too many like it were waiting: the oldest go
    LANDED_LATE = "accidental: landed after its latest start"  # its first frame reached the panel too late to count
    GRANT_LAPSED = "accidental: landed after its grant lapsed"  # its first frame landed once its grant no longer held
    UNCONFIRMED = "its display can't confirm it"  # a display that never says what it showed can't start it
    INTERRUPTED = "interrupted"  # the host slept or stalled, or the display lost its panel


class ChangeKind(StrEnumMixin, str, Enum):
    UP = "up"  # its first frame was committed
    FRAME = "frame"  # a later frame was committed
    BLANKED = "blanked"  # the display lost the frame on air: it counts from its next commit
    AGAIN = "again"  # a blanked frame was committed again
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


@dataclass(frozen=True, slots=True)
class _Bounds:
    """A segment's availability, latest start and expiry, on the monotonic clock."""

    available: MonotonicTime
    latest_start: MonotonicTime | None
    expires: MonotonicTime | None


def _pinned(segment: Segment[PayloadT], at: ClockReading) -> _Bounds:
    """``segment``'s UTC bounds placed on the monotonic clock as ``at`` reads both."""

    def place(instant: datetime) -> MonotonicTime:
        return at.monotonic + (instant - at.utc)

    return _Bounds(
        available=place(segment.available_at),
        latest_start=place(segment.latest_start) if segment.latest_start is not None else None,
        expires=place(segment.expires_at) if segment.expires_at is not None else None,
    )


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
        self._blanked = False  # the frame on air was committed, then blanked (so it waits for a commit)
        self._stale = False
        self._invalidated = False  # the segment on air no longer describes what's around it
        self._brief = False
        self._by: MonotonicTime | None = None  # a close-out's bound
        self._last: SegmentKey | None = None  # the segment a bounded close-out keeps room for, last
        self._changes: list[LaneChange[PayloadT]] = []
        self._pins: dict[SegmentKey, _Bounds] = {}  # bounds pinned when offered, by segment

    @property
    def idle(self) -> bool:
        """Nothing on air and nothing waiting."""
        return self._current is None and not self._waiting

    @property
    def dwelling(self) -> bool:
        """The frame on air has been committed, so its dwell runs."""
        return self._current is not None and self._since is not None

    def offer(self, segments: Iterable[Segment[PayloadT]], *, at: ClockReading | None = None) -> None:
        """Queue segments behind any waiting. One already offered changes only by a newer revision.

        With ``at``, each one taken has its bounds pinned to the monotonic clock as of then.
        """
        for segment in segments:
            if segment.key.surface is not self._surface:
                raise ValueError(f"a {segment.key.surface.value} segment offered to the {self._surface.value} lane")
            known = self._known.get(segment.key)
            if known is not None and segment.revision <= known:
                continue
            self._known[segment.key] = segment.revision
            if at is not None:
                self._pins[segment.key] = _pinned(segment, at)
            else:
                self._pins.pop(segment.key, None)
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
        """What's on air no longer describes what's around it: it goes once its frame has had its minimum, or
        at once if it was never shown."""
        self._stale = self._invalidated = True

    def close_out(self, *, by: MonotonicTime | None = None, last: SegmentKey | None = None) -> None:
        """What's on air and what's waiting get one frame each, at its minimum, with no holds.

        With ``by``, nothing starts that can't have its minimum before then, keeping room for ``last`` if it
        waits; what doesn't fit is dropped as having missed its air window. ``last`` itself may start until
        ``by``, and keeps its minimum once started. Called again, the newest bound and last segment stand.
        """
        self._stale = self._brief = True
        self._by, self._last = by, last

    def resume(self) -> None:
        """Frames get their full time again, with no bound."""
        self._brief = False
        self._by = self._last = None

    def keeping(self, at: ClockReading) -> bool:
        """Whether the segment a bounded close-out keeps for last is on air, shown, and still owed its
        minimum as of ``at``."""
        current = self._current
        return (
            current is not None
            and current.key == self._last
            and self._since is not None
            and at.monotonic.since(self._since) < current.dwell.minimum
        )

    def blanked(self, at: ClockReading) -> None:
        """The display lost what it showed: the frame on air, if it had been committed, goes up again from
        its next commit, with its whole dwell."""
        current = self._current
        if current is None or self._since is None:
            return
        aired = at.monotonic.since(self._since)
        self._changes.append(LaneChange(ChangeKind.BLANKED, current, at, frame=self._frame, aired=aired))
        self._since, self._blanked = None, True

    def failed(self, cue: Cue[PayloadT], at: ClockReading) -> None:
        """The display couldn't draw ``cue``: if its segment, at that revision, is on air, it comes off the
        lane, unseen from here on."""
        current = self._current
        if current is None or cue.segment.key != current.key or cue.segment.revision != current.revision:
            return
        self._down(EndReason.UNDRAWN, at)

    def clear(self, at: ClockReading, reason: EndReason = EndReason.CLEARED) -> list[Segment[PayloadT]]:
        """Empty the lane; returns what goes unseen (everything waiting, and anything on air not yet drawn).
        What had aired comes down for ``reason``."""
        dropped = list(self._waiting)
        self._waiting.clear()
        if self._current is not None:
            if self._aired:
                self._down(reason, at)
            else:
                dropped.insert(0, self._current)
                self._current = None
        return dropped

    def shed(self, at: ClockReading, *, keep: int, among: Callable[[Segment[PayloadT]], bool]) -> None:
        """Of the waiting segments ``among`` picks, keep the newest ``keep``: the older ones are dropped unseen,
        as a backlog. What's on air, and what ``among`` passes over, stay."""
        picked = [segment.key for segment in self._waiting if among(segment)]
        excess = set(picked[: max(len(picked) - keep, 0)])
        for segment in self._waiting:
            if segment.key in excess:
                self._changes.append(LaneChange(ChangeKind.DROPPED, segment, at, reason=EndReason.BACKLOG))
        self._waiting = deque(segment for segment in self._waiting if segment.key not in excess)

    def cue(self, at: ClockReading) -> Cue[PayloadT] | None:
        """The frame that should be on air at ``at``, once the rules have run: the one up, its next, the next
        segment's first, or nothing."""
        current = self._current
        if current is not None:
            bounds = self._bounds(current, at)
            if bounds.expires is not None and at.monotonic >= bounds.expires:
                if not self._held_on_panel(current):
                    self._down(EndReason.EXPIRED, at)  # on air or not, shown or not
                elif (pending := self._pending(at)) is not None:
                    self._down(pending, at)  # a hold on the panel outlasts its expiry only while it's current
            elif not self._aired and self._invalidated:
                self._down(EndReason.INVALIDATED, at)  # never shown, and no longer current
            elif not self._aired and bounds.latest_start is not None and at.monotonic > bounds.latest_start:
                self._down(EndReason.TOO_LATE, at)  # cued, but the display never showed it in time
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
        while self._current is None and self._waiting and self._available(self._waiting[0], at):
            segment = self._waiting.popleft()
            bounds = self._bounds(segment, at)
            late = bounds.latest_start is not None and at.monotonic > bounds.latest_start
            if late or (bounds.expires is not None and at.monotonic >= bounds.expires):
                reason = EndReason.TOO_LATE if late else EndReason.EXPIRED
                self._changes.append(LaneChange(ChangeKind.DROPPED, segment, at, reason=reason))
                continue
            if not self._fits(segment, at):
                self._changes.append(LaneChange(ChangeKind.DROPPED, segment, at, reason=EndReason.MISSED_WINDOW))
                continue
            self._current, self._frame, self._since, self._aired, self._stale = segment, 0, None, False, False
            self._blanked = self._invalidated = False
        return Cue(self._current, self._frame) if self._current is not None else None

    def committed(self, cue: Cue[PayloadT], at: ClockReading) -> None:
        """The display drew ``cue`` at ``at``. The first commit of a frame starts its dwell."""
        current = self._current
        if current is None or self._since is not None:
            return  # nothing on air, or its dwell already runs
        if cue.segment.key != current.key or cue.segment.revision != current.revision or cue.frame != self._frame:
            return  # not what's on air now: another segment, an older revision, another frame
        self._since = at.monotonic
        kind = ChangeKind.AGAIN if self._blanked else ChangeKind.FRAME if self._aired else ChangeKind.UP
        self._aired, self._blanked = True, False
        self._changes.append(LaneChange(kind, current, at, frame=self._frame))

    def next_deadline(self, at: ClockReading) -> MonotonicTime | None:
        """When the lane will next change by itself, as of ``at``.

        None means only an offer, a commit or a call like `invalidate` can change it.
        """
        current = self._current
        if current is None:
            if not self._waiting:
                return None
            return max(at.monotonic, self._bounds(self._waiting[0], at).available)
        bounds = self._bounds(current, at)
        expired = bounds.expires is not None and at.monotonic >= bounds.expires
        current_hold = self._held_on_panel(current) and self._pending(at) is None  # outlasts its expiry
        deadlines: list[MonotonicTime | None] = [None if current_hold else bounds.expires]
        if not self._aired:  # cued, not yet shown
            deadlines.append(at.monotonic if self._invalidated else bounds.latest_start)
        if self._since is not None:
            minimum_ends = self._since + current.dwell.minimum
            if self._pending(at) is not None:
                deadlines.append(minimum_ends)
            else:
                if current.hold is None:
                    deadlines.append(self._since + current.dwell.maximum)
                if self._waiting:  # the next isn't available yet; once it is, it's pending
                    available = self._bounds(self._waiting[0], at).available
                    deadlines.append(available if expired else max(available, minimum_ends))
        return earliest(deadlines)

    def changes(self) -> list[LaneChange[PayloadT]]:
        """What happened since the last ask, oldest first."""
        changes, self._changes = self._changes, []
        return changes

    def _fits(self, segment: Segment[PayloadT], at: ClockReading) -> bool:
        """Whether ``segment`` may start at ``at`` under a bounded close-out: the last segment until the
        bound, anything else only if its minimum, and the last's if it waits behind, end by then."""
        if self._by is None:
            return True
        if segment.key == self._last:
            return at.monotonic < self._by
        room = segment.dwell.minimum
        room += next((waiting.dwell.minimum for waiting in self._waiting if waiting.key == self._last), timedelta(0))
        return at.monotonic + room <= self._by

    def _bounds(self, segment: Segment[PayloadT], at: ClockReading) -> _Bounds:
        """``segment``'s bounds on the monotonic clock: as pinned when it was offered, or as ``at`` places them."""
        pinned = self._pins.get(segment.key)
        return pinned if pinned is not None else _pinned(segment, at)

    def _held_on_panel(self, segment: Segment[PayloadT]) -> bool:
        """Whether ``segment`` is held and its frame on air is on the panel (committed, not blanked)."""
        return segment.hold is not None and self._since is not None

    def _available(self, segment: Segment[PayloadT], at: ClockReading) -> bool:
        return at.monotonic >= self._bounds(segment, at).available

    def _pending(self, at: ClockReading) -> EndReason | None:
        """Why the segment on air should go once its frame has had its minimum, if it should."""
        if self._brief:
            return EndReason.CLOSING
        if self._waiting and self._available(self._waiting[0], at):
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
