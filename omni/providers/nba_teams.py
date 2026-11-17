"""NBA team registry: abbreviations -> typed `BasketballTeam`s, with their logo tiles and colors.

The club table is owned by `omni` so the typed island stays self-contained; names, conferences and
divisions are BALLDONTLIE's, and its abbreviations are the canonical keys. Feeds don't agree on a few
abbreviations, so each spelling a source uses resolves to one canonical abbreviation.
"""

from __future__ import annotations

from collections.abc import Mapping

from omni.core.enum import League
from omni.core.ids import LeagueScopedId, SourceRef
from omni.domain.base import LogoAsset
from omni.domain.teams import BasketballTeam
from omni.providers.nba_palette import FREED_BASE_METER_COLOR, LOGO_ALT_COLOR, LOGO_BASE_COLOR

__all__ = ["NBA_TEAMS", "NbaTeamRegistry", "canonical_abbreviation"]

_SOURCE = SourceRef("nba", None)

# abbreviation -> (full name, nickname, conference, division). The 30 current clubs.
NBA_TEAMS: dict[str, tuple[str, str, str, str]] = {
    "ATL": ("Atlanta Hawks", "Hawks", "East", "Southeast"),
    "BOS": ("Boston Celtics", "Celtics", "East", "Atlantic"),
    "BKN": ("Brooklyn Nets", "Nets", "East", "Atlantic"),
    "CHA": ("Charlotte Hornets", "Hornets", "East", "Southeast"),
    "CHI": ("Chicago Bulls", "Bulls", "East", "Central"),
    "CLE": ("Cleveland Cavaliers", "Cavaliers", "East", "Central"),
    "DAL": ("Dallas Mavericks", "Mavericks", "West", "Southwest"),
    "DEN": ("Denver Nuggets", "Nuggets", "West", "Northwest"),
    "DET": ("Detroit Pistons", "Pistons", "East", "Central"),
    "GSW": ("Golden State Warriors", "Warriors", "West", "Pacific"),
    "HOU": ("Houston Rockets", "Rockets", "West", "Southwest"),
    "IND": ("Indiana Pacers", "Pacers", "East", "Central"),
    "LAC": ("Los Angeles Clippers", "Clippers", "West", "Pacific"),
    "LAL": ("Los Angeles Lakers", "Lakers", "West", "Pacific"),
    "MEM": ("Memphis Grizzlies", "Grizzlies", "West", "Southwest"),
    "MIA": ("Miami Heat", "Heat", "East", "Southeast"),
    "MIL": ("Milwaukee Bucks", "Bucks", "East", "Central"),
    "MIN": ("Minnesota Timberwolves", "Timberwolves", "West", "Northwest"),
    "NOP": ("New Orleans Pelicans", "Pelicans", "West", "Southwest"),
    "NYK": ("New York Knicks", "Knicks", "East", "Atlantic"),
    "OKC": ("Oklahoma City Thunder", "Thunder", "West", "Northwest"),
    "ORL": ("Orlando Magic", "Magic", "East", "Southeast"),
    "PHI": ("Philadelphia 76ers", "76ers", "East", "Atlantic"),
    "PHX": ("Phoenix Suns", "Suns", "West", "Pacific"),
    "POR": ("Portland Trail Blazers", "Trail Blazers", "West", "Northwest"),
    "SAC": ("Sacramento Kings", "Kings", "West", "Pacific"),
    "SAS": ("San Antonio Spurs", "Spurs", "West", "Southwest"),
    "TOR": ("Toronto Raptors", "Raptors", "East", "Atlantic"),
    "UTA": ("Utah Jazz", "Jazz", "West", "Northwest"),
    "WAS": ("Washington Wizards", "Wizards", "East", "Southeast"),
}

# Other spellings feeds use -> the canonical abbreviation. "LA" is left out: it could be either club.
_ALIASES = {
    "BRK": "BKN",
    "CHO": "CHA",
    "GS": "GSW",
    "NO": "NOP",
    "NOR": "NOP",
    "NY": "NYK",
    "PHO": "PHX",
    "SA": "SAS",
    "UTAH": "UTA",
    "WSH": "WAS",
}


def canonical_abbreviation(raw: str) -> str:
    """The canonical abbreviation for any spelling a feed uses; `KeyError` for an unknown club."""
    abbreviation = raw.strip().upper()
    abbreviation = _ALIASES.get(abbreviation, abbreviation)
    if abbreviation not in NBA_TEAMS:
        raise KeyError(raw)
    return abbreviation


def _build(abbreviation: str, source: SourceRef) -> BasketballTeam:
    name, nickname, conference, division = NBA_TEAMS[abbreviation]
    key = abbreviation.lower()
    return BasketballTeam(
        id=LeagueScopedId(League.NBA, source, abbreviation),
        league=League.NBA,
        display_name=name,
        short_name=nickname,
        abbreviation=abbreviation,
        primary_color=LOGO_BASE_COLOR[abbreviation],
        secondary_color=LOGO_ALT_COLOR[abbreviation],
        logo=LogoAsset(
            key=f"nba/{key}",
            path=f"assets/logos/nba/{key}.png",
            preferred_background=LOGO_BASE_COLOR[abbreviation],
            meter_color=FREED_BASE_METER_COLOR.get(abbreviation),
        ),
        logo_alt=LogoAsset(
            key=f"nba/{key}-alt",
            path=f"assets/logos/nba/{key}-alt.png",
            preferred_background=LOGO_ALT_COLOR[abbreviation],
        ),
        conference=conference,
        division=division,
    )


class NbaTeamRegistry:
    """Resolves any feed's abbreviation into the typed `BasketballTeam`."""

    def __init__(self, teams: Mapping[str, BasketballTeam]) -> None:
        self._teams = dict(teams)

    @classmethod
    def default(cls, *, source: SourceRef | None = None) -> NbaTeamRegistry:
        """Every current club, from the committed table and palette."""
        src = source if source is not None else _SOURCE
        return cls({abbreviation: _build(abbreviation, src) for abbreviation in NBA_TEAMS})

    def resolve(self, raw: str) -> BasketballTeam:
        """The club a feed's abbreviation names; `KeyError` if it names none."""
        return self._teams[canonical_abbreviation(raw)]

    def __len__(self) -> int:
        return len(self._teams)
