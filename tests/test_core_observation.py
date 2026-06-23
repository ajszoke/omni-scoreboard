"""Tests for ProviderSequence and the Observation envelope."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from omni.core.enum import League
from omni.core.ids import LeagueScopedId, ProviderSequence, SourceRef
from omni.core.observation import Observation

SOURCE = SourceRef("mlb_statsapi", "https://statsapi.mlb.com")
SUBJECT = LeagueScopedId(League.MLB, SOURCE, "777")
OBSERVED = datetime(2026, 6, 17, 23, 30, tzinfo=timezone.utc)
HAPPENED = datetime(2026, 6, 17, 23, 29, tzinfo=timezone.utc)


def test_provider_sequence_orders_by_value() -> None:
    assert ProviderSequence(1) < ProviderSequence(2)
    assert ProviderSequence(5) == ProviderSequence(5)
    assert max(ProviderSequence(3), ProviderSequence(7)) == ProviderSequence(7)


def test_provider_sequence_rejects_negative() -> None:
    with pytest.raises(ValueError, match="cannot be negative"):
        ProviderSequence(-1)


def test_observation_carries_value_and_lineage() -> None:
    obs = Observation(
        subject_id=SUBJECT,
        source=SOURCE,
        observed_at=OBSERVED,
        value={"score": 3},
        source_time=HAPPENED,
        sequence=ProviderSequence(42),
    )
    assert obs.value == {"score": 3}
    assert obs.subject_id is SUBJECT
    assert obs.source_time == HAPPENED
    assert obs.sequence == ProviderSequence(42)


def test_observation_source_time_and_sequence_default_to_none() -> None:
    obs = Observation(subject_id=SUBJECT, source=SOURCE, observed_at=OBSERVED, value=7)
    assert obs.source_time is None
    assert obs.sequence is None


def test_observation_rejects_naive_observed_at() -> None:
    with pytest.raises(ValueError, match="observed_at must be timezone-aware"):
        Observation(subject_id=SUBJECT, source=SOURCE, observed_at=datetime(2026, 6, 17, 23, 30), value=7)


def test_observation_rejects_naive_source_time() -> None:
    with pytest.raises(ValueError, match="source_time must be timezone-aware"):
        Observation(
            subject_id=SUBJECT,
            source=SOURCE,
            observed_at=OBSERVED,
            value=7,
            source_time=datetime(2026, 6, 17, 23, 29),
        )
