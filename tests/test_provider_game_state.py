"""Tests for parsing the MLB game feed into typed BaseballGameState."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from omni.domain.baseball import InningPhase
from omni.providers.base import ProviderError
from omni.providers.mlb_statsapi import (
    MlbStatsApiProvider,
    _parse_game_state,
    _phase_from_inning_state,
)
from omni.providers.mlb_teams import MlbTeamRegistry

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "providers" / "mlb_game_live.json"
NOW = datetime(2026, 6, 17, 23, 30, tzinfo=timezone.utc)


def _feed() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(FIXTURE.read_text())
    return data


def test_parse_game_state_from_fixture() -> None:
    state = _parse_game_state(_feed())
    assert (state.away_score, state.home_score) == (3, 5)
    assert state.inning == 7
    assert state.phase is InningPhase.TOP
    assert (state.count.balls, state.count.strikes, state.count.outs) == (2, 1, 2)
    # offense had first + third occupied; second empty.
    assert state.bases.first and state.bases.third and not state.bases.second


def test_fetch_game_state_uses_injected_fetcher() -> None:
    calls: list[Any] = []

    def fetch_game(game_pk: Any) -> dict[str, Any]:
        calls.append(game_pk)
        return _feed()

    provider = MlbStatsApiProvider(MlbTeamRegistry({}), fetch_game=fetch_game)
    state = provider.fetch_game_state(700001)
    assert calls == [700001]
    assert state.home_score == 5


def test_fetch_game_failure_becomes_provider_error() -> None:
    def boom(game_pk: Any) -> dict[str, Any]:
        raise RuntimeError("timeout")

    provider = MlbStatsApiProvider(MlbTeamRegistry({}), fetch_game=boom)
    with pytest.raises(ProviderError) as exc_info:
        provider.fetch_game_state(700001)
    assert isinstance(exc_info.value.__cause__, RuntimeError)


def test_empty_bases_when_no_runners() -> None:
    feed = _feed()
    feed["liveData"]["linescore"]["offense"] = {"batter": {"id": 1}}
    state = _parse_game_state(feed)
    assert not state.bases.first and not state.bases.second and not state.bases.third


@pytest.mark.parametrize(
    "inning_state, expected",
    [
        ("Top", InningPhase.TOP),
        ("Middle", InningPhase.MIDDLE),  # breaks are now distinct, not collapsed
        ("Bottom", InningPhase.BOTTOM),
        ("End", InningPhase.END),
        ("???", InningPhase.TOP),  # unknown label defaults to TOP
    ],
)
def test_phase_from_inning_state(inning_state: str, expected: InningPhase) -> None:
    assert _phase_from_inning_state(inning_state) is expected


def test_missing_linescore_raises_provider_error() -> None:
    with pytest.raises(ProviderError):
        _parse_game_state({"gameData": {}})


def test_not_yet_live_feed_raises_provider_error() -> None:
    # A scheduled game has currentInning 0 and no live state to render.
    feed = {"liveData": {"linescore": {"currentInning": 0}}}
    with pytest.raises(ProviderError):
        _parse_game_state(feed)


def test_impossible_count_raises_provider_error() -> None:
    feed = _feed()
    feed["liveData"]["linescore"]["outs"] = 5  # past the terminal maximum
    with pytest.raises(ProviderError):
        _parse_game_state(feed)
