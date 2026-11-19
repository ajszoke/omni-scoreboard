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
    # The play happened at 0, in an answer as of 5 that reached the board then: it may surface at 45, pinned on
    # monotonic time.
    arrival = Arrival(received=_at(5), occurred=T, as_of=T + 5 * S)
    assert POLICY.release(arrival, _at(5)) == _mono(45)
    assert POLICY.release(arrival, _at(20)) == _mono(45)  # admitted later, the same instant


def test_without_an_occurrence_it_waits_the_full_delay_from_receipt() -> None:
    assert POLICY.release(Arrival(received=_at(5)), _at(7)) == _mono(50)


def test_without_the_answer_s_own_time_it_waits_the_full_delay_from_receipt() -> None:
    # The play's age can only be told on the source's clock: its time against the answer's.
    assert POLICY.release(Arrival(received=_at(5), occurred=T), _at(5)) == _mono(50)


def test_the_play_s_age_is_told_on_the_source_s_clock_so_a_wall_clock_off_from_boot_can_t_shorten_it() -> None:
    # The play happened 5 s before the answer that reports it, and the answer reached the board at 5. The board's
    # wall clock has read a minute off since it booted, with no jump to notice: the play still may surface at 45.
    for off in (60 * S, -60 * S):
        board = _at(5, jumped=off)
        released = POLICY.release(Arrival(received=board, occurred=T, as_of=T + 5 * S), board)
        assert released >= _mono(45)  # never sooner than the delay after the play
        assert released <= _mono(50)  # nor later than the full delay from receipt
    ahead = _at(5, jumped=60 * S)
    assert POLICY.release(Arrival(received=ahead, occurred=T, as_of=T + 5 * S), ahead) == _mono(45)
    behind = _at(5, jumped=-60 * S)  # the source's times look like our future: one clock is wrong
    assert POLICY.release(Arrival(received=behind, occurred=T, as_of=T + 5 * S), behind) == _mono(50)


def test_an_answer_older_than_its_receipt_only_makes_the_release_later() -> None:
    # The answer stood at 2 (a cached copy, a slow fetch) and reached the board at 5: the play's age is told as of
    # 2, so it's released 3 s later than its true age would allow, never sooner.
    assert POLICY.release(Arrival(received=_at(5), occurred=T, as_of=T + 2 * S), _at(5)) == _mono(48)


def test_a_policy_measured_from_receipt_ignores_the_occurrence() -> None:
    policy = DelayPolicy(broadcast_lag=LAG, anchor=DelayAnchor.RECEIPT)
    assert policy.release(Arrival(received=_at(5), occurred=T), _at(5)) == _mono(50)


def test_a_play_received_late_surfaces_at_its_receipt_never_before() -> None:
    arrival = Arrival(received=_at(100), occurred=T, as_of=T + 100 * S)  # a minute and 40 s late: its delay has passed
    assert POLICY.release(arrival, _at(100)) == _mono(100)


def test_a_source_time_after_its_receipt_waits_the_full_delay() -> None:
    # A board that booted without the right time sees every source time far in its future.
    later = T + timedelta(hours=3)
    assert POLICY.release(Arrival(received=_at(5), occurred=later, as_of=later), _at(5)) == _mono(50)
    past = T + 11 * S  # past the 5 s skew
    assert POLICY.release(Arrival(received=_at(5), occurred=past, as_of=past), _at(5)) == _mono(50)
    within = T + 9 * S
    assert POLICY.release(Arrival(received=_at(5), occurred=within, as_of=within), _at(5)) == _mono(50)


def test_a_play_dated_after_the_answer_that_reports_it_waits_the_full_delay() -> None:
    assert POLICY.release(Arrival(received=_at(5), occurred=T + 11 * S, as_of=T + 5 * S), _at(5)) == _mono(50)
    # Within a few seconds, the answer's whole-second time explains it: it is no age at all, not a later release.
    assert POLICY.release(Arrival(received=_at(5), occurred=T + 9 * S, as_of=T + 5 * S), _at(5)) == _mono(50)


def test_a_source_time_implausibly_long_before_its_receipt_waits_the_full_delay() -> None:
    old = Arrival(received=_at(5), occurred=T - timedelta(minutes=5), as_of=T + 5 * S)
    assert POLICY.release(old, _at(5)) == _mono(50)
    # A copy of the answer minutes old still puts the play long before our receipt: one of the clocks is wrong.
    stale = Arrival(received=_at(5), occurred=T - timedelta(minutes=5), as_of=T - timedelta(minutes=4))
    assert POLICY.release(stale, _at(5)) == _mono(50)


def test_a_source_answer_dated_ahead_of_our_clock_waits_the_full_delay() -> None:
    arrival = Arrival(received=_at(5), occurred=T, as_of=T + timedelta(minutes=10))
    assert POLICY.release(arrival, _at(5)) == _mono(50)
    # The play looks only just ahead of our receipt, but the answer reporting it is dated well past it.
    ahead = Arrival(received=_at(5), occurred=T + 8 * S, as_of=T + 13 * S)
    assert POLICY.release(ahead, _at(5)) == _mono(50)
    assert POLICY.release(Arrival(received=_at(5), occurred=T, as_of=T + 5 * S), _at(5)) == _mono(45)


def test_a_wall_jump_between_receipt_and_admission_waits_the_full_delay() -> None:
    # Received at 5; by its admission at 10 the wall clock has jumped 40 s ahead: occurrence + delay would
    # look 40 s closer than it is.
    arrival = Arrival(received=_at(5), occurred=T, as_of=T + 5 * S)
    assert POLICY.release(arrival, _at(10, jumped=40 * S)) == _mono(50)
    assert POLICY.release(arrival, _at(10, jumped=-40 * S)) == _mono(50)


def test_while_the_wall_clock_is_suspect_everything_waits_the_full_delay() -> None:
    arrival = Arrival(received=_at(5), occurred=T, as_of=T + 5 * S)
    assert POLICY.release(arrival, _at(5), utc_trusted=False) == _mono(50)


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
    assert guard.pin(Arrival(received=_at(5), occurred=T, as_of=T + 5 * S), _at(5)) == _mono(45)
    # The wall clock jumps a minute ahead. A play received now with a plausible-looking time waits its full
    # delay from receipt, however its times read against the new wall clock.
    jumped = 60 * S
    plausible = Arrival(received=_at(20, jumped=jumped), occurred=T + 75 * S, as_of=T + 80 * S)
    assert guard.pin(plausible, _at(20, jumped=jumped)) == _mono(65)


def test_a_board_that_says_its_clock_cant_be_vouched_for_pins_from_receipt_until_it_settles() -> None:
    """A wake that the readings alone don't show: after ``distrust``, a play with a plausible time waits its full
    delay from receipt until `SETTLE_AFTER_JUMP` has passed, and a suspicion already longer stands."""
    guard = ReleaseGuard(POLICY)
    assert guard.observe(_at(0))
    guard.distrust(_at(10))
    plausible = Arrival(received=_at(15), occurred=T + 10 * S, as_of=T + 15 * S)
    assert guard.pin(plausible, _at(15)) == _mono(15 + 45)  # the full delay from receipt
    settled = 10 + SETTLE_AFTER_JUMP.total_seconds()
    assert not guard.observe(_at(settled - 1))
    later = Arrival(received=_at(settled), occurred=T + (settled - 5) * S, as_of=T + settled * S)
    assert guard.pin(later, _at(settled)) == _mono(settled + 45 - 5)  # trusted again: 5 s of its age come off

    watch = WallClockWatch()
    watch.observe(_at(0))
    assert not watch.observe(_at(10, jumped=60 * S))  # a jump: suspect until 130
    watch.distrust(_at(20, jumped=60 * S))  # suspect until 140 now
    assert not watch.observe(_at(135, jumped=60 * S))
    assert watch.observe(_at(140, jumped=60 * S))
    watch.distrust(_at(150, jumped=60 * S))
    watch.distrust(_at(100, jumped=60 * S))  # an older word changes nothing: suspect until 270 still
    assert not watch.observe(_at(260, jumped=60 * S)) and watch.observe(_at(270, jumped=60 * S))
