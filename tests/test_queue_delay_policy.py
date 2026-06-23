"""Tests for DelayPolicy: source-time-anchored TV-delay eligibility."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from omni.core.enum import League
from omni.core.ids import LeagueScopedId, SourceRef
from omni.core.observation import Observation
from omni.core.time import DurationSeconds
from omni.queue.delay_policy import DelayAnchor, DelayPolicy

SOURCE = SourceRef("mlb_statsapi")
SUBJECT = LeagueScopedId(League.MLB, SOURCE, "777")
LAG = DurationSeconds(45)


def _obs(*, observed_at: datetime, source_time: datetime | None) -> Observation[int]:
    return Observation(subject_id=SUBJECT, source=SOURCE, observed_at=observed_at, value=0, source_time=source_time)


def test_default_anchor_is_source_time() -> None:
    assert DelayPolicy(broadcast_lag=LAG).anchor is DelayAnchor.SOURCE_TIME


def test_source_time_anchor_uses_when_the_play_happened() -> None:
    # Play happened at T; we received it 5s later. Eligible = source_time + 45s.
    happened = datetime(2026, 6, 17, 23, 0, 0, tzinfo=timezone.utc)
    received = happened + timedelta(seconds=5)
    policy = DelayPolicy(broadcast_lag=LAG)
    assert policy.eligible_at(_obs(observed_at=received, source_time=happened)) == happened + timedelta(seconds=45)


def test_source_time_anchor_falls_back_to_observed_at_when_absent() -> None:
    received = datetime(2026, 6, 17, 23, 0, 0, tzinfo=timezone.utc)
    policy = DelayPolicy(broadcast_lag=LAG)
    assert policy.eligible_at(_obs(observed_at=received, source_time=None)) == received + timedelta(seconds=45)


def test_observed_at_anchor_ignores_source_time() -> None:
    happened = datetime(2026, 6, 17, 23, 0, 0, tzinfo=timezone.utc)
    received = happened + timedelta(seconds=5)
    policy = DelayPolicy(broadcast_lag=LAG, anchor=DelayAnchor.OBSERVED_AT)
    assert policy.eligible_at(_obs(observed_at=received, source_time=happened)) == received + timedelta(seconds=45)


def test_eligibility_never_predates_receipt() -> None:
    # A stale observation (play happened long ago) is still not revealed before we
    # actually had it: the result is clamped to observed_at.
    happened = datetime(2026, 6, 17, 22, 0, 0, tzinfo=timezone.utc)
    received = datetime(2026, 6, 17, 23, 0, 0, tzinfo=timezone.utc)  # an hour later
    policy = DelayPolicy(broadcast_lag=LAG)
    assert policy.eligible_at(_obs(observed_at=received, source_time=happened)) == received
