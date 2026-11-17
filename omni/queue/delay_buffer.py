"""TV-delay holding buffer: hold live items so they never spoil a broadcast.

A friends-and-family unit often sits next to a TV that's seconds behind the live
feed. Pushing every observation through a `DelayBuffer` set to that lag means the
scoreboard reveals a run/score only once the watcher could have seen it too.

Generic over the held item (a card, an event, a state) so one mechanism serves
the whole pipeline. The buffer owns delay timing only, measured from each item's
receipt on the monotonic clock, so a wall-clock jump can't release an item early or
hold it longer. **Display priority never shortens the delay** — a sports ALERT
(walk-off, no-hitter) is exactly the most spoiler-heavy content, so it waits like
everything else. Only a separate system/setup status (network fault, configuration),
which is not sports content, may use a different policy; that lives outside this buffer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, TypeVar

from omni.core.clock import ClockReading, MonotonicTime
from omni.core.time import DurationSeconds

__all__ = ["DelayBuffer"]

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class _Held(Generic[T]):
    release_at: MonotonicTime
    item: T


class DelayBuffer(Generic[T]):
    """Holds items for a fixed TV-delay, releasing each once its delay elapses.

    Push items as they are received; `release(at)` yields those whose delay has
    elapsed, in the order they were pushed (the delay is fixed, so insertion
    order is release order). A zero delay releases on the next `release` call.
    """

    def __init__(self, delay: DurationSeconds) -> None:
        self._delay = delay
        self._held: list[_Held[T]] = []

    @property
    def delay(self) -> DurationSeconds:
        return self._delay

    def push(self, item: T, *, received: ClockReading) -> MonotonicTime:
        """Hold `item`; returns when it will become available (its receipt + the delay, on the monotonic clock)."""
        release_at = received.monotonic + self._delay.as_timedelta()
        self._held.append(_Held(release_at=release_at, item=item))
        return release_at

    def release(self, at: ClockReading) -> list[T]:
        """Pop and return every item whose delay has elapsed by ``at``."""
        ready = [held.item for held in self._held if held.release_at <= at.monotonic]
        self._held = [held for held in self._held if held.release_at > at.monotonic]
        return ready

    def pending(self) -> int:
        """How many items are still being held."""
        return len(self._held)

    def next_release_at(self) -> MonotonicTime | None:
        """When the earliest still-held item becomes available, or None if empty."""
        return min((held.release_at for held in self._held), default=None)
