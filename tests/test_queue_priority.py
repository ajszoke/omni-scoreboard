"""Tests for the PriorityScorer signal matrix."""

from __future__ import annotations

from datetime import datetime, timezone

from omni.core.enum import DisplayPriority, GameStatus, League
from omni.core.ids import LeagueScopedId, SourceRef
from omni.domain.baseball import BaseballBaseState, BaseballCount, BaseballGameState, InningPhase
from omni.domain.contest import TeamGame
from omni.providers.mlb_teams import MlbTeamRegistry
from omni.queue.priority import PriorityScorer

T = datetime(2026, 6, 17, 23, 10, tzinfo=timezone.utc)
SOURCE = SourceRef("mlb_statsapi", "https://statsapi.mlb.com")
_IDS = {"COL": 115, "LAD": 119, "NYY": 147, "BOS": 111}
_REG = MlbTeamRegistry.from_color_file()


def _game(away: str = "COL", home: str = "LAD") -> TeamGame:
    return TeamGame(
        id=LeagueScopedId(League.MLB, SOURCE, "g1"),
        league=League.MLB,
        status=GameStatus.LIVE,
        scheduled_start=T,
        away=_REG.resolve(_IDS[away]),
        home=_REG.resolve(_IDS[home]),
    )


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
    p = PriorityScorer(favorites=frozenset({"LAD"})).score_live_baseball(_game(), _state())
    assert p.band is DisplayPriority.FAVORITE
    assert p.score == 30.0
    assert any("favorite" in r for r in p.reasons)


def test_favorite_matches_away_team_too() -> None:
    p = PriorityScorer(favorites=frozenset({"COL"})).score_live_baseball(_game(away="COL"), _state())
    assert p.band is DisplayPriority.FAVORITE
    assert "COL" in p.reasons[0]


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
    p = PriorityScorer(favorites=frozenset({"COL"})).score_live_baseball(
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
