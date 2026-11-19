"""Clock: the single source of time for the running app, read two ways.

A `Protocol` so the app reads time from one seam: a `SystemClock` in production, and a `FakeClock`
that tests and fixture replay advance by hand. Both run the same ``run_once(now)`` code, which is
what makes queue traces deterministic.

Each clock gives two readings, for two different jobs:
- **UTC** (`now`, timezone-aware) dates things: when a play happened, when an observation arrived,
  when something is released to the viewer. Local-calendar questions (which sports day is "today")
  are a separate, IANA-zoned step on top, never `now.date()`.
- **Monotonic** (`monotonic`) measures spans: dwell, time since receipt, backoff, animation phase,
  pacing. It never jumps, so a wall-clock change can't stretch or cut a span.

A reading may also carry a third, **suspend-inclusive** elapsed clock (`suspend_inclusive`), where the platform
has one: Linux's ``CLOCK_BOOTTIME``, which, unlike the monotonic clock, keeps counting while the host sleeps. Set
against the monotonic clock it tells a sleep from a wall-clock correction: across a sleep it runs ahead of the
monotonic clock, and across a correction it doesn't. It is None where the platform has no such clock.

`FakeClock.advance` moves both readings together (and the suspend-inclusive one, if it has one); `FakeClock.set`
and `FakeClock.jump` move the wall clock alone, the way an NTP correction or a DST mistake would, so tests can check
that spans don't follow it; `FakeClock.suspend` fakes a host's sleep: the wall clock and the suspend-inclusive clock
move on while the monotonic clock stands still.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Protocol, runtime_checkable

from omni.core.time import DurationSeconds

__all__ = ["Clock", "ClockReading", "FakeClock", "MonotonicTime", "Pacer", "SystemClock", "boottime"]


@dataclass(frozen=True, slots=True, order=True)
class MonotonicTime:
    """A point on a clock that only moves forward; meaningful only against another such point."""

    seconds: float

    def __add__(self, span: timedelta) -> MonotonicTime:
        return MonotonicTime(self.seconds + span.total_seconds())

    def since(self, earlier: MonotonicTime) -> timedelta:
        """How long after ``earlier`` this is."""
        return timedelta(seconds=self.seconds - earlier.seconds)


@dataclass(frozen=True, slots=True)
class ClockReading:
    """One moment, read both ways: UTC to date it, monotonic to measure from it. Where the platform has one, a
    suspend-inclusive elapsed reading too, which keeps counting while the host sleeps; None where it hasn't."""

    utc: datetime
    monotonic: MonotonicTime
    suspend_inclusive: MonotonicTime | None = None

    def __post_init__(self) -> None:
        if self.utc.tzinfo is None:
            raise ValueError("a clock reading's UTC time must be timezone-aware")

    def project(self, instant: MonotonicTime) -> datetime:
        """The UTC this reading puts on ``instant``: its own UTC, moved by the monotonic span to ``instant``."""
        return self.utc + instant.since(self.monotonic)


@runtime_checkable
class Clock(Protocol):
    """Anything that can report the current moment."""

    def now(self) -> datetime:
        """The current instant, timezone-aware UTC."""
        ...

    def monotonic(self) -> MonotonicTime:
        """The current point on a clock that never jumps."""
        ...

    def read(self) -> ClockReading:
        """Both readings at once."""
        ...


def boottime() -> Callable[[], float] | None:
    """The platform's suspend-inclusive elapsed clock, in seconds: ``CLOCK_BOOTTIME`` where it has one (Linux); None
    elsewhere."""
    clock_id = getattr(time, "CLOCK_BOOTTIME", None)
    if clock_id is None:
        return None
    return lambda: time.clock_gettime(clock_id)


class SystemClock:
    """The machine's clocks: wall time in UTC, `time.monotonic`, and the suspend-inclusive clock where there is one.

    ``suspend_inclusive`` reads that clock, in seconds: the platform's (`boottime`) unless given; None for none."""

    def __init__(self, *, suspend_inclusive: Callable[[], float] | None = boottime()) -> None:
        self._suspend_inclusive = suspend_inclusive

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def monotonic(self) -> MonotonicTime:
        return MonotonicTime(time.monotonic())

    def read(self) -> ClockReading:
        elapsed = self._suspend_inclusive
        return ClockReading(self.now(), self.monotonic(), MonotonicTime(elapsed()) if elapsed is not None else None)


@dataclass(slots=True)
class FakeClock:
    """A hand-advanced clock for deterministic tests and fixture replay."""

    current: datetime
    elapsed: MonotonicTime = field(default_factory=lambda: MonotonicTime(0.0))
    suspend_inclusive: MonotonicTime | None = None  # a host with a suspend-inclusive clock: where it starts

    def __post_init__(self) -> None:
        if self.current.tzinfo is None:
            raise ValueError("FakeClock time must be timezone-aware")

    def now(self) -> datetime:
        return self.current

    def monotonic(self) -> MonotonicTime:
        return self.elapsed

    def read(self) -> ClockReading:
        return ClockReading(self.current, self.elapsed, self.suspend_inclusive)

    def advance(self, by: DurationSeconds | timedelta) -> None:
        """Let time pass: every reading moves forward together."""
        span = _span(by)
        self.current = self.current + span
        self.elapsed = self.elapsed + span
        if self.suspend_inclusive is not None:
            self.suspend_inclusive = self.suspend_inclusive + span

    def suspend(self, by: DurationSeconds | timedelta) -> None:
        """The host sleeps for ``by``: the wall clock and the suspend-inclusive clock move on, the monotonic clock
        stands still."""
        span = _span(by)
        self.current = self.current + span
        if self.suspend_inclusive is not None:
            self.suspend_inclusive = self.suspend_inclusive + span

    def set(self, to: datetime) -> None:
        """Jump the wall clock to ``to``; monotonic time doesn't move."""
        if to.tzinfo is None:
            raise ValueError("FakeClock time must be timezone-aware")
        self.current = to

    def jump(self, by: timedelta) -> None:
        """Jump the wall clock by ``by``, forward or back, the way a clock correction does; monotonic time
        doesn't move."""
        self.current = self.current + by


def _span(by: DurationSeconds | timedelta) -> timedelta:
    span = by.as_timedelta() if isinstance(by, DurationSeconds) else by
    if span < timedelta(0):
        raise ValueError("time can't pass backward; use set() to jump the wall clock")
    return span


class Pacer:
    """Keeps a loop to a steady beat on the clock's monotonic reading.

    Each `wait` sleeps until the next beat. A loop that falls behind starts its next beat from now
    rather than racing to catch up, so a stall never turns into a burst.
    """

    def __init__(self, clock: Clock, every: timedelta, *, sleep: Callable[[float], object] = time.sleep) -> None:
        if every <= timedelta(0):
            raise ValueError("a pacer needs a positive beat")
        self._clock = clock
        self._every = every
        self._sleep = sleep
        self._next = clock.monotonic() + every

    def wait(self) -> None:
        """Sleep until the next beat."""
        now = self._clock.monotonic()
        if now < self._next:
            self._sleep(self._next.since(now).total_seconds())
            self._next = self._next + self._every
        else:
            self._next = now + self._every
