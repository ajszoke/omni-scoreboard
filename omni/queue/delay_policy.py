"""The release guard: when a held item may surface, pinned on the monotonic clock when it's admitted.

The kernel's TV-delay rule is "reveal a play only once the watcher could have seen it on their (lagging)
broadcast". An item carries three times, kept apart (`Arrival`):
- **receipt:** when it reached this board, read both ways (UTC and monotonic);
- **occurrence:** when the play happened, by the source's clock;
- **as-of:** when the source's answer stood, by the source's clock.

On admission, `DelayPolicy.release` pins the item's earliest release as a monotonic instant:
- never before its receipt;
- with a credible occurrence, at receipt + the delay, less the play's age: how long before the source's
  answer it happened, by the source's own clock. Our wall clock never enters that age, so a board whose
  clock runs ahead or behind can't shorten the delay. An answer older than its receipt (a cached copy, a
  slow fetch) only understates the age, so the release comes later, the safe way;
- otherwise, at receipt + the full delay. That covers an item with no occurrence or no as-of, one whose
  times don't fit each other or its receipt (a source clock ahead of ours, or a board that booted without
  the right time), one whose receipt and admission disagree about how much time passed, and anything
  admitted while the wall clock is suspect.

Eligibility is then decided on the monotonic clock alone, so a later wall-clock jump can neither release a
held item early nor hold it longer, and what has been released and dropped stays dropped.

`WallClockWatch` notices a jump: between two readings UTC moved further than monotonic time did. UTC is
then suspect for `SETTLE_AFTER_JUMP`, and everything admitted meanwhile waits its full delay from receipt.
`ReleaseGuard` is one board's rule: its policy and its watch.

**Display priority never shortens the delay.** A sports ALERT (walk-off, no-hitter) is exactly the most
spoiler-heavy content, so it waits like everything else; only a separate system/setup status (not sports
content) may use a different policy.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Generic, TypeVar

from omni.core.clock import ClockReading, MonotonicTime
from omni.core.enum import StrEnumMixin
from omni.core.time import DurationSeconds

__all__ = [
    "CLOCK_TOLERANCE",
    "SETTLE_AFTER_JUMP",
    "SOURCE_LATENESS",
    "SOURCE_LEAD",
    "Arrival",
    "DelayAnchor",
    "DelayPolicy",
    "Held",
    "ReleaseGuard",
    "WallClockWatch",
]

T = TypeVar("T")

# How far a play's time may run ahead of the answer that reports it, or a source's time ahead of our receipt,
# and still be believed: the source dates its answer to the whole second, and two clocks never agree exactly.
# Further ahead, one of them is wrong (a board that booted without the right time, say).
SOURCE_LEAD = timedelta(seconds=5)

# How long before the answer that reports it, or before our receipt, a play may have happened and still be
# believed. A play normally reaches the board within a poll or two; one reported much earlier says a clock is
# off. Waiting the full delay from receipt costs a late play no more than the delay.
SOURCE_LATENESS = timedelta(minutes=2)

# How far UTC may move apart from monotonic time between two readings before it counts as a jump.
CLOCK_TOLERANCE = timedelta(seconds=2)

# How long UTC stays suspect after a jump: whatever is admitted meanwhile waits its full delay from receipt.
SETTLE_AFTER_JUMP = timedelta(minutes=2)


class DelayAnchor(StrEnumMixin, str, Enum):
    """What the broadcast delay is measured from."""

    OCCURRENCE = "occurrence"  # when the play happened, if that's credible: the default
    RECEIPT = "receipt"  # when it reached the board: for a source whose event times aren't trustworthy


@dataclass(frozen=True, slots=True, kw_only=True)
class Arrival:
    """An item's three times, kept apart: its local receipt, and the source's occurrence and as-of."""

    received: ClockReading  # when it reached this board
    occurred: datetime | None = None  # when the play happened, by the source's clock
    as_of: datetime | None = None  # when the source's answer stood, by its clock

    def __post_init__(self) -> None:
        for instant in (self.occurred, self.as_of):
            if instant is not None and instant.tzinfo is None:
                raise ValueError("a source's times must be timezone-aware")


@dataclass(frozen=True, slots=True)
class Held(Generic[T]):
    """An item admitted to a delay: its value, its times, and the monotonic instant it may surface."""

    value: T
    arrival: Arrival
    release: MonotonicTime


@dataclass(frozen=True, slots=True, kw_only=True)
class DelayPolicy:
    """The broadcast delay, and what it is measured from."""

    broadcast_lag: DurationSeconds
    anchor: DelayAnchor = DelayAnchor.OCCURRENCE

    def release(self, arrival: Arrival, admitted: ClockReading, *, utc_trusted: bool = True) -> MonotonicTime:
        """The earliest monotonic instant an item admitted at ``admitted`` may surface.

        With a credible age it is receipt + the delay less that age, and never before receipt. Without one, or
        while the wall clock isn't ``utc_trusted``, it is receipt + the full delay.
        """
        lag = self.broadcast_lag.as_timedelta()
        age = self.credible_age(arrival, admitted) if utc_trusted else None
        if age is None:
            return arrival.received.monotonic + lag
        return arrival.received.monotonic + max(lag - age, timedelta(0))

    def credible_age(self, arrival: Arrival, admitted: ClockReading) -> timedelta | None:
        """How long before the source's answer the item's play happened, by the source's own clock, if the
        delay can be shortened by it.

        None when the policy measures from receipt, or the source gave no occurrence or no as-of. None too when
        the times don't fit: the play is dated after the answer that reports it, or implausibly long before; our
        wall clock and the source's disagree; or the wall clock moved apart from monotonic time between the
        item's receipt and its admission. A play dated just after its answer has no age: the answer's time is
        to the whole second.
        """
        occurred, as_of = arrival.occurred, arrival.as_of
        if self.anchor is DelayAnchor.RECEIPT or occurred is None or as_of is None:
            return None
        received = arrival.received
        if abs((admitted.utc - received.utc) - admitted.monotonic.since(received.monotonic)) > CLOCK_TOLERANCE:
            return None  # the wall clock jumped between its receipt and now
        age = as_of - occurred
        if not -SOURCE_LEAD <= age <= SOURCE_LATENESS:
            return None  # by the source's clock, it happened after the answer that reports it, or long before
        if not received.utc - SOURCE_LATENESS <= occurred <= received.utc + SOURCE_LEAD:
            return None  # on our clock, it happened after it reached us, or long before: one clock is wrong
        if as_of > received.utc + SOURCE_LEAD:
            return None  # the source's clock runs ahead of ours
        return max(age, timedelta(0))


class WallClockWatch:
    """Watches UTC against monotonic time, reading by reading. A jump between two readings, one way or the
    other, makes UTC suspect until ``settle`` has passed since it."""

    def __init__(self, *, settle: timedelta = SETTLE_AFTER_JUMP, tolerance: timedelta = CLOCK_TOLERANCE) -> None:
        self._settle = settle
        self._tolerance = tolerance
        self._origin: datetime | None = None  # the UTC the last reading put at monotonic zero
        self._suspect_until: MonotonicTime | None = None

    def observe(self, at: ClockReading) -> bool:
        """Note ``at``; whether UTC can be trusted then."""
        origin = at.utc - timedelta(seconds=at.monotonic.seconds)
        if self._origin is not None and abs(origin - self._origin) > self._tolerance:
            self._suspect_until = at.monotonic + self._settle
        self._origin = origin
        return self._suspect_until is None or at.monotonic >= self._suspect_until


class ReleaseGuard:
    """One board's release rule: its delay policy, and a watch on its wall clock."""

    def __init__(self, policy: DelayPolicy, *, watch: WallClockWatch | None = None) -> None:
        self._policy = policy
        self._watch = watch if watch is not None else WallClockWatch()

    @property
    def policy(self) -> DelayPolicy:
        return self._policy

    def observe(self, at: ClockReading) -> bool:
        """Note a reading of the clock (each decision pass does); whether UTC can be trusted at it."""
        return self._watch.observe(at)

    def pin(self, arrival: Arrival, admitted: ClockReading) -> MonotonicTime:
        """When an item admitted at ``admitted`` may surface, fixed now on the monotonic clock."""
        return self._policy.release(arrival, admitted, utc_trusted=self._watch.observe(admitted))
