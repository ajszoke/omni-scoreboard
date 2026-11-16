"""InterleavedCardQueue: fair, two-level rotation across contests and their cards.

The board's selection policy. Cards (already scored and TV-delayed) are ingested and deduped by
`DedupeKey`; `peek(at, profile)` says what should air next, and `book` records that it did. The
board (`omni.tower.board`) books a card when its first frame is committed, not when it is chosen, so
a card that never reached the panel spends none of its turn, its rotation or its reminder.

Selection filters first, then chooses:
1. **Eligible:** available, not expired, and supported on the profile.
2. **In order:** a card waits for the cards it must follow (`ScoreboardCard.after`, a game's decisive
   play before its final). Finals that haven't aired go in the order their games ended: only the
   earliest is eligible.
3. **A usable window:** a card about to expire, with less time left than its minimum, is passed over.

Then it chooses, two-level, so nothing starves:
1. **Group:** the most-overdue *rotation group* (a contest), weighted by its highest-priority card, so
   a favorite or a high-leverage game gets more airtime while a quiet one is never buried.
2. **Card:** within that group, the most-overdue *card*, weighted by its band, so siblings for one game
   (LIVE, BIG_PLAY, FINAL, ...) share airtime fairly.

Attention is **bounded and separate from priority**. A `BURST` card takes over once, ahead of
rotation, the first time it airs; after that it rotates like any other. The board bounds how much
takeover airtime runs back to back. A `RECURRING` card (an active no-hitter) resurfaces on its
`cooldown` beat, counted on the monotonic clock from when it last aired, ahead of rotation and capped
by `max_repeats`; in between it is held back, so it reminds periodically rather than constantly.
Display priority alone never grants a permanent takeover.

The queue is thread-safe: the pipeline ingests on the decision thread while the board reads it on the
frame thread.
"""

from __future__ import annotations

import threading
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Iterable

from omni.cards.attention import AttentionMode
from omni.cards.base import CardKind, ScoreboardCard
from omni.core.clock import ClockReading, MonotonicTime
from omni.core.enum import DisplayPriority, PanelProfile, StrEnumMixin
from omni.core.ids import LeagueScopedId

__all__ = ["InterleavedCardQueue", "Pick", "SelectionReason"]

# How much more airtime each band earns (multiplies time-since-last-shown).
_BAND_WEIGHT: dict[DisplayPriority, int] = {
    DisplayPriority.BACKGROUND: 1,
    DisplayPriority.NORMAL: 1,
    DisplayPriority.FAVORITE: 2,
    DisplayPriority.HIGH_LEVERAGE: 3,
    DisplayPriority.ALERT: 4,
    DisplayPriority.STICKY: 5,
}


class SelectionReason(StrEnumMixin, str, Enum):
    """Why the queue chose the card it did, strongest first."""

    TAKEOVER = "takeover"  # a BURST card that hasn't aired yet
    REMINDER = "reminder"  # a RECURRING card on its cooldown beat
    ROTATION = "rotation"  # the most overdue game, then its most overdue card

    @property
    def rank(self) -> int:
        """How strongly it claims the board: a takeover outranks a reminder, which outranks rotation."""
        return _RANK[self]


_RANK = {SelectionReason.TAKEOVER: 2, SelectionReason.REMINDER: 1, SelectionReason.ROTATION: 0}


@dataclass(frozen=True, slots=True)
class Pick:
    """The queue's choice, and why."""

    card: ScoreboardCard[Any]
    reason: SelectionReason


class InterleavedCardQueue:
    """Holds deduped cards and says which should air next, fairly and by priority.

    Cards are heterogeneous (different payload types), so they are held as
    `ScoreboardCard[Any]`; the renderer dispatches on the concrete payload.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._cards: dict[str, ScoreboardCard[Any]] = {}  # DedupeKey.raw -> card
        self._tick = 0
        self._group_last_shown: dict[LeagueScopedId, int] = {}  # contest id -> tick
        self._card_last_shown: dict[str, int] = {}  # DedupeKey.raw -> tick; a key here has aired
        self._reminded_at: dict[str, MonotonicTime] = {}  # DedupeKey.raw -> when a RECURRING card last aired
        self._recurring_count: dict[str, int] = {}  # DedupeKey.raw -> times a RECURRING card has aired

    def ingest(self, card: ScoreboardCard[Any]) -> None:
        """Add a card, or replace the one sharing its `DedupeKey` (a fresh update)."""
        with self._lock:
            self._cards[card.dedupe_key.raw] = card

    def ingest_all(self, cards: Iterable[ScoreboardCard[Any]]) -> None:
        for card in cards:
            self.ingest(card)

    def remove(self, dedupe_key: str) -> None:
        with self._lock:
            self._cards.pop(dedupe_key, None)
            self._forget_orphans()

    def prune(self, now: datetime) -> list[ScoreboardCard[Any]]:
        """Drop cards whose `expires_at` has passed; returns those that expired without ever airing."""
        with self._lock:
            expired = [
                key
                for key, card in self._cards.items()
                if card.timing.expires_at is not None and now >= card.timing.expires_at
            ]
            unseen = [self._cards[key] for key in expired if key not in self._card_last_shown]
            for key in expired:
                del self._cards[key]
            if expired:
                self._forget_orphans()
            return unseen

    def __len__(self) -> int:
        with self._lock:
            return len(self._cards)

    def card(self, dedupe_key: str) -> ScoreboardCard[Any] | None:
        """The newest version of a card, or None once it has left the queue."""
        with self._lock:
            return self._cards.get(dedupe_key)

    def contests(self) -> frozenset[LeagueScopedId]:
        """The contests with a card in the queue."""
        with self._lock:
            return frozenset(card.contest.id for card in self._cards.values())

    def aired(self, dedupe_key: str) -> bool:
        """Whether a card still in the queue has aired."""
        with self._lock:
            return dedupe_key in self._card_last_shown

    def eligible(self, now: datetime, profile: PanelProfile) -> list[ScoreboardCard[Any]]:
        """Cards that may show right now on `profile` (available, in-window, supported)."""
        with self._lock:
            return [card for card in self._cards.values() if self._showable(card, now, profile)]

    def peek(
        self, at: ClockReading, profile: PanelProfile, *, takeovers: bool = True, skip: frozenset[str] = frozenset()
    ) -> Pick | None:
        """What should air next, with the reason; None if nothing is eligible. Records nothing.

        With ``takeovers`` off the board wants a turn of its own: BURST cards, aired or not, wait for
        another card to air first, unless nothing else is eligible. Cards whose dedupe keys are in
        ``skip`` (ones the display can't draw) are passed over, and hold up nothing ordered behind them.
        """
        with self._lock:
            eligible = [card for card in self.eligible(at.utc, profile) if card.dedupe_key.raw not in skip]
            pool = self._usable(self._in_order(eligible), at.utc)
            if not takeovers:
                pool = [card for card in pool if card.attention.mode is not AttentionMode.BURST] or pool
            if not pool:
                return None
            bursting = [card for card in pool if takeovers and self._is_bursting(card)]
            due_recurring = [card for card in pool if self._is_recurring_due(card, at.monotonic)]
            if bursting:
                return Pick(max(bursting, key=self._card_rank), SelectionReason.TAKEOVER)
            if due_recurring:
                return Pick(max(due_recurring, key=self._card_rank), SelectionReason.REMINDER)
            # Two-level rotation, with not-yet-due RECURRING cards held back.
            rotating = [card for card in pool if card.attention.mode is not AttentionMode.RECURRING]
            if not rotating:
                return None  # only RECURRING cards remain and none is due yet: nothing to show
            groups: dict[LeagueScopedId, list[ScoreboardCard[Any]]] = defaultdict(list)
            for card in rotating:
                groups[card.contest.id].append(card)
            chosen_group = max(groups, key=lambda gid: self._group_rank(gid, groups[gid]))
            return Pick(max(groups[chosen_group], key=self._card_rank), SelectionReason.ROTATION)

    def book(self, pick: Pick, at: ClockReading) -> None:
        """Record that ``pick`` aired at ``at``: its turn in rotation, and a reminder's cooldown and count."""
        card = pick.card
        key = card.dedupe_key.raw
        with self._lock:
            if key not in self._cards:
                return  # it left the queue while it went up; there's nothing to remember it by
            self._tick += 1
            self._group_last_shown[card.contest.id] = self._tick
            self._card_last_shown[key] = self._tick
            if card.attention.mode is AttentionMode.RECURRING:
                self._reminded_at[key] = at.monotonic
                self._recurring_count[key] = self._recurring_count.get(key, 0) + 1

    def pick(self, at: ClockReading, profile: PanelProfile) -> Pick | None:
        """Prune what has expired, then choose and book the next card at once (as if it aired at ``at``)."""
        with self._lock:
            self.prune(at.utc)
            pick = self.peek(at, profile)
            if pick is not None:
                self.book(pick, at)
            return pick

    def next_card(self, at: ClockReading, profile: PanelProfile) -> ScoreboardCard[Any] | None:
        """`pick`, for just the card."""
        pick = self.pick(at, profile)
        return pick.card if pick is not None else None

    def next_deadline(self, at: ClockReading, profile: PanelProfile) -> MonotonicTime | None:
        """When eligibility next changes by itself, as of ``at``: a card becomes available or expires, its
        window grows too short to use, or a reminder comes due. None if no card will change on its own."""
        deadlines: list[MonotonicTime] = []
        with self._lock:
            for key, card in self._cards.items():
                if not card.layout_support.supports(profile):
                    continue
                timing = card.timing
                instants = [timing.available_at]
                if timing.expires_at is not None:
                    instants += [timing.expires_at, timing.expires_at - timing.min_display.as_timedelta()]
                deadlines.extend(at.monotonic + (instant - at.utc) for instant in instants if instant > at.utc)
                reminded = self._reminded_at.get(key)
                if reminded is not None:
                    due = reminded + card.attention.cooldown.as_timedelta()
                    if due > at.monotonic:
                        deadlines.append(due)
        return min(deadlines, default=None)

    def _showable(self, card: ScoreboardCard[Any], now: datetime, profile: PanelProfile) -> bool:
        return card.timing.is_available(now) and card.layout_support.supports(profile)

    def _in_order(self, pool: list[ScoreboardCard[Any]]) -> list[ScoreboardCard[Any]]:
        """The cards whose turn it is: none waiting on an eligible card that hasn't aired, and of the finals
        that haven't aired, only the one whose game ended first."""
        waiting = {card.dedupe_key for card in pool if card.dedupe_key.raw not in self._card_last_shown}
        finals = sorted(
            (card for card in pool if card.kind is CardKind.FINAL and card.dedupe_key in waiting),
            key=lambda card: (card.timing.available_at, card.dedupe_key.raw),
        )
        next_final = finals[0].dedupe_key if finals else None
        return [
            card
            for card in pool
            if not any(before in waiting for before in card.after)
            and not (card.kind is CardKind.FINAL and card.dedupe_key in waiting and card.dedupe_key != next_final)
        ]

    @staticmethod
    def _usable(pool: list[ScoreboardCard[Any]], now: datetime) -> list[ScoreboardCard[Any]]:
        """The cards with time left for at least their minimum before they expire."""
        return [
            card
            for card in pool
            if card.timing.expires_at is None or card.timing.expires_at - now >= card.timing.min_display.as_timedelta()
        ]

    def _is_bursting(self, card: ScoreboardCard[Any]) -> bool:
        return card.attention.mode is AttentionMode.BURST and card.dedupe_key.raw not in self._card_last_shown

    def _is_recurring_due(self, card: ScoreboardCard[Any], now: MonotonicTime) -> bool:
        # A RECURRING card is due on its first sight, then once per `cooldown`, until it
        # has surfaced `max_repeats` times (None = unbounded while the card lives).
        policy = card.attention
        if policy.mode is not AttentionMode.RECURRING:
            return False
        key = card.dedupe_key.raw
        if policy.max_repeats is not None and self._recurring_count.get(key, 0) >= policy.max_repeats:
            return False
        reminded = self._reminded_at.get(key)
        return reminded is None or now.since(reminded) >= policy.cooldown.as_timedelta()

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
        self._reminded_at = {key: at for key, at in self._reminded_at.items() if key in live_keys}
        self._recurring_count = {key: c for key, c in self._recurring_count.items() if key in live_keys}
