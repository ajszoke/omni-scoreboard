"""NFL team registry: abbreviations -> typed `FootballTeam`s, with their logo tiles and colors.

The club table is owned by `omni` so the typed island stays self-contained; its provenance is nflverse's
team table (CC BY 4.0). Feeds don't agree on a few abbreviations, so each spelling a source uses
resolves to one canonical abbreviation, the one NFLMeta uses.
"""

from __future__ import annotations

from collections.abc import Mapping

from omni.core.enum import League
from omni.core.ids import LeagueScopedId, SourceRef
from omni.domain.base import LogoAsset
from omni.domain.teams import FootballTeam
from omni.providers.nfl_palette import FREED_BASE_METER_COLOR, LOGO_ALT_COLOR, LOGO_BASE_COLOR

__all__ = ["NFL_TEAMS", "NflTeamRegistry", "canonical_abbreviation"]

_SOURCE = SourceRef("nfl", None)

# abbreviation -> (full name, nickname, conference, division). The 32 current clubs.
NFL_TEAMS: dict[str, tuple[str, str, str, str]] = {
    "ARI": ("Arizona Cardinals", "Cardinals", "NFC", "NFC West"),
    "ATL": ("Atlanta Falcons", "Falcons", "NFC", "NFC South"),
    "BAL": ("Baltimore Ravens", "Ravens", "AFC", "AFC North"),
    "BUF": ("Buffalo Bills", "Bills", "AFC", "AFC East"),
    "CAR": ("Carolina Panthers", "Panthers", "NFC", "NFC South"),
    "CHI": ("Chicago Bears", "Bears", "NFC", "NFC North"),
    "CIN": ("Cincinnati Bengals", "Bengals", "AFC", "AFC North"),
    "CLE": ("Cleveland Browns", "Browns", "AFC", "AFC North"),
    "DAL": ("Dallas Cowboys", "Cowboys", "NFC", "NFC East"),
    "DEN": ("Denver Broncos", "Broncos", "AFC", "AFC West"),
    "DET": ("Detroit Lions", "Lions", "NFC", "NFC North"),
    "GB": ("Green Bay Packers", "Packers", "NFC", "NFC North"),
    "HOU": ("Houston Texans", "Texans", "AFC", "AFC South"),
    "IND": ("Indianapolis Colts", "Colts", "AFC", "AFC South"),
    "JAX": ("Jacksonville Jaguars", "Jaguars", "AFC", "AFC South"),
    "KC": ("Kansas City Chiefs", "Chiefs", "AFC", "AFC West"),
    "LAC": ("Los Angeles Chargers", "Chargers", "AFC", "AFC West"),
    "LAR": ("Los Angeles Rams", "Rams", "NFC", "NFC West"),
    "LV": ("Las Vegas Raiders", "Raiders", "AFC", "AFC West"),
    "MIA": ("Miami Dolphins", "Dolphins", "AFC", "AFC East"),
    "MIN": ("Minnesota Vikings", "Vikings", "NFC", "NFC North"),
    "NE": ("New England Patriots", "Patriots", "AFC", "AFC East"),
    "NO": ("New Orleans Saints", "Saints", "NFC", "NFC South"),
    "NYG": ("New York Giants", "Giants", "NFC", "NFC East"),
    "NYJ": ("New York Jets", "Jets", "AFC", "AFC East"),
    "PHI": ("Philadelphia Eagles", "Eagles", "NFC", "NFC East"),
    "PIT": ("Pittsburgh Steelers", "Steelers", "AFC", "AFC North"),
    "SEA": ("Seattle Seahawks", "Seahawks", "NFC", "NFC West"),
    "SF": ("San Francisco 49ers", "49ers", "NFC", "NFC West"),
    "TB": ("Tampa Bay Buccaneers", "Buccaneers", "NFC", "NFC South"),
    "TEN": ("Tennessee Titans", "Titans", "AFC", "AFC South"),
    "WAS": ("Washington Commanders", "Commanders", "NFC", "NFC East"),
}

# Other spellings feeds use -> the canonical abbreviation.
_ALIASES = {"LA": "LAR", "WSH": "WAS", "JAC": "JAX", "LVR": "LV"}


def canonical_abbreviation(raw: str) -> str:
    """The canonical abbreviation for any spelling a feed uses; `KeyError` for an unknown club."""
    abbreviation = raw.strip().upper()
    abbreviation = _ALIASES.get(abbreviation, abbreviation)
    if abbreviation not in NFL_TEAMS:
        raise KeyError(raw)
    return abbreviation


def _build(abbreviation: str, source: SourceRef) -> FootballTeam:
    name, nickname, conference, division = NFL_TEAMS[abbreviation]
    key = abbreviation.lower()
    return FootballTeam(
        id=LeagueScopedId(League.NFL, source, abbreviation),
        league=League.NFL,
        display_name=name,
        short_name=nickname,
        abbreviation=abbreviation,
        primary_color=LOGO_BASE_COLOR[abbreviation],
        secondary_color=LOGO_ALT_COLOR[abbreviation],
        logo=LogoAsset(
            key=f"nfl/{key}",
            path=f"assets/logos/nfl/{key}.png",
            preferred_background=LOGO_BASE_COLOR[abbreviation],
            meter_color=FREED_BASE_METER_COLOR.get(abbreviation),
        ),
        logo_alt=LogoAsset(
            key=f"nfl/{key}-alt",
            path=f"assets/logos/nfl/{key}-alt.png",
            preferred_background=LOGO_ALT_COLOR[abbreviation],
        ),
        conference=conference,
        division=division,
    )


class NflTeamRegistry:
    """Resolves any feed's abbreviation into the typed `FootballTeam`."""

    def __init__(self, teams: Mapping[str, FootballTeam]) -> None:
        self._teams = dict(teams)

    @classmethod
    def default(cls, *, source: SourceRef | None = None) -> NflTeamRegistry:
        """Every current club, from the committed table and palette."""
        src = source if source is not None else _SOURCE
        return cls({abbreviation: _build(abbreviation, src) for abbreviation in NFL_TEAMS})

    def resolve(self, raw: str) -> FootballTeam:
        """The club a feed's abbreviation names; `KeyError` if it names none."""
        return self._teams[canonical_abbreviation(raw)]

    def __len__(self) -> int:
        return len(self._teams)
