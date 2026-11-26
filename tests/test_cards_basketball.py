"""Basketball card payloads: what goes with which moment of a game, and the panels' own picks."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from omni.cards.basketball import (
    BasketballGameCardPayload,
    BasketballPregameCardPayload,
    BestPlayers,
    CaptionPanel,
    LineScorePanel,
    PlayerLinesPanel,
    TeamBoxPanel,
)
from omni.core.enum import HomeAway
from omni.domain.basketball import (
    BasketballGameState,
    BasketballPhase,
    BasketballSide,
    BySide,
    GameClock,
    ScoringRun,
    TeamBox,
)
from omni.events.basketball import TimeoutTaken
from tests.basketball_fixtures import MIN_FINAL, MIN_Q3, POR_FINAL, POR_Q3

A, H = HomeAway.AWAY, HomeAway.HOME
T = datetime(2026, 11, 17, 2, 30, tzinfo=timezone.utc)


def _state(phase: BasketballPhase) -> BasketballGameState:
    period = {BasketballPhase.HALFTIME: 2, BasketballPhase.FINAL: 4}.get(phase, 3)
    clock = GameClock(4380) if phase is BasketballPhase.LIVE else None
    return BasketballGameState(
        phase=phase, period=period, away=BasketballSide(score=58), home=BasketballSide(score=68), clock=clock
    )


_CAPTION = CaptionPanel(caption=TimeoutTaken(side=A, left=4))
_RUN = ScoringRun(team=H, scored=7, allowed=0, span_seconds=88)
_BEST = BestPlayers.of(POR_FINAL, MIN_FINAL)


@pytest.mark.parametrize(
    "phase, changes",
    [
        (BasketballPhase.HALFTIME, {"bottom": _CAPTION}),
        (BasketballPhase.END_OF_PERIOD, {"bottom": _CAPTION}),
        (BasketballPhase.FINAL, {"bottom": TeamBoxPanel.of(POR_Q3, MIN_Q3)}),
        (BasketballPhase.END_OF_PERIOD, {"run": _RUN}),
        (BasketballPhase.LIVE, {"break_ends_at": T}),
        (BasketballPhase.HALFTIME, {"break_ends_at": datetime(2026, 11, 17, 2, 30)}),
        (BasketballPhase.LIVE, {"best": _BEST}),
    ],
    ids=str,
)
def test_a_game_payload_refuses_what_doesnt_go_with_its_moment(phase: BasketballPhase, changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        BasketballGameCardPayload(state=_state(phase), **changes)


@pytest.mark.parametrize(
    "phase, changes",
    [
        (BasketballPhase.LIVE, {"bottom": _CAPTION, "run": _RUN}),
        (BasketballPhase.HALFTIME, {"break_ends_at": T + timedelta(minutes=12)}),
        (BasketballPhase.HALFTIME, {"bottom": PlayerLinesPanel.ranked(0, POR_Q3, MIN_Q3)}),
        (BasketballPhase.FINAL, {"best": _BEST}),
    ],
    ids=str,
)
def test_a_game_payload_takes_what_goes_with_its_moment(phase: BasketballPhase, changes: dict[str, Any]) -> None:
    payload = BasketballGameCardPayload(state=_state(phase), **changes)
    assert payload.records == BySide(None, None)


def test_player_lines_walk_each_teams_plus_minus_order() -> None:
    top = PlayerLinesPanel.ranked(0, POR_Q3, MIN_Q3).players
    assert (top[A].name, top[H].name) == ("T.Camara", "A.Edwards")  # type: ignore[union-attr]
    second = PlayerLinesPanel.ranked(1, POR_Q3, MIN_Q3).players
    assert (second[A].name, second[H].name) == ("D.Avdija", "R.Gobert")  # type: ignore[union-attr]
    past = PlayerLinesPanel.ranked(6, POR_Q3, MIN_Q3[:7])
    assert past.players == BySide(None, None)  # neither team has a seventh player


def test_player_lines_count_their_rank_from_zero() -> None:
    with pytest.raises(ValueError):
        PlayerLinesPanel.ranked(-1, POR_Q3, MIN_Q3)


def test_the_box_sums_each_team() -> None:
    panel = TeamBoxPanel.of(POR_Q3, MIN_Q3)
    assert panel.box[A] == TeamBox(fg_pct=44, threes_made=8, threes_attempted=20, rebounds=25, assists=13, turnovers=8)
    assert panel.box[H].fg_pct == 54


def test_a_finals_best_players_go_by_plus_minus_not_points() -> None:
    best = BestPlayers.of(POR_FINAL, MIN_FINAL).players
    assert (best[A].name, best[H].name) == ("T.Camara", "R.Gobert")  # type: ignore[union-attr]
    assert BestPlayers.of((), MIN_FINAL).players[A] is None


@pytest.mark.parametrize(
    "changes",
    [
        {"phase": BasketballPhase.LIVE},
        {"phase": BasketballPhase.SCHEDULED, "tip": None},
        {"tip": datetime(2026, 11, 17, 1, 0)},
    ],
    ids=str,
)
def test_a_pregame_payload_is_for_a_game_not_under_way_with_its_tip(changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        BasketballPregameCardPayload(**{"phase": BasketballPhase.SCHEDULED, "tip": T, **changes})


def test_a_postponed_game_may_not_have_a_new_tip_yet() -> None:
    payload = BasketballPregameCardPayload(phase=BasketballPhase.POSTPONED, tip=None)
    assert payload.ranks == BySide(None, None) and payload.line is None


def test_the_line_score_is_the_default_panel() -> None:
    assert BasketballGameCardPayload(state=_state(BasketballPhase.LIVE)).bottom == LineScorePanel()
