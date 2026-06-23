"""Tests for the Clock seam: SystemClock and the deterministic FakeClock."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from omni.app.clock import Clock, FakeClock, SystemClock
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
