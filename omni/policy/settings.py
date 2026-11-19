"""The board policy's user settings (`BOARD_POLICY.md` §8): which policy decides, mutes, league weights, the turn,
and whether promotions may cut in. Everything else is the versioned policy. Favorites and each league's delay
are set where they always were (`omni.preferences.favorites`, the app's delay flags)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from types import MappingProxyType

from omni.core.enum import League
from omni.domain.contest import TeamGame
from omni.policy.names import PolicyName

__all__ = ["BOARD_POLICIES", "Mutes", "PolicySettings"]

BOARD_POLICIES = (PolicyName.BOARD_V1, PolicyName.BOARD_V2)  # the shared board's policies a run may choose
_WEIGHTS = (0.5, 2.0)  # a league's weight is allowed within these
_SLOTS = (timedelta(seconds=8), timedelta(seconds=30))  # a safe turn length


@dataclass(frozen=True, slots=True)
class Mutes:
    """Leagues and teams the board never shows. A muted team's games are muted whoever they play, a favorite
    included. Teams are canonical abbreviations, per league."""

    leagues: frozenset[League] = frozenset()
    teams: Mapping[League, frozenset[str]] = field(default_factory=lambda: MappingProxyType({}))

    @classmethod
    def of(cls, leagues: Iterable[League] = (), teams: Mapping[League, Iterable[str]] | None = None) -> Mutes:
        chosen = teams if teams is not None else {}
        return cls(frozenset(leagues), MappingProxyType({league: frozenset(t) for league, t in chosen.items() if t}))

    def mutes(self, game: TeamGame) -> bool:
        """Whether ``game`` is muted: its league, or either team."""
        if game.league in self.leagues:
            return True
        muted = self.teams.get(game.league, frozenset())
        return game.away.abbreviation in muted or game.home.abbreviation in muted

    def __bool__(self) -> bool:
        return bool(self.leagues) or any(self.teams.values())

    def __str__(self) -> str:
        """``mlb, nfl:DAL`` style, in a fixed order; ``none`` when nothing is muted."""
        parts = [league.value for league in League if league in self.leagues]
        parts += [f"{league.value}:{team}" for league in League for team in sorted(self.teams.get(league, ()))]
        return ", ".join(parts) or "none"


@dataclass(frozen=True, slots=True, kw_only=True)
class PolicySettings:
    """What the user may set about how the shared board shares its airtime."""

    policy: PolicyName = PolicyName.BOARD_V2
    mutes: Mutes = Mutes()
    league_weights: Mapping[League, float] = field(default_factory=lambda: MappingProxyType({}))
    slot: timedelta = timedelta(seconds=12)
    promotions: bool = True  # off keeps the weighted live rotation, with no cut-ins

    def __post_init__(self) -> None:
        if self.policy not in BOARD_POLICIES:
            raise ValueError(f"{self.policy.value} isn't a shared-board policy")
        low, high = _WEIGHTS
        for league, weight in self.league_weights.items():
            if not low <= weight <= high:
                raise ValueError(f"{league.value}'s weight {weight} is outside {low}-{high}")
        if not _SLOTS[0] <= self.slot <= _SLOTS[1]:
            raise ValueError(f"a turn of {self.slot.total_seconds():g}s is outside 8-30s")

    def weight(self, league: League) -> float:
        """A league's weight: 1 unless the user set one."""
        return self.league_weights.get(league, 1.0)
