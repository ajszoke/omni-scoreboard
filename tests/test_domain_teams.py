"""Tests for omni.domain teams and the Competitor protocol."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from omni.core.colors import RGBColor
from omni.core.enum import League
from omni.core.ids import LeagueScopedId, SourceRef
from omni.domain.base import Competitor, LogoAsset
from omni.domain.teams import BaseballTeam, Team


def make_team(*, primary_color: RGBColor = RGBColor(51, 0, 111)) -> Team:
    return Team(
        id=LeagueScopedId(League.MLB, SourceRef("mlb_statsapi"), "115"),
        league=League.MLB,
        display_name="Colorado Rockies",
        short_name="Rockies",
        abbreviation="COL",
        primary_color=primary_color,
        secondary_color=RGBColor(196, 206, 212),
        logo=LogoAsset(key="col", path="assets/col.png"),
    )


def test_team_satisfies_competitor_protocol() -> None:
    assert isinstance(make_team(), Competitor)


def test_best_text_color_white_on_dark_primary() -> None:
    team = make_team(primary_color=RGBColor(0, 0, 90))  # dark navy
    assert team.best_text_color_on_primary() == RGBColor(255, 255, 255)


def test_best_text_color_black_on_light_primary() -> None:
    team = make_team(primary_color=RGBColor(255, 215, 0))  # gold
    assert team.best_text_color_on_primary() == RGBColor(0, 0, 0)


def test_team_is_frozen_and_slotted() -> None:
    team = make_team()
    with pytest.raises(FrozenInstanceError):
        setattr(team, "abbreviation", "XXX")
    assert not hasattr(team, "__dict__")


def test_baseball_team_is_a_team_and_competitor() -> None:
    team = BaseballTeam(
        id=LeagueScopedId(League.MLB, SourceRef("mlb_statsapi"), "115"),
        league=League.MLB,
        display_name="Colorado Rockies",
        short_name="Rockies",
        abbreviation="COL",
        primary_color=RGBColor(51, 0, 111),
        secondary_color=RGBColor(196, 206, 212),
        logo=LogoAsset(key="col", path="assets/col.png"),
        division="NL West",
    )
    assert isinstance(team, Team)
    assert isinstance(team, Competitor)
    assert team.division == "NL West"
    assert team.league_side is None
