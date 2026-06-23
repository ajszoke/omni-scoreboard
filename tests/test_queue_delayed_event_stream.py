"""Tests for DelayedEventStream: source-time-anchored, once-each discrete event release."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from omni.core.enum import League
from omni.core.ids import LeagueScopedId, SourceRef
from omni.core.observation import Observation
from omni.core.time import DurationSeconds
from omni.queue.delay_policy import DelayPolicy
from omni.queue.delayed_event_stream import DelayedEventStream

T0 = datetime(2026, 6, 17, 23, 0, 0, tzinfo=timezone.utc)
SOURCE = SourceRef("mlb_statsapi", "https://statsapi.mlb.com")
SUBJECT = LeagueScopedId(League.MLB, SOURCE, "g1")
LAG = DurationSeconds(30)


def _policy() -> DelayPolicy:
    return DelayPolicy(broadcast_lag=LAG)


def _obs(value: str, *, source_time: datetime, observed_at: datetime | None = None) -> Observation[str]:
    return Observation(
        subject_id=SUBJECT,
        source=SOURCE,
        observed_at=observed_at if observed_at is not None else source_time,
        value=value,
        source_time=source_time,
    )


def test_push_holds_a_new_event() -> None:
    stream: DelayedEventStream[str] = DelayedEventStream(_policy())
    assert stream.push(_obs("a", source_time=T0), key="a") is True
    assert stream.pending() == 1


def test_push_is_idempotent_on_key() -> None:
    stream: DelayedEventStream[str] = DelayedEventStream(_policy())
    stream.push(_obs("a", source_time=T0), key="a")
    assert stream.push(_obs("a-again", source_time=T0), key="a") is False  # same key ignored
    assert stream.pending() == 1


def test_mark_seen_suppresses_a_later_push() -> None:
    stream: DelayedEventStream[str] = DelayedEventStream(_policy())
    stream.mark_seen("a")  # backlog suppression — never hold this key
    assert stream.push(_obs("a", source_time=T0), key="a") is False
    assert stream.pending() == 0


def test_release_waits_for_the_tv_delay() -> None:
    stream: DelayedEventStream[str] = DelayedEventStream(_policy())
    stream.push(_obs("hr", source_time=T0), key="hr")
    assert stream.release(T0 + timedelta(seconds=29)) == []  # still inside the lag
    released = stream.release(T0 + timedelta(seconds=30))  # source_time + lag reached
    assert [o.value for o in released] == ["hr"]


def test_release_yields_each_event_once() -> None:
    stream: DelayedEventStream[str] = DelayedEventStream(_policy())
    stream.push(_obs("hr", source_time=T0), key="hr")
    after = T0 + timedelta(seconds=60)
    assert [o.value for o in stream.release(after)] == ["hr"]
    assert stream.release(after) == []  # not replayed
    assert stream.pending() == 0


def test_release_holds_not_yet_eligible_events_for_later() -> None:
    stream: DelayedEventStream[str] = DelayedEventStream(_policy())
    stream.push(_obs("early", source_time=T0), key="early")
    stream.push(_obs("late", source_time=T0 + timedelta(seconds=20)), key="late")
    # At T0+35: early (elig T0+30) is safe, late (elig T0+50) is not yet.
    assert [o.value for o in stream.release(T0 + timedelta(seconds=35))] == ["early"]
    assert stream.pending() == 1
    assert [o.value for o in stream.release(T0 + timedelta(seconds=50))] == ["late"]


def test_release_preserves_insertion_order() -> None:
    stream: DelayedEventStream[str] = DelayedEventStream(_policy())
    stream.push(_obs("first", source_time=T0), key="1")
    stream.push(_obs("second", source_time=T0), key="2")
    assert [o.value for o in stream.release(T0 + timedelta(seconds=30))] == ["first", "second"]


def test_late_received_old_play_is_eligible_at_receipt_not_before() -> None:
    # A play that happened at T0 but we only fetched at T0+100: the delay is clamped to
    # receipt (we can't reveal before we had it), but it never waits a second lag again.
    stream: DelayedEventStream[str] = DelayedEventStream(_policy())
    received = T0 + timedelta(seconds=100)
    stream.push(_obs("late-fetch", source_time=T0, observed_at=received), key="x")
    assert stream.release(received - timedelta(seconds=1)) == []
    assert [o.value for o in stream.release(received)] == ["late-fetch"]


def test_policy_is_exposed() -> None:
    policy = _policy()
    assert DelayedEventStream[str](policy).policy is policy
