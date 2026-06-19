"""The provider contract shared by every data source.

A `Provider` turns one upstream source into typed domain objects. The raw API
shape lives entirely inside the concrete provider module (e.g.
`omni.providers.mlb_statsapi`); callers only ever touch a `ProviderUpdate`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from omni.core.enum import League
from omni.core.ids import SourceRef
from omni.domain.contest import Contest
from omni.events.base import GameEvent

__all__ = ["Provider", "ProviderError", "ProviderUpdate"]


class ProviderError(Exception):
    """A provider could not produce a usable update (e.g. the fetch failed).

    Per-row problems (an unknown team, a malformed game) are reported as
    `ProviderUpdate.warnings` and skipped; `ProviderError` is reserved for a
    whole-update failure where there is nothing usable to return.
    """


@dataclass(frozen=True, slots=True, kw_only=True)
class ProviderUpdate:
    """One typed snapshot from a source: the contests (and, later, events) it saw."""

    source: SourceRef
    observed_at: datetime
    contests: tuple[Contest, ...] = ()
    # Events are heterogeneous across sports (each is typed by its own
    # event-type/payload), so `Any` is the honest type at this aggregate
    # boundary. Populated once play-by-play parsing lands; empty for now.
    events: tuple[GameEvent[Any, Any], ...] = ()
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
