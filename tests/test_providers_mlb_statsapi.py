"""Tests for the MLB StatsAPI schedule provider (fixture-driven, no network)."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from omni.core.enum import GameStatus, League
from omni.core.ids import SourceRef
from omni.domain.contest import TeamGame
from omni.providers.base import ProviderError, ProviderUpdate
from omni.providers.mlb_statsapi import (
    MlbStatsApiProvider,
    _parse_start,
    _SkipGame,
    map_game_status,
)
from omni.providers.mlb_teams import MlbTeamRegistry

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "providers" / "mlb_schedule.json"
NOW = datetime(2026, 6, 17, 21, 0, tzinfo=timezone.utc)


def _rows() -> list[dict[str, Any]]:
    data: list[dict[str, Any]] = json.loads(FIXTURE.read_text())
    return data


def _provider(rows: list[dict[str, Any]] | None = None) -> tuple[MlbStatsApiProvider, list[tuple[date, str]]]:
    calls: list[tuple[date, str]] = []
    data = _rows() if rows is None else rows

    def fetch(game_date: date, sport_ids: str) -> list[dict[str, Any]]:
        calls.append((game_date, sport_ids))
        return data

    return MlbStatsApiProvider(MlbTeamRegistry.from_color_file(), fetch), calls


def _by_id(update: ProviderUpdate, raw: str) -> TeamGame:
    game = next(c for c in update.contests if c.id.raw == raw)
    assert isinstance(game, TeamGame)
    return game


def test_refresh_localizes_the_schedule_date_to_the_configured_zone() -> None:
    calls: list[date] = []

    def fetch(game_date: date, sport_ids: str) -> list[dict[str, Any]]:
        calls.append(game_date)
        return []

    provider = MlbStatsApiProvider(
        MlbTeamRegistry.from_color_file(), fetch, schedule_timezone=ZoneInfo("America/New_York")
    )
    # 02:00 UTC Jun 18 == 22:00 EDT Jun 17 — still "tonight" in New York.
    provider.refresh(datetime(2026, 6, 18, 2, 0, tzinfo=timezone.utc))
    assert calls == [date(2026, 6, 17)]  # today's games, not tomorrow's (UTC would say Jun 18)


def test_refresh_parses_known_games_and_warns_on_unknown_team() -> None:
    provider, calls = _provider()
    update = provider.refresh(NOW)

    assert calls == [(NOW.date(), "1,51")]  # 21:00Z == 17:00 ET same day, so local == UTC date here
    assert update.observed_at == NOW
    assert update.source.name == "mlb_statsapi"
    assert update.events == ()

    # 5 fixture rows; the one with an unknown team id is skipped + recorded.
    assert len(update.contests) == 4
    assert len(update.warnings) == 1
    assert "999" in update.warnings[0]


def test_live_game_is_fully_typed() -> None:
    update, _ = _provider()[0].refresh(NOW), None
    game = _by_id(update, "700001")

    assert game.status is GameStatus.LIVE
    assert game.league is League.MLB
    assert game.away.abbreviation == "COL"
    assert game.home.abbreviation == "LAD"
    assert game.away.display_name == "Colorado Rockies"  # full name from the schedule row
    assert game.home.short_name == "Dodgers"
    assert game.venue_name == "Dodger Stadium"
    assert game.scheduled_start == datetime(2026, 6, 17, 23, 10, tzinfo=timezone.utc)
    assert game.competitors == (game.away, game.home)


def test_status_mapping_across_games() -> None:
    update = _provider()[0].refresh(NOW)
    assert _by_id(update, "700002").status is GameStatus.PREGAME
    assert _by_id(update, "700003").status is GameStatus.FINAL
    assert _by_id(update, "700004").status is GameStatus.DELAYED  # "Delayed: Rain" prefix


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Scheduled", GameStatus.SCHEDULED),
        ("Pre-Game", GameStatus.PREGAME),
        ("Warmup", GameStatus.PREGAME),
        ("In Progress", GameStatus.LIVE),
        ("Manager Challenge", GameStatus.LIVE),
        ("Final", GameStatus.FINAL),
        ("Game Over", GameStatus.FINAL),
        ("Postponed", GameStatus.POSTPONED),
        ("Cancelled", GameStatus.CANCELED),
        ("Canceled", GameStatus.CANCELED),
        ("Delayed: Rain", GameStatus.DELAYED),
        ("Suspended: Inclement Weather", GameStatus.SUSPENDED),
        ("Frobnicated", GameStatus.UNKNOWN),
    ],
)
def test_map_game_status(raw: str, expected: GameStatus) -> None:
    assert map_game_status(raw) is expected


def test_fetch_failure_becomes_provider_error() -> None:
    def boom(game_date: date, sport_ids: str) -> list[dict[str, Any]]:
        raise RuntimeError("network down")

    provider = MlbStatsApiProvider(MlbTeamRegistry.from_color_file(), boom)
    with pytest.raises(ProviderError) as exc_info:
        provider.refresh(NOW)
    assert isinstance(exc_info.value.__cause__, RuntimeError)


def test_empty_schedule_yields_empty_update() -> None:
    update = _provider(rows=[])[0].refresh(NOW)
    assert update.contests == ()
    assert update.warnings == ()


def test_parse_game_skips_row_missing_team_ids() -> None:
    provider = MlbStatsApiProvider(MlbTeamRegistry.from_color_file(), lambda d, s: [])
    with pytest.raises(_SkipGame):
        provider._parse_game({"game_id": 1, "status": "Final", "game_datetime": "2026-06-17T18:00:00Z"})


def test_parse_game_blank_venue_becomes_none() -> None:
    provider = MlbStatsApiProvider(MlbTeamRegistry.from_color_file(), lambda d, s: [])
    game = provider._parse_game(
        {
            "game_id": 1,
            "away_id": 115,
            "home_id": 119,
            "status": "Final",
            "game_datetime": "2026-06-17T18:00:00Z",
            "venue_name": "",
        }
    )
    assert game.venue_name is None


def test_parse_start_handles_zulu_naive_and_bad_values() -> None:
    assert _parse_start("2026-06-17T23:10:00Z", 1) == datetime(2026, 6, 17, 23, 10, tzinfo=timezone.utc)
    # A naive timestamp is treated as UTC so timing math always has a tz.
    assert _parse_start("2026-06-17T23:10:00", 1) == datetime(2026, 6, 17, 23, 10, tzinfo=timezone.utc)
    with pytest.raises(_SkipGame):
        _parse_start(None, 1)
    with pytest.raises(_SkipGame):
        _parse_start("not-a-date", 1)
