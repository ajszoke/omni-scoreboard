"""Tests for the PriorityScorer signal matrix."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from omni.core.enum import DisplayPriority, GameStatus, League
from omni.core.ids import LeagueScopedId, SourceRef
from omni.domain.baseball import BaseballBaseState, BaseballCount, BaseballGameState, InningPhase
from omni.domain.contest import TeamGame
from omni.preferences.favorites import Favorites
from omni.providers.mlb_teams import MlbTeamRegistry
from omni.providers.nfl_teams import NflTeamRegistry
from omni.queue.priority import PriorityScorer

T = datetime(2026, 6, 17, 23, 10, tzinfo=timezone.utc)
SOURCE = SourceRef("mlb_statsapi", "https://statsapi.mlb.com")
_IDS = {"COL": 115, "LAD": 119, "NYY": 147, "BOS": 111, "ATL": 144, "MIN": 142}
_REG = MlbTeamRegistry.from_color_file()
_NFL = NflTeamRegistry.default()


def _game(away: str = "COL", home: str = "LAD") -> TeamGame:
    return TeamGame(
        id=LeagueScopedId(League.MLB, SOURCE, "g1"),
        league=League.MLB,
        status=GameStatus.LIVE,
        scheduled_start=T,
        away=_REG.resolve(_IDS[away]),
        home=_REG.resolve(_IDS[home]),
    )


def _nfl_game(away: str, home: str) -> TeamGame:
    """An NFL game whose raw id is the MLB game's too."""
    return TeamGame(
        id=LeagueScopedId(League.NFL, SourceRef("nflmeta"), "g1"),
        league=League.NFL,
        status=GameStatus.LIVE,
        scheduled_start=T,
        away=_NFL.resolve(away),
        home=_NFL.resolve(home),
    )


def _favorites(**ranked: tuple[str, ...]) -> Favorites:
    return Favorites({League(league): teams for league, teams in ranked.items()})


def _state(
    *,
    away: int = 8,
    home: int = 3,
    inning: int = 2,
    phase: InningPhase = InningPhase.TOP,
    balls: int = 0,
    strikes: int = 0,
    outs: int = 0,
    first: bool = False,
    second: bool = False,
    third: bool = False,
) -> BaseballGameState:
    return BaseballGameState(
        away_score=away,
        home_score=home,
        inning=inning,
        phase=phase,
        count=BaseballCount(balls=balls, strikes=strikes, outs=outs),
        bases=BaseballBaseState(first=first, second=second, third=third),
    )


def test_boring_early_game_is_normal_and_unremarkable() -> None:
    p = PriorityScorer().score_live_baseball(_game(), _state())
    assert p.band is DisplayPriority.NORMAL
    assert p.score == 0.0
    assert p.reasons == ()


def test_favorite_team_lifts_band_and_explains_itself() -> None:
    p = PriorityScorer(favorites=_favorites(mlb=("LAD",))).score_live_baseball(_game(), _state())
    assert p.band is DisplayPriority.FAVORITE
    assert p.score == 30.0
    assert p.reasons == ("favorite #1 LAD",)


def test_favorite_matches_away_team_too() -> None:
    p = PriorityScorer(favorites=_favorites(mlb=("ATL", "COL"))).score_live_baseball(_game(away="COL"), _state())
    assert p.band is DisplayPriority.FAVORITE
    assert p.reasons == ("favorite #2 COL",)  # any rank gets the favorite band; the reason names the rank


@pytest.mark.parametrize(("away", "home", "named"), [("COL", "ATL", "#1 ATL"), ("ATL", "COL", "#1 ATL")])
def test_two_favorites_meeting_count_once_and_the_better_ranked_one_is_named(away: str, home: str, named: str) -> None:
    p = PriorityScorer(favorites=_favorites(mlb=("ATL", "COL"))).score_live_baseball(_game(away, home), _state())
    assert p.band is DisplayPriority.FAVORITE
    assert p.score == 30.0  # one favorite's worth, not two
    assert p.reasons == (f"favorite {named}",)


@pytest.mark.parametrize(
    ("favorites", "game"),
    [
        (_favorites(nfl=("MIN",)), _game("NYY", "MIN")),  # a Vikings favorite never marks the Twins
        (_favorites(mlb=("MIN",)), _nfl_game("GB", "MIN")),  # nor a Twins favorite the Vikings
    ],
)
def test_a_favorite_s_abbreviation_never_marks_another_league_s_club(favorites: Favorites, game: TeamGame) -> None:
    p = PriorityScorer(favorites=favorites).score_live_baseball(game, _state())
    assert p.band is DisplayPriority.NORMAL
    assert p.score == 0.0 and p.reasons == ()


def test_close_late_without_runners_stays_normal_band() -> None:
    p = PriorityScorer().score_live_baseball(_game(), _state(away=4, home=5, inning=8))
    assert p.band is DisplayPriority.NORMAL  # no runners on -> not yet high leverage
    assert "close & late" in p.reasons
    assert p.score == 25.0


def test_high_leverage_when_late_close_and_runners_on() -> None:
    p = PriorityScorer().score_live_baseball(_game(), _state(away=4, home=5, inning=8, second=True))
    assert p.band is DisplayPriority.HIGH_LEVERAGE
    assert {"close & late", "high leverage"} <= set(p.reasons)


def test_bases_loaded_tie_ninth_is_maximum_drama() -> None:
    p = PriorityScorer().score_live_baseball(
        _game(),
        _state(
            away=5,
            home=5,
            inning=9,
            phase=InningPhase.BOTTOM,
            balls=3,
            strikes=2,
            outs=2,
            first=True,
            second=True,
            third=True,
        ),
    )
    assert p.band is DisplayPriority.HIGH_LEVERAGE
    assert {"close & late", "9th or later", "high leverage", "bases loaded", "full count, two outs"} <= set(p.reasons)
    # Sum: close+late 25 + ninth 10 + leverage 20 + loaded 8 + full count 4 = 67
    assert p.score == 67.0


def test_favorite_in_high_leverage_keeps_the_more_urgent_band() -> None:
    p = PriorityScorer(favorites=_favorites(mlb=("COL",))).score_live_baseball(
        _game(), _state(away=5, home=4, inning=9, third=True)
    )
    assert p.band is DisplayPriority.HIGH_LEVERAGE  # 30 > FAVORITE's 20
    assert any("favorite" in r for r in p.reasons)
    assert "high leverage" in p.reasons


def test_scoring_position_credited_even_when_not_late() -> None:
    p = PriorityScorer().score_live_baseball(_game(), _state(inning=3, second=True))
    assert p.band is DisplayPriority.NORMAL
    assert p.reasons == ("runner in scoring position",)
    assert p.score == 5.0


def test_runner_on_first_only_is_not_scoring_position() -> None:
    p = PriorityScorer().score_live_baseball(_game(), _state(inning=3, first=True))
    assert p.reasons == ()
    assert p.score == 0.0


def test_score_is_monotonic_in_drama() -> None:
    scorer = PriorityScorer()
    boring = scorer.score_live_baseball(_game(), _state()).score
    close_late = scorer.score_live_baseball(_game(), _state(away=4, home=5, inning=8)).score
    high_lev = scorer.score_live_baseball(
        _game(), _state(away=4, home=5, inning=8, first=True, second=True, third=True)
    ).score
    assert boring < close_late < high_lev
