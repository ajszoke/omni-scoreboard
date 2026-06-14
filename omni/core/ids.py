"""Source and identity value objects for the Omni domain.

Providers resolve raw API ids into :class:`LeagueScopedId` so the same numeric id
from two leagues/sources never collides, and renderers never see raw provider ids.
"""

from __future__ import annotations

from dataclasses import dataclass

from omni.core.enum import League

__all__ = ["SourceRef", "LeagueScopedId"]


@dataclass(frozen=True, slots=True)
class SourceRef:
    """Names a data source (e.g. ``mlb_statsapi``, ``espn``, ``datagolf``)."""

    name: str
    raw_url: str | None = None


@dataclass(frozen=True, slots=True)
class LeagueScopedId:
    """A raw provider id scoped by league + source."""

    league: League
    source: SourceRef
    raw: str

    def __str__(self) -> str:
        return f"{self.league}:{self.source.name}:{self.raw}"
