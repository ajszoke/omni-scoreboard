"""PollPlan: what to ask each source, when, and what it last answered.

The decision pass never fetches. It reads the slates, game feeds, win probabilities, pregame details
and odds from the plan, and each read also tells the plan what is still wanted. Workers
(`omni.app.poller`) ask whatever the plan says is due, and file the answers back. The plan keeps the
rules in one place:
- **One ask out per key.** A slow source never stacks a second request on the first.
- **Leases.** An ask is handed out only when one of its source's workers is free to start it, so none
  waits behind other sources' slow requests. Its lease runs from when it was handed out until a worker
  begins it, and again from then: an ask not started, or not answered, within its source's
  `answer_within` counts as failed, so a hung request can't hold its key forever. A worker checks with
  the plan right before it makes the request (`begin`); an ask given up on, or whose key was dropped,
  is never sent. Whatever a given-up ask brings back late is ignored. Ask numbers run across the whole
  plan and are never reused, so a late answer can't pass for the ask of a key dropped and wanted again.
- **Each source's own workers.** A source runs at most `at_once` asks at a time, counting requests
  still running after they were given up on, so a source that hangs ties up only its own workers.
- **Priority cadence within quota.** A key is due again after its source's cadence for its priority
  (a favorite's live game sooner than another game's). A source's per-minute quota is shared out in
  priority order, so when asks queue, the most important go first.
- **Bounded retries.** After a failure a key is retried on a short schedule a few times. After that it
  waits its normal cadence, so a source that is down isn't hammered. While a key is failing, its latest
  failure says why and when it was filed.
- **Last good, until it expires.** A failed ask doesn't erase the last answer. That answer stands for
  `keep_for`, so a blip doesn't drop a game from the board, and an outage eventually does. Answers that
  still stand are kept, so a read can ask for the newest one that arrived by an earlier moment.
- **Forgetting.** A key that no decision has asked about for `forget_after` is dropped.
- **Keys.** A game's key names the game by its full id; a slate's names its `PollScope` (source, league and
  window), so two leagues' slates for the same date are two keys.

Cadence, retries and expiry run on the clock's monotonic reading. Each answer is stamped with both
readings when it arrives, so the decision pass can date what it shows by receipt.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum, IntEnum
from typing import Any, Generic, TypeVar

from omni.core.clock import ClockReading, MonotonicTime
from omni.core.enum import StrEnumMixin
from omni.core.ids import LeagueScopedId
from omni.providers.base import PollScope

__all__ = ["Answer", "Ask", "Failure", "PollKey", "PollPlan", "PollPolicy", "PollPriority", "PollSource"]

T = TypeVar("T")

_HISTORY = 64  # answers kept per key at most, newest last; older ones go sooner once they no longer stand


class PollSource(StrEnumMixin, str, Enum):
    """A kind of thing the board asks a provider for."""

    SLATE = "slate"  # one scope's schedule: a league's slate for one local date
    GAME_FEED = "game_feed"  # one game's live feed
    MARKET_WIN_PROBABILITY = "market_win_probability"  # one game's market price
    MODEL_WIN_PROBABILITY = "model_win_probability"  # one game's model probability, the fallback
    PREGAME_DETAILS = "pregame_details"  # one upcoming game's records and probable starters
    GAME_LINES = "game_lines"  # one upcoming game's odds


class PollPriority(IntEnum):
    """How much a key matters right now; higher goes first and, with a focus cadence, sooner."""

    BACKGROUND = 0  # a final still settling, yesterday's late games
    NORMAL = 1
    FOCUS = 2  # a favorite's live game


@dataclass(frozen=True, slots=True)
class PollPolicy:
    """How one source is asked: cadence, retries, how long an answer stands, its quota, and its workers."""

    every: timedelta
    keep_for: timedelta  # how long the last good answer stands
    focus_every: timedelta | None = None  # a faster cadence for focus keys
    background_every: timedelta | None = None  # a slower cadence for background keys
    retries: tuple[timedelta, ...] = (timedelta(seconds=2), timedelta(seconds=5), timedelta(seconds=15))
    per_minute: int | None = None  # this source's quota; None for none
    answer_within: timedelta = timedelta(seconds=30)  # an ask not started, or not answered, in this has failed
    at_once: int = 1  # asks of this source that may run at the same time

    def __post_init__(self) -> None:
        spans = (self.every, self.keep_for, self.focus_every, self.background_every, self.answer_within, *self.retries)
        if any(span is not None and span <= timedelta(0) for span in spans):
            raise ValueError("a poll policy's spans must be positive")
        if self.per_minute is not None and self.per_minute < 1:
            raise ValueError("a quota must allow at least one ask a minute")
        if self.at_once < 1:
            raise ValueError("a source needs at least one worker")

    def cadence(self, priority: PollPriority) -> timedelta:
        if priority is PollPriority.FOCUS and self.focus_every is not None:
            return self.focus_every
        if priority is PollPriority.BACKGROUND and self.background_every is not None:
            return self.background_every
        return self.every


@dataclass(frozen=True, slots=True)
class PollKey(Generic[T]):
    """What is asked: a source, about one subject (a game, or a slate's scope)."""

    source: PollSource
    subject: LeagueScopedId | PollScope


@dataclass(frozen=True, slots=True)
class Answer(Generic[T]):
    """A source's answer, stamped when it arrived."""

    value: T
    received: ClockReading


@dataclass(frozen=True, slots=True)
class Failure:
    """Why a key's latest ask failed, and when that failure was filed."""

    error: str
    at: ClockReading


@dataclass(frozen=True, slots=True)
class Ask:
    """One ask the plan has handed out: run ``fetch`` as of a UTC instant, then file the result."""

    key: PollKey[Any]
    fetch: Callable[[datetime], Any]
    priority: PollPriority
    number: int  # this ask's number in the plan, never reused: an answer to an ask given up on is ignored


@dataclass(slots=True)
class _Out:
    """The ask out for a key: its number, and when its lease began (at its hand-out, then at its start)."""

    number: int
    leased: MonotonicTime


@dataclass(slots=True)
class _Entry:
    fetch: Callable[[datetime], Any]
    priority: PollPriority
    wanted: MonotonicTime  # when a decision last asked about it
    due: MonotonicTime
    out: _Out | None = None  # the ask out for it, if one is
    failures: int = 0
    failure: Failure | None = None  # the latest failure, while the key is failing
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
        self._asks = 0  # asks handed out so far, across every key: the last ask's number
        self._lock = threading.Lock()

    def capacity(self) -> dict[PollSource, int]:
        """How many asks of each source may run at the same time."""
        return {source: policy.at_once for source, policy in self._policies.items()}

    def policy(self, source: PollSource) -> PollPolicy:
        """How ``source`` is asked."""
        return self._policies[source]

    def want(
        self, key: PollKey[T], fetch: Callable[[datetime], T], *, priority: PollPriority, at: ClockReading
    ) -> None:
        """A decision needs this key: keep asking it, at this priority. A new key is due at once."""
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                self._entries[key] = _Entry(fetch=fetch, priority=priority, wanted=at.monotonic, due=at.monotonic)
                return
            if priority > entry.priority and entry.out is None and entry.answers:
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

    def latest_by(self, key: PollKey[T], received_by: MonotonicTime, at: ClockReading) -> Answer[T] | None:
        """The newest answer that arrived no later than ``received_by`` (on the monotonic clock), if it still
        stands."""
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            for answer in reversed(entry.answers):
                if answer.received.monotonic <= received_by:
                    return answer if self._standing(key, answer, at) else None
            return None

    def last_error(self, key: PollKey[Any]) -> str | None:
        """Why the last ask of this key failed, while it's still failing."""
        failure = self.failure(key)
        return failure.error if failure is not None else None

    def failure(self, key: PollKey[Any]) -> Failure | None:
        """The last ask of this key's failure, and when it was filed, while it's still failing."""
        with self._lock:
            entry = self._entries.get(key)
            return entry.failure if entry is not None else None

    def take_due(self, at: ClockReading, *, room: Mapping[PollSource, int] | None = None) -> list[Ask]:
        """Hand out every ask that is due, most important first, as far as each quota allows.

        ``room`` is how many more asks of each source its workers can start right now; a due ask beyond it
        stays due, and goes first when a worker frees up. None hands out everything due (asks run one at a
        time, as they do inline).
        """
        now = at.monotonic
        with self._lock:
            self._forget(now)
            for key, entry in self._entries.items():
                if entry.out is not None and now >= entry.out.leased + self._policy(key).answer_within:
                    self._give_up(key, entry, at)
            due = [(key, entry) for key, entry in self._entries.items() if entry.out is None and entry.due <= now]
            due.sort(key=lambda item: (-item[1].priority, item[1].due.seconds))
            handed: dict[PollSource, int] = {}
            asks: list[Ask] = []
            for key, entry in due:
                if room is not None and handed.get(key.source, 0) >= room.get(key.source, 0):
                    continue  # no worker free for its source: it stays due
                if not self._spend(key.source, now):
                    continue  # over quota: it stays due, and goes first when a token frees up
                handed[key.source] = handed.get(key.source, 0) + 1
                self._asks += 1
                entry.out = _Out(number=self._asks, leased=now)
                asks.append(Ask(key=key, fetch=entry.fetch, priority=entry.priority, number=self._asks))
            return asks

    def begin(self, ask: Ask, at: ClockReading) -> bool:
        """A worker is about to make ``ask``'s request: whether it should. Its lease starts again now.

        False when the ask was given up on or its key dropped meanwhile, or when it waited past its lease to
        start (which gives it up here): its answer would only be thrown away.
        """
        with self._lock:
            entry = self._entries.get(ask.key)
            if entry is None or not _answers(entry, ask.number):
                return False
            out = entry.out
            assert out is not None  # an ask that answers its key is the one out
            if at.monotonic >= out.leased + self._policy(ask.key).answer_within:
                self._give_up(ask.key, entry, at)
                return False
            out.leased = at.monotonic
            return True

    def answered(self, key: PollKey[T], value: T, received: ClockReading, *, ask: int | None = None) -> None:
        """File an answer; ``ask`` is the number of the ask it answers (an answer to one given up on is
        ignored). None files it regardless."""
        with self._lock:
            entry = self._entries.get(key)
            if entry is None or (ask is not None and not _answers(entry, ask)):
                return  # forgotten while out, or given up on
            entry.out, entry.failures, entry.failure = None, 0, None
            entry.answers.append(Answer(value, received))
            while len(entry.answers) > 1 and not self._standing(key, entry.answers[0], received):
                entry.answers.popleft()
            entry.due = received.monotonic + self._policy(key).cadence(entry.priority)

    def failed(self, key: PollKey[Any], error: str, at: ClockReading, *, ask: int | None = None) -> None:
        """File a failure; ``ask`` as for `answered`."""
        with self._lock:
            entry = self._entries.get(key)
            if entry is None or (ask is not None and not _answers(entry, ask)):
                return  # forgotten while out, or given up on
            self._failed(key, entry, error, at)

    def next_due(self) -> MonotonicTime | None:
        """When the next ask falls due, or the next one out is given up on; the poller's next deadline."""
        with self._lock:
            waiting = [
                entry.out.leased + self._policy(key).answer_within if entry.out is not None else entry.due
                for key, entry in self._entries.items()
            ]
            return min(waiting) if waiting else None

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def _give_up(self, key: PollKey[Any], entry: _Entry, at: ClockReading) -> None:
        """The ask out ran past its lease: it has failed, and whatever it brings back late is ignored."""
        within = self._policy(key).answer_within
        self._failed(key, entry, f"no answer within {within.total_seconds():g}s", at)

    def _failed(self, key: PollKey[Any], entry: _Entry, error: str, at: ClockReading) -> None:
        """The ask out failed: retry on the short schedule, then at the normal cadence."""
        policy = self._policy(key)
        entry.out, entry.failure = None, Failure(error, at)
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


def _answers(entry: _Entry, number: int) -> bool:
    """Whether the ask numbered ``number`` is the one out for this key."""
    return entry.out is not None and entry.out.number == number
