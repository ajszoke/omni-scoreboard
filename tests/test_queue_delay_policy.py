"""Tests for the release guard: an item's release, pinned on the monotonic clock when it's admitted."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from omni.core.clock import ClockReading, MonotonicTime
from omni.core.time import DurationSeconds
from omni.queue.delay_policy import (
    SETTLE_AFTER_JUMP,
    Arrival,
    DelayAnchor,
    DelayPolicy,
    ReleaseGuard,
    WallClockWatch,
)

T = datetime(2026, 6, 17, 23, 0, 0, tzinfo=timezone.utc)
LAG = DurationSeconds(45)
POLICY = DelayPolicy(broadcast_lag=LAG)
S = timedelta(seconds=1)


def _at(seconds: float, *, jumped: timedelta = timedelta(0)) -> ClockReading:
    """``seconds`` into the run on both clocks, the wall clock moved by ``jumped``."""
    return ClockReading(T + timedelta(seconds=seconds) + jumped, MonotonicTime(seconds))


def _mono(seconds: float) -> MonotonicTime:
    return MonotonicTime(seconds)


def test_the_delay_is_measured_from_the_occurrence_by_default() -> None:
    assert POLICY.anchor is DelayAnchor.OCCURRENCE


def test_a_plausible_occurrence_releases_the_delay_after_the_play() -> None:
    # The play happened at 0 and reached the board 5 s later: it may surface at 45, pinned on monotonic time.
    arrival = Arrival(received=_at(5), occurred=T)
    assert POLICY.release(arrival, _at(5)) == _mono(45)
    assert POLICY.release(arrival, _at(20)) == _mono(45)  # admitted later, the same instant


def test_without_an_occurrence_it_waits_the_full_delay_from_receipt() -> None:
    assert POLICY.release(Arrival(received=_at(5)), _at(7)) == _mono(50)


def test_a_policy_measured_from_receipt_ignores_the_occurrence() -> None:
    policy = DelayPolicy(broadcast_lag=LAG, anchor=DelayAnchor.RECEIPT)
    assert policy.release(Arrival(received=_at(5), occurred=T), _at(5)) == _mono(50)


def test_a_play_received_late_surfaces_at_its_receipt_never_before() -> None:
    arrival = Arrival(received=_at(100), occurred=T)  # a minute and 40 s late: its delay has passed
    assert POLICY.release(arrival, _at(100)) == _mono(100)


def test_a_source_time_after_its_receipt_waits_the_full_delay() -> None:
    # A board that booted without the right time sees every source time far in its future.
    assert POLICY.release(Arrival(received=_at(5), occurred=T + timedelta(hours=3)), _at(5)) == _mono(50)
    assert POLICY.release(Arrival(received=_at(5), occurred=T + 11 * S), _at(5)) == _mono(50)  # past the 5 s skew
    assert POLICY.release(Arrival(received=_at(5), occurred=T + 9 * S), _at(5)) == _mono(54)  # within it


def test_a_source_time_implausibly_long_before_its_receipt_waits_the_full_delay() -> None:
    old = Arrival(received=_at(5), occurred=T - timedelta(minutes=5))
    assert POLICY.release(old, _at(5)) == _mono(50)


def test_a_source_answer_dated_ahead_of_our_clock_waits_the_full_delay() -> None:
    arrival = Arrival(received=_at(5), occurred=T, as_of=T + timedelta(minutes=10))
    assert POLICY.release(arrival, _at(5)) == _mono(50)
    assert POLICY.release(Arrival(received=_at(5), occurred=T, as_of=T + 5 * S), _at(5)) == _mono(45)


def test_a_wall_jump_between_receipt_and_admission_waits_the_full_delay() -> None:
    # Received at 5; by its admission at 10 the wall clock has jumped 40 s ahead: occurrence + delay would
    # look 40 s closer than it is.
    arrival = Arrival(received=_at(5), occurred=T)
    assert POLICY.release(arrival, _at(10, jumped=40 * S)) == _mono(50)
    assert POLICY.release(arrival, _at(10, jumped=-40 * S)) == _mono(50)


def test_while_the_wall_clock_is_suspect_everything_waits_the_full_delay() -> None:
    assert POLICY.release(Arrival(received=_at(5), occurred=T), _at(5), utc_trusted=False) == _mono(50)


def test_a_source_time_must_be_timezone_aware() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        Arrival(received=_at(0), occurred=datetime(2026, 6, 17, 23, 0))
    with pytest.raises(ValueError, match="timezone-aware"):
        Arrival(received=_at(0), as_of=datetime(2026, 6, 17, 23, 0))


def test_a_wall_jump_either_way_makes_utc_suspect_until_it_settles() -> None:
    watch = WallClockWatch()
    assert watch.observe(_at(0)) and watch.observe(_at(5))  # steady: trusted
    assert watch.observe(_at(6, jumped=1 * S))  # a second of drift is not a jump
    assert not watch.observe(_at(10, jumped=60 * S))  # a minute ahead: suspect
    settled = 10 + SETTLE_AFTER_JUMP.total_seconds()
    assert not watch.observe(_at(settled - 1, jumped=60 * S))
    assert watch.observe(_at(settled, jumped=60 * S))  # the new offset has held long enough
    assert not watch.observe(_at(settled + 1))  # back again: suspect again


def test_the_guard_pins_from_the_occurrence_until_a_jump_then_from_receipt() -> None:
    guard = ReleaseGuard(POLICY)
    assert guard.policy is POLICY
    assert guard.observe(_at(0))
    assert guard.pin(Arrival(received=_at(5), occurred=T), _at(5)) == _mono(45)
    # The wall clock jumps a minute ahead. A play received now with a plausible-looking time waits its full
    # delay from receipt, however its occurrence reads against the new wall clock.
    jumped = 60 * S
    assert guard.pin(Arrival(received=_at(20, jumped=jumped), occurred=T + 75 * S), _at(20, jumped=jumped)) == _mono(65)
