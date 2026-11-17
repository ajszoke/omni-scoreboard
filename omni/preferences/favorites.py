"""Favorite teams, ranked within each league.

A board's owner lists the teams they follow per league, best first: MLB ATL then COL, NFL CAR. A team is
a favorite only in its own league, so a Vikings favorite (NFL MIN) never marks the Twins (MLB MIN). Each
abbreviation is checked against its league's club table as the favorites are built, so a typo is a
configuration error instead of a team that silently never matches.

On the command line each ``--favorite`` is ``LEAGUE:ABBR``, and their order is the rank. A bare ``ABBR``
is the flag's older form, which only ever meant MLB, so it's read as ``mlb:ABBR``. That reading is a named
migration (`ConfigMigration`): each one applied is reported with the entry as given beside what it became
(`AppliedMigration`), so a run can say which it applied and the original can be shown or written back.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass
from enum import Enum

from omni.core.enum import League, StrEnumMixin
from omni.domain.contest import TeamGame
from omni.domain.teams import Team
from omni.providers import mlb_teams, nba_teams, nfl_teams

__all__ = ["AppliedMigration", "ConfigMigration", "Favorites", "parse_favorites"]

# Each league with a club table: how a spelling resolves to its canonical abbreviation, and every club.
_CLUBS: dict[League, tuple[Callable[[str], str], Collection[str]]] = {
    League.MLB: (mlb_teams.canonical_abbreviation, mlb_teams.MLB_ABBREVIATIONS),
    League.NFL: (nfl_teams.canonical_abbreviation, nfl_teams.NFL_TEAMS),
    League.NBA: (nba_teams.canonical_abbreviation, nba_teams.NBA_TEAMS),
}


class ConfigMigration(StrEnumMixin, str, Enum):
    """A rewrite that reads a setting's older form as today's."""

    BARE_FAVORITE_IS_MLB = "bare_favorite_is_mlb"  # ``--favorite ATL`` is ``mlb:ATL``


# What each migration says when it reads an entry, filled with the entry as given and what it became.
_SAYS = {
    ConfigMigration.BARE_FAVORITE_IS_MLB: "--favorite {original} read as {migrated} (a team without a league is MLB's)"
}


@dataclass(frozen=True, slots=True)
class AppliedMigration:
    """One entry a migration rewrote: the entry exactly as given, kept beside what it became."""

    migration: ConfigMigration
    original: str
    migrated: str

    def __str__(self) -> str:
        return _SAYS[self.migration].format(original=self.original, migrated=self.migrated)


@dataclass(frozen=True, slots=True, init=False)
class Favorites:
    """The board's favorite teams: per league, an ordered tuple of abbreviations, best first.

    Built from ``{league: abbreviations}`` in rank order. Each abbreviation is resolved to its club's
    canonical spelling, and raises `ValueError` if its league has no such club. A team listed twice
    keeps its first place.
    """

    ranked: tuple[tuple[League, tuple[str, ...]], ...]

    def __init__(self, ranked: Mapping[League, Iterable[str]] | None = None) -> None:
        chosen = ranked if ranked is not None else {}
        leagues: list[tuple[League, tuple[str, ...]]] = []
        for league in League:  # a fixed order, so the same choices always make equal favorites
            teams: list[str] = []
            for raw in chosen.get(league, ()):
                abbreviation = _canonical(league, raw)
                if abbreviation not in teams:
                    teams.append(abbreviation)
            if teams:
                leagues.append((league, tuple(teams)))
        object.__setattr__(self, "ranked", tuple(leagues))

    def teams(self, league: League) -> tuple[str, ...]:
        """A league's favorites, best first."""
        for each, teams in self.ranked:
            if each is league:
                return teams
        return ()

    def rank(self, team: Team) -> int | None:
        """``team``'s rank among its own league's favorites, 1 the best, or None if it isn't one."""
        teams = self.teams(team.league)
        return teams.index(team.abbreviation) + 1 if team.abbreviation in teams else None

    def of(self, game: TeamGame) -> int | None:
        """A game's favorite rank: the better of its two teams', so two favorites meeting count once."""
        ranks = [rank for rank in (self.rank(game.away), self.rank(game.home)) if rank is not None]
        return min(ranks, default=None)

    def __bool__(self) -> bool:
        return bool(self.ranked)

    def __str__(self) -> str:
        """The chosen order, e.g. ``mlb 1 ATL, 2 COL; nfl 1 CAR``."""
        if not self.ranked:
            return "none"
        return "; ".join(
            f"{league} " + ", ".join(f"{rank} {team}" for rank, team in enumerate(teams, start=1))
            for league, teams in self.ranked
        )


def parse_favorites(entries: Iterable[str], *, note: Callable[[AppliedMigration], None]) -> Favorites:
    """Favorites from ``--favorite`` entries in rank order, each ``LEAGUE:ABBR`` or a bare ``ABBR`` for MLB.

    A bare entry is read as ``mlb:ABBR``, and ``note`` is handed that migration, once for each. Raises
    `ValueError` naming the entry for an unknown league or team.
    """
    ranked: dict[League, list[str]] = {}
    for entry in entries:
        name, colon, abbreviation = entry.partition(":")
        if not colon:
            league, abbreviation = League.MLB, name
        else:
            try:
                league = League(name.strip().lower())
            except ValueError:
                known = ", ".join(str(each) for each in _CLUBS)
                raise ValueError(f"--favorite {entry}: unknown league {name!r} (one of {known})") from None
        try:
            team = _canonical(league, abbreviation)
        except ValueError as exc:
            raise ValueError(f"--favorite {entry}: {exc}") from None
        if not colon:
            note(AppliedMigration(ConfigMigration.BARE_FAVORITE_IS_MLB, original=entry, migrated=f"mlb:{team}"))
        ranked.setdefault(league, []).append(team)
    return Favorites(ranked)


def _canonical(league: League, raw: str) -> str:
    """``raw`` in its club's canonical spelling; `ValueError` if ``league`` has no such club."""
    clubs = _CLUBS.get(league)
    if clubs is None:
        raise ValueError(f"{league.value.upper()} has no team list yet, so it can't have favorites")
    resolve, known = clubs
    try:
        return resolve(raw)
    except KeyError:
        listed = ", ".join(sorted(known))
        raise ValueError(f"no {league.value.upper()} team {raw!r} ({league.value.upper()} teams: {listed})") from None
