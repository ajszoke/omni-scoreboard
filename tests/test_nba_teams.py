"""The NBA team registry: every club, every spelling a feed uses, and the logo tiles each one names."""

from __future__ import annotations

from pathlib import Path

import pytest

from omni.core.colors import RGBColor
from omni.core.enum import League
from omni.core.ids import SourceRef
from omni.domain.teams import BasketballTeam
from omni.providers.nba_palette import LOGO_ALT_COLOR, LOGO_BASE_COLOR
from omni.providers.nba_teams import NBA_TEAMS, NbaTeamRegistry, canonical_abbreviation

REGISTRY = NbaTeamRegistry.default()
_ROOT = Path(__file__).resolve().parents[1]


def test_the_thirty_clubs_are_keyed_by_balldontlies_abbreviations() -> None:
    assert sorted(NBA_TEAMS) == sorted(
        "ATL BOS BKN CHA CHI CLE DAL DEN DET GSW HOU IND LAC LAL MEM "
        "MIA MIL MIN NOP NYK OKC ORL PHI PHX POR SAC SAS TOR UTA WAS".split()
    )
    assert len(REGISTRY) == 30


def test_the_clubs_split_into_two_conferences_of_three_divisions_of_five() -> None:
    conferences = {info[2] for info in NBA_TEAMS.values()}
    assert conferences == {"East", "West"}
    divisions = [(info[2], info[3]) for info in NBA_TEAMS.values()]
    assert sorted(set(divisions)) == sorted(
        [("East", d) for d in ("Atlantic", "Central", "Southeast")]
        + [("West", d) for d in ("Northwest", "Pacific", "Southwest")]
    )
    assert all(divisions.count(division) == 5 for division in set(divisions))


@pytest.mark.parametrize(
    "raw, canonical",
    [
        ("BRK", "BKN"),
        ("CHO", "CHA"),
        ("GS", "GSW"),
        ("NO", "NOP"),
        ("NOR", "NOP"),
        ("NY", "NYK"),
        ("PHO", "PHX"),
        ("SA", "SAS"),
        ("UTAH", "UTA"),
        ("wsh", "WAS"),
        (" lal ", "LAL"),
    ],
)
def test_every_spelling_a_feed_uses_names_one_club(raw: str, canonical: str) -> None:
    assert canonical_abbreviation(raw) == canonical
    assert REGISTRY.resolve(raw).abbreviation == canonical


@pytest.mark.parametrize("raw", ["SEA", "NJN", "VAN", "NOH", "CHH", "XYZ", ""])
def test_an_old_or_unknown_name_is_not_a_club(raw: str) -> None:
    with pytest.raises(KeyError):
        REGISTRY.resolve(raw)


def test_a_spelling_two_clubs_share_names_neither() -> None:
    # "LA" could be the Lakers or the Clippers, so it resolves to no club rather than guess.
    with pytest.raises(KeyError):
        canonical_abbreviation("LA")


def test_a_club_carries_its_names_division_and_palette() -> None:
    team = REGISTRY.resolve("DEN")
    assert isinstance(team, BasketballTeam)
    assert (team.display_name, team.short_name, team.conference, team.division) == (
        "Denver Nuggets",
        "Nuggets",
        "West",
        "Northwest",
    )
    assert team.league is League.NBA
    assert (team.logo.key, team.logo.path) == ("nba/den", "assets/logos/nba/den.png")
    assert team.logo.preferred_background == LOGO_BASE_COLOR["DEN"]
    assert team.logo_alt is not None and team.logo_alt.preferred_background == LOGO_ALT_COLOR["DEN"]


def test_a_navy_club_spends_white_on_its_meter_beside_its_alt() -> None:
    assert REGISTRY.resolve("IND").logo.meter_color == RGBColor(255, 255, 255)
    # Denver's navy lifts to the skyline blue the club wears, so it keeps that.
    assert REGISTRY.resolve("DEN").logo.meter_color is None
    assert REGISTRY.resolve("LAL").logo.meter_color is None


@pytest.mark.parametrize("abbreviation", sorted(NBA_TEAMS))
def test_every_club_names_tiles_that_exist(abbreviation: str) -> None:
    team = REGISTRY.resolve(abbreviation)
    assert (_ROOT / team.logo.path).is_file()
    assert team.logo_alt is not None and (_ROOT / team.logo_alt.path).is_file()


def test_a_registry_can_be_built_for_any_source() -> None:
    team = NbaTeamRegistry.default(source=SourceRef("balldontlie")).resolve("BOS")
    assert team.id.source == SourceRef("balldontlie")
    assert team.id.league is League.NBA
