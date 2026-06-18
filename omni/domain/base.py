"""Shared domain abstractions: the Competitor protocol and logo assets."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from omni.core.colors import RGBColor
from omni.core.ids import LeagueScopedId

__all__ = ["Competitor", "LogoAsset"]


@runtime_checkable
class Competitor(Protocol):
    """A contest participant — a team or an individual athlete.

    The general concept is modeled first because not every league has teams
    (golf is individuals). Members are read-only properties so frozen dataclasses
    (`Team`, `Golfer`) structurally satisfy the protocol.
    """

    @property
    def id(self) -> LeagueScopedId: ...

    @property
    def display_name(self) -> str: ...

    @property
    def short_name(self) -> str: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class LogoAsset:
    """A logo image reference resolved by a provider/registry, not a renderer."""

    key: str
    path: str
    preferred_background: RGBColor | None = None
