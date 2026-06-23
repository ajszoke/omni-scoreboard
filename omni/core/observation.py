"""Observation: a typed, time-stamped value as one source saw it.

The running app's input envelope. It wraps a provider-produced value (a live game
state, an event, ...) with the identity it concerns, the source it came from, and
two distinct times: ``source_time`` — when the source's own clock says the thing
happened — versus ``observed_at`` — when we received it. That distinction is what
makes the TV-delay correct: the delay anchors to when the play actually happened
(see :class:`omni.queue.delay_policy.DelayPolicy`), so a slow fetch can never leak
a score before the watcher's lagging broadcast reaches it.

Carrying ``subject_id`` + ``sequence`` also gives event-derived cards the lineage
the snapshot pipeline lacked: a card can point back at the observation(s) it came
from, and stale/out-of-order updates are detectable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Generic, TypeVar

from omni.core.ids import LeagueScopedId, ProviderSequence, SourceRef

__all__ = ["Observation"]

ObservedValueT = TypeVar("ObservedValueT")


@dataclass(frozen=True, slots=True, kw_only=True)
class Observation(Generic[ObservedValueT]):
    """One source's time-stamped view of a value about a known subject.

    Validated on construction (like ``DisplayTiming``) so a long-running appliance
    fails fast on naive datetimes rather than silently mixing aware and naive.
    """

    subject_id: LeagueScopedId
    source: SourceRef
    observed_at: datetime
    value: ObservedValueT
    source_time: datetime | None = None
    sequence: ProviderSequence | None = None

    def __post_init__(self) -> None:
        if self.observed_at.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")
        if self.source_time is not None and self.source_time.tzinfo is None:
            raise ValueError("source_time must be timezone-aware")
