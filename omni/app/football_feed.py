"""The network side of a football board: when to ask NFLMeta and the market about one game, and asking.

`GameFeed` keeps the asking cadence. It asks the provider when its last answer says to, retries 30
seconds after a failure, and stops once the final has settled. It also reads the market with each
answer, when there is a market. Each answer comes back as a `FeedResult`, stamped with when it was
asked and when it actually arrived. The board dates what it shows from the arrival.

The board collects answers once per frame through a `FeedSource`:
- `GameFeed` itself asks inline, when due. That is deterministic, and what tests and replay use.
- `BackgroundFeed` asks on its own thread and hands over whatever has arrived, so a slow request
  never holds up a frame.
"""

from __future__ import annotations

import queue
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from omni.app.background import Repeating
from omni.app.clock import Clock
from omni.domain.baseball import WinProbability
from omni.domain.contest import TeamGame
from omni.providers.base import ProviderError
from omni.providers.nflmeta import NflLiveSnapshot, NflMetaLiveProvider

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
    next_ask: datetime  # when the feed is next due
    snapshot: NflLiveSnapshot | None  # None when the ask failed
    error: str | None = None
    held: bool = False  # the provider repeated its last answer during a hold, without asking
    holding: bool = False  # after this answer, the provider is holding off the feed (halftime)
    quote: MarketQuote | None = None  # None without a market, or when the answer was held


class FeedSource(Protocol):
    """Where a board collects its game's answers, once a frame."""

    def collect(self, now: datetime) -> list[FeedResult]:
        """The answers that arrived since the last collect, oldest first."""
        ...


class GameFeed:
    """Asks about one game when due; a `FeedSource` that asks inline."""

    def __init__(
        self,
        provider: NflMetaLiveProvider,
        game_id: int,
        *,
        win_probability: WinProbabilityFetcher | None = None,
    ) -> None:
        self._provider = provider
        self._game_id = game_id
        self._win_probability = win_probability
        self._next_ask: datetime | None = None

    def due(self, now: datetime) -> bool:
        return self._next_ask is None or now >= self._next_ask

    def back_off(self, now: datetime) -> datetime:
        """Wait the failure retry before asking again; returns when the next ask is due."""
        self._next_ask = now + RETRY_AFTER_FAILURE
        return self._next_ask

    def ask(self, now: datetime, *, arrived: Callable[[], datetime] | None = None) -> FeedResult:
        """Ask the provider (and the market) as of ``now``; ``arrived`` reads the time the answer came."""
        held = self._provider.holding(self._game_id, now)  # a held poll repeats the last answer, unasked
        try:
            snapshot = self._provider.poll(self._game_id, now)
        except ProviderError as exc:
            retry = self.back_off(now)
            return FeedResult(
                asked_at=now,
                received_at=arrived() if arrived is not None else now,
                next_ask=retry,
                snapshot=None,
                error=str(exc),
            )
        self._next_ask = now + (AFTER_FINAL if snapshot.settled else snapshot.poll_every.as_timedelta())
        quote = None
        if not held and self._win_probability is not None:
            try:
                quote = MarketQuote(self._win_probability(snapshot.game, now))
            except ProviderError as exc:
                quote = MarketQuote(None, error=str(exc))
        return FeedResult(
            asked_at=now,
            received_at=arrived() if arrived is not None else now,
            next_ask=self._next_ask,
            snapshot=snapshot,
            held=held,
            holding=self._provider.holding(self._game_id, now),
            quote=quote,
        )

    def collect(self, now: datetime, *, arrived: Callable[[], datetime] | None = None) -> list[FeedResult]:
        return [self.ask(now, arrived=arrived)] if self.due(now) else []


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

    def collect(self, now: datetime) -> list[FeedResult]:
        answers: list[FeedResult] = []
        while True:
            try:
                answers.append(self._answers.get_nowait())
            except queue.Empty:
                return answers

    def _check(self) -> None:
        for answer in self._feed.collect(self._clock.now(), arrived=self._clock.now):
            self._answers.put(answer)

    def _failed(self, exc: Exception) -> None:
        """An ask that broke outright: report it, and wait the retry rather than ask again at once."""
        now = self._clock.now()
        retry = self._feed.back_off(now)
        self._answers.put(
            FeedResult(
                asked_at=now, received_at=now, next_ask=retry, snapshot=None, error=f"{type(exc).__name__}: {exc}"
            )
        )
