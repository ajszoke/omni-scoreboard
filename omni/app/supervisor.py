"""ProviderSupervisor: a plain `Provider` as a schedule source of one scope.

A provider that isn't planned is asked inline, once a pass, for its current slate. A running appliance must
survive a flaky network, so the supervisor **isolates any failure** and hands it on as a failed observation:
the board keeps what it had, and the scope's health says how old that is. After a failure it **backs off**,
on the monotonic clock, so a source that is down is not hammered every pass and a wall-clock jump neither
stretches nor cuts the wait.

Its one scope is the provider's source and league over `CurrentSlate`. Each answer is received at the pass's
clock reading. Freshness, staleness and the last error are the store's to judge (`ContestStore`), one scope
at a time.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass

from omni.core.clock import ClockReading, MonotonicTime
from omni.core.time import DurationSeconds
from omni.providers.base import Coverage, CurrentSlate, PollScope, Provider, ScopeObservation, ScopeReport

__all__ = ["BackoffPolicy", "ProviderSupervisor"]


@dataclass(frozen=True, slots=True, kw_only=True)
class BackoffPolicy:
    """Exponential backoff between retries after consecutive failures."""

    base: DurationSeconds = DurationSeconds(5)
    cap: DurationSeconds = DurationSeconds(300)

    def delay_after(self, consecutive_failures: int) -> DurationSeconds:
        """Backoff before the next attempt: ``base * 2**(failures-1)``, capped."""
        if consecutive_failures < 1:
            return DurationSeconds(0)
        shift = min(consecutive_failures - 1, 30)  # cap the exponent before the value cap
        return DurationSeconds(min(self.base.value << shift, self.cap.value))


class ProviderSupervisor:
    """Asks a `Provider` once a pass, unless backing off, and reports its answer or failure as its one scope's."""

    def __init__(
        self,
        provider: Provider,
        *,
        max_age: DurationSeconds,
        backoff: BackoffPolicy | None = None,
    ) -> None:
        self._provider = provider
        self._coverage = Coverage(
            current=PollScope(provider.source, provider.league, CurrentSlate()), max_age=max_age.as_timedelta()
        )
        self._backoff = backoff if backoff is not None else BackoffPolicy()
        self._failures = 0
        self._next_retry_at: MonotonicTime | None = None

    @property
    def scope(self) -> PollScope:
        """The provider's current slate, the one scope it answers for."""
        return self._coverage.current

    def ready_to_poll(self, at: MonotonicTime) -> bool:
        """Whether the backoff has run out, so the provider should be asked."""
        return self._next_retry_at is None or at >= self._next_retry_at

    def observe(self, at: ClockReading, held: Collection[PollScope] = ()) -> ScopeReport:
        """Ask the provider as of ``at``, unless backing off, and report its answer or failure."""
        if not self.ready_to_poll(at.monotonic):
            return ScopeReport(coverage=self._coverage)
        try:
            answer = ScopeObservation.answer(self.scope, self._provider.refresh(at.utc), at)
        except Exception as exc:  # resilience boundary: survive ANY provider failure, a malformed answer included
            self._failures += 1
            self._next_retry_at = at.monotonic + self._backoff.delay_after(self._failures).as_timedelta()
            failure = ScopeObservation.failure(self.scope, f"{type(exc).__name__}: {exc}", at)
            return ScopeReport(coverage=self._coverage, observations=(failure,))
        self._failures = 0
        self._next_retry_at = None
        return ScopeReport(coverage=self._coverage, observations=(answer,))
