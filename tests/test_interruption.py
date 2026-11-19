"""Interruptions (`omni.tower.interruption`): a host's suspend or stall read from two passes' clock readings, and an
output loss numbered with them."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from omni.core.clock import ClockReading, FakeClock, MonotonicTime
from omni.tower.interruption import Interruption, InterruptionPolicy, InterruptionReason, InterruptionWatch

T = datetime(2026, 11, 22, 17, 0, tzinfo=timezone.utc)


def _watched(clock: FakeClock) -> InterruptionWatch:
    watch = InterruptionWatch()
    assert watch.observe(clock.read()) is None  # the first pass has nothing to compare with
    return watch


def test_a_49_minute_sleep_is_a_host_suspend_read_from_the_suspend_inclusive_clock_whatever_the_wall_did() -> None:
    clock = FakeClock(T, suspend_inclusive=MonotonicTime(1000.0))
    watch = _watched(clock)
    before = clock.read()
    clock.suspend(timedelta(minutes=49))  # the monotonic clock stands still; both elapsed clocks move on
    clock.jump(timedelta(minutes=-3))  # and NTP corrects the wall clock as it wakes
    interruption = watch.observe(clock.read())
    assert interruption is not None and interruption.reason is InterruptionReason.HOST_SUSPEND
    assert interruption.clock_trusted and interruption.host and interruption.serial == 1
    assert (interruption.before, interruption.after) == (before, clock.read())
    assert interruption.gap == timedelta(minutes=49)
    assert watch.observe(clock.read()) is None  # taken once


def test_a_wall_clock_correction_either_way_is_no_interruption_where_the_suspend_inclusive_clock_kept_pace() -> None:
    clock = FakeClock(T, suspend_inclusive=MonotonicTime(0.0))
    watch = _watched(clock)
    for jump in (timedelta(minutes=49), timedelta(minutes=-49), timedelta(hours=3)):
        clock.jump(jump)
        clock.advance(timedelta(seconds=1))
        assert watch.observe(clock.read()) is None
    clock.suspend(timedelta(seconds=5))  # a sleep no longer than the threshold
    assert watch.observe(clock.read()) is None
    clock.suspend(timedelta(seconds=5.5))
    assert watch.observe(clock.read()) is not None


def test_without_a_suspend_inclusive_clock_a_wall_clock_30_s_ahead_is_an_ambiguous_suspend() -> None:
    clock = FakeClock(T)
    watch = _watched(clock)
    clock.jump(timedelta(seconds=30))
    assert watch.observe(clock.read()) is None  # a correction this small isn't a sleep
    clock.jump(timedelta(seconds=-120))  # nor is any step back
    assert watch.observe(clock.read()) is None
    clock.suspend(timedelta(minutes=49))
    interruption = watch.observe(clock.read())
    assert interruption is not None and interruption.reason is InterruptionReason.HOST_SUSPEND
    assert not interruption.clock_trusted  # a sleep or a jump: the wall clock can't say which
    assert interruption.gap == timedelta(minutes=49)


def test_a_pass_more_than_30_s_past_its_5_s_health_tick_is_a_process_stall() -> None:
    clock = FakeClock(T, suspend_inclusive=MonotonicTime(0.0))
    watch = _watched(clock)
    assert watch.next_tick() == MonotonicTime(5.0)
    clock.advance(timedelta(seconds=35))  # 30 s past its tick, not more
    assert watch.observe(clock.read()) is None
    clock.advance(timedelta(seconds=35.5))
    interruption = watch.observe(clock.read())
    assert interruption is not None and interruption.reason is InterruptionReason.PROCESS_STALL
    assert interruption.clock_trusted and interruption.gap == timedelta(seconds=35.5)
    plain = FakeClock(T)
    stalled = _watched(plain)
    plain.advance(timedelta(minutes=2))
    found = stalled.observe(plain.read())
    assert found is not None and found.reason is InterruptionReason.PROCESS_STALL
    assert found.gap == timedelta(minutes=2)  # on the wall clock, which ran with it


def test_a_reading_older_than_the_last_one_seen_says_nothing() -> None:
    watch = InterruptionWatch()
    assert watch.next_tick() is None
    watch.observe(ClockReading(T, MonotonicTime(100.0)))
    assert watch.observe(ClockReading(T, MonotonicTime(10.0))) is None  # the other pass's, read before
    assert watch.next_tick() == MonotonicTime(105.0)


def test_an_output_loss_is_numbered_with_the_host_s_interruptions_and_its_gap_runs_from_the_display_s_last_word() -> (
    None
):
    clock = FakeClock(T)
    watch = _watched(clock)
    clock.advance(timedelta(minutes=5))
    assert watch.observe(clock.read()) is not None  # a stall: number 1
    last_word = clock.read()
    clock.advance(timedelta(seconds=9))
    lost = watch.output_lost(
        before=last_word, after=clock.read(), old_session=3, new_session=4, last_swap=last_word, outstanding=(41, 42)
    )
    assert lost.serial == 2 and lost.reason is InterruptionReason.OUTPUT_LOSS and not lost.host
    assert lost.gap == timedelta(seconds=9) and (lost.old_session, lost.new_session) == (3, 4)
    assert isinstance(lost, Interruption) and watch.policy == InterruptionPolicy()


def test_an_interruption_s_thresholds_must_be_positive() -> None:
    with pytest.raises(ValueError, match="positive"):
        InterruptionPolicy(health_tick=timedelta(0))
