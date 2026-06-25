"""Generic event model: importance scoring and the typed GameEvent base."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Generic, TypeVar

from omni.core.enum import DisplayPriority, League, UpdateUrgency
from omni.core.ids import LeagueScopedId, SourceRef
from omni.domain.base import Competitor
from omni.domain.contest import Contest

__all__ = ["EventTypeT", "PayloadT", "EventImportance", "GameEvent"]

EventTypeT = TypeVar("EventTypeT", bound=Enum)
PayloadT = TypeVar("PayloadT")


@dataclass(frozen=True, slots=True, kw_only=True)
class EventImportance:
    """Why an event matters, as explainable components rather than a bare float.

    `leverage`, `rarity`, and `favorite_relevance` are normalized 0..1 so the
    priority scorer can weight them consistently; `reasons` carries human-readable
    codes for debugging why a card was promoted.
    """

    priority: DisplayPriority
    urgency: UpdateUrgency
    leverage: float
    rarity: float
    favorite_relevance: float
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name, value in (
            ("leverage", self.leverage),
            ("rarity", self.rarity),
            ("favorite_relevance", self.favorite_relevance),
        ):
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be normalized to 0..1")

    def combined_score(self) -> float:
        return (
            int(self.priority)
            + int(self.urgency) * 5
            + self.leverage * 20
            + self.rarity * 15
            + self.favorite_relevance * 20
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class GameEvent(Generic[EventTypeT, PayloadT]):
    """A typed, sport-parameterized event observed within a contest.

    `event_type` and `payload` are parameterized per sport (e.g.
    `GameEvent[BaseballGameEventType, BaseballPlayPayload]`), so a renderer or
    scorer for one sport can never be handed another sport's event shape.
    """

    id: LeagueScopedId
    contest: Contest
    event_type: EventTypeT
    source: SourceRef
    source_time: datetime
    observed_at: datetime
    importance: EventImportance
    payload: PayloadT
    competitors: tuple[Competitor, ...] = ()

    def __post_init__(self) -> None:
        # Mirror Observation's time invariants — the TV-delay math mixes these two datetimes,
        # so a naive one would raise mid-run — and require a real lineage id: an event's `id` is
        # its dedupe/replay key (e.g. ``<gamePk>:ab:<atBatIndex>``), so an empty one would collide.
        if not self.id.raw:
            raise ValueError("a game event needs a non-empty id (its lineage key)")
        if self.source_time.tzinfo is None:
            raise ValueError("source_time must be timezone-aware")
        if self.observed_at.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")

    @property
    def league(self) -> League:
        return self.contest.league
