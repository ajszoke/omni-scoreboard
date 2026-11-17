"""InterleavedCardQueue: fair, two-level rotation across contests and their cards.

The board's selection policy. Cards (already scored and TV-delayed) are staged by the decision thread,
deduped by `DedupeKey`, and once a refresh is done `publish` makes them the board's as one numbered
`CardView`. The board selects against the latest view, never against half a refresh: `peek(at, profile)`
says what should air next, and `book` records that it did. The board books a card when its first frame
is committed, not when it is chosen, so a card that never reached the panel spends none of its turn,
its rotation or its reminder; and only if the latest view still holds the card as it was drawn, so a
card that changed or left while it was drawn books nothing.

A card's window runs on the monotonic clock. Its timing says, in UTC, when it may first air and when it
expires; the queue pins both to the monotonic clock when it takes the card in (`CardWindow`), as that clock
reading places them, and judges the window against the monotonic clock from then on. So a step of the wall
clock neither takes a card off the board until the wall clock catches up nor cuts its window short. A card
staged again with the same timing keeps its pins; new timing is pinned anew. The card keeps its UTC instants,
for the trace and the display.

Selection filters first, then chooses:
1. **Eligible:** available, not expired, and supported on the profile.
2. **A usable window:** a card about to expire, with less time left than its minimum, is passed over.
3. **In order:** a card waits for the cards it must follow while they can still air
   (`ScoreboardCard.after`, a game's decisive play before its final). Finals that haven't aired go in
   the order their games ended (`ScoreboardCard.ended`): only the earliest is eligible.

Then it chooses, two-level, so nothing starves:
1. **Group:** the most-overdue *rotation group* (a contest), weighted by its highest-priority card, so
   a favorite or a high-leverage game gets more airtime while a quiet one is never buried.
2. **Card:** within that group, the most-overdue *card*, weighted by its band, so siblings for one game
   (LIVE, BIG_PLAY, FINAL, ...) share airtime fairly.

Attention is **bounded and separate from priority**. A `BURST` card takes over once, ahead of
rotation, the first time it airs; after that it rotates beside the other cards, but never alone. The
board bounds how much takeover airtime runs back to back, and then asks for a *base* card (one that is
neither a takeover nor a reminder) to get a turn of its own. A `RECURRING` card (an active no-hitter)
resurfaces on its `cooldown` beat, counted on the monotonic clock from when it last aired, ahead of
rotation and capped by `max_repeats`; in between it is held back, so it reminds periodically rather
than constantly. Display priority alone never grants a permanent takeover.

A refresh drops what can no longer air (`prune`): a card past its expiry, and one that never aired and
has less time left than its minimum. Each that never aired is reported with why it missed (`Missed`).

The queue is thread-safe: the pipeline stages and publishes on the decision thread while the board
reads views and books on the frame thread.
"""

from __future__ import annotations

import threading
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import Any, Iterable

from omni.cards.attention import AttentionMode
from omni.cards.base import CardKind, DisplayTiming, ScoreboardCard
from omni.core.clock import ClockReading, MonotonicTime
from omni.core.enum import DisplayPriority, PanelProfile, StrEnumMixin
from omni.core.ids import LeagueScopedId

__all__ = ["CardView", "CardWindow", "InterleavedCardQueue", "MissReason", "Missed", "Pick", "SelectionReason"]

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


class MissReason(StrEnumMixin, str, Enum):
    """Why a card left the queue without ever airing."""

    TOO_LATE = "too late to start"  # it had less time left than its minimum
    EXPIRED = "expired before it aired"  # its expiry passed
    UNDRAWN = "failed to draw"  # the display couldn't draw it


@dataclass(frozen=True, slots=True)
class Missed:
    """A card that left without ever airing, and why."""

    card: ScoreboardCard[Any]
    reason: MissReason


@dataclass(frozen=True, slots=True)
class Pick:
    """The queue's choice, and why."""

    card: ScoreboardCard[Any]
    reason: SelectionReason


@dataclass(frozen=True, slots=True)
class CardWindow:
    """When a card may air, on the monotonic clock: pinned from its UTC timing as the clock read when the queue
    took the card in, so a later step of the wall clock moves none of it."""

    timing: DisplayTiming  # the timing it was pinned from
    available: MonotonicTime  # the earliest it may air
    expires: MonotonicTime | None = None  # gone by then, on air or not; None if it never expires

    @classmethod
    def pin(cls, timing: DisplayTiming, at: ClockReading) -> CardWindow:
        """``timing``'s instants placed on the monotonic clock as ``at`` reads both."""

        def place(instant: datetime) -> MonotonicTime:
            return at.monotonic + (instant - at.utc)

        expires = place(timing.expires_at) if timing.expires_at is not None else None
        return cls(timing, place(timing.available_at), expires)

    @property
    def latest_start(self) -> MonotonicTime | None:
        """The last moment it can start and still have its minimum before it expires; None if it never expires."""
        if self.expires is None:
            return None
        return self.expires + (-self.timing.min_display.as_timedelta())

    def is_open(self, now: MonotonicTime) -> bool:
        """Whether it may show at ``now``: available, and not yet expired."""
        return now >= self.available and (self.expires is None or now < self.expires)

    def is_usable(self, now: MonotonicTime) -> bool:
        """Whether it has time left at ``now`` for at least its minimum before it expires."""
        return self.expires is None or self.expires.since(now) >= self.timing.min_display.as_timedelta()


@dataclass(frozen=True, slots=True, eq=False)
class CardView:
    """The cards one refresh published, numbered: what the board selects from until the next one."""

    generation: int
    cards: Mapping[str, ScoreboardCard[Any]]  # DedupeKey.raw -> card; read-only
    windows: Mapping[str, CardWindow]  # DedupeKey.raw -> its card's window; read-only

    def card(self, dedupe_key: str) -> ScoreboardCard[Any] | None:
        """The card under ``dedupe_key`` in this view, or None if it isn't in it."""
        return self.cards.get(dedupe_key)

    def successor(self, dedupe_key: str) -> ScoreboardCard[Any] | None:
        """The card in this view that takes the place of ``dedupe_key`` on the board (a final, its game's live
        card), if there is one."""
        replacing = (
            card for card in self.cards.values() if card.replaces is not None and card.replaces.raw == dedupe_key
        )
        return next(replacing, None)


_EMPTY = CardView(0, MappingProxyType({}), MappingProxyType({}))


class InterleavedCardQueue:
    """Holds deduped cards and says which should air next, fairly and by priority.

    Cards are heterogeneous (different payload types), so they are held as
    `ScoreboardCard[Any]`; the renderer dispatches on the concrete payload.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._cards: dict[str, ScoreboardCard[Any]] = {}  # staged: DedupeKey.raw -> card, until published
        self._windows: dict[str, CardWindow] = {}  # DedupeKey.raw -> a staged card's window
        self._published = _EMPTY
        self._tick = 0
        self._group_last_shown: dict[LeagueScopedId, int] = {}  # contest id -> tick
        self._card_last_shown: dict[str, int] = {}  # DedupeKey.raw -> tick; a key here has aired
        self._reminded_at: dict[str, MonotonicTime] = {}  # DedupeKey.raw -> when a RECURRING card last aired
        self._recurring_count: dict[str, int] = {}  # DedupeKey.raw -> times a RECURRING card has aired
        self._undrawn: set[str] = set()  # DedupeKey.raw of cards the display couldn't draw

    # --- the decision thread: staging, then publishing -----------------------------------

    def ingest(self, card: ScoreboardCard[Any], at: ClockReading) -> None:
        """Stage a card as of ``at``, or replace the one sharing its `DedupeKey` (a fresh update).

        Its window is pinned to the monotonic clock as ``at`` places it; one staged again with the same timing
        keeps the window it had.
        """
        key = card.dedupe_key.raw
        with self._lock:
            window = self._windows.get(key)
            if window is None or window.timing != card.timing:
                self._windows[key] = CardWindow.pin(card.timing, at)
            self._cards[key] = card
            self._undrawn.discard(key)  # a new version gets a fresh chance to draw

    def ingest_all(self, cards: Iterable[ScoreboardCard[Any]], at: ClockReading) -> None:
        for card in cards:
            self.ingest(card, at)

    def remove(self, dedupe_key: str) -> None:
        with self._lock:
            self._cards.pop(dedupe_key, None)
            self._windows.pop(dedupe_key, None)

    def prune(self, at: ClockReading) -> list[Missed]:
        """Unstage what can no longer air at ``at``: cards past their expiry, and cards that never aired with
        less time left than their minimum. Returns those that never aired, with why each missed."""
        now = at.monotonic
        with self._lock:
            missed: list[Missed] = []
            for key, card in list(self._cards.items()):
                window = self._windows[key]
                if window.expires is None:
                    continue
                aired = key in self._card_last_shown
                expired = now >= window.expires
                if not expired and (aired or window.is_usable(now)):
                    continue  # it can still start, or it has aired and stays until its expiry
                del self._cards[key], self._windows[key]
                if not aired:
                    why = MissReason.EXPIRED if expired else MissReason.TOO_LATE
                    missed.append(Missed(card, MissReason.UNDRAWN if key in self._undrawn else why))
            return missed

    def next_miss(self, at: ClockReading) -> MonotonicTime | None:
        """When a staged card that hasn't aired next runs out of time to air, after ``at``, on the monotonic
        clock; None if none will."""
        with self._lock:
            instants = [
                window.latest_start for key, window in self._windows.items() if key not in self._card_last_shown
            ]
        return min((instant for instant in instants if instant is not None and instant > at.monotonic), default=None)

    def publish(self) -> CardView:
        """Make the staged cards the board's, as one new numbered view. What's remembered of cards no longer
        in it is forgotten."""
        with self._lock:
            generation = self._published.generation + 1
            cards, windows = MappingProxyType(dict(self._cards)), MappingProxyType(dict(self._windows))
            self._published = CardView(generation, cards, windows)
            self._forget_orphans()
            return self._published

    def __len__(self) -> int:
        with self._lock:
            return len(self._cards)

    def card(self, dedupe_key: str) -> ScoreboardCard[Any] | None:
        """The newest staged version of a card, or None once it has left the queue."""
        with self._lock:
            return self._cards.get(dedupe_key)

    def contests(self) -> frozenset[LeagueScopedId]:
        """The contests with a staged card."""
        with self._lock:
            return frozenset(card.contest.id for card in self._cards.values())

    def eligible(self, at: ClockReading, profile: PanelProfile) -> list[ScoreboardCard[Any]]:
        """Staged cards that may show at ``at`` on `profile` (available, in-window, supported)."""
        with self._lock:
            return [
                card for key, card in self._cards.items() if _showable(card, self._windows[key], at.monotonic, profile)
            ]

    # --- the frame thread: selecting against a view, and booking ------------------------

    @property
    def published(self) -> CardView:
        """The latest view published."""
        with self._lock:
            return self._published

    def aired(self, dedupe_key: str) -> bool:
        """Whether a card has aired (and is still in the latest view)."""
        with self._lock:
            return dedupe_key in self._card_last_shown

    def peek(
        self,
        at: ClockReading,
        profile: PanelProfile,
        *,
        view: CardView | None = None,
        recovery: bool = False,
        prefer: str | None = None,
        skip: frozenset[str] = frozenset(),
    ) -> Pick | None:
        """What should air next from ``view`` (the latest published, by default), with the reason; None if
        nothing is eligible. Records nothing.

        With ``recovery`` the board owes a base card a turn of its own: while one can air, takeovers and
        reminders wait. ``prefer`` names a card that takes its rotation turn ahead of the others if it can
        air (a final, in its live card's place). Cards whose dedupe keys are in ``skip`` (ones the display
        can't draw) are passed over, and hold up nothing ordered behind them.
        """
        with self._lock:
            view = view if view is not None else self._published
            now, windows = at.monotonic, view.windows
            usable = [
                card
                for key, card in view.cards.items()
                if key not in skip and _showable(card, windows[key], now, profile) and windows[key].is_usable(now)
            ]
            pool = self._in_order(usable)
            if recovery:
                pool = [card for card in pool if _is_base(card)] or pool
            bursting = [card for card in pool if self._is_bursting(card)]
            due_recurring = [card for card in pool if self._is_recurring_due(card, at.monotonic)]
            if bursting:
                return Pick(max(bursting, key=self._card_rank), SelectionReason.TAKEOVER)
            if due_recurring:
                return Pick(max(due_recurring, key=self._card_rank), SelectionReason.REMINDER)
            # Two-level rotation, with not-yet-due RECURRING cards held back, and spent takeovers only beside
            # a base card: alone, a takeover that has had its turn leaves the board.
            rotating = [card for card in pool if card.attention.mode is not AttentionMode.RECURRING]
            if not any(_is_base(card) for card in rotating):
                return None
            preferred = [card for card in rotating if card.dedupe_key.raw == prefer]
            if preferred:
                return Pick(preferred[0], SelectionReason.ROTATION)
            groups: dict[LeagueScopedId, list[ScoreboardCard[Any]]] = defaultdict(list)
            for card in rotating:
                groups[card.contest.id].append(card)
            chosen_group = max(groups, key=lambda gid: self._group_rank(gid, groups[gid]))
            return Pick(max(groups[chosen_group], key=self._card_rank), SelectionReason.ROTATION)

    def book(self, pick: Pick, at: ClockReading) -> bool:
        """Record that ``pick`` aired at ``at``: its turn in rotation, and a reminder's cooldown and count.

        Only if the latest view still holds the very card picked: one that changed or left while it went up
        books nothing, since what aired isn't what the board now holds. Returns whether it was booked.
        """
        card = pick.card
        key = card.dedupe_key.raw
        with self._lock:
            if self._published.card(key) is not card:
                return False
            self._tick += 1
            self._group_last_shown[card.contest.id] = self._tick
            self._card_last_shown[key] = self._tick
            if card.attention.mode is AttentionMode.RECURRING:
                self._reminded_at[key] = at.monotonic
                self._recurring_count[key] = self._recurring_count.get(key, 0) + 1
            return True

    def undrawn(self, card: ScoreboardCard[Any]) -> None:
        """The display couldn't draw ``card``: if it never airs, that's why it missed."""
        with self._lock:
            self._undrawn.add(card.dedupe_key.raw)

    def pick(self, at: ClockReading, profile: PanelProfile) -> Pick | None:
        """Prune and publish what's staged, then choose and book the next card at once (as if it aired at
        ``at``)."""
        with self._lock:
            self.prune(at)
            self.publish()
            pick = self.peek(at, profile)
            if pick is not None:
                self.book(pick, at)
            return pick

    def next_card(self, at: ClockReading, profile: PanelProfile) -> ScoreboardCard[Any] | None:
        """`pick`, for just the card."""
        pick = self.pick(at, profile)
        return pick.card if pick is not None else None

    def next_deadline(
        self, at: ClockReading, profile: PanelProfile, *, view: CardView | None = None
    ) -> MonotonicTime | None:
        """When eligibility in ``view`` (the latest published, by default) next changes by itself, as of ``at``:
        a card becomes available or expires, its window grows too short to use, or a reminder comes due. None
        if no card will change on its own."""
        deadlines: list[MonotonicTime] = []
        with self._lock:
            view = view if view is not None else self._published
            for key, card in view.cards.items():
                if not card.layout_support.supports(profile):
                    continue
                window = view.windows[key]
                instants = (window.available, window.expires, window.latest_start)
                deadlines.extend(instant for instant in instants if instant is not None and instant > at.monotonic)
                reminded = self._reminded_at.get(key)
                if reminded is not None:
                    due = reminded + card.attention.cooldown.as_timedelta()
                    if due > at.monotonic:
                        deadlines.append(due)
        return min(deadlines, default=None)

    def _in_order(self, pool: list[ScoreboardCard[Any]]) -> list[ScoreboardCard[Any]]:
        """The cards whose turn it is: none waiting on a card in the pool that hasn't aired, and of the finals
        that haven't aired, only the one whose game ended first."""
        waiting = {card.dedupe_key for card in pool if card.dedupe_key.raw not in self._card_last_shown}
        finals = sorted(
            (card for card in pool if card.kind is CardKind.FINAL and card.dedupe_key in waiting),
            key=lambda card: (_ended(card), card.dedupe_key.raw),
        )
        next_final = finals[0].dedupe_key if finals else None
        return [
            card
            for card in pool
            if not any(before in waiting for before in card.after)
            and not (card.kind is CardKind.FINAL and card.dedupe_key in waiting and card.dedupe_key != next_final)
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
        # Drop what's remembered of cards and groups no longer in the published view (no slow leak).
        cards = self._published.cards
        live_groups = {card.contest.id for card in cards.values()}
        self._card_last_shown = {key: tick for key, tick in self._card_last_shown.items() if key in cards}
        self._group_last_shown = {gid: tick for gid, tick in self._group_last_shown.items() if gid in live_groups}
        self._reminded_at = {key: at for key, at in self._reminded_at.items() if key in cards}
        self._recurring_count = {key: c for key, c in self._recurring_count.items() if key in cards}
        self._undrawn &= set(cards) | set(self._cards)


def _showable(card: ScoreboardCard[Any], window: CardWindow, now: MonotonicTime, profile: PanelProfile) -> bool:
    """Whether ``card`` may show at ``now`` on ``profile``: inside its window, and supported."""
    return window.is_open(now) and card.layout_support.supports(profile)


def _is_base(card: ScoreboardCard[Any]) -> bool:
    """A card in the board's own rotation: neither a takeover nor a reminder."""
    return card.attention.mode not in (AttentionMode.BURST, AttentionMode.RECURRING)


def _ended(card: ScoreboardCard[Any]) -> datetime:
    """When a final's game ended, for its order; one that doesn't say goes by when it became available."""
    return card.ended.at if card.ended is not None else card.timing.available_at
