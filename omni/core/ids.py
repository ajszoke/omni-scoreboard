"""Source and identity value objects for the Omni domain.

Providers resolve raw API ids into :class:`LeagueScopedId` so the same numeric id
from two leagues/sources never collides, and renderers never see raw provider ids.
"""

from __future__ import annotations

from dataclasses import dataclass

from omni.core.enum import League

__all__ = ["SourceRef", "LeagueScopedId", "ProviderSequence"]


@dataclass(frozen=True, slots=True)
class SourceRef:
    """Names a data source (e.g. ``mlb_statsapi``, ``espn``, ``datagolf``)."""

    name: str
    raw_url: str | None = None


@dataclass(frozen=True, slots=True, order=True)
class ProviderSequence:
    """A monotonic version stamp from a provider.

    Lets a later observation be ordered against an earlier one so out-of-order or
    stale updates are detectable (``order=True`` compares by ``value``). What the
    integer counts is provider-specific — a feed timestamp epoch, an update index;
    only the ordering is relied upon.
    """

    value: int

    def __post_init__(self) -> None:
        if self.value < 0:
            raise ValueError("provider sequence cannot be negative")


@dataclass(frozen=True, slots=True)
class LeagueScopedId:
    """A raw provider id scoped by league + source."""

    league: League
    source: SourceRef
    raw: str

    def __str__(self) -> str:
        return f"{self.league}:{self.source.name}:{self.raw}"
