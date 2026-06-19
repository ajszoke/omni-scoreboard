"""Tests for the MLB team registry: id -> typed BaseballTeam, colors from file."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omni.core.colors import RGBColor
from omni.core.enum import League
from omni.core.ids import LeagueScopedId, SourceRef
from omni.domain.base import LogoAsset
from omni.domain.teams import BaseballTeam
from omni.providers.mlb_teams import MlbTeamRegistry

# Concrete values from colors/teams.example.json (home -> primary, accent -> secondary).
LAD_PRIMARY = RGBColor(0, 47, 108)
LAD_SECONDARY = RGBColor(145, 157, 157)
COL_PRIMARY = RGBColor(51, 0, 114)


def test_from_color_file_loads_all_thirty_clubs() -> None:
    registry = MlbTeamRegistry.from_color_file()
    assert len(registry) == 30
    assert 119 in registry
    assert 99999 not in registry


def test_resolve_returns_typed_team_with_colors() -> None:
    dodgers = MlbTeamRegistry.from_color_file().resolve(119)
    assert isinstance(dodgers, BaseballTeam)
    assert dodgers.abbreviation == "LAD"
    assert dodgers.short_name == "Dodgers"
    assert dodgers.display_name == "Dodgers"  # nickname until a full name is given
    assert dodgers.league is League.MLB
    assert dodgers.id == LeagueScopedId(League.MLB, SourceRef("mlb_statsapi", "https://statsapi.mlb.com"), "119")
    assert dodgers.primary_color == LAD_PRIMARY
    assert dodgers.secondary_color == LAD_SECONDARY
    assert dodgers.logo.key == "lad"


def test_resolve_applies_full_name_as_display_only() -> None:
    dodgers = MlbTeamRegistry.from_color_file().resolve(119, full_name="Los Angeles Dodgers")
    assert dodgers.display_name == "Los Angeles Dodgers"
    assert dodgers.short_name == "Dodgers"  # short name stays the nickname


def test_resolve_full_name_matching_nickname_is_a_noop() -> None:
    registry = MlbTeamRegistry.from_color_file()
    assert registry.resolve(119, full_name="Dodgers") == registry.resolve(119)


def test_resolve_unknown_id_raises_keyerror() -> None:
    with pytest.raises(KeyError):
        MlbTeamRegistry.from_color_file().resolve(99999)


def test_multiword_nicknames_are_not_truncated() -> None:
    # The embedded nickname table beats name.split()[-1], which would give "Sox"/"Jays".
    registry = MlbTeamRegistry.from_color_file()
    assert registry.resolve(111).short_name == "Red Sox"
    assert registry.resolve(145).short_name == "White Sox"
    assert registry.resolve(141).short_name == "Blue Jays"


def test_another_team_colors_for_good_measure() -> None:
    rockies = MlbTeamRegistry.from_color_file().resolve(115)
    assert rockies.abbreviation == "COL"
    assert rockies.short_name == "Rockies"
    assert rockies.primary_color == COL_PRIMARY


def test_direct_injection_for_tests() -> None:
    team = BaseballTeam(
        id=LeagueScopedId(League.MLB, SourceRef("test"), "119"),
        league=League.MLB,
        display_name="Dodgers",
        short_name="Dodgers",
        abbreviation="LAD",
        primary_color=RGBColor(1, 2, 3),
        secondary_color=RGBColor(4, 5, 6),
        logo=LogoAsset(key="lad", path="x.png"),
    )
    registry = MlbTeamRegistry({119: team})
    assert len(registry) == 1
    assert registry.resolve(119) is team


def test_from_color_file_skips_clubs_without_colors_and_falls_back_accent(tmp_path: Path) -> None:
    # A partial colors file: only LAD (with accent) and COL (no accent) present;
    # non-team keys ignored. Exercises the skip + accent-fallback branches.
    colors = {
        "$schema": "ignored",
        "format": 9.0,
        "lad": {"home": {"r": 0, "g": 47, "b": 108}, "accent": {"r": 9, "g": 9, "b": 9}},
        "col": {"home": {"r": 51, "g": 0, "b": 114}},
    }
    path = tmp_path / "partial.json"
    path.write_text(json.dumps(colors))

    registry = MlbTeamRegistry.from_color_file(path, source=SourceRef("custom"))
    assert len(registry) == 2  # only the two clubs with colors; the other 28 skipped
    assert registry.resolve(119).secondary_color == RGBColor(9, 9, 9)
    # COL has no accent -> secondary falls back to the primary color.
    assert registry.resolve(115).secondary_color == COL_PRIMARY
    assert registry.resolve(115).id.source == SourceRef("custom")
