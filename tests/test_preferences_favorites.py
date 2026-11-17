"""Favorites: ranked per league, scoped by league, checked against each league's clubs, and read from the CLI."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from omni.core.enum import GameStatus, League
from omni.core.ids import LeagueScopedId, SourceRef
from omni.domain.contest import TeamGame
from omni.domain.teams import Team
from omni.preferences.favorites import Favorites, parse_favorites
from omni.providers.mlb_teams import MlbTeamRegistry
from omni.providers.nba_teams import NbaTeamRegistry
from omni.providers.nfl_teams import NflTeamRegistry

T = datetime(2026, 9, 17, 23, 10, tzinfo=timezone.utc)
_MLB = MlbTeamRegistry.from_color_file()
_NFL = NflTeamRegistry.default()
_NBA = NbaTeamRegistry.default()
_MLB_IDS = {"ATL": 144, "COL": 115, "LAD": 119, "MIN": 142, "NYY": 147}

ATL, COL, LAD, TWINS = (_MLB.resolve(_MLB_IDS[abbreviation]) for abbreviation in ("ATL", "COL", "LAD", "MIN"))
CAR, VIKINGS = _NFL.resolve("CAR"), _NFL.resolve("MIN")
CHA = _NBA.resolve("CHA")
USER = Favorites({League.MLB: ("ATL", "COL"), League.NFL: ("CAR",), League.NBA: ("CHA",)})


def _game(away: Team, home: Team) -> TeamGame:
    return TeamGame(
        id=LeagueScopedId(away.league, SourceRef("test"), "g1"),
        league=away.league,
        status=GameStatus.LIVE,
        scheduled_start=T,
        away=away,
        home=home,
    )


def _silent(line: str) -> None:
    raise AssertionError(f"unexpected note: {line}")


# --- rank ------------------------------------------------------------------------------------------------


def test_order_is_rank_within_each_league() -> None:
    assert (USER.rank(ATL), USER.rank(COL), USER.rank(LAD)) == (1, 2, None)
    assert (USER.rank(CAR), USER.rank(CHA)) == (1, 1)  # each league counts from its own first
    assert USER.teams(League.MLB) == ("ATL", "COL") and USER.teams(League.NHL) == ()


def test_a_game_between_two_favorites_takes_the_better_rank_whichever_side_it_is_on() -> None:
    assert USER.of(_game(COL, ATL)) == 1 and USER.of(_game(ATL, COL)) == 1
    assert USER.of(_game(COL, LAD)) == 2
    assert USER.of(_game(LAD, _MLB.resolve(_MLB_IDS["NYY"]))) is None


def test_a_team_is_a_favorite_only_in_its_own_league() -> None:
    assert Favorites({League.NFL: ("MIN",)}).rank(TWINS) is None  # a Vikings favorite never marks the Twins
    assert Favorites({League.MLB: ("MIN",)}).rank(VIKINGS) is None  # nor a Twins favorite the Vikings
    assert Favorites({League.MLB: ("MIN",)}).rank(TWINS) == 1


def test_a_team_listed_twice_keeps_its_first_place() -> None:
    assert Favorites({League.MLB: ("COL", "ATL", "col")}).teams(League.MLB) == ("COL", "ATL")


def test_each_spelling_resolves_to_its_club_s_abbreviation() -> None:
    favorites = Favorites({League.MLB: (" atl ",), League.NFL: ("WSH",), League.NBA: ("BRK",)})
    assert favorites.ranked == ((League.MLB, ("ATL",)), (League.NFL, ("WAS",)), (League.NBA, ("BKN",)))


def test_an_unknown_team_is_a_configuration_error_that_lists_the_league_s_teams() -> None:
    with pytest.raises(ValueError, match=r"no MLB team 'ARI' \(MLB teams: ATH, ATL, AZ, ") as error:
        Favorites({League.MLB: ("ARI",)})
    assert "CWS" in str(error.value)


def test_a_league_without_a_team_list_cant_have_favorites_yet() -> None:
    with pytest.raises(ValueError, match="NHL has no team list yet"):
        Favorites({League.NHL: ("BOS",)})


def test_the_same_choices_make_equal_favorites_whatever_order_the_leagues_came_in() -> None:
    one = Favorites({League.NFL: ("CAR",), League.MLB: ("ATL", "COL")})
    other = Favorites({League.MLB: ("ATL", "COL"), League.NFL: ("CAR",), League.NBA: ()})
    assert one == other and hash(one) == hash(other)
    assert Favorites() == Favorites({}) and not Favorites() and USER


def test_the_chosen_order_reads_back_in_one_line() -> None:
    assert str(USER) == "mlb 1 ATL, 2 COL; nfl 1 CAR; nba 1 CHA"
    assert str(Favorites()) == "none"


# --- the command line ------------------------------------------------------------------------------------


def test_command_line_order_is_rank_and_the_league_is_read_in_any_case() -> None:
    favorites = parse_favorites(["mlb:ATL", "NFL:car", "Mlb:COL", "nba:CHA"], note=_silent)
    assert favorites == USER


def test_a_bare_team_is_read_as_mlb_and_said_so_once() -> None:
    notes: list[str] = []
    favorites = parse_favorites(["atl", "mlb:COL"], note=notes.append)
    assert favorites == Favorites({League.MLB: ("ATL", "COL")})
    assert notes == ["--favorite atl read as mlb:ATL (a team without a league is MLB's)"]


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ("xfl:DAL", "--favorite xfl:DAL: unknown league 'xfl' (one of mlb, nfl, nba)"),
        ("nfl:XYZ", "--favorite nfl:XYZ: no NFL team 'XYZ' (NFL teams: ARI, ATL, BAL, "),
        ("nhl:BOS", "--favorite nhl:BOS: NHL has no team list yet, so it can't have favorites"),
        ("ARI", "--favorite ARI: no MLB team 'ARI' (MLB teams: ATH, ATL, AZ, "),
        ("mlb:", "--favorite mlb:: no MLB team ''"),
    ],
)
def test_an_unknown_league_or_team_is_rejected_naming_the_entry(entry: str, message: str) -> None:
    with pytest.raises(ValueError) as error:
        parse_favorites(["mlb:ATL", entry], note=_silent)
    assert str(error.value).startswith(message)
