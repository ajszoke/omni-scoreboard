"""InterleavedCardQueue: fair, priority-weighted rotation across contests.

The capstone of the display pipeline. Cards (already scored and TV-delayed)
are ingested and deduped by `DedupeKey`; `next_card(now, profile)` decides what
to show next, balancing two goals:

- **Priority** — favorite / high-leverage cards get more airtime, and
  ALERT/STICKY cards take over the screen entirely while they're live.
- **Fairness** — no single contest monopolizes the display; every eligible
  game still surfaces.

Selection is "most overdue wins": the time since a contest was last shown times
a per-band weight. A high-priority contest goes overdue faster (so it shows more
often), but a low-priority one's wait still grows, so it is never buried. With
few contests this naturally alternates; with many, priority cuts the line.

The queue answers "what next" only — dwell (min/max display) is the caller's
render loop. True per-league round-robin (vs. per-contest) is a future
refinement; priority weighting already keeps one busy league from drowning a
lone high-leverage game elsewhere.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

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

    def __init__(self, *, sticky_band: DisplayPriority = DisplayPriority.ALERT) -> None:
        self._cards: dict[str, ScoreboardCard[Any]] = {}  # DedupeKey.raw -> card
        self._sticky_band = sticky_band
        self._tick = 0
        self._last_shown: dict[LeagueScopedId, int] = {}

    def ingest(self, card: ScoreboardCard[Any]) -> None:
        """Add a card, or replace the one sharing its `DedupeKey` (a fresh update)."""
        self._cards[card.dedupe_key.raw] = card

    def ingest_all(self, cards: Iterable[ScoreboardCard[Any]]) -> None:
        for card in cards:
            self.ingest(card)

    def remove(self, dedupe_key: str) -> None:
        self._cards.pop(dedupe_key, None)

    def prune(self, now: datetime) -> int:
        """Drop cards whose `expires_at` has passed; returns how many were removed."""
        expired = [
            key
            for key, card in self._cards.items()
            if card.timing.expires_at is not None and now >= card.timing.expires_at
        ]
        for key in expired:
            del self._cards[key]
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
        # An ALERT/STICKY card takes over the screen while it's live.
        stickies = [card for card in pool if card.priority.band >= self._sticky_band]
        chosen = max(stickies or pool, key=self._rank)
        self._tick += 1
        self._last_shown[chosen.contest.id] = self._tick
        return chosen

    def _showable(self, card: ScoreboardCard[Any], now: datetime, profile: PanelProfile) -> bool:
        return card.timing.is_available(now) and card.layout_support.supports(profile)

    def _rank(self, card: ScoreboardCard[Any]) -> tuple[int, int, float, str]:
        # most overdue first; ties broken by band, then score, then id (deterministic).
        staleness = self._tick - self._last_shown.get(card.contest.id, -1)
        weight = _BAND_WEIGHT.get(card.priority.band, 1)
        return (staleness * weight, int(card.priority.band), card.priority.score, card.id.raw)
