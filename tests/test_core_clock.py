"""Tests for the Clock seam: SystemClock and the deterministic FakeClock."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from omni.core.clock import Clock, ClockReading, FakeClock, MonotonicTime, Pacer, SystemClock, boottime
from omni.core.time import DurationSeconds

T = datetime(2026, 6, 17, 23, 30, tzinfo=timezone.utc)


def test_system_clock_returns_aware_utc_now() -> None:
    clock = SystemClock()
    assert isinstance(clock, Clock)
    moment = clock.now()
    assert moment.tzinfo is not None
    assert moment.utcoffset() == timedelta(0)  # UTC


def test_fake_clock_reports_and_advances_deterministically() -> None:
    clock = FakeClock(T)
    assert isinstance(clock, Clock)
    assert clock.now() == T
    clock.advance(DurationSeconds(90))
    assert clock.now() == T + timedelta(seconds=90)
    clock.set(T)
    assert clock.now() == T


def test_fake_clock_rejects_naive_construction() -> None:
    with pytest.raises(ValueError, match="must be timezone-aware"):
        FakeClock(datetime(2026, 6, 17, 23, 30))


def test_fake_clock_rejects_naive_set() -> None:
    clock = FakeClock(T)
    with pytest.raises(ValueError, match="must be timezone-aware"):
        clock.set(datetime(2026, 6, 17, 23, 30))


def test_a_clock_reads_both_ways() -> None:
    clock = SystemClock()
    first, second = clock.read(), clock.read()
    assert first.utc.utcoffset() == timedelta(0)
    assert second.monotonic >= first.monotonic  # monotonic time never runs backward
    assert isinstance(clock.monotonic(), MonotonicTime)


def test_a_clock_reading_is_timezone_aware() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        ClockReading(datetime(2026, 6, 17, 23, 30), MonotonicTime(0.0))


def test_monotonic_time_measures_spans() -> None:
    start = MonotonicTime(5.0)
    later = start + timedelta(seconds=2.5)
    assert later.since(start) == timedelta(seconds=2.5)
    assert start < later


def test_fake_time_passing_moves_both_readings_and_a_wall_jump_moves_one() -> None:
    clock = FakeClock(T)
    clock.advance(timedelta(milliseconds=1500))
    assert clock.read() == ClockReading(T + timedelta(seconds=1.5), MonotonicTime(1.5))
    clock.set(T + timedelta(hours=1))  # the wall clock jumps an hour; no time has passed
    assert clock.now() == T + timedelta(hours=1)
    assert clock.monotonic() == MonotonicTime(1.5)


def test_a_wall_jump_either_way_leaves_monotonic_time_alone() -> None:
    clock = FakeClock(T)
    clock.advance(timedelta(seconds=10))
    clock.jump(timedelta(minutes=-5))
    assert clock.read() == ClockReading(T + timedelta(seconds=10) - timedelta(minutes=5), MonotonicTime(10))


def test_a_reading_puts_a_monotonic_instant_on_its_own_wall_clock() -> None:
    reading = ClockReading(T, MonotonicTime(100.0))
    assert reading.project(MonotonicTime(145.0)) == T + timedelta(seconds=45)
    assert reading.project(MonotonicTime(40.0)) == T - timedelta(minutes=1)


def test_fake_time_cannot_pass_backward() -> None:
    with pytest.raises(ValueError, match="can't pass backward"):
        FakeClock(T).advance(timedelta(seconds=-1))


def test_a_pacer_keeps_a_steady_beat_and_starts_over_when_behind() -> None:
    clock = FakeClock(T)
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(round(seconds, 6))
        clock.advance(timedelta(seconds=seconds))

    pacer = Pacer(clock, timedelta(milliseconds=100), sleep=sleep)
    clock.advance(timedelta(milliseconds=30))  # the work takes 30ms
    pacer.wait()
    assert slept == [0.07] and clock.monotonic() == MonotonicTime(0.1)
    clock.advance(timedelta(milliseconds=250))  # a stall: past the next beat
    pacer.wait()
    assert slept == [0.07]  # no sleep, and no burst of beats to catch up
    clock.advance(timedelta(milliseconds=40))
    pacer.wait()
    assert slept == [0.07, 0.06]  # the beat restarted from when it fell behind


def test_a_pacer_needs_a_positive_beat() -> None:
    with pytest.raises(ValueError, match="positive beat"):
        Pacer(FakeClock(T), timedelta(0))


def test_a_reading_carries_no_suspend_inclusive_clock_unless_the_host_has_one() -> None:
    assert ClockReading(T, MonotonicTime(1.0)).suspend_inclusive is None
    assert FakeClock(T).read().suspend_inclusive is None
    assert SystemClock(suspend_inclusive=None).read().suspend_inclusive is None
    assert SystemClock(suspend_inclusive=lambda: 42.5).read().suspend_inclusive == MonotonicTime(42.5)


def test_the_platform_s_suspend_inclusive_clock_is_boottime_where_there_is_one(monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    if hasattr(time, "CLOCK_BOOTTIME"):
        read = boottime()
        assert read is not None and read() >= 0.0
        monkeypatch.delattr(time, "CLOCK_BOOTTIME")
    assert boottime() is None


def test_a_faked_sleep_moves_the_wall_and_suspend_inclusive_clocks_and_leaves_the_monotonic_one() -> None:
    clock = FakeClock(T, suspend_inclusive=MonotonicTime(100.0))
    clock.advance(timedelta(seconds=10))
    assert clock.read() == ClockReading(T + timedelta(seconds=10), MonotonicTime(10.0), MonotonicTime(110.0))
    clock.suspend(timedelta(minutes=49))
    woke = clock.read()
    assert woke.monotonic == MonotonicTime(10.0)
    assert woke.utc == T + timedelta(minutes=49, seconds=10)
    assert woke.suspend_inclusive == MonotonicTime(110.0 + 49 * 60)
    clock.jump(timedelta(seconds=-30))  # a wall-clock correction alone
    assert clock.read().suspend_inclusive == woke.suspend_inclusive and clock.monotonic() == woke.monotonic
    plain = FakeClock(T)
    plain.suspend(DurationSeconds(60))  # a host without the clock: only the wall clock shows the sleep
    assert plain.read() == ClockReading(T + timedelta(seconds=60), MonotonicTime(0.0))
    with pytest.raises(ValueError, match="backward"):
        plain.suspend(timedelta(seconds=-1))
