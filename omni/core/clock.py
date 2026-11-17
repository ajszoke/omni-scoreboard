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

`FakeClock.advance` moves both readings together; `FakeClock.set` and `FakeClock.jump` move the wall clock
alone, the way an NTP correction or a DST mistake would, so tests can check that spans don't follow it.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Protocol, runtime_checkable

from omni.core.time import DurationSeconds

__all__ = ["Clock", "ClockReading", "FakeClock", "MonotonicTime", "Pacer", "SystemClock"]


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
    """One moment, read both ways: UTC to date it, monotonic to measure from it."""

    utc: datetime
    monotonic: MonotonicTime

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


class SystemClock:
    """The machine's clocks: wall time in UTC, and `time.monotonic`."""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def monotonic(self) -> MonotonicTime:
        return MonotonicTime(time.monotonic())

    def read(self) -> ClockReading:
        return ClockReading(self.now(), self.monotonic())


@dataclass(slots=True)
class FakeClock:
    """A hand-advanced clock for deterministic tests and fixture replay."""

    current: datetime
    elapsed: MonotonicTime = field(default_factory=lambda: MonotonicTime(0.0))

    def __post_init__(self) -> None:
        if self.current.tzinfo is None:
            raise ValueError("FakeClock time must be timezone-aware")

    def now(self) -> datetime:
        return self.current

    def monotonic(self) -> MonotonicTime:
        return self.elapsed

    def read(self) -> ClockReading:
        return ClockReading(self.current, self.elapsed)

    def advance(self, by: DurationSeconds | timedelta) -> None:
        """Let time pass: both readings move forward together."""
        span = by.as_timedelta() if isinstance(by, DurationSeconds) else by
        if span < timedelta(0):
            raise ValueError("time can't pass backward; use set() to jump the wall clock")
        self.current = self.current + span
        self.elapsed = self.elapsed + span

    def set(self, to: datetime) -> None:
        """Jump the wall clock to ``to``; monotonic time doesn't move."""
        if to.tzinfo is None:
            raise ValueError("FakeClock time must be timezone-aware")
        self.current = to

    def jump(self, by: timedelta) -> None:
        """Jump the wall clock by ``by``, forward or back, the way a clock correction does; monotonic time
        doesn't move."""
        self.current = self.current + by


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
