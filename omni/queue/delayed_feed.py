"""DelayedFeed: the latest TV-safe value of a continuously-changing subject.

For a LIVE card the TV-delay means "show the state from ``lag`` ago", not "delay when
the card appears" — anchoring a perpetually-rebuilt card's availability to
``observed_at + lag`` would push it forever into the future and it would never show.

So a feed holds a timeline of `Observation`s of one subject (a game's state) and
``latest_eligible(now)`` returns the newest observation a `DelayPolicy` deems safe to
reveal — i.e. observed at least ``lag`` ago. The card is then built from that lag-old
value and shown immediately. (Discrete one-shot items use `DelayBuffer` instead; this
is for a value that keeps changing.)
"""

from __future__ import annotations

from datetime import datetime
from typing import Generic, TypeVar

from omni.core.observation import Observation
from omni.queue.delay_policy import DelayPolicy

__all__ = ["DelayedFeed"]

FeedValueT = TypeVar("FeedValueT")


class DelayedFeed(Generic[FeedValueT]):
    """A per-subject buffer that yields the newest TV-safe observation of a value."""

    def __init__(self, policy: DelayPolicy) -> None:
        self._policy = policy
        self._held: list[Observation[FeedValueT]] = []

    @property
    def policy(self) -> DelayPolicy:
        return self._policy

    def push(self, observation: Observation[FeedValueT]) -> None:
        """Record a new observation of the subject."""
        self._held.append(observation)

    def latest_eligible(self, now: datetime) -> Observation[FeedValueT] | None:
        """The newest observation TV-safe to reveal at ``now``, or None if none yet.

        Drops that observation and any older one still held (superseded); observations
        not yet eligible are kept for a future tick.
        """
        eligible = [obs for obs in self._held if self._policy.eligible_at(obs) <= now]
        if not eligible:
            return None
        chosen = max(eligible, key=lambda obs: obs.observed_at)
        self._held = [obs for obs in self._held if obs.observed_at > chosen.observed_at]
        return chosen

    def pending(self) -> int:
        """How many observations are still held (not yet superseded)."""
        return len(self._held)
