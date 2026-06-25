"""Tests for fetching/parsing MLB win probability (the game_contextMetrics endpoint)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from omni.core.enum import GameStatus, HomeAway, League
from omni.core.ids import LeagueScopedId, SourceRef
from omni.domain.contest import TeamGame
from omni.providers.base import ProviderError
from omni.providers.mlb_statsapi import MlbStatsApiProvider, _parse_win_probability
from omni.providers.mlb_teams import MlbTeamRegistry

NOW = datetime(2026, 6, 17, 23, 30, tzinfo=timezone.utc)
SOURCE = SourceRef("mlb_statsapi", "https://statsapi.mlb.com")
_REG = MlbTeamRegistry.from_color_file()


def _game() -> TeamGame:
    return TeamGame(
        id=LeagueScopedId(League.MLB, SOURCE, "700001"),
        league=League.MLB,
        status=GameStatus.LIVE,
        scheduled_start=NOW,
        away=_REG.resolve(115),
        home=_REG.resolve(119),
    )


def _provider(metrics: object) -> tuple[MlbStatsApiProvider, list[Any]]:
    calls: list[Any] = []

    def fetch_context_metrics(game_pk: Any) -> dict[str, Any]:
        calls.append(game_pk)
        return metrics  # type: ignore[return-value]  # tests feed deliberately malformed payloads

    return MlbStatsApiProvider(MlbTeamRegistry({}), fetch_context_metrics=fetch_context_metrics), calls


def test_fetch_win_probability_parses_the_metrics() -> None:
    provider, calls = _provider({"homeWinProbability": 26.0, "awayWinProbability": 74.0})
    wp = provider.fetch_win_probability(_game())
    assert calls == ["700001"]  # fetched by the game's raw id
    assert wp is not None and wp.favored is HomeAway.AWAY
    assert (wp.home, wp.away) == (26.0, 74.0)


def test_fetch_win_probability_is_none_without_the_block() -> None:
    provider, _ = _provider({"game": {}})  # a payload that carries no probability yet
    assert provider.fetch_win_probability(_game()) is None


def test_parse_win_probability_ignores_bool_and_non_numeric() -> None:
    # bool is an int subclass, and a string is not a percentage — neither is a probability.
    assert _parse_win_probability({"homeWinProbability": True, "awayWinProbability": 50.0}) is None
    assert _parse_win_probability({"homeWinProbability": "60", "awayWinProbability": 40.0}) is None


def test_parse_win_probability_drops_out_of_range_payloads() -> None:
    assert _parse_win_probability({"homeWinProbability": 150.0, "awayWinProbability": -50.0}) is None


def test_win_probability_fetch_failure_becomes_provider_error() -> None:
    def boom(game_pk: Any) -> dict[str, Any]:
        raise RuntimeError("network down")

    provider = MlbStatsApiProvider(MlbTeamRegistry({}), fetch_context_metrics=boom)
    with pytest.raises(ProviderError) as exc_info:
        provider.fetch_win_probability(_game())
    assert isinstance(exc_info.value.__cause__, RuntimeError)
