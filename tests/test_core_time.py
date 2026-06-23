"""Tests for omni.core.time: DurationSeconds and the timezone-aware local_date."""

from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from omni.core.time import DurationSeconds, local_date


def test_duration_seconds_rejects_negative() -> None:
    with pytest.raises(ValueError, match="cannot be negative"):
        DurationSeconds(-1)


def test_local_date_uses_the_zone_not_utc() -> None:
    # 02:00 UTC on Jun 18 is still 22:00 (Jun 17) in New York.
    now = datetime(2026, 6, 18, 2, 0, tzinfo=timezone.utc)
    assert local_date(now, ZoneInfo("America/New_York")) == date(2026, 6, 17)
    assert now.date() == date(2026, 6, 18)  # the UTC trap we avoid


def test_local_date_matches_utc_when_zone_is_utc() -> None:
    now = datetime(2026, 6, 17, 21, 0, tzinfo=timezone.utc)
    assert local_date(now, ZoneInfo("UTC")) == date(2026, 6, 17)


def test_local_date_rejects_naive_now() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        local_date(datetime(2026, 6, 18, 2, 0), ZoneInfo("America/New_York"))
