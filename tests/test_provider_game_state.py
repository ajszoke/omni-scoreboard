"""Tests for parsing the MLB game feed into typed BaseballGameState."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from omni.core.enum import GameStatus, League
from omni.core.ids import LeagueScopedId, SourceRef
from omni.domain.baseball import InningPhase, PitchingDecisions
from omni.domain.contest import TeamGame
from omni.providers.base import ProviderError
from omni.providers.mlb_statsapi import (
    MlbStatsApiProvider,
    _parse_decisions,
    _parse_game_state,
    _phase_from_inning_state,
)
from omni.providers.mlb_teams import MlbTeamRegistry

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "providers" / "mlb_game_live.json"
NOW = datetime(2026, 6, 17, 23, 30, tzinfo=timezone.utc)
SOURCE = SourceRef("mlb_statsapi", "https://statsapi.mlb.com")
_REG = MlbTeamRegistry.from_color_file()


def _feed() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(FIXTURE.read_text())
    return data


def _game() -> TeamGame:
    return TeamGame(
        id=LeagueScopedId(League.MLB, SOURCE, "700001"),
        league=League.MLB,
        status=GameStatus.LIVE,
        scheduled_start=NOW,
        away=_REG.resolve(115),
        home=_REG.resolve(119),
    )


def test_parse_game_state_from_fixture() -> None:
    state = _parse_game_state(_feed())
    assert (state.away_score, state.home_score) == (3, 5)
    assert (state.away_hits, state.home_hits) == (7, 9)  # R/H/E hits, for no-hitter detection
    assert state.inning == 7
    assert state.phase is InningPhase.TOP
    assert (state.count.balls, state.count.strikes, state.count.outs) == (2, 1, 2)
    # offense had first + third occupied; second empty.
    assert state.bases.first and state.bases.third and not state.bases.second


def test_fetch_live_feed_uses_injected_fetcher() -> None:
    calls: list[Any] = []

    def fetch_game(game_pk: Any) -> dict[str, Any]:
        calls.append(game_pk)
        return _feed()

    provider = MlbStatsApiProvider(MlbTeamRegistry({}), fetch_game=fetch_game)
    feed = provider.fetch_live_feed(_game(), now=NOW)
    assert calls == ["700001"]  # fetched by the game's raw id
    assert feed.state.home_score == 5


def test_fetch_live_feed_surfaces_the_pitching_decisions() -> None:
    # The fixture carries a `decisions` block (winner/loser/save) so the whole path is
    # exercised: the `fields` whitelist keeps it, and the feed surfaces typed decisions.
    provider = MlbStatsApiProvider(MlbTeamRegistry({}), fetch_game=lambda _pk: _feed())
    decisions = provider.fetch_live_feed(_game(), now=NOW).decisions
    assert decisions is not None
    assert (decisions.winner, decisions.loser, decisions.save) == ("Clayton Kershaw", "German Marquez", "Tanner Scott")


def test_parse_decisions_handles_the_block_shapes() -> None:
    person = lambda name: {"fullName": name}  # noqa: E731 - terse fixture helper
    full = {"liveData": {"decisions": {"winner": person("W"), "loser": person("L"), "save": person("S")}}}
    assert _parse_decisions(full) == PitchingDecisions(winner="W", loser="L", save="S")
    no_save = {"liveData": {"decisions": {"winner": person("W"), "loser": person("L")}}}
    assert _parse_decisions(no_save) == PitchingDecisions(winner="W", loser="L", save=None)
    assert _parse_decisions({"liveData": {}}) is None  # no decisions block (game in progress)
    assert _parse_decisions({"liveData": {"decisions": {"winner": person("W")}}}) is None  # loser missing
    assert _parse_decisions({"liveData": {"decisions": {"winner": {"fullName": ""}, "loser": person("L")}}}) is None


def test_fetch_game_failure_becomes_provider_error() -> None:
    def boom(game_pk: Any) -> dict[str, Any]:
        raise RuntimeError("timeout")

    provider = MlbStatsApiProvider(MlbTeamRegistry({}), fetch_game=boom)
    with pytest.raises(ProviderError) as exc_info:
        provider.fetch_live_feed(_game(), now=NOW)
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
