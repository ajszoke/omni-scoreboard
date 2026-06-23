"""Clock: the single source of "now" for the running app.

A `Protocol` so the orchestration loop reads time from one seam: a `SystemClock`
on real wall-time in production, and a `FakeClock` the test/fixture-replay harness
hand-advances — the *same* ``run_once(now)`` code path both ways, which is what
makes deterministic queue traces possible. Every clock returns timezone-aware UTC;
local-calendar concerns (which sports day is "today") are a separate, IANA-zoned
step layered on top, never `now.date()`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

from omni.core.time import DurationSeconds

__all__ = ["Clock", "SystemClock", "FakeClock"]


@runtime_checkable
class Clock(Protocol):
    """Anything that can report the current instant as an aware datetime."""

    def now(self) -> datetime: ...


class SystemClock:
    """Wall-clock time in UTC."""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


@dataclass(slots=True)
class FakeClock:
    """A hand-advanced clock for deterministic tests and fixture replay."""

    current: datetime

    def __post_init__(self) -> None:
        if self.current.tzinfo is None:
            raise ValueError("FakeClock time must be timezone-aware")

    def now(self) -> datetime:
        return self.current

    def advance(self, by: DurationSeconds) -> None:
        """Move the clock forward by a duration."""
        self.current = self.current + by.as_timedelta()

    def set(self, to: datetime) -> None:
        """Jump the clock to a specific (timezone-aware) instant."""
        if to.tzinfo is None:
            raise ValueError("FakeClock time must be timezone-aware")
        self.current = to
