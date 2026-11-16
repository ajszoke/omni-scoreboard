"""The football board's network side: asking off the frame path, and dating answers by arrival."""

from __future__ import annotations

import threading
import time
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any, Callable

from omni.app.clock import SystemClock
from omni.app.football_feed import RETRY_AFTER_FAILURE, BackgroundFeed, FeedResult, GameFeed
from omni.providers.nfl_teams import NflTeamRegistry
from omni.providers.nflmeta import NFLMETA_SOURCE, NflMetaLiveProvider
from tests.test_nflmeta import GAME, T0, FakeFeed, _change, _page, _scores, _synced

_SOON = 5.0  # seconds a test waits for the feed thread before calling it stuck
_QUICK = timedelta(milliseconds=1)


class _StalledFeed(FakeFeed):
    """NFLMeta, stalled until released."""

    def __init__(self) -> None:
        super().__init__()
        self.asked = threading.Event()
        self.release = threading.Event()

    def __call__(self, path: str, query: Mapping[str, str | int]) -> Any:
        self.asked.set()
        assert self.release.wait(_SOON)
        return super().__call__(path, query)


class _BrokenFeed(GameFeed):
    """An ask that fails outright, not as a provider error (a bug, say)."""

    def ask(self, now: datetime, *, arrived: Callable[[], datetime] | None = None) -> FeedResult:
        raise RuntimeError("parse bug")


def _provider(feed: FakeFeed) -> NflMetaLiveProvider:
    return NflMetaLiveProvider(NflTeamRegistry.default(source=NFLMETA_SOURCE), fetch=feed)


def _collect_within(feed: BackgroundFeed) -> list[FeedResult]:
    deadline = time.monotonic() + _SOON
    while time.monotonic() < deadline:
        if results := feed.collect(SystemClock().now()):
            return results
        time.sleep(0.001)
    return []


def test_a_slow_answer_never_holds_up_collecting() -> None:
    nflmeta = _StalledFeed()
    nflmeta.scores = [_scores(_synced("p1"))]
    nflmeta.plays = [_page(_change("p1", 1, "C.Brown up the middle for 4 yards.", yards_gained=4))]
    feed = BackgroundFeed(GameFeed(_provider(nflmeta), GAME), SystemClock(), check_every=_QUICK)
    feed.start()
    assert nflmeta.asked.wait(_SOON)
    assert feed.collect(SystemClock().now()) == []  # the ask is stalled; collecting doesn't wait for it
    nflmeta.release.set()
    results = _collect_within(feed)
    feed.stop(_SOON)
    assert len(results) == 1
    answer = results[0]
    assert answer.snapshot is not None and answer.error is None
    assert answer.received_at >= answer.asked_at  # dated by when it arrived, after the stall


def test_an_ask_that_breaks_outright_is_reported_and_waits_before_asking_again() -> None:
    broken = _BrokenFeed(_provider(FakeFeed()), GAME)
    feed = BackgroundFeed(broken, SystemClock(), check_every=_QUICK)
    feed.start()
    results = _collect_within(feed)
    feed.stop(_SOON)
    assert [result.error for result in results] == ["RuntimeError: parse bug"]
    answer = results[0]
    assert answer.snapshot is None and answer.next_ask == answer.asked_at + RETRY_AFTER_FAILURE
    assert not broken.due(answer.asked_at + RETRY_AFTER_FAILURE - timedelta(seconds=1))


def test_inline_answers_are_dated_when_asked() -> None:
    nflmeta = FakeFeed()
    nflmeta.scores = [_scores(_synced("p1"))]
    nflmeta.plays = [_page(_change("p1", 1, "C.Brown up the middle for 4 yards.", yards_gained=4))]
    feed = GameFeed(_provider(nflmeta), GAME)
    [answer] = feed.collect(T0)
    assert answer.asked_at == answer.received_at == T0
    assert feed.collect(T0) == []  # not due again until the scoreboard's next poll
