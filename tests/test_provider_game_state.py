"""Tests for parsing the MLB game feed into typed BaseballGameState."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from omni.core.enum import GameStatus, League
from omni.core.ids import LeagueScopedId, SourceRef
from omni.domain.baseball import (
    BatterGameLine,
    InningPhase,
    PitcherGameLine,
    PitchingDecisions,
    PitchSnapshot,
    PitchType,
)
from omni.domain.contest import TeamGame
from omni.providers.base import ProviderError
from omni.providers.mlb_statsapi import (
    MlbStatsApiProvider,
    _batting_walks_hbp,
    _boxscore_player,
    _current_batter,
    _current_pitcher,
    _last_name,
    _last_pitch_snapshot,
    _lineup_spot,
    _parse_decisions,
    _parse_game_state,
    _phase_from_inning_state,
    _reached_base,
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
    # both sides have hits, so both have plainly reached base.
    assert state.away_reached_base is True and state.home_reached_base is True
    # current batter / pitcher with their game lines, looked up from the boxscore.
    assert state.batter == BatterGameLine(name="Batter", order=4, at_bats=4, hits=2, rbi=1, home_runs=0)
    assert state.pitcher == PitcherGameLine(name="Miller", innings_pitched="6.1", pitches=95, strikeouts=7)
    # the current play's last pitch (an 86.7mph sweeper, walked past a mound visit) drives the snapshot
    assert state.last_pitch == PitchSnapshot(velocity_mph=87, pitch_type=PitchType.SWEEPER)


def _current_play(events: Any) -> dict[str, Any]:
    return {"liveData": {"plays": {"currentPlay": {"playEvents": events}}}}


def test_last_pitch_snapshot_reads_the_current_plays_latest_pitch() -> None:
    raw = _current_play(
        [
            {"isPitch": True, "pitchData": {"startSpeed": 95.0}, "details": {"type": {"code": "FF"}}},
            {"isPitch": False, "details": {"description": "Mound Visit"}},
            {"isPitch": True, "pitchData": {"startSpeed": 86.6}, "details": {"type": {"code": "ST"}}},
        ]
    )
    # walks from the end past the mound visit; rounds 86.6 -> 87.
    assert _last_pitch_snapshot(raw) == PitchSnapshot(velocity_mph=87, pitch_type=PitchType.SWEEPER)


def test_last_pitch_snapshot_is_none_without_a_tracked_pitch() -> None:
    assert _last_pitch_snapshot({"liveData": {"plays": {}}}) is None  # no current play
    assert _last_pitch_snapshot({"liveData": {"plays": {"currentPlay": []}}}) is None  # current play not an object
    assert _last_pitch_snapshot(_current_play(None)) is None  # no playEvents list
    assert _last_pitch_snapshot(_current_play([{"isPitch": False}])) is None  # only non-pitch events


@pytest.mark.parametrize(
    "event",
    [
        {"isPitch": True, "pitchData": {"startSpeed": 90.0}, "details": {"type": {"code": "ZZ"}}},  # unrecognized type
        {"isPitch": True, "details": {"type": {"code": "FF"}}},  # no pitchData / speed
        {"isPitch": True, "pitchData": {"startSpeed": True}, "details": {"type": {"code": "FF"}}},  # bool isn't a speed
        {"isPitch": True, "pitchData": {"startSpeed": 0.0}, "details": {"type": {"code": "FF"}}},  # rounds to 0 mph
    ],
)
def test_last_pitch_snapshot_skips_an_unusable_last_pitch(event: dict[str, Any]) -> None:
    assert _last_pitch_snapshot(_current_play([event])) is None


def test_last_name_takes_the_surname_and_skips_a_suffix() -> None:
    assert _last_name("Caleb Kilian") == "Kilian"
    assert _last_name("Vladimir Guerrero Jr.") == "Guerrero"  # the suffix is skipped
    assert _last_name("Cedric") == "Cedric"  # a single token degrades to itself


def test_boxscore_player_finds_either_side_and_none_when_absent() -> None:
    raw = {"liveData": {"boxscore": {"teams": {"home": {"players": {"ID7": {"battingOrder": "300"}}}}}}}
    assert _boxscore_player(raw, 7) == {"battingOrder": "300"}  # found on the home roster
    assert _boxscore_player(raw, 999) is None  # player on no roster -> None


def test_lineup_spot_parses_the_batting_order() -> None:
    assert _lineup_spot({"battingOrder": "400"}) == 4  # 4th in the order
    assert _lineup_spot({"battingOrder": "901"}) == 9  # a substitution still maps to the spot
    assert _lineup_spot({}) is None  # no battingOrder -> unknown
    assert _lineup_spot({"battingOrder": "x"}) is None  # non-numeric degrades, never raises


def test_current_batter_and_pitcher_are_none_when_unnamed() -> None:
    # No offense/defense block at all (between innings, or a thin feed) -> no batter/pitcher line.
    assert _current_batter({"liveData": {"linescore": {}}}) is None
    assert _current_pitcher({"liveData": {"linescore": {}}}) is None
    # Named, but not on any boxscore roster -> still None (no game line to show).
    named = {"liveData": {"linescore": {"offense": {"batter": {"id": 1, "fullName": "A. B"}}}}}
    assert _current_batter(named) is None


def _boxscore(side_stats: dict[str, dict[str, int]]) -> dict[str, Any]:
    teams = {side: {"teamStats": {"batting": stats}} for side, stats in side_stats.items()}
    return {"liveData": {"boxscore": {"teams": teams}}}


def test_batting_walks_hbp_sums_from_the_boxscore() -> None:
    assert _batting_walks_hbp(_boxscore({"away": {"baseOnBalls": 2, "hitByPitch": 1}}), "away") == 3


def test_batting_walks_hbp_none_when_absent_or_null() -> None:
    assert _batting_walks_hbp({"liveData": {}}, "away") is None  # no boxscore block
    assert _batting_walks_hbp({"liveData": {"boxscore": None}}, "home") is None  # a null node degrades, never raises


def test_reached_base_true_on_a_hit_or_a_defensive_error() -> None:
    assert _reached_base({}, side="away", hits=1, fielding_errors=0) is True
    assert _reached_base({}, side="away", hits=0, fielding_errors=1) is True  # reached on the defense's error


def test_reached_base_unknown_without_the_boxscore() -> None:
    # Hitless and errorless, but no walk/HBP data — unknown, so a perfect game is never claimed.
    assert _reached_base({"liveData": {}}, side="away", hits=0, fielding_errors=0) is None


def test_reached_base_clean_sheet_versus_a_walk() -> None:
    clean = _boxscore({"home": {"baseOnBalls": 0, "hitByPitch": 0}})
    assert _reached_base(clean, side="home", hits=0, fielding_errors=0) is False
    walked = _boxscore({"home": {"baseOnBalls": 1, "hitByPitch": 0}})
    assert _reached_base(walked, side="home", hits=0, fielding_errors=0) is True


def test_parse_game_state_reads_a_clean_sheet_for_a_no_hit_side() -> None:
    feed = _feed()
    line = feed["liveData"]["linescore"]
    line["teams"]["away"]["hits"] = 0  # away hitless...
    line["teams"]["home"]["errors"] = 0  # ...and no away batter reached on a home error
    feed["liveData"]["boxscore"] = {"teams": {"away": {"teamStats": {"batting": {"baseOnBalls": 0, "hitByPitch": 0}}}}}
    state = _parse_game_state(feed)
    assert state.away_hits == 0 and state.away_reached_base is False  # a clean sheet -> perfect-game eligible


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


def test_null_nested_object_raises_provider_error() -> None:
    # A null `linescore` (or `teams`) makes a nested `.get` hit None; that AttributeError is
    # caught as a typed ProviderError, never escaping the provider boundary as a raw error.
    with pytest.raises(ProviderError):
        _parse_game_state({"liveData": {"linescore": None}})
    with pytest.raises(ProviderError):
        _parse_game_state({"liveData": {"linescore": {"currentInning": 7, "teams": None}}})
