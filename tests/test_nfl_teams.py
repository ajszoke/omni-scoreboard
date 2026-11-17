"""The NFL team registry: every club, every spelling a feed uses, and the logo tiles each one names."""

from __future__ import annotations

from pathlib import Path

import pytest

from omni.core.enum import League
from omni.core.ids import SourceRef
from omni.domain.teams import FootballTeam
from omni.providers.nfl_palette import LOGO_ALT_COLOR, LOGO_BASE_COLOR
from omni.providers.nfl_teams import NFL_TEAMS, NflTeamRegistry, canonical_abbreviation

REGISTRY = NflTeamRegistry.default()
_ROOT = Path(__file__).resolve().parents[1]


def test_there_are_thirty_two_clubs_split_evenly() -> None:
    assert len(REGISTRY) == 32
    divisions = [info[3] for info in NFL_TEAMS.values()]
    assert sorted(set(divisions)) == sorted(
        f"{c} {d}" for c in ("AFC", "NFC") for d in ("East", "North", "South", "West")
    )
    assert all(divisions.count(division) == 4 for division in set(divisions))


@pytest.mark.parametrize(
    "raw, canonical",
    [
        ("WSH", "WAS"),
        ("la", "LAR"),
        ("JAC", "JAX"),
        ("LVR", "LV"),
        (" kc ", "KC"),
        ("BLT", "BAL"),  # the league's play-by-play codes, as play text writes them
        ("CLV", "CLE"),
        ("ARZ", "ARI"),
        ("HST", "HOU"),
    ],
)
def test_every_spelling_a_feed_uses_names_one_club(raw: str, canonical: str) -> None:
    assert canonical_abbreviation(raw) == canonical
    assert REGISTRY.resolve(raw).abbreviation == canonical


@pytest.mark.parametrize("raw", ["OAK", "SD", "STL", "XYZ", ""])
def test_an_old_or_unknown_name_is_not_a_club(raw: str) -> None:
    with pytest.raises(KeyError):
        REGISTRY.resolve(raw)


def test_a_club_carries_its_names_division_and_palette() -> None:
    team = REGISTRY.resolve("DEN")
    assert isinstance(team, FootballTeam)
    assert (team.display_name, team.short_name, team.conference, team.division) == (
        "Denver Broncos",
        "Broncos",
        "AFC",
        "AFC West",
    )
    assert team.league is League.NFL
    assert team.logo.preferred_background == LOGO_BASE_COLOR["DEN"]
    assert team.logo_alt is not None and team.logo_alt.preferred_background == LOGO_ALT_COLOR["DEN"]


def test_a_navy_club_spends_white_on_its_meter_beside_its_alt() -> None:
    assert REGISTRY.resolve("DAL").logo.meter_color is not None
    assert REGISTRY.resolve("KC").logo.meter_color is None


@pytest.mark.parametrize("abbreviation", sorted(NFL_TEAMS))
def test_every_club_names_tiles_that_exist(abbreviation: str) -> None:
    team = REGISTRY.resolve(abbreviation)
    assert (_ROOT / team.logo.path).is_file()
    assert team.logo_alt is not None and (_ROOT / team.logo_alt.path).is_file()


def test_a_registry_can_be_built_for_any_source() -> None:
    team = NflTeamRegistry.default(source=SourceRef("nflmeta")).resolve("PIT")
    assert team.id.source == SourceRef("nflmeta")
