"""Tests for DelayedFeed: the newest TV-safe value of a changing subject, released on the monotonic clock."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from omni.core.clock import ClockReading, MonotonicTime
from omni.core.time import DurationSeconds
from omni.queue.delay_policy import Arrival, DelayPolicy, ReleaseGuard
from omni.queue.delayed_feed import DelayedFeed

T = datetime(2026, 6, 17, 23, 0, 0, tzinfo=timezone.utc)
POLICY = DelayPolicy(broadcast_lag=DurationSeconds(30))


def _at(seconds: float, *, jumped: timedelta = timedelta(0)) -> ClockReading:
    return ClockReading(T + timedelta(seconds=seconds) + jumped, MonotonicTime(seconds))


def _feed() -> DelayedFeed[str]:
    return DelayedFeed(ReleaseGuard(POLICY))


def _push(feed: DelayedFeed[str], value: str, received: float) -> None:
    """A value received at ``received`` seconds and admitted then."""
    feed.push(value, Arrival(received=_at(received)), _at(received))


def test_empty_feed() -> None:
    feed = _feed()
    assert feed.latest_eligible(_at(0)) is None
    assert feed.pending() == 0
    assert feed.policy is POLICY
    assert feed.next_eligible(_at(0)) is None


def test_holds_until_delay_elapses() -> None:
    feed = _feed()
    _push(feed, "s0", 0)
    assert feed.latest_eligible(_at(29)) is None  # still within the delay
    assert feed.pending() == 1
    assert feed.next_eligible(_at(29)) == MonotonicTime(30)
    got = feed.latest_eligible(_at(30))  # eligible at the boundary
    assert got is not None and got.value == "s0" and got.release == MonotonicTime(30)
    assert feed.pending() == 0  # consumed once surfaced


def test_returns_newest_eligible_and_prunes_older() -> None:
    feed = _feed()
    _push(feed, "s0", 0)
    _push(feed, "s10", 10)
    _push(feed, "s20", 20)
    # At +45s: s0 (elig +30) and s10 (elig +40) are safe; s20 (elig +50) is not.
    got = feed.latest_eligible(_at(45))
    assert got is not None and got.value == "s10"  # newest of the eligible
    assert feed.pending() == 1  # s0 + s10 pruned, s20 retained
    later = feed.latest_eligible(_at(50))
    assert later is not None and later.value == "s20"


def test_keeps_future_values_for_a_later_pass() -> None:
    feed = _feed()
    _push(feed, "s0", 0)
    assert feed.latest_eligible(_at(0)) is None  # nothing safe yet
    assert feed.pending() == 1  # but retained, not dropped
    got = feed.latest_eligible(_at(30))
    assert got is not None and got.value == "s0"


def test_a_credible_occurrence_makes_it_eligible_by_when_it_happened() -> None:
    feed = _feed()
    feed.push("play", Arrival(received=_at(20), occurred=T, as_of=T + timedelta(seconds=20)), _at(20))  # 20 s late
    assert feed.latest_eligible(_at(29)) is None
    got = feed.latest_eligible(_at(30))
    assert got is not None and got.value == "play"


def test_a_value_no_newer_than_one_already_pushed_is_ignored() -> None:
    feed = _feed()
    _push(feed, "answer", 10)
    feed.push("answer", Arrival(received=_at(10)), _at(12))  # the same answer read again on a later pass
    feed.push("older", Arrival(received=_at(5)), _at(12))  # one that arrived out of order
    assert feed.pending() == 1
    eligible = feed.latest_eligible(_at(40))
    assert eligible is not None and eligible.value == "answer"
    feed.push("answer", Arrival(received=_at(10)), _at(41))  # read again after it surfaced: still nothing new
    assert feed.pending() == 0


def test_a_wall_jump_while_holding_moves_no_release() -> None:
    feed = _feed()
    _push(feed, "s0", 0)
    assert feed.latest_eligible(_at(10, jumped=timedelta(minutes=5))) is None  # five minutes ahead: still held
    assert feed.next_eligible(_at(10, jumped=timedelta(minutes=5))) == MonotonicTime(30)
    got = feed.latest_eligible(_at(30, jumped=timedelta(minutes=-5)))  # five back: released on time all the same
    assert got is not None and got.value == "s0"
