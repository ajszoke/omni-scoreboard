"""Football's live state: what it accepts, and what it derives for the board."""

from __future__ import annotations

from typing import Any

import pytest

from omni.core.enum import HomeAway
from omni.domain.football import (
    FieldPosition,
    FootballGameState,
    PlaySummary,
    PlayTone,
    TeamChallenges,
    TeamTimeouts,
    other_side,
)

A, H = HomeAway.AWAY, HomeAway.HOME


def _state(**changes: Any) -> FootballGameState:
    values: dict[str, Any] = {"period": 2, "clock_seconds": 300, "away_score": 7, "home_score": 3}
    return FootballGameState(**{**values, **changes})


@pytest.mark.parametrize(
    "changes",
    [
        {"period": 0},
        {"clock_seconds": -1},
        {"away_score": -1},
        {"home_score": -2},
        {"down": 0},
        {"down": 5},
        {"distance": -1},
        {"yards_to_goal": 0},
        {"yards_to_goal": 100},
    ],
)
def test_an_impossible_moment_is_refused(changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        _state(**changes)


def test_a_play_summary_needs_a_headline() -> None:
    with pytest.raises(ValueError):
        PlaySummary(tone=PlayTone.STANDARD, headline="")


@pytest.mark.parametrize("away, home", [(-1, 0), (0, 4)])
def test_timeouts_are_three_a_half_at_most(away: int, home: int) -> None:
    with pytest.raises(ValueError):
        TeamTimeouts(away=away, home=home)


@pytest.mark.parametrize("away, home", [(-1, 0), (0, 3)])
def test_challenges_are_two_at_most(away: int, home: int) -> None:
    with pytest.raises(ValueError):
        TeamChallenges(away=away, home=home)


@pytest.mark.parametrize("side, yard", [(A, 0), (A, 51), (None, 30), (H, 50)])
def test_a_spot_is_a_sides_yard_line_or_midfield(side: HomeAway | None, yard: int) -> None:
    with pytest.raises(ValueError):
        FieldPosition(side=side, yard_line=yard)


def test_the_opponent_is_the_other_side() -> None:
    assert other_side(A) is H and other_side(H) is A


@pytest.mark.parametrize(
    "yards_to_goal, spot",
    [
        (75, FieldPosition(side=A, yard_line=25)),  # in its own half
        (50, FieldPosition(side=None, yard_line=50)),
        (38, FieldPosition(side=H, yard_line=38)),  # across midfield, on the defense's side
    ],
)
def test_the_spot_is_named_from_the_side_of_the_field_its_on(yards_to_goal: int, spot: FieldPosition) -> None:
    assert _state(possession=A, down=1, distance=10, yards_to_goal=yards_to_goal).ball_on == spot


def test_no_spot_without_both_a_possession_and_a_field_position() -> None:
    assert _state(possession=A).ball_on is None
    assert _state(yards_to_goal=30).ball_on is None


def test_the_red_zone_is_inside_the_twenty_with_someone_on_offense() -> None:
    assert _state(possession=H, down=2, distance=5, yards_to_goal=20).in_red_zone
    assert not _state(possession=H, down=2, distance=5, yards_to_goal=21).in_red_zone
    assert not _state(yards_to_goal=10).in_red_zone


def test_goal_to_go_when_the_line_to_gain_is_the_goal_line() -> None:
    assert _state(possession=A, down=1, distance=8, yards_to_goal=8).goal_to_go
    assert not _state(possession=A, down=1, distance=10, yards_to_goal=30).goal_to_go
    assert not _state(possession=A, yards_to_goal=8).goal_to_go


def test_overtime_counts_on_past_the_fourth() -> None:
    assert not _state(period=4).is_overtime
    assert _state(period=5).is_overtime
