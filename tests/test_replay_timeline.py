"""Tests for the replay Timeline format: effective-as-of-now resolution + validation."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from omni.core.enum import GameStatus, League
from omni.core.ids import LeagueScopedId, SourceRef
from omni.domain.baseball import BaseballBaseState, BaseballCount, BaseballGameState, HalfInning
from omni.domain.contest import TeamGame
from omni.providers.mlb_teams import MlbTeamRegistry
from omni.replay.timeline import GameFrame, Timeline

T0 = datetime(2026, 6, 17, 23, 0, tzinfo=timezone.utc)
SOURCE = SourceRef("mlb_statsapi", "https://statsapi.mlb.com")
_REG = MlbTeamRegistry.from_color_file()


def _game(gid: str, status: GameStatus) -> TeamGame:
    return TeamGame(
        id=LeagueScopedId(League.MLB, SOURCE, gid),
        league=League.MLB,
        status=status,
        scheduled_start=T0,
        away=_REG.resolve(115),
        home=_REG.resolve(119),
    )


def _state(away: int = 0, home: int = 0) -> BaseballGameState:
    return BaseballGameState(
        away_score=away,
        home_score=home,
        inning=1,
        half=HalfInning.TOP,
        count=BaseballCount(balls=0, strikes=0, outs=0),
        bases=BaseballBaseState(),
    )


def _gid(gid: str) -> LeagueScopedId:
    return LeagueScopedId(League.MLB, SOURCE, gid)


def test_empty_timeline_rejected() -> None:
    with pytest.raises(ValueError, match="at least one frame"):
        Timeline(frames=())


def test_frame_rejects_naive_at() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        GameFrame(at=datetime(2026, 6, 17, 23, 0), game=_game("g1", GameStatus.PREGAME))


def test_live_frame_requires_state() -> None:
    with pytest.raises(ValueError, match="LIVE frame"):
        GameFrame(at=T0, game=_game("g1", GameStatus.LIVE), state=None)


def test_start_and_end_span_all_frames() -> None:
    timeline = Timeline(
        frames=(
            GameFrame(at=T0 + timedelta(hours=1), game=_game("g1", GameStatus.PREGAME)),
            GameFrame(at=T0, game=_game("g2", GameStatus.PREGAME)),
        )
    )
    assert timeline.start == T0
    assert timeline.end == T0 + timedelta(hours=1)


def test_schedule_empty_before_first_frame() -> None:
    timeline = Timeline(frames=(GameFrame(at=T0, game=_game("g1", GameStatus.PREGAME)),))
    assert timeline.schedule_at(T0 - timedelta(minutes=1)) == ()


def test_schedule_and_state_track_the_latest_frame() -> None:
    # One game advancing pregame -> live -> final across the timeline.
    timeline = Timeline(
        frames=(
            GameFrame(at=T0, game=_game("g1", GameStatus.PREGAME)),
            GameFrame(at=T0 + timedelta(hours=1), game=_game("g1", GameStatus.LIVE), state=_state(away=2)),
            GameFrame(at=T0 + timedelta(hours=3), game=_game("g1", GameStatus.FINAL)),
        )
    )
    gid = _gid("g1")

    pregame = timeline.schedule_at(T0)
    assert len(pregame) == 1 and pregame[0].status is GameStatus.PREGAME
    assert timeline.state_at(gid, T0) is None  # no live state pregame

    live = timeline.schedule_at(T0 + timedelta(hours=1))
    assert live[0].status is GameStatus.LIVE
    state = timeline.state_at(gid, T0 + timedelta(hours=2))  # still the 1h frame, an hour later
    assert state is not None and state.away_score == 2

    final = timeline.schedule_at(T0 + timedelta(hours=3))
    assert final[0].status is GameStatus.FINAL
    assert timeline.state_at(gid, T0 + timedelta(hours=3)) is None  # final frame carries no state


def test_schedule_is_sorted_by_id_for_stable_traces() -> None:
    timeline = Timeline(
        frames=(
            GameFrame(at=T0, game=_game("g2", GameStatus.PREGAME)),
            GameFrame(at=T0, game=_game("g1", GameStatus.PREGAME)),
        )
    )
    raws = [game.id.raw for game in timeline.schedule_at(T0)]
    assert raws == sorted(raws)
