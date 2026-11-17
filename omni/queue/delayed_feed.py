"""DelayedFeed: the latest TV-safe value of a continuously-changing subject.

For a LIVE card the TV-delay means "show the state from ``lag`` ago", not "delay when
the card appears" — anchoring a perpetually-rebuilt card's availability to
``receipt + lag`` would push it forever into the future and it would never show.

So a feed holds a timeline of values of one subject (a game's state), each admitted with
its `Arrival` and its release pinned then by the `ReleaseGuard`, and
``latest_eligible(at)`` returns the newest one whose release has passed on the monotonic
clock. The card is then built from that lag-old value and shown immediately. (Discrete
one-shot items use `DelayedEventStream` instead; this is for a value that keeps changing.)
"""

from __future__ import annotations

from typing import Generic, TypeVar

from omni.core.clock import ClockReading, MonotonicTime
from omni.queue.delay_policy import Arrival, DelayPolicy, Held, ReleaseGuard

__all__ = ["DelayedFeed"]

FeedValueT = TypeVar("FeedValueT")


class DelayedFeed(Generic[FeedValueT]):
    """A per-subject buffer that yields the newest TV-safe value."""

    def __init__(self, guard: ReleaseGuard) -> None:
        self._guard = guard
        self._held: list[Held[FeedValueT]] = []
        self._newest: MonotonicTime | None = None  # the receipt of the newest value pushed so far

    @property
    def policy(self) -> DelayPolicy:
        return self._guard.policy

    def push(self, value: FeedValueT, arrival: Arrival, admitted: ClockReading) -> None:
        """Admit a new value of the subject at ``admitted``, its release pinned now.

        One received no later than a value already pushed is ignored: the same answer read again on a later
        pass, or one that arrived out of order.
        """
        received = arrival.received.monotonic
        if self._newest is not None and received <= self._newest:
            return
        self._newest = received
        self._held.append(Held(value, arrival, self._guard.pin(arrival, admitted)))

    def latest_eligible(self, at: ClockReading) -> Held[FeedValueT] | None:
        """The newest value TV-safe to reveal at ``at``, or None if none is yet.

        Drops that value and any older one still held (superseded); values not yet eligible are kept for a
        future pass.
        """
        eligible = [held for held in self._held if held.release <= at.monotonic]
        if not eligible:
            return None
        chosen = max(eligible, key=lambda held: held.arrival.received.monotonic)
        newest = chosen.arrival.received.monotonic
        self._held = [held for held in self._held if held.arrival.received.monotonic > newest]
        return chosen

    def pending(self) -> int:
        """How many values are still held (not yet superseded)."""
        return len(self._held)

    def next_eligible(self, at: ClockReading) -> MonotonicTime | None:
        """When the next held value clears the delay, if one is still inside it at ``at``."""
        return min((held.release for held in self._held if held.release > at.monotonic), default=None)
