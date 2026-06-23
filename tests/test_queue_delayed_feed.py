"""Tests for DelayedFeed: newest TV-safe observation of a changing value."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from omni.core.enum import League
from omni.core.ids import LeagueScopedId, SourceRef
from omni.core.observation import Observation
from omni.core.time import DurationSeconds
from omni.queue.delay_policy import DelayPolicy
from omni.queue.delayed_feed import DelayedFeed

SOURCE = SourceRef("mlb_statsapi")
SUBJ = LeagueScopedId(League.MLB, SOURCE, "g1")
T = datetime(2026, 6, 17, 23, 0, 0, tzinfo=timezone.utc)
POLICY = DelayPolicy(broadcast_lag=DurationSeconds(30))


def _obs(value: str, at: datetime) -> Observation[str]:
    return Observation(subject_id=SUBJ, source=SOURCE, observed_at=at, value=value)


def test_empty_feed() -> None:
    feed: DelayedFeed[str] = DelayedFeed(POLICY)
    assert feed.latest_eligible(T) is None
    assert feed.pending() == 0
    assert feed.policy is POLICY


def test_holds_until_delay_elapses() -> None:
    feed: DelayedFeed[str] = DelayedFeed(POLICY)
    feed.push(_obs("s0", T))
    assert feed.latest_eligible(T + timedelta(seconds=29)) is None  # still within the delay
    assert feed.pending() == 1
    got = feed.latest_eligible(T + timedelta(seconds=30))  # eligible at the boundary
    assert got is not None and got.value == "s0"
    assert feed.pending() == 0  # consumed once surfaced


def test_returns_newest_eligible_and_prunes_older() -> None:
    feed: DelayedFeed[str] = DelayedFeed(POLICY)
    feed.push(_obs("s0", T))
    feed.push(_obs("s10", T + timedelta(seconds=10)))
    feed.push(_obs("s20", T + timedelta(seconds=20)))
    # At +45s: s0 (elig +30) and s10 (elig +40) are safe; s20 (elig +50) is not.
    got = feed.latest_eligible(T + timedelta(seconds=45))
    assert got is not None and got.value == "s10"  # newest of the eligible
    assert feed.pending() == 1  # s0 + s10 pruned, s20 retained
    later = feed.latest_eligible(T + timedelta(seconds=50))
    assert later is not None and later.value == "s20"


def test_keeps_future_observations_for_a_later_tick() -> None:
    feed: DelayedFeed[str] = DelayedFeed(POLICY)
    feed.push(_obs("s0", T))
    assert feed.latest_eligible(T) is None  # nothing safe yet
    assert feed.pending() == 1  # but retained, not dropped
    got = feed.latest_eligible(T + timedelta(seconds=30))
    assert got is not None and got.value == "s0"


def test_source_time_anchoring_makes_it_eligible_by_when_it_happened() -> None:
    feed: DelayedFeed[str] = DelayedFeed(POLICY)
    happened = T
    received = T + timedelta(seconds=20)  # 20s fetch lag
    feed.push(Observation(subject_id=SUBJ, source=SOURCE, observed_at=received, value="play", source_time=happened))
    # eligible_at = max(received, happened + 30) = max(T+20, T+30) = T+30
    assert feed.latest_eligible(T + timedelta(seconds=29)) is None
    got = feed.latest_eligible(T + timedelta(seconds=30))
    assert got is not None and got.value == "play"
