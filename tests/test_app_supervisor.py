"""Tests for ProviderSupervisor: a plain provider's answers and failures as observations, and its backoff."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from omni.app.supervisor import BackoffPolicy, ProviderSupervisor
from omni.core.clock import ClockReading, MonotonicTime
from omni.core.enum import GameStatus, League
from omni.core.ids import LeagueScopedId, SourceRef
from omni.core.time import DurationSeconds
from omni.domain.contest import GolfTournament
from omni.providers.base import CurrentSlate, PollScope, ProviderError, ProviderUpdate, ScopeOutcome

T = datetime(2026, 6, 17, 23, 0, 0, tzinfo=timezone.utc)
SOURCE = SourceRef("mlb_statsapi")
MAX_AGE = DurationSeconds(120)
SCOPE = PollScope(SOURCE, League.MLB, CurrentSlate())


def _at(seconds: float, *, wall: float | None = None) -> ClockReading:
    """``seconds`` on the monotonic clock; the wall clock reads ``wall`` seconds, if it has moved apart."""
    return ClockReading(T + timedelta(seconds=seconds if wall is None else wall), MonotonicTime(seconds))


class _FakeProvider:
    """A Provider whose refresh succeeds or fails on command, counting calls."""

    source = SOURCE
    league = League.MLB

    def __init__(self) -> None:
        self.calls: list[datetime] = []
        self.fail = False
        self.skipped: tuple[str, ...] = ()

    def refresh(self, now: datetime) -> ProviderUpdate:
        self.calls.append(now)
        if self.fail:
            raise ProviderError("network down")
        return ProviderUpdate(source=SOURCE, observed_at=now, warnings=self.skipped)


def test_an_answer_is_its_one_scope_s_observation_received_at_the_pass() -> None:
    provider = _FakeProvider()
    supervisor = ProviderSupervisor(provider, max_age=MAX_AGE)
    report = supervisor.observe(_at(0))
    assert supervisor.scope == SCOPE and report.span.current == SCOPE and report.span.earlier == ()
    assert report.span.max_age == timedelta(seconds=120)
    [answer] = report.observations
    assert (answer.scope, answer.outcome, answer.received) == (SCOPE, ScopeOutcome.COMPLETE, _at(0))
    assert provider.calls == [T]
    provider.skipped = ("ignored row",)
    [partial] = supervisor.observe(_at(5)).observations
    assert partial.outcome is ScopeOutcome.PARTIAL and partial.warnings == ("ignored row",)


def test_a_failure_is_a_failed_observation_saying_why() -> None:
    provider = _FakeProvider()
    provider.fail = True
    [failure] = ProviderSupervisor(provider, max_age=MAX_AGE).observe(_at(0)).observations
    assert (failure.outcome, failure.error, failure.received) == (
        ScopeOutcome.FAILED,
        "ProviderError: network down",
        _at(0),
    )


def test_an_answer_with_another_league_s_game_is_a_failure_not_a_crash() -> None:
    golf = GolfTournament(
        id=LeagueScopedId(League.PGA, SOURCE, "t1"),
        league=League.PGA,
        status=GameStatus.LIVE,
        scheduled_start=T,
        tournament_name="Open",
    )

    class _Mixed(_FakeProvider):
        def refresh(self, now: datetime) -> ProviderUpdate:
            return ProviderUpdate(source=SOURCE, observed_at=now, contests=(golf,))

    [failure] = ProviderSupervisor(_Mixed(), max_age=MAX_AGE).observe(_at(0)).observations
    assert failure.outcome is ScopeOutcome.FAILED and failure.error is not None
    assert failure.error.startswith("ValueError: every contest of a mlb scope")


def test_after_a_failure_it_backs_off_on_the_monotonic_clock() -> None:
    provider = _FakeProvider()
    provider.fail = True
    supervisor = ProviderSupervisor(provider, max_age=MAX_AGE, backoff=BackoffPolicy(base=DurationSeconds(5)))
    supervisor.observe(_at(0))  # the first failure: back off 5 s
    assert not supervisor.ready_to_poll(MonotonicTime(3))
    held_back = supervisor.observe(_at(3, wall=600))  # the wall clock jumped ten minutes ahead: still waiting
    assert held_back.observations == () and held_back.span.current == SCOPE and len(provider.calls) == 1
    assert supervisor.ready_to_poll(MonotonicTime(5))
    supervisor.observe(_at(5, wall=-600))  # and back: the wait is over when 5 s have passed
    assert len(provider.calls) == 2


def test_recovery_resets_the_backoff() -> None:
    provider = _FakeProvider()
    provider.fail = True
    supervisor = ProviderSupervisor(provider, max_age=MAX_AGE, backoff=BackoffPolicy(base=DurationSeconds(5)))
    supervisor.observe(_at(0))
    provider.fail = False
    [answer] = supervisor.observe(_at(5)).observations
    assert answer.outcome is ScopeOutcome.COMPLETE
    assert supervisor.ready_to_poll(MonotonicTime(6))  # backoff cleared


def test_backoff_policy_doubles_and_caps() -> None:
    policy = BackoffPolicy(base=DurationSeconds(5), cap=DurationSeconds(30))
    assert policy.delay_after(0) == DurationSeconds(0)
    assert policy.delay_after(1) == DurationSeconds(5)
    assert policy.delay_after(2) == DurationSeconds(10)
    assert policy.delay_after(3) == DurationSeconds(20)
    assert policy.delay_after(4) == DurationSeconds(30)  # 40 capped to 30
    assert policy.delay_after(99) == DurationSeconds(30)  # stays capped, no overflow
