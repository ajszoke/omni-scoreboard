"""DelayedEventStream: release each discrete TV-safe event once, source-time anchored.

The discrete sibling of `DelayedFeed`. A feed yields the *newest* TV-safe value of a
continuously-changing subject (a game's state); a stream of distinct one-shot events
(home runs, big plays) is different — every event matters and each must surface exactly
once, the moment a `DelayPolicy` deems it safe to reveal. That instant is anchored to
when the play *happened* (`source_time`), never to when our poll received it, so a slow
or bursty fetch can't leak a score before the watcher's lagging broadcast reaches it.

The stream also dedupes by a caller-supplied key (a play's stable event id), so the same
event seen on successive polls is held once, not re-flashed every tick. `mark_seen` lets
a caller suppress a *backlog* — the plays that predate when we tuned in — so joining a
game mid-stream doesn't dump every earlier big play onto the screen at once.

Display priority never shortens the wait (a walk-off is the most spoiler-heavy content of
all); only a separate system/setup status, which is not sports content, may bypass delay.
"""

from __future__ import annotations

from datetime import datetime
from typing import Generic, TypeVar

from omni.core.observation import Observation
from omni.queue.delay_policy import DelayPolicy

__all__ = ["DelayedEventStream"]

EventValueT = TypeVar("EventValueT")


class DelayedEventStream(Generic[EventValueT]):
    """Holds discrete observations of distinct events, releasing each once TV-safe."""

    def __init__(self, policy: DelayPolicy) -> None:
        self._policy = policy
        self._pending: list[Observation[EventValueT]] = []
        self._seen: set[str] = set()  # keys ever handled — push is idempotent on these

    @property
    def policy(self) -> DelayPolicy:
        return self._policy

    def push(self, observation: Observation[EventValueT], *, key: str) -> bool:
        """Hold a new event keyed by `key`; ignore a key already handled.

        Returns True if the event was newly held, False if `key` was seen before
        (already pending, already released, or suppressed via `mark_seen`).
        """
        if key in self._seen:
            return False
        self._seen.add(key)
        self._pending.append(observation)
        return True

    def mark_seen(self, key: str) -> None:
        """Record `key` as handled *without* holding it, so a later `push` of the same
        key is ignored. Used to suppress a backlog (events from before we tuned in)."""
        self._seen.add(key)

    def release(self, now: datetime) -> list[Observation[EventValueT]]:
        """Pop and return every held observation now TV-safe to reveal, once each.

        Insertion order is preserved among those released; observations not yet
        eligible are kept for a future tick.
        """
        ready: list[Observation[EventValueT]] = []
        holding: list[Observation[EventValueT]] = []
        for obs in self._pending:
            (ready if self._policy.eligible_at(obs) <= now else holding).append(obs)
        self._pending = holding
        return ready

    def pending(self) -> int:
        """How many events are held, waiting for their TV-delay to elapse."""
        return len(self._pending)
