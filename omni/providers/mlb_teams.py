"""MLB team registry: raw StatsAPI team ids -> typed `BaseballTeam`.

This is a provider-boundary module, so reading the raw colors JSON is allowed
here (and only here). Downstream code receives fully typed teams.

The id -> (abbreviation, nickname) table is owned by `omni` (not imported from
the upstream `data/` package) so the typed island stays self-contained; its
provenance is `data/teams.py`. Colors come from the committed
`colors/teams.example.json` so a fresh checkout/CI resolves teams deterministically.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

from omni.core.colors import RGBColor
from omni.core.enum import League
from omni.core.ids import LeagueScopedId, SourceRef
from omni.domain.base import LogoAsset
from omni.domain.teams import BaseballTeam

__all__ = ["MlbTeamRegistry"]

_DEFAULT_SOURCE = SourceRef("mlb_statsapi", "https://statsapi.mlb.com")
_DEFAULT_COLOR_FILE = Path(__file__).resolve().parents[2] / "colors" / "teams.example.json"

# id -> (abbreviation, nickname). The 30 current MLB clubs; provenance: data/teams.py.
# WBC/all-star "special" teams are intentionally omitted until they're needed.
_TEAM_ID_INFO: dict[int, tuple[str, str]] = {
    108: ("LAA", "Angels"),
    109: ("AZ", "D-backs"),
    110: ("BAL", "Orioles"),
    111: ("BOS", "Red Sox"),
    112: ("CHC", "Cubs"),
    113: ("CIN", "Reds"),
    114: ("CLE", "Guardians"),
    115: ("COL", "Rockies"),
    116: ("DET", "Tigers"),
    117: ("HOU", "Astros"),
    118: ("KC", "Royals"),
    119: ("LAD", "Dodgers"),
    120: ("WSH", "Nationals"),
    121: ("NYM", "Mets"),
    133: ("ATH", "Athletics"),
    134: ("PIT", "Pirates"),
    135: ("SD", "Padres"),
    136: ("SEA", "Mariners"),
    137: ("SF", "Giants"),
    138: ("STL", "Cardinals"),
    139: ("TB", "Rays"),
    140: ("TEX", "Rangers"),
    141: ("TOR", "Blue Jays"),
    142: ("MIN", "Twins"),
    143: ("PHI", "Phillies"),
    144: ("ATL", "Braves"),
    145: ("CWS", "White Sox"),
    146: ("MIA", "Marlins"),
    147: ("NYY", "Yankees"),
    158: ("MIL", "Brewers"),
}


class MlbTeamRegistry:
    """Resolves raw MLB team ids into typed `BaseballTeam`s.

    Build it from the committed colors file (`from_color_file`) for production
    and dogfooding, or inject a `{team_id: BaseballTeam}` mapping directly in tests.
    """

    def __init__(self, teams: Mapping[int, BaseballTeam]) -> None:
        self._teams = dict(teams)

    @classmethod
    def from_color_file(
        cls,
        path: str | Path | None = None,
        *,
        source: SourceRef | None = None,
    ) -> MlbTeamRegistry:
        colors = _load_team_colors(Path(path) if path is not None else _DEFAULT_COLOR_FILE)
        src = source if source is not None else _DEFAULT_SOURCE
        teams: dict[int, BaseballTeam] = {}
        for team_id, (abbr, nickname) in _TEAM_ID_INFO.items():
            pair = colors.get(abbr.lower())
            if pair is None:
                continue  # no colors on file -> can't render this team; skip it
            primary, secondary = pair
            teams[team_id] = BaseballTeam(
                id=LeagueScopedId(League.MLB, src, str(team_id)),
                league=League.MLB,
                display_name=nickname,
                short_name=nickname,
                abbreviation=abbr,
                primary_color=primary,
                secondary_color=secondary,
                logo=LogoAsset(key=abbr.lower(), path=f"assets/logos/mlb/{abbr.lower()}.png"),
            )
        return cls(teams)

    def resolve(self, team_id: int, *, full_name: str | None = None) -> BaseballTeam:
        """Return the typed team for `team_id`, raising `KeyError` if unknown.

        `full_name` (the schedule's full club name, e.g. "Los Angeles Dodgers")
        overrides the static nickname as `display_name`; `short_name` keeps the
        nickname ("Dodgers"), which is what small panels render.
        """
        team = self._teams[team_id]
        if full_name and full_name != team.display_name:
            return replace(team, display_name=full_name)
        return team

    def __contains__(self, team_id: object) -> bool:
        return team_id in self._teams

    def __len__(self) -> int:
        return len(self._teams)


def _load_team_colors(path: Path) -> dict[str, tuple[RGBColor, RGBColor]]:
    """Parse the colors JSON into `{abbr_lower: (primary, secondary)}`.

    `home` is the primary color; `accent` (the secondary brand color) is used as
    secondary, falling back to the primary when absent. Non-team keys
    (`$schema`, `format`, ...) are ignored.
    """
    raw: Any = json.loads(path.read_text())  # provider boundary: raw JSON allowed here
    out: dict[str, tuple[RGBColor, RGBColor]] = {}
    for key, value in raw.items():
        if not _is_rgb(value.get("home") if isinstance(value, dict) else None):
            continue
        primary = _rgb(value["home"])
        accent = value.get("accent")
        secondary = _rgb(accent) if _is_rgb(accent) else primary
        out[str(key).lower()] = (primary, secondary)
    return out


def _is_rgb(d: Any) -> bool:
    return isinstance(d, dict) and all(k in d for k in ("r", "g", "b"))


def _rgb(d: Any) -> RGBColor:
    return RGBColor(int(d["r"]), int(d["g"]), int(d["b"]))
