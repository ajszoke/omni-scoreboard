"""The provider contract shared by every data source.

A `Provider` turns one upstream source into typed domain objects. The raw API
shape lives entirely inside the concrete provider module (e.g.
`omni.providers.mlb_statsapi`); callers only ever touch a `ProviderUpdate`.

Once received, an answer belongs to a **scope** (`PollScope`): the source, the league, and the window it
covers, such as one local date's slate. A `ScopeObservation` is one scope's answer, or its failure, stamped
with when it arrived. It says how much of the scope it covers:
- **complete:** the whole scope, so a contest it leaves out has left;
- **partial:** the source skipped rows it couldn't read, so a contest it leaves out isn't refuted;
- **failed:** nothing usable came back.

A `ScopeSource` is a league's schedule seen this way. Each pass it names the scopes its window covers
(`ScopeSpan`) and hands over what each scope it asked last said (`ScopeReport`).
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Protocol, runtime_checkable

from omni.core.clock import ClockReading
from omni.core.enum import League, StrEnumMixin
from omni.core.ids import SourceRef
from omni.domain.contest import Contest

__all__ = [
    "ScopeSpan",
    "CoverageWindow",
    "CurrentSlate",
    "LocalDate",
    "PollScope",
    "Provider",
    "ProviderError",
    "ProviderUpdate",
    "ScopeObservation",
    "ScopeOutcome",
    "ScopeReport",
    "ScopeSource",
]


class ProviderError(Exception):
    """A provider could not produce a usable update (e.g. the fetch failed).

    Per-row problems (an unknown team, a malformed game) are reported as
    `ProviderUpdate.warnings` and skipped; `ProviderError` is reserved for a
    whole-update failure where there is nothing usable to return.
    """


@dataclass(frozen=True, slots=True, kw_only=True)
class ProviderUpdate:
    """One typed answer from a source: the contests it listed, and a warning for each row it skipped.

    ``observed_at`` is the UTC instant the source was asked as of. Nothing is ordered by it: an answer is
    ordered by its receipt, on the monotonic clock, once it's a `ScopeObservation`. A game's plays travel
    with its own feed, never here.
    """

    source: SourceRef
    observed_at: datetime
    contests: tuple[Contest, ...] = ()
    warnings: tuple[str, ...] = ()


@runtime_checkable
class Provider(Protocol):
    """A single source of typed updates for one league.

    Members are read-only properties so a concrete provider may back them with
    either plain attributes or properties and still structurally satisfy this.
    """

    @property
    def source(self) -> SourceRef: ...

    @property
    def league(self) -> League: ...

    def refresh(self, now: datetime) -> ProviderUpdate:
        """Fetch and parse the source as of `now`, returning typed domain objects."""
        ...


@dataclass(frozen=True, slots=True)
class LocalDate:
    """A window of one local calendar date: that date's slate."""

    day: date


@dataclass(frozen=True, slots=True)
class CurrentSlate:
    """A window of whatever the source answers as its current slate, asked without a date."""


# The windows a scope can cover. A week or a "live now" window joins this union.
CoverageWindow = LocalDate | CurrentSlate


@dataclass(frozen=True, slots=True)
class PollScope:
    """What one ask covers: one source's contests in one league over one window."""

    source: SourceRef
    league: League
    window: CoverageWindow


class ScopeOutcome(StrEnumMixin, str, Enum):
    """How much of its scope an answer covers."""

    COMPLETE = "complete"  # the whole scope: what it leaves out has left
    PARTIAL = "partial"  # the source skipped rows: what it leaves out isn't refuted
    FAILED = "failed"  # nothing usable: the scope keeps what it had


@dataclass(frozen=True, slots=True, kw_only=True)
class ScopeObservation:
    """One scope's answer, or its failure, stamped with when it arrived (both clock readings)."""

    scope: PollScope
    outcome: ScopeOutcome
    received: ClockReading
    contests: tuple[Contest, ...] = ()
    warnings: tuple[str, ...] = ()
    error: str | None = None  # why it failed; a failure always says, and an answer never does

    def __post_init__(self) -> None:
        failed = self.outcome is ScopeOutcome.FAILED
        if failed != (self.error is not None):
            raise ValueError("a failed observation says why it failed, and an answer has no error")
        if failed and self.contests:
            raise ValueError("a failed observation lists no contests")
        if any(contest.league is not self.scope.league for contest in self.contests):
            raise ValueError(f"every contest of a {self.scope.league} scope is a {self.scope.league} contest")

    @classmethod
    def answer(cls, scope: PollScope, update: ProviderUpdate, received: ClockReading) -> ScopeObservation:
        """The source's answer for ``scope``: complete, or partial when it skipped rows it couldn't read."""
        outcome = ScopeOutcome.PARTIAL if update.warnings else ScopeOutcome.COMPLETE
        return cls(scope=scope, outcome=outcome, received=received, contests=update.contests, warnings=update.warnings)

    @classmethod
    def failure(cls, scope: PollScope, error: str, received: ClockReading) -> ScopeObservation:
        """The source failed to answer for ``scope``; ``received`` is when the failure was known."""
        return cls(scope=scope, outcome=ScopeOutcome.FAILED, received=received, error=error)


@dataclass(frozen=True, slots=True, kw_only=True)
class ScopeSpan:
    """The scopes one source covers for one league now: its current one (the league's health is that scope's)
    and the earlier ones still in its window, newest first, such as yesterday's slate while a late game runs
    past midnight. ``max_age`` is how long an answer of any of them counts as fresh."""

    current: PollScope
    earlier: tuple[PollScope, ...] = ()
    max_age: timedelta

    def __post_init__(self) -> None:
        for scope in self.earlier:
            if (scope.source, scope.league) != (self.current.source, self.current.league):
                raise ValueError("a span's scopes share one source and one league")
        if self.current in self.earlier or len(set(self.earlier)) != len(self.earlier):
            raise ValueError("a span names each scope once")
        if self.max_age <= timedelta(0):
            raise ValueError("a span's max age must be positive")

    @property
    def scopes(self) -> tuple[PollScope, ...]:
        return (self.current, *self.earlier)


@dataclass(frozen=True, slots=True, kw_only=True)
class ScopeReport:
    """One pass over a source's scopes: what it covers now, and what each scope it asked last said."""

    span: ScopeSpan
    observations: tuple[ScopeObservation, ...] = ()


class ScopeSource(Protocol):
    """A league's schedule, scope by scope."""

    def observe(self, at: ClockReading, held: Collection[PollScope]) -> ScopeReport:
        """Ask the scopes in its window, and those of its own the board still ``held``, as of ``at``; report what
        each last said."""
        ...
