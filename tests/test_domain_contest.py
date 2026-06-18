"""Tests for omni.domain contests: Contest, TeamGame, GolfTournament."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from omni.core.colors import RGBColor
from omni.core.enum import GameStatus, League, Sport
from omni.core.ids import LeagueScopedId, SourceRef
from omni.domain.athletes import Golfer
from omni.domain.base import Competitor, LogoAsset
from omni.domain.contest import Contest, GolfTournament, TeamGame
from omni.domain.teams import Team

KICKOFF = datetime(2026, 6, 17, 19, 5, tzinfo=timezone.utc)


def make_team(team_id: str, name: str, abbr: str) -> Team:
    return Team(
        id=LeagueScopedId(League.MLB, SourceRef("mlb_statsapi"), team_id),
        league=League.MLB,
        display_name=name,
        short_name=name.split()[-1],
        abbreviation=abbr,
        primary_color=RGBColor(51, 0, 111),
        secondary_color=RGBColor(196, 206, 212),
        logo=LogoAsset(key=abbr.lower(), path=f"assets/{abbr.lower()}.png"),
    )


def make_golfer(golfer_id: str, name: str) -> Golfer:
    return Golfer(
        id=LeagueScopedId(League.PGA, SourceRef("espn"), golfer_id),
        display_name=name,
        short_name=name.split()[-1],
    )


def make_team_game(*, status: GameStatus = GameStatus.LIVE) -> TeamGame:
    return TeamGame(
        id=LeagueScopedId(League.MLB, SourceRef("mlb_statsapi"), "g1"),
        league=League.MLB,
        status=status,
        scheduled_start=KICKOFF,
        away=make_team("115", "Colorado Rockies", "COL"),
        home=make_team("119", "Los Angeles Dodgers", "LAD"),
    )


def test_contest_sport_derives_from_league() -> None:
    assert make_team_game().sport is Sport.BASEBALL


def test_team_game_auto_derives_competitors_from_home_away() -> None:
    game = make_team_game()
    assert game.competitors == (game.away, game.home)
    assert isinstance(game, Contest)
    assert all(isinstance(c, Competitor) for c in game.competitors)


def test_team_game_requires_distinct_teams() -> None:
    rockies = make_team("115", "Colorado Rockies", "COL")
    with pytest.raises(ValueError):
        TeamGame(
            id=LeagueScopedId(League.MLB, SourceRef("mlb_statsapi"), "g1"),
            league=League.MLB,
            status=GameStatus.SCHEDULED,
            scheduled_start=KICKOFF,
            away=rockies,
            home=rockies,
        )


def test_team_game_preserves_explicit_competitors() -> None:
    # A provider may supply `competitors` itself; don't overwrite it.
    away = make_team("115", "Colorado Rockies", "COL")
    home = make_team("119", "Los Angeles Dodgers", "LAD")
    game = TeamGame(
        id=LeagueScopedId(League.MLB, SourceRef("mlb_statsapi"), "g1"),
        league=League.MLB,
        status=GameStatus.LIVE,
        scheduled_start=KICKOFF,
        competitors=(home, away),  # deliberately reversed
        away=away,
        home=home,
    )
    assert game.competitors == (home, away)


def test_golf_tournament_holds_individual_competitors() -> None:
    golfers = (make_golfer("1", "Scottie Scheffler"), make_golfer("2", "Rory McIlroy"))
    tourney = GolfTournament(
        id=LeagueScopedId(League.PGA, SourceRef("espn"), "t1"),
        league=League.PGA,
        status=GameStatus.LIVE,
        scheduled_start=KICKOFF,
        competitors=golfers,
        tournament_name="U.S. Open",
        cut_line=2,
    )
    assert tourney.sport is Sport.GOLF
    assert tourney.tournament_name == "U.S. Open"
    assert tourney.cut_line == 2
    assert all(isinstance(c, Competitor) for c in tourney.competitors)
