"""InterleavedCardQueue: fair, two-level rotation across contests and their cards.

The capstone of the display pipeline. Cards (already scored and TV-delayed) are
ingested and deduped by `DedupeKey`; `next_card(now, profile)` decides what to show
next. Selection is **two-level** so nothing starves:

1. **Group** — pick the most-overdue *rotation group* (a contest), weighted by its
   highest-priority card, so a favorite / high-leverage game gets more airtime while
   a quiet one is never buried.
2. **Card** — within that group, pick the most-overdue *card*, weighted by its band,
   so siblings for one game (LIVE, BIG_PLAY, FINAL, ...) share airtime fairly.

Attention is **bounded and separate from priority** (the verdict's High #3 fix): a
card with a `BURST` `AttentionPolicy` takes over the screen only for its
`takeover_for` window (derived from its `available_at`), then rejoins normal
rotation — display priority alone never grants a permanent takeover.

The queue answers "what next" only — per-card dwell (min/max display) is the render
loop's job.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any, Iterable

from omni.cards.attention import AttentionMode
from omni.cards.base import ScoreboardCard
from omni.core.enum import DisplayPriority, PanelProfile
from omni.core.ids import LeagueScopedId

__all__ = ["InterleavedCardQueue"]

# How much more airtime each band earns (multiplies time-since-last-shown).
_BAND_WEIGHT: dict[DisplayPriority, int] = {
    DisplayPriority.BACKGROUND: 1,
    DisplayPriority.NORMAL: 1,
    DisplayPriority.FAVORITE: 2,
    DisplayPriority.HIGH_LEVERAGE: 3,
    DisplayPriority.ALERT: 4,
    DisplayPriority.STICKY: 5,
}


class InterleavedCardQueue:
    """Holds deduped cards and picks the next one to show, fairly and by priority.

    Cards are heterogeneous (different payload types), so they are held as
    `ScoreboardCard[Any]`; the renderer dispatches on the concrete payload.
    """

    def __init__(self) -> None:
        self._cards: dict[str, ScoreboardCard[Any]] = {}  # DedupeKey.raw -> card
        self._tick = 0
        self._group_last_shown: dict[LeagueScopedId, int] = {}  # contest id -> tick
        self._card_last_shown: dict[str, int] = {}  # DedupeKey.raw -> tick

    def ingest(self, card: ScoreboardCard[Any]) -> None:
        """Add a card, or replace the one sharing its `DedupeKey` (a fresh update)."""
        self._cards[card.dedupe_key.raw] = card

    def ingest_all(self, cards: Iterable[ScoreboardCard[Any]]) -> None:
        for card in cards:
            self.ingest(card)

    def remove(self, dedupe_key: str) -> None:
        self._cards.pop(dedupe_key, None)
        self._forget_orphans()

    def prune(self, now: datetime) -> int:
        """Drop cards whose `expires_at` has passed; returns how many were removed."""
        expired = [
            key
            for key, card in self._cards.items()
            if card.timing.expires_at is not None and now >= card.timing.expires_at
        ]
        for key in expired:
            del self._cards[key]
        if expired:
            self._forget_orphans()
        return len(expired)

    def __len__(self) -> int:
        return len(self._cards)

    def eligible(self, now: datetime, profile: PanelProfile) -> list[ScoreboardCard[Any]]:
        """Cards that may show right now on `profile` (available, in-window, supported)."""
        return [card for card in self._cards.values() if self._showable(card, now, profile)]

    def next_card(self, now: datetime, profile: PanelProfile) -> ScoreboardCard[Any] | None:
        """Pick and record the next card to display, or None if nothing is eligible."""
        self.prune(now)
        pool = self.eligible(now, profile)
        if not pool:
            return None

        bursting = [card for card in pool if self._is_bursting(card, now)]
        if bursting:
            # A bounded BURST takes over the screen for its window, then yields.
            chosen = max(bursting, key=self._card_rank)
        else:
            groups: dict[LeagueScopedId, list[ScoreboardCard[Any]]] = defaultdict(list)
            for card in pool:
                groups[card.contest.id].append(card)
            chosen_group = max(groups, key=lambda gid: self._group_rank(gid, groups[gid]))
            chosen = max(groups[chosen_group], key=self._card_rank)

        self._tick += 1
        self._group_last_shown[chosen.contest.id] = self._tick
        self._card_last_shown[chosen.dedupe_key.raw] = self._tick
        return chosen

    def _showable(self, card: ScoreboardCard[Any], now: datetime, profile: PanelProfile) -> bool:
        return card.timing.is_available(now) and card.layout_support.supports(profile)

    def _is_bursting(self, card: ScoreboardCard[Any], now: datetime) -> bool:
        policy = card.attention
        if policy.mode is not AttentionMode.BURST or policy.takeover_for.value <= 0:
            return False
        end = card.timing.available_at + policy.takeover_for.as_timedelta()
        return card.timing.available_at <= now < end

    def _group_rank(self, group_id: LeagueScopedId, cards: list[ScoreboardCard[Any]]) -> tuple[int, int, float, str]:
        # most overdue group first; weighted by its highest-priority card, then ties.
        staleness = self._tick - self._group_last_shown.get(group_id, -1)
        weight = max(_BAND_WEIGHT.get(card.priority.band, 1) for card in cards)
        top_band = max(int(card.priority.band) for card in cards)
        top_score = max(card.priority.score for card in cards)
        return (staleness * weight, top_band, top_score, str(group_id))

    def _card_rank(self, card: ScoreboardCard[Any]) -> tuple[int, int, float, str]:
        # most overdue card first; ties broken by band, then score, then id.
        staleness = self._tick - self._card_last_shown.get(card.dedupe_key.raw, -1)
        weight = _BAND_WEIGHT.get(card.priority.band, 1)
        return (staleness * weight, int(card.priority.band), card.priority.score, card.id.raw)

    def _forget_orphans(self) -> None:
        # Drop recency bookkeeping for cards/groups that no longer exist (no slow leak).
        live_keys = set(self._cards)
        live_groups = {card.contest.id for card in self._cards.values()}
        self._card_last_shown = {key: tick for key, tick in self._card_last_shown.items() if key in live_keys}
        self._group_last_shown = {gid: tick for gid, tick in self._group_last_shown.items() if gid in live_groups}
