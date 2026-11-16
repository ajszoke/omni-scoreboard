"""PollPlan: what to ask each source, when, and what it last answered.

The decision pass no longer fetches its slates, game feeds or win probabilities. It reads their answers
from the plan, and each read also tells the plan what is still wanted. (A pregame card's details and
odds are still fetched inline, on their own slow pacing and under their requests' timeouts, until they
join the plan.) Workers (`omni.app.poller`) ask whatever the plan says is due, and file
the answers back. The plan keeps the rules in one place:
- **One ask in flight per key.** A slow source never stacks a second request on the first. An ask
  with no answer within its source's `answer_within` counts as failed, so a hung request can't hold its
  key forever; whatever it brings back late is ignored.
- **Priority cadence within quota.** A key is due again after its source's cadence for its priority
  (a favorite's live game sooner than another game's). A source's per-minute quota is shared out in
  priority order, so when asks queue, the most important go first.
- **Bounded retries.** After a failure a key is retried on a short schedule a few times. After that it
  waits its normal cadence, so a source that is down isn't hammered.
- **Last good, until it expires.** A failed ask doesn't erase the last answer. That answer stands for
  `keep_for`, so a blip doesn't drop a game from the board, and an outage eventually does.
- **Forgetting.** A key that no decision has asked about for `forget_after` is dropped.

Cadence, retries and expiry run on the clock's monotonic reading. Each answer is stamped with both
readings when it arrives, so the decision pass can date what it shows by receipt.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import Enum, IntEnum
from typing import Any, Generic, TypeVar

from omni.core.clock import ClockReading, MonotonicTime
from omni.core.enum import StrEnumMixin
from omni.core.ids import LeagueScopedId

__all__ = ["Answer", "Ask", "PollKey", "PollPlan", "PollPolicy", "PollPriority", "PollSource"]

T = TypeVar("T")

_HISTORY = 6  # answers kept per key, newest last, for reads gated to an earlier receipt


class PollSource(StrEnumMixin, str, Enum):
    """A kind of thing the board asks a provider for."""

    SLATE = "slate"  # one local date's schedule
    GAME_FEED = "game_feed"  # one game's live feed
    MARKET_WIN_PROBABILITY = "market_win_probability"  # one game's market price
    MODEL_WIN_PROBABILITY = "model_win_probability"  # one game's model probability, the fallback


class PollPriority(IntEnum):
    """How much a key matters right now; higher goes first and, with a focus cadence, sooner."""

    BACKGROUND = 0  # a final still settling, yesterday's late games
    NORMAL = 1
    FOCUS = 2  # a favorite's live game


@dataclass(frozen=True, slots=True)
class PollPolicy:
    """How one source is asked: cadence, retries, how long an answer stands, and its quota."""

    every: timedelta
    keep_for: timedelta  # how long the last good answer stands
    focus_every: timedelta | None = None  # a faster cadence for focus keys
    background_every: timedelta | None = None  # a slower cadence for background keys
    retries: tuple[timedelta, ...] = (timedelta(seconds=2), timedelta(seconds=5), timedelta(seconds=15))
    per_minute: int | None = None  # this source's quota; None for none
    answer_within: timedelta = timedelta(seconds=30)  # an ask still out after this has failed

    def __post_init__(self) -> None:
        spans = (self.every, self.keep_for, self.focus_every, self.background_every, self.answer_within, *self.retries)
        if any(span is not None and span <= timedelta(0) for span in spans):
            raise ValueError("a poll policy's spans must be positive")
        if self.per_minute is not None and self.per_minute < 1:
            raise ValueError("a quota must allow at least one ask a minute")

    def cadence(self, priority: PollPriority) -> timedelta:
        if priority is PollPriority.FOCUS and self.focus_every is not None:
            return self.focus_every
        if priority is PollPriority.BACKGROUND and self.background_every is not None:
            return self.background_every
        return self.every


@dataclass(frozen=True, slots=True)
class PollKey(Generic[T]):
    """What is asked: a source, about one subject (a game, or a date's slate)."""

    source: PollSource
    subject: LeagueScopedId | date


@dataclass(frozen=True, slots=True)
class Answer(Generic[T]):
    """A source's answer, stamped when it arrived."""

    value: T
    received: ClockReading


@dataclass(frozen=True, slots=True)
class Ask:
    """One ask the plan has handed out: run ``fetch`` as of a UTC instant, then file the result."""

    key: PollKey[Any]
    fetch: Callable[[datetime], Any]
    priority: PollPriority
    number: int = 0  # which ask of its key this is: an answer to an ask given up on is ignored


@dataclass(slots=True)
class _Entry:
    fetch: Callable[[datetime], Any]
    priority: PollPriority
    wanted: MonotonicTime  # when a decision last asked about it
    due: MonotonicTime
    in_flight: bool = False
    asked: MonotonicTime | None = None  # when the ask in flight was handed out
    asks: int = 0  # asks handed out (or given up on) so far: the number of the one in flight
    failures: int = 0
    error: str | None = None
    answers: deque[Answer[Any]] = field(default_factory=lambda: deque(maxlen=_HISTORY))


@dataclass(slots=True)
class _Bucket:
    """A per-minute quota as a token bucket that refills continuously."""

    per_minute: int
    tokens: float
    refilled: MonotonicTime

    def take(self, now: MonotonicTime) -> bool:
        elapsed = now.since(self.refilled).total_seconds()
        self.tokens = min(float(self.per_minute), self.tokens + elapsed * self.per_minute / 60.0)
        self.refilled = now
        if self.tokens < 1.0:
            return False
        self.tokens -= 1.0
        return True


class PollPlan:
    """The schedule of asks, the answers they brought, and the rules between the two. Thread-safe."""

    def __init__(
        self, policies: Mapping[PollSource, PollPolicy], *, forget_after: timedelta = timedelta(minutes=2)
    ) -> None:
        self._policies = dict(policies)
        self._forget_after = forget_after
        self._entries: dict[PollKey[Any], _Entry] = {}
        self._buckets: dict[PollSource, _Bucket] = {}
        self._lock = threading.Lock()

    def want(
        self, key: PollKey[T], fetch: Callable[[datetime], T], *, priority: PollPriority, at: ClockReading
    ) -> None:
        """A decision needs this key: keep asking it, at this priority. A new key is due at once."""
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                self._entries[key] = _Entry(fetch=fetch, priority=priority, wanted=at.monotonic, due=at.monotonic)
                return
            if priority > entry.priority and not entry.in_flight and entry.answers:
                # Promoted: due by the faster cadence, counted from the last answer.
                sooner = entry.answers[-1].received.monotonic + self._policy(key).cadence(priority)
                entry.due = min(entry.due, sooner)
            entry.fetch, entry.priority, entry.wanted = fetch, priority, at.monotonic

    def latest(self, key: PollKey[T], at: ClockReading) -> Answer[T] | None:
        """The newest answer, unless it has outlived its source's `keep_for`."""
        with self._lock:
            entry = self._entries.get(key)
            if entry is None or not entry.answers:
                return None
            answer: Answer[T] = entry.answers[-1]
            return answer if self._standing(key, answer, at) else None

    def latest_by(self, key: PollKey[T], received_by: datetime, at: ClockReading) -> Answer[T] | None:
        """The newest answer that arrived no later than ``received_by``, if it still stands."""
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            for answer in reversed(entry.answers):
                if answer.received.utc <= received_by:
                    return answer if self._standing(key, answer, at) else None
            return None

    def last_error(self, key: PollKey[Any]) -> str | None:
        """Why the last ask of this key failed, while it's still failing."""
        with self._lock:
            entry = self._entries.get(key)
            return entry.error if entry is not None else None

    def take_due(self, at: ClockReading) -> list[Ask]:
        """Hand out every ask that is due, most important first, as far as each quota allows."""
        now = at.monotonic
        with self._lock:
            self._forget(now)
            for key, entry in self._entries.items():
                within = self._policy(key).answer_within
                if entry.in_flight and entry.asked is not None and now >= entry.asked + within:
                    entry.asks += 1  # given up on: whatever it brings back late is ignored
                    self._give_up(key, entry, f"no answer within {within.total_seconds():g}s", at)
            due = [(key, entry) for key, entry in self._entries.items() if not entry.in_flight and entry.due <= now]
            due.sort(key=lambda item: (-item[1].priority, item[1].due.seconds))
            asks: list[Ask] = []
            for key, entry in due:
                if not self._spend(key.source, now):
                    continue  # over quota: it stays due, and goes first when a token frees up
                entry.in_flight, entry.asked = True, now
                entry.asks += 1
                asks.append(Ask(key=key, fetch=entry.fetch, priority=entry.priority, number=entry.asks))
            return asks

    def answered(self, key: PollKey[T], value: T, received: ClockReading, *, ask: int | None = None) -> None:
        """File an answer; ``ask`` is the number of the ask it answers (an answer to one given up on is
        ignored). None files it regardless."""
        with self._lock:
            entry = self._entries.get(key)
            if entry is None or (ask is not None and ask != entry.asks):
                return  # forgotten while in flight, or given up on
            entry.in_flight, entry.asked, entry.failures, entry.error = False, None, 0, None
            entry.answers.append(Answer(value, received))
            entry.due = received.monotonic + self._policy(key).cadence(entry.priority)

    def failed(self, key: PollKey[Any], error: str, at: ClockReading, *, ask: int | None = None) -> None:
        """File a failure; ``ask`` as for `answered`."""
        with self._lock:
            entry = self._entries.get(key)
            if entry is None or (ask is not None and ask != entry.asks):
                return  # forgotten while in flight, or given up on
            self._give_up(key, entry, error, at)

    def next_due(self) -> MonotonicTime | None:
        """When the next ask falls due, or the next one in flight is given up on; the poller's next deadline."""
        with self._lock:
            waiting = [
                (
                    entry.asked + self._policy(key).answer_within
                    if entry.in_flight and entry.asked is not None
                    else entry.due
                )
                for key, entry in self._entries.items()
            ]
            return min(waiting) if waiting else None

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def _give_up(self, key: PollKey[Any], entry: _Entry, error: str, at: ClockReading) -> None:
        """The ask in flight failed: retry on the short schedule, then at the normal cadence."""
        policy = self._policy(key)
        entry.in_flight, entry.asked, entry.error = False, None, error
        entry.failures += 1
        retry = policy.retries[entry.failures - 1] if entry.failures <= len(policy.retries) else None
        entry.due = at.monotonic + (retry if retry is not None else policy.cadence(entry.priority))

    def _policy(self, key: PollKey[Any]) -> PollPolicy:
        return self._policies[key.source]

    def _standing(self, key: PollKey[Any], answer: Answer[Any], at: ClockReading) -> bool:
        return at.monotonic.since(answer.received.monotonic) <= self._policy(key).keep_for

    def _forget(self, now: MonotonicTime) -> None:
        stale = [key for key, entry in self._entries.items() if now.since(entry.wanted) > self._forget_after]
        for key in stale:
            del self._entries[key]

    def _spend(self, source: PollSource, now: MonotonicTime) -> bool:
        per_minute = self._policies[source].per_minute
        if per_minute is None:
            return True
        bucket = self._buckets.get(source)
        if bucket is None:
            bucket = self._buckets[source] = _Bucket(per_minute=per_minute, tokens=float(per_minute), refilled=now)
        return bucket.take(now)
