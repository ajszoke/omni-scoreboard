"""Core card types: kind, identity, timing, layout support, priority, ScoreboardCard."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Generic, TypeVar

from omni.cards.attention import NORMAL_ATTENTION, AttentionPolicy
from omni.core.enum import DisplayPriority, League, PanelProfile, StrEnumMixin
from omni.core.ids import LeagueScopedId
from omni.core.time import DurationSeconds
from omni.domain.contest import Contest

__all__ = [
    "CardKind",
    "CardId",
    "DedupeKey",
    "DisplayTiming",
    "LayoutSupport",
    "CardPriority",
    "CardPayloadT",
    "ScoreboardCard",
]


class CardKind(StrEnumMixin, str, Enum):
    LIVE_GAME = "live_game"
    PREGAME = "pregame"
    FINAL = "final"
    BIG_PLAY = "big_play"
    NO_HITTER = "no_hitter"
    ALERT = "alert"
    LEADERBOARD = "leaderboard"
    OFFDAY = "offday"


@dataclass(frozen=True, slots=True)
class CardId:
    raw: str


@dataclass(frozen=True, slots=True)
class DedupeKey:
    raw: str


@dataclass(frozen=True, slots=True, kw_only=True)
class DisplayTiming:
    """When a card may show and for how long — typed instead of loose datetimes/ints.

    Validated on construction so a long-running appliance fails fast rather than
    silently comparing naive vs. aware datetimes (or holding an already-expired
    or zero/negative-window card): every datetime must be timezone-aware,
    ``min_display <= max_display``, and any ``expires_at`` must be after
    ``available_at``.
    """

    available_at: datetime
    min_display: DurationSeconds
    max_display: DurationSeconds
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.available_at.tzinfo is None:
            raise ValueError("available_at must be timezone-aware")
        if self.expires_at is not None and self.expires_at.tzinfo is None:
            raise ValueError("expires_at must be timezone-aware")
        if self.min_display.value > self.max_display.value:
            raise ValueError("min_display cannot exceed max_display")
        if self.expires_at is not None and self.expires_at <= self.available_at:
            raise ValueError("expires_at must be after available_at")

    def is_available(self, now: datetime) -> bool:
        return now >= self.available_at and (self.expires_at is None or now < self.expires_at)


@dataclass(frozen=True, slots=True)
class LayoutSupport:
    """Which panel profiles a card can render on, and any compromises made."""

    profiles: frozenset[PanelProfile]
    compromise_notes: tuple[str, ...] = ()

    def supports(self, profile: PanelProfile) -> bool:
        return profile in self.profiles


@dataclass(frozen=True, slots=True, kw_only=True)
class CardPriority:
    band: DisplayPriority
    score: float
    reasons: tuple[str, ...] = ()


CardPayloadT = TypeVar("CardPayloadT")


@dataclass(frozen=True, slots=True, kw_only=True)
class ScoreboardCard(Generic[CardPayloadT]):
    """A renderable card: a contest, a sport-specific payload, and display metadata."""

    id: CardId
    kind: CardKind
    contest: Contest
    timing: DisplayTiming
    priority: CardPriority
    layout_support: LayoutSupport
    dedupe_key: DedupeKey
    payload: CardPayloadT
    source_event_ids: tuple[LeagueScopedId, ...] = ()
    attention: AttentionPolicy = NORMAL_ATTENTION

    @property
    def league(self) -> League:
        return self.contest.league
