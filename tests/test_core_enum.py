"""Tests for ``omni.core.enum`` — typed enums, serialization, tolerant coercion."""

from __future__ import annotations

import pytest

from omni.core.enum import (
    DisplayPriority,
    GameStatus,
    League,
    PanelProfile,
    Sport,
    UpdateUrgency,
    try_coerce_enum,
)


def test_str_enum_value_is_canonical() -> None:
    assert League.MLB.value == "mlb"
    assert str(League.MLB) == "mlb"
    assert League.MLB.to_json_value() == "mlb"
    assert League.MLB == "mlb"  # str-backed: equal to its raw string


def test_str_enum_constructs_from_value() -> None:
    assert League("mlb") is League.MLB
    assert GameStatus("live") is GameStatus.LIVE


def test_panel_profiles_cover_supported_displays() -> None:
    # The three profiles AGENTS.md requires every UI feature to consider.
    assert {p.to_json_value() for p in PanelProfile} == {
        "single_64x32",
        "stack_64x64",
        "quad_128x64",
    }


def test_int_enum_serializes_by_name_and_orders_by_value() -> None:
    assert int(DisplayPriority.NORMAL) == 10
    assert str(DisplayPriority.NORMAL) == "normal"
    assert DisplayPriority.NORMAL.to_json_value() == "normal"
    assert DisplayPriority.ALERT > DisplayPriority.NORMAL > DisplayPriority.BACKGROUND


def test_to_json_value_is_always_str_for_these_enums() -> None:
    assert isinstance(League.MLB.to_json_value(), str)
    assert isinstance(DisplayPriority.NORMAL.to_json_value(), str)


@pytest.mark.parametrize("league", list(League))
def test_every_league_maps_to_a_sport(league: League) -> None:
    assert isinstance(league.sport, Sport)


def test_league_sport_mapping_is_correct() -> None:
    assert League.MLB.sport is Sport.BASEBALL
    assert League.NFL.sport is Sport.FOOTBALL
    assert League.NCAAF.sport is Sport.FOOTBALL
    assert League.NBA.sport is Sport.BASKETBALL
    assert League.NCAAB.sport is Sport.BASKETBALL
    assert League.NHL.sport is Sport.HOCKEY
    assert League.PGA.sport is Sport.GOLF


def test_coerce_passthrough_and_by_value() -> None:
    assert try_coerce_enum(League, League.NFL) is League.NFL
    assert try_coerce_enum(League, "nfl") is League.NFL
    assert try_coerce_enum(DisplayPriority, 10) is DisplayPriority.NORMAL


def test_coerce_by_name_is_case_insensitive() -> None:
    # value is "live"; by-name lookup tolerates upper/mixed case input.
    assert try_coerce_enum(GameStatus, "LIVE") is GameStatus.LIVE
    assert try_coerce_enum(GameStatus, "Final") is GameStatus.FINAL
    # int enums serialize by name, so they must coerce back by name too.
    assert try_coerce_enum(DisplayPriority, "normal") is DisplayPriority.NORMAL


def test_coerce_rejects_unknown_and_bools() -> None:
    assert try_coerce_enum(League, "xfl") is None
    assert try_coerce_enum(GameStatus, None) is None
    # bool must not slip through as int 1 (UpdateUrgency(1) == NORMAL).
    assert try_coerce_enum(UpdateUrgency, True) is None
    assert try_coerce_enum(UpdateUrgency, 1) is UpdateUrgency.NORMAL


def test_str_enums_round_trip_through_json_value() -> None:
    # Distinct loop names so mypy types each member to its own enum class.
    for league in League:
        assert try_coerce_enum(League, league.to_json_value()) is league
    for status in GameStatus:
        assert try_coerce_enum(GameStatus, status.to_json_value()) is status
    for profile in PanelProfile:
        assert try_coerce_enum(PanelProfile, profile.to_json_value()) is profile
    for sport in Sport:
        assert try_coerce_enum(Sport, sport.to_json_value()) is sport


def test_int_enums_round_trip_through_json_value() -> None:
    for priority in DisplayPriority:
        assert try_coerce_enum(DisplayPriority, priority.to_json_value()) is priority
    for urgency in UpdateUrgency:
        assert try_coerce_enum(UpdateUrgency, urgency.to_json_value()) is urgency


def test_coerce_rejects_unhashable_and_numeric_types() -> None:
    # Non-str, non-enum inputs hit the TypeError/ValueError arm and fall through to None.
    assert try_coerce_enum(League, ["mlb"]) is None  # unhashable -> TypeError
    assert try_coerce_enum(League, 3.5) is None
    assert try_coerce_enum(League, object()) is None
