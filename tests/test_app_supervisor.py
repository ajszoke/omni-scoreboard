"""Tests for ProviderSupervisor: failure isolation, last-known-good, backoff."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from omni.app.supervisor import BackoffPolicy, ProviderStatus, ProviderSupervisor
from omni.core.enum import League
from omni.core.ids import SourceRef
from omni.core.time import DurationSeconds
from omni.providers.base import ProviderError, ProviderUpdate

T = datetime(2026, 6, 17, 23, 0, 0, tzinfo=timezone.utc)
SOURCE = SourceRef("mlb_statsapi")
MAX_AGE = DurationSeconds(120)


class _FakeProvider:
    """A Provider whose refresh succeeds or fails on command, counting calls."""

    source = SOURCE
    league = League.MLB

    def __init__(self) -> None:
        self.calls = 0
        self.fail = False

    def refresh(self, now: datetime) -> ProviderUpdate:
        self.calls += 1
        if self.fail:
            raise ProviderError("network down")
        return ProviderUpdate(source=SOURCE, observed_at=now, warnings=("ignored row",))


def test_never_loaded_before_first_poll() -> None:
    snap = ProviderSupervisor(_FakeProvider(), max_age=MAX_AGE).current(T)
    assert snap.status is ProviderStatus.NEVER_LOADED
    assert snap.update is None and snap.age is None


def test_successful_poll_is_fresh() -> None:
    snap = ProviderSupervisor(_FakeProvider(), max_age=MAX_AGE).poll(T)
    assert snap.status is ProviderStatus.FRESH
    assert snap.update is not None and snap.age == DurationSeconds(0)
    assert snap.consecutive_failures == 0 and snap.last_error is None


def test_snapshot_goes_stale_past_max_age() -> None:
    sup = ProviderSupervisor(_FakeProvider(), max_age=MAX_AGE)
    sup.poll(T)
    assert sup.current(T + timedelta(seconds=120)).status is ProviderStatus.FRESH  # at the boundary
    assert sup.current(T + timedelta(seconds=121)).status is ProviderStatus.STALE


def test_failure_keeps_last_known_good() -> None:
    provider = _FakeProvider()
    sup = ProviderSupervisor(provider, max_age=MAX_AGE)
    good = sup.poll(T).update
    provider.fail = True
    snap = sup.poll(T + timedelta(seconds=300))  # ready again (no prior failure backoff), now fails
    assert snap.update is good  # still serving the last good update
    assert snap.status is ProviderStatus.STALE  # but flagged stale by age
    assert snap.consecutive_failures == 1
    assert snap.last_error is not None and "network down" in snap.last_error


def test_first_failure_with_no_data_stays_never_loaded() -> None:
    provider = _FakeProvider()
    provider.fail = True
    snap = ProviderSupervisor(provider, max_age=MAX_AGE).poll(T)
    assert snap.status is ProviderStatus.NEVER_LOADED
    assert snap.consecutive_failures == 1 and snap.update is None


def test_backoff_suppresses_polling_until_window_elapses() -> None:
    provider = _FakeProvider()
    provider.fail = True
    sup = ProviderSupervisor(provider, max_age=MAX_AGE, backoff=BackoffPolicy(base=DurationSeconds(5)))
    sup.poll(T)  # 1st failure -> backoff 5s
    assert provider.calls == 1
    assert not sup.ready_to_poll(T + timedelta(seconds=3))
    sup.poll(T + timedelta(seconds=3))  # within backoff -> provider NOT called again
    assert provider.calls == 1
    assert sup.ready_to_poll(T + timedelta(seconds=5))
    sup.poll(T + timedelta(seconds=5))  # backoff elapsed -> attempted
    assert provider.calls == 2


def test_recovery_resets_failures_and_backoff() -> None:
    provider = _FakeProvider()
    provider.fail = True
    sup = ProviderSupervisor(provider, max_age=MAX_AGE, backoff=BackoffPolicy(base=DurationSeconds(5)))
    sup.poll(T)
    provider.fail = False
    snap = sup.poll(T + timedelta(seconds=5))
    assert snap.status is ProviderStatus.FRESH
    assert snap.consecutive_failures == 0 and snap.last_error is None
    assert sup.ready_to_poll(T + timedelta(seconds=6))  # backoff cleared


def test_backoff_policy_doubles_and_caps() -> None:
    policy = BackoffPolicy(base=DurationSeconds(5), cap=DurationSeconds(30))
    assert policy.delay_after(0) == DurationSeconds(0)
    assert policy.delay_after(1) == DurationSeconds(5)
    assert policy.delay_after(2) == DurationSeconds(10)
    assert policy.delay_after(3) == DurationSeconds(20)
    assert policy.delay_after(4) == DurationSeconds(30)  # 40 capped to 30
    assert policy.delay_after(99) == DurationSeconds(30)  # stays capped, no overflow


def test_age_never_negative_under_clock_skew() -> None:
    sup = ProviderSupervisor(_FakeProvider(), max_age=MAX_AGE)
    sup.poll(T)
    snap = sup.current(T - timedelta(seconds=10))  # now earlier than observed_at
    assert snap.age == DurationSeconds(0)
