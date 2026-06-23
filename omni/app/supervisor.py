"""ProviderSupervisor: a resilient wrapper around one `Provider`.

A running appliance must survive a flaky network: a failed refresh cannot crash the
loop or blank the screen. The supervisor calls ``refresh(now)``, **isolates any
failure**, keeps the last successful `ProviderUpdate` as last-known-good, and reports
a typed status + age so the orchestrator can decide when a stale snapshot should
yield to a typed offline card. After a failure it **backs off**, so a source that is
down is not hammered every tick.

It owns resilience only — poll cadence is the caller's (the loop calls ``poll(now)``
each tick; the supervisor self-throttles via the backoff). It takes ``now`` as a
parameter rather than holding a `Clock`, so it stays pure and deterministic.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from omni.core.enum import StrEnumMixin
from omni.core.time import DurationSeconds
from omni.providers.base import Provider, ProviderUpdate

__all__ = ["ProviderStatus", "BackoffPolicy", "SupervisedSnapshot", "ProviderSupervisor"]


class ProviderStatus(StrEnumMixin, str, Enum):
    """How trustworthy the supervisor's current snapshot is."""

    NEVER_LOADED = "never_loaded"  # no successful refresh yet — nothing to show
    FRESH = "fresh"  # last-known-good is within max_age
    STALE = "stale"  # last-known-good is older than max_age — consider an offline card


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


@dataclass(frozen=True, slots=True, kw_only=True)
class SupervisedSnapshot:
    """The best data available right now, plus how much to trust it."""

    update: ProviderUpdate | None
    status: ProviderStatus
    age: DurationSeconds | None  # since the last successful refresh; None if never loaded
    consecutive_failures: int
    last_error: str | None


class ProviderSupervisor:
    """Wraps a `Provider`, surviving failures and serving last-known-good data."""

    def __init__(
        self,
        provider: Provider,
        *,
        max_age: DurationSeconds,
        backoff: BackoffPolicy | None = None,
    ) -> None:
        self._provider = provider
        self._max_age = max_age
        self._backoff = backoff if backoff is not None else BackoffPolicy()
        self._update: ProviderUpdate | None = None
        self._failures = 0
        self._last_error: str | None = None
        self._next_retry_at: datetime | None = None

    def ready_to_poll(self, now: datetime) -> bool:
        """Whether the backoff window has elapsed and a refresh should be attempted."""
        return self._next_retry_at is None or now >= self._next_retry_at

    def poll(self, now: datetime) -> SupervisedSnapshot:
        """Attempt a refresh (unless backing off) and return the current snapshot."""
        if not self.ready_to_poll(now):
            return self.current(now)
        try:
            update = self._provider.refresh(now)
        except Exception as exc:  # resilience boundary: survive ANY provider failure
            self._failures += 1
            self._last_error = f"{type(exc).__name__}: {exc}"
            self._next_retry_at = now + self._backoff.delay_after(self._failures).as_timedelta()
            return self.current(now)
        self._update = update
        self._failures = 0
        self._last_error = None
        self._next_retry_at = None
        return self.current(now)

    def current(self, now: datetime) -> SupervisedSnapshot:
        """The best snapshot available as of ``now`` (no refresh attempted)."""
        if self._update is None:
            return SupervisedSnapshot(
                update=None,
                status=ProviderStatus.NEVER_LOADED,
                age=None,
                consecutive_failures=self._failures,
                last_error=self._last_error,
            )
        # Clamp to >= 0 so backwards clock skew can't produce a negative duration.
        age = DurationSeconds(max(0, int((now - self._update.observed_at).total_seconds())))
        status = ProviderStatus.STALE if age.value > self._max_age.value else ProviderStatus.FRESH
        return SupervisedSnapshot(
            update=self._update,
            status=status,
            age=age,
            consecutive_failures=self._failures,
            last_error=self._last_error,
        )
