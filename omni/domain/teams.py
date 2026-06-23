"""Team competitors for team-sport leagues."""

from __future__ import annotations

from dataclasses import dataclass

from omni.core.colors import RGBColor
from omni.core.enum import League
from omni.core.ids import LeagueScopedId
from omni.domain.base import LogoAsset

__all__ = ["Team", "BaseballTeam"]


@dataclass(frozen=True, slots=True, kw_only=True)
class Team:
    """A team competitor carrying the colors/logo needed to render it."""

    id: LeagueScopedId
    league: League
    display_name: str
    short_name: str
    abbreviation: str
    primary_color: RGBColor
    secondary_color: RGBColor
    logo: LogoAsset

    def __post_init__(self) -> None:
        # `league` is also carried by `id`; they must never disagree.
        if self.league != self.id.league:
            raise ValueError(f"team league {self.league} disagrees with id.league {self.id.league}")

    def best_text_color_on_primary(self) -> RGBColor:
        """White or black text, whichever contrasts better on the primary color."""
        white = RGBColor(255, 255, 255)
        black = RGBColor(0, 0, 0)
        if white.contrast_ratio(self.primary_color) >= black.contrast_ratio(self.primary_color):
            return white
        return black


@dataclass(frozen=True, slots=True, kw_only=True)
class BaseballTeam(Team):
    """MLB specialization; division/league_side stay loose until modeled."""

    division: str | None = None
    league_side: str | None = None  # AL/NL
