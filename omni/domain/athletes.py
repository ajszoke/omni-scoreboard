"""Individual-athlete competitors (e.g. golf)."""

from __future__ import annotations

from dataclasses import dataclass

from omni.core.ids import LeagueScopedId

__all__ = ["Golfer"]


@dataclass(frozen=True, slots=True, kw_only=True)
class Golfer:
    """An individual competitor: no team, only a personal identity."""

    id: LeagueScopedId
    display_name: str
    short_name: str
    country: str | None = None
