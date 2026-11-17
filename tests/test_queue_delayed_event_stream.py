"""Tests for DelayedEventStream: once-each discrete event release, pinned on admission."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from omni.core.clock import ClockReading, MonotonicTime
from omni.core.time import DurationSeconds
from omni.queue.delay_policy import Arrival, DelayPolicy, ReleaseGuard
from omni.queue.delayed_event_stream import DelayedEventStream

T0 = datetime(2026, 6, 17, 23, 0, 0, tzinfo=timezone.utc)
LAG = DurationSeconds(30)
POLICY = DelayPolicy(broadcast_lag=LAG)


def _at(seconds: float, *, jumped: timedelta = timedelta(0)) -> ClockReading:
    return ClockReading(T0 + timedelta(seconds=seconds) + jumped, MonotonicTime(seconds))


def _stream() -> DelayedEventStream[str]:
    return DelayedEventStream(ReleaseGuard(POLICY))


def _push(stream: DelayedEventStream[str], value: str, *, happened: float, received: float | None = None) -> bool:
    """An event that happened at ``happened`` seconds, received (and admitted) at ``received``, or then."""
    receipt = _at(received if received is not None else happened)
    arrival = Arrival(received=receipt, occurred=T0 + timedelta(seconds=happened))
    return stream.push(value, arrival, receipt, key=value)


def _values(stream: DelayedEventStream[str], at: ClockReading) -> list[str]:
    return [held.value for held in stream.release(at)]


def test_push_holds_a_new_event() -> None:
    stream = _stream()
    assert _push(stream, "a", happened=0) is True
    assert stream.pending() == 1


def test_push_is_idempotent_on_key() -> None:
    stream = _stream()
    _push(stream, "a", happened=0)
    assert _push(stream, "a", happened=1) is False  # same key ignored
    assert stream.pending() == 1


def test_mark_seen_suppresses_a_later_push() -> None:
    stream = _stream()
    stream.mark_seen("a")  # backlog suppression — never hold this key
    assert _push(stream, "a", happened=0) is False
    assert stream.pending() == 0


def test_release_waits_for_the_tv_delay() -> None:
    stream = _stream()
    _push(stream, "hr", happened=0)
    assert stream.next_eligible() == MonotonicTime(30)
    assert stream.release(_at(29)) == []  # still inside the lag
    assert _values(stream, _at(30)) == ["hr"]  # occurrence + lag reached
    assert stream.next_eligible() is None


def test_release_yields_each_event_once() -> None:
    stream = _stream()
    _push(stream, "hr", happened=0)
    assert _values(stream, _at(60)) == ["hr"]
    assert stream.release(_at(60)) == []  # not replayed
    assert stream.pending() == 0


def test_release_holds_not_yet_eligible_events_for_later() -> None:
    stream = _stream()
    _push(stream, "early", happened=0)
    _push(stream, "late", happened=20)
    # At +35: early (elig +30) is safe, late (elig +50) is not yet.
    assert _values(stream, _at(35)) == ["early"]
    assert stream.pending() == 1
    assert _values(stream, _at(50)) == ["late"]


def test_release_preserves_insertion_order() -> None:
    stream = _stream()
    _push(stream, "first", happened=0)
    _push(stream, "second", happened=0)
    assert _values(stream, _at(30)) == ["first", "second"]


def test_late_received_old_play_is_eligible_at_receipt_not_before() -> None:
    # A play that happened at 0 but was only fetched at +100: it can't surface before the board had it, but it
    # never waits a second delay either.
    stream = _stream()
    _push(stream, "late-fetch", happened=0, received=100)
    assert stream.release(_at(99)) == []
    assert _values(stream, _at(100)) == ["late-fetch"]


def test_a_wall_jump_while_holding_moves_no_release() -> None:
    stream = _stream()
    _push(stream, "hr", happened=0)
    assert stream.release(_at(5, jumped=timedelta(minutes=10))) == []  # ten minutes ahead on the wall: held
    assert _values(stream, _at(30, jumped=timedelta(minutes=-10))) == ["hr"]  # ten back: released on time


def test_policy_is_exposed() -> None:
    assert _stream().policy is POLICY
