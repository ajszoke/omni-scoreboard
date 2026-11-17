"""DelayedEventStream: release each discrete TV-safe event once, its release pinned on admission.

The discrete sibling of `DelayedFeed`. A feed yields the *newest* TV-safe value of a
continuously-changing subject (a game's state); a stream of distinct one-shot events
(home runs, big plays) is different — every event matters and each must surface exactly
once, the moment the `ReleaseGuard` deems it safe to reveal. That instant is measured from
when the play *happened* (its occurrence) when that is credible, never from when our poll
happened to receive it, so a slow or bursty fetch can't leak a score before the watcher's
lagging broadcast reaches it; it is pinned on the monotonic clock when the event is admitted.

The stream also dedupes by a caller-supplied key (a play's stable event id), so the same
event seen on successive polls is held once, not re-flashed every tick. `mark_seen` lets
a caller suppress a *backlog* — the plays that predate when we tuned in — so joining a
game mid-stream doesn't dump every earlier big play onto the screen at once.

Display priority never shortens the wait (a walk-off is the most spoiler-heavy content of
all); only a separate system/setup status, which is not sports content, may bypass delay.
"""

from __future__ import annotations

from typing import Generic, TypeVar

from omni.core.clock import ClockReading, MonotonicTime
from omni.queue.delay_policy import Arrival, DelayPolicy, Held, ReleaseGuard

__all__ = ["DelayedEventStream"]

EventValueT = TypeVar("EventValueT")


class DelayedEventStream(Generic[EventValueT]):
    """Holds distinct events, releasing each once TV-safe."""

    def __init__(self, guard: ReleaseGuard) -> None:
        self._guard = guard
        self._pending: list[Held[EventValueT]] = []
        self._seen: set[str] = set()  # keys ever handled — push is idempotent on these

    @property
    def policy(self) -> DelayPolicy:
        return self._guard.policy

    def push(self, value: EventValueT, arrival: Arrival, admitted: ClockReading, *, key: str) -> bool:
        """Hold a new event keyed by `key`, admitted at ``admitted``; ignore a key already handled.

        Returns True if the event was newly held, False if `key` was seen before
        (already pending, already released, or suppressed via `mark_seen`).
        """
        if key in self._seen:
            return False
        self._seen.add(key)
        self._pending.append(Held(value, arrival, self._guard.pin(arrival, admitted)))
        return True

    def mark_seen(self, key: str) -> None:
        """Record `key` as handled *without* holding it, so a later `push` of the same
        key is ignored. Used to suppress a backlog (events from before we tuned in)."""
        self._seen.add(key)

    def release(self, at: ClockReading) -> list[Held[EventValueT]]:
        """Pop and return every held event now TV-safe to reveal, once each.

        Insertion order is preserved among those released; events not yet
        eligible are kept for a future pass.
        """
        ready: list[Held[EventValueT]] = []
        holding: list[Held[EventValueT]] = []
        for held in self._pending:
            (ready if held.release <= at.monotonic else holding).append(held)
        self._pending = holding
        return ready

    def pending(self) -> int:
        """How many events are held, waiting for their TV-delay to elapse."""
        return len(self._pending)

    def next_eligible(self) -> MonotonicTime | None:
        """When the next held event clears the delay, or None if none is held."""
        return min((held.release for held in self._pending), default=None)
