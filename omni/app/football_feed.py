"""The network side of a football board: when to ask NFLMeta and the market about one game, and asking.

`GameFeed` keeps the asking cadence. It asks the provider when its last answer says to, retries 30
seconds after a failure, and stops once the final has settled. Given a `QuotaLedger`, it asks only what
the ledger admits on NFLMeta's account, as focus: the one game the board follows keeps its cadence until
the month is exhausted. A refused ask makes no request; it comes back as a failed answer that says why,
and is tried again once the minute has room, or after the failure retry. While the final drains it asks at the
live pace whatever the answer says, and once more on the drain's boundary, so the drain gets its reads.
Each request a poll makes (its scores, its plays' pages) is admitted on the account in turn, as the ask's own
(`omni.app.quota.Admission.asking`), so a poll that pages can't overrun the minute it shares with the rest of the
account's asks. Its due times run on the monotonic clock, so a wall-clock step neither stops the asks nor hurries
them.
It also reads the market with each answer, when there is a market. Each answer comes back as a
`FeedResult`, stamped with when it was asked and when it actually arrived, on both clocks. The board
dates what it shows from the arrival, and holds it back from the arrival on the monotonic clock, which
a wall-clock jump doesn't move.

The board collects answers once per frame through a `FeedSource`:
- `GameFeed` itself asks inline, when due. That is deterministic, and what tests and replay use.
- `BackgroundFeed` asks on its own thread and hands over whatever has arrived, so a slow request
  never holds up a frame.
"""

from __future__ import annotations

import queue
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from omni.app.background import Repeating
from omni.app.poll_plan import Demand, PollPriority
from omni.app.quota import Admission, ProviderAccount, QuotaLedger, tallying
from omni.core.clock import Clock, ClockReading, MonotonicTime
from omni.domain.baseball import WinProbability
from omni.domain.contest import TeamGame
from omni.providers.base import ProviderError
from omni.providers.nflmeta import DEFAULT_POLL, NflLiveSnapshot, NflMetaLiveProvider

__all__ = [
    "AFTER_FINAL",
    "BackgroundFeed",
    "FeedResult",
    "FeedSource",
    "GameFeed",
    "MarketQuote",
    "RETRY_AFTER_FAILURE",
    "WinProbabilityFetcher",
]

WinProbabilityFetcher = Callable[[TeamGame, datetime], WinProbability | None]

AFTER_FINAL = timedelta(hours=12)  # once the final and its last play are in, there's nothing left to ask
RETRY_AFTER_FAILURE = timedelta(seconds=30)
_CHECK_EVERY = timedelta(milliseconds=250)  # how often the background thread looks whether an ask is due


@dataclass(frozen=True, slots=True)
class MarketQuote:
    """The market's answer alongside a feed answer: a probability, no price at all, or a failure."""

    probability: WinProbability | None
    error: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class FeedResult:
    """One ask of the feed, and of the market with it, stamped when asked and when it arrived."""

    asked_at: datetime
    received_at: datetime
    next_ask: datetime  # when the feed is next due, dated as of the ask
    snapshot: NflLiveSnapshot | None  # None when the ask failed
    error: str | None = None
    held: bool = False  # the provider repeated its last answer during a hold, without asking
    holding: bool = False  # after this answer, the provider is holding off the feed (halftime)
    quote: MarketQuote | None = None  # None without a market, or when the answer was held
    received_monotonic: MonotonicTime | None = None  # its arrival on the monotonic clock; None from a trace
    refused: Admission | None = None  # the quota held the ask back: no request was made


class FeedSource(Protocol):
    """Where a board collects its game's answers, once a frame."""

    def collect(self, at: ClockReading) -> list[FeedResult]:
        """The answers that arrived since the last collect, as of ``at``, oldest first."""
        ...


class GameFeed:
    """Asks about one game when due; a `FeedSource` that asks inline."""

    def __init__(
        self,
        provider: NflMetaLiveProvider,
        game_id: int,
        *,
        win_probability: WinProbabilityFetcher | None = None,
        ledger: QuotaLedger | None = None,
    ) -> None:
        self._provider = provider
        self._game_id = game_id
        self._win_probability = win_probability
        self._ledger = ledger
        self._rows = 1  # what the next poll is expected to bill: the last poll's rows, once one said
        self._next_ask: MonotonicTime | None = None

    def due(self, at: ClockReading) -> bool:
        """Whether an ask is due as of ``at``, on the monotonic clock."""
        return self._next_ask is None or at.monotonic >= self._next_ask

    def back_off(self, at: ClockReading) -> datetime:
        """Wait the failure retry before asking again; returns when the next ask is due, dated as of ``at``."""
        self._next_ask = at.monotonic + RETRY_AFTER_FAILURE
        return at.utc + RETRY_AFTER_FAILURE

    def ask(self, at: ClockReading, *, arrived: Callable[[], ClockReading] | None = None) -> FeedResult:
        """Ask the provider (and the market) as of ``at``; ``arrived`` reads the clock when the answer came."""
        now = at.utc
        held = self._provider.holding(self._game_id, at)  # a held poll repeats the last answer, unasked
        asking: AbstractContextManager[None] = nullcontext()
        if not held and self._ledger is not None:
            admission = self._ledger.admit(ProviderAccount.NFLMETA, Demand(PollPriority.FOCUS, self._rows), at)
            if not admission.admitted:
                return self._refused(admission, at, arrived)
            asking = admission.asking()
        try:
            with tallying() as billed, asking:
                try:
                    snapshot = self._provider.poll(self._game_id, at)
                finally:
                    self._rows = billed.rows if billed.rows is not None else self._rows
        except ProviderError as exc:
            retry = self.back_off(at)
            received = arrived() if arrived is not None else at
            return FeedResult(
                asked_at=now,
                received_at=received.utc,
                received_monotonic=received.monotonic,
                next_ask=retry,
                snapshot=None,
                error=str(exc),
            )
        wait = _wait(snapshot)
        self._next_ask = at.monotonic + wait
        quote = None
        if not held and self._win_probability is not None:
            try:
                quote = MarketQuote(self._win_probability(snapshot.game, now))
            except ProviderError as exc:
                quote = MarketQuote(None, error=str(exc))
        received = arrived() if arrived is not None else at
        return FeedResult(
            asked_at=now,
            received_at=received.utc,
            received_monotonic=received.monotonic,
            next_ask=now + wait,
            snapshot=snapshot,
            held=held,
            holding=self._provider.holding(self._game_id, at),
            quote=quote,
        )

    def collect(self, at: ClockReading, *, arrived: Callable[[], ClockReading] | None = None) -> list[FeedResult]:
        return [self.ask(at, arrived=arrived)] if self.due(at) else []

    def _refused(
        self, admission: Admission, at: ClockReading, arrived: Callable[[], ClockReading] | None
    ) -> FeedResult:
        """An ask the quota held back: no request. It's due again once the minute has room, or after the
        failure retry when it's the month that's spent."""
        wait = admission.retry_at.since(at.monotonic) if admission.retry_at is not None else RETRY_AFTER_FAILURE
        self._next_ask = at.monotonic + wait
        received = arrived() if arrived is not None else at
        return FeedResult(
            asked_at=at.utc,
            received_at=received.utc,
            received_monotonic=received.monotonic,
            next_ask=at.utc + wait,
            snapshot=None,
            error=admission.reason,
            refused=admission,
        )


class BackgroundFeed:
    """Asks a `GameFeed` on its own thread; the frame loop collects what has arrived without waiting."""

    def __init__(self, feed: GameFeed, clock: Clock, *, check_every: timedelta = _CHECK_EVERY) -> None:
        self._feed = feed
        self._clock = clock
        self._answers: queue.SimpleQueue[FeedResult] = queue.SimpleQueue()
        self._worker = Repeating("nfl-feed", clock, check_every, self._check, on_error=self._failed)

    def start(self) -> None:
        self._worker.start()

    def stop(self, timeout: float | None = None) -> None:
        self._worker.stop(timeout)

    def collect(self, at: ClockReading) -> list[FeedResult]:
        answers: list[FeedResult] = []
        while True:
            try:
                answers.append(self._answers.get_nowait())
            except queue.Empty:
                return answers

    def _check(self) -> None:
        for answer in self._feed.collect(self._clock.read(), arrived=self._clock.read):
            self._answers.put(answer)

    def _failed(self, exc: Exception) -> None:
        """An ask that broke outright: report it, and wait the retry rather than ask again at once."""
        now = self._clock.read()
        retry = self._feed.back_off(now)
        self._answers.put(
            FeedResult(
                asked_at=now.utc,
                received_at=now.utc,
                received_monotonic=now.monotonic,
                next_ask=retry,
                snapshot=None,
                error=f"{type(exc).__name__}: {exc}",
            )
        )


def _wait(snapshot: NflLiveSnapshot) -> timedelta:
    """How long after an answer the feed is next asked: as the answer says, until the final has settled.
    While the final drains, no longer than the live pace, nor past the drain's last read."""
    if snapshot.settled:
        return AFTER_FINAL
    wait = snapshot.poll_every.as_timedelta()
    if snapshot.drain_left is not None:
        wait = min(wait, DEFAULT_POLL.as_timedelta(), snapshot.drain_left)
    return wait
