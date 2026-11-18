"""Tests for parsing the MLB game feed into typed BaseballGameState."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import pytest

from omni.core.enum import GameStatus, HomeAway, League
from omni.core.ids import LeagueScopedId, SourceRef
from omni.domain.baseball import (
    BaseballBaseState,
    BaseballCount,
    BaseballField,
    BaseballGameState,
    BatterGameLine,
    DueUp,
    InningPhase,
    PitcherGameLine,
    PitchingDecisions,
    PitchingFeatKind,
    PitchSnapshot,
    PitchType,
    PregameDetails,
    PregameSide,
    StartingPitcher,
    WinLossRecord,
    pitching_feat_progress,
)
from omni.domain.contest import TeamGame
from omni.domain.coverage import CoverageMap, FieldCoverage
from omni.providers.base import ProviderError
from omni.providers.mlb_statsapi import (
    _GAME_FIELDS,
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
    _ReachLedger,
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


def test_batting_walks_hbp_unknown_when_either_count_is_missing() -> None:
    assert _batting_walks_hbp(_boxscore({"away": {"baseOnBalls": 0}}), "away") is None
    assert _batting_walks_hbp(_boxscore({"away": {"hitByPitch": 0}}), "away") is None


_CLEAN_LOG = _ReachLedger(reached=frozenset(), complete=frozenset(HomeAway))  # every play on record, no one reached
_NO_LOG = _ReachLedger(reached=frozenset(), complete=frozenset())  # no play log to go by


def test_reached_base_true_on_a_hit_or_a_defensive_error() -> None:
    # A count that shows a baserunner settles it, with or without a play log.
    assert _reached_base({}, side=HomeAway.AWAY, hits=1, fielding_errors=0, ledger=_NO_LOG) is True
    assert _reached_base({}, side=HomeAway.AWAY, hits=0, fielding_errors=1, ledger=_NO_LOG) is True  # on an error


def test_reached_base_unknown_without_the_boxscore() -> None:
    # Hitless and errorless, but no walk/HBP data — unknown, so a perfect game is never claimed.
    assert _reached_base({"liveData": {}}, side=HomeAway.AWAY, hits=0, fielding_errors=0, ledger=_CLEAN_LOG) is None


def test_reached_base_clean_sheet_versus_a_walk() -> None:
    clean = _boxscore({"home": {"baseOnBalls": 0, "hitByPitch": 0}})
    assert _reached_base(clean, side=HomeAway.HOME, hits=0, fielding_errors=0, ledger=_CLEAN_LOG) is False
    walked = _boxscore({"home": {"baseOnBalls": 1, "hitByPitch": 0}})
    assert _reached_base(walked, side=HomeAway.HOME, hits=0, fielding_errors=0, ledger=_CLEAN_LOG) is True


def test_a_clean_sheet_needs_the_defense_s_errors_on_the_linescore() -> None:
    clean = _boxscore({"home": {"baseOnBalls": 0, "hitByPitch": 0}})
    unknown = _reached_base(clean, side=HomeAway.HOME, hits=0, fielding_errors=None, ledger=_CLEAN_LOG)
    assert unknown is None  # no perfect game
    walked = _boxscore({"home": {"baseOnBalls": 1, "hitByPitch": 0}})
    on_anyway = _reached_base(walked, side=HomeAway.HOME, hits=0, fielding_errors=None, ledger=_CLEAN_LOG)
    assert on_anyway is True  # a walk put a man on anyway


def test_zero_counts_are_no_clean_sheet_without_a_complete_play_log() -> None:
    clean = _boxscore({"home": {"baseOnBalls": 0, "hitByPitch": 0}})
    assert _reached_base(clean, side=HomeAway.HOME, hits=0, fielding_errors=0, ledger=_NO_LOG) is None


# --- the play log: who reached base, plate appearance by plate appearance ---------------------------


def _batter(end: str | None, event: str) -> dict[str, Any]:
    """The batter's entry in a play's runners, as StatsAPI gives it: no origin base, and safe at ``end``, or put out
    at first when ``end`` is None."""
    movement = {"originBase": None, "start": None, "end": end, "outBase": None if end else "1B", "isOut": end is None}
    return {"movement": movement, "details": {"eventType": event}}


def _runner(origin: str, *, out_at: str, event: str) -> dict[str, Any]:
    """A runner already on ``origin`` put out at ``out_at``: a caught stealing, say."""
    movement = {"originBase": origin, "start": origin, "end": None, "outBase": out_at, "isOut": True}
    return {"movement": movement, "details": {"eventType": event}}


def _play(index: int, inning: int, half: str, *runners: dict[str, Any], complete: bool = True) -> dict[str, Any]:
    """One `allPlays` entry: its at-bat index, its inning and half, whether it is over, and its runners."""
    about = {"atBatIndex": index, "halfInning": half, "inning": inning, "isComplete": complete}
    return {"atBatIndex": index, "about": about, "runners": list(runners)}


TOP_OF_THE_3RD = 12  # the top of the 3rd's leadoff at-bat, after two innings of three up and three down
BOTTOM_OF_THE_3RD = 15


def _clean_log() -> list[dict[str, Any]]:
    """The play log up to the top of the 7th with two out, every batter put out: six innings of three up and three
    down, two outs in the 7th, and the at-bat in progress, which has no runners yet."""
    halves = [(inning, half) for inning in range(1, 7) for half in ("top", "bottom") for _ in range(3)]
    halves += [(7, "top"), (7, "top")]
    log = [_play(index, inning, half, _batter(None, "strikeout")) for index, (inning, half) in enumerate(halves)]
    log.append(_play(len(log), 7, "top", complete=False))
    return log


def _reached_at(at_bat: int) -> list[dict[str, Any]]:
    """The clean log, but the batter at ``at_bat`` struck out and reached first on the wild pitch."""
    log = _clean_log()
    log[at_bat]["runners"] = [_batter("1B", "strikeout")]
    return log


def _dropped_third_strike_log() -> list[dict[str, Any]]:
    """The top of the 3rd's leadoff man reached first on a strikeout's wild pitch, then was caught stealing in the
    next at-bat. No hit, walk, HBP or error is charged, and the half still sends up only three men."""
    log = _reached_at(TOP_OF_THE_3RD)
    caught = _runner("1B", out_at="2B", event="caught_stealing_2b")
    log[TOP_OF_THE_3RD + 1]["runners"] = [caught, _batter(None, "field_out")]
    return log


def _no_hit_feed(plays: Any, *, away_walks: int = 0) -> dict[str, Any]:
    """The fixture's top of the 7th, two out and the bases empty, with neither side yet to get a hit, a walk, an HBP
    or an error, over the play log ``plays``."""
    feed = _feed()
    line = feed["liveData"]["linescore"]
    line["teams"] = {"away": {"runs": 0, "hits": 0, "errors": 0}, "home": {"runs": 0, "hits": 0, "errors": 0}}
    line["offense"] = {}
    batting = {"away": {"baseOnBalls": away_walks, "hitByPitch": 0}, "home": {"baseOnBalls": 0, "hitByPitch": 0}}
    feed["liveData"]["boxscore"] = _boxscore(batting)["liveData"]["boxscore"]
    feed["liveData"]["plays"]["allPlays"] = plays
    return feed


def test_a_batter_who_reached_on_a_dropped_third_strike_spoils_the_perfect_game_but_not_the_no_hitter() -> None:
    state = _parse_game_state(_no_hit_feed(_dropped_third_strike_log()))
    assert (state.away_hits, state.home_errors) == (0, 0)  # every count is still zero...
    assert state.away_reached_base is True  # ...but the play log shows a man reached first
    feat = pitching_feat_progress(state, min_completed_innings=6)
    assert feat is not None and feat.kind is PitchingFeatKind.NO_HITTER and not feat.perfect


def test_a_complete_clean_play_log_with_every_count_zero_is_a_perfect_game() -> None:
    # The at-bat in progress has no runners yet; every finished one has its batter put out.
    state = _parse_game_state(_no_hit_feed(_clean_log()))
    assert state.away_reached_base is False
    feat = pitching_feat_progress(state, min_completed_innings=6)
    assert feat is not None and feat.kind is PitchingFeatKind.PERFECT_GAME and feat.completed_innings == 6


def test_a_walk_in_the_counts_is_a_baserunner_whatever_the_play_log_says() -> None:
    state = _parse_game_state(_no_hit_feed(_clean_log(), away_walks=1))
    assert state.away_reached_base is True


@pytest.mark.parametrize(
    ("at_bat", "away", "home"),
    [(TOP_OF_THE_3RD, True, False), (BOTTOM_OF_THE_3RD, False, True)],
)
def test_a_reach_counts_against_the_batting_side_only(at_bat: int, away: bool, home: bool) -> None:
    state = _parse_game_state(_no_hit_feed(_reached_at(at_bat)))
    assert (state.away_reached_base, state.home_reached_base) == (away, home)
    # Both sides are hitless, so the bid shown is the home staff's, on the away side's batters.
    feat = pitching_feat_progress(state, min_completed_innings=6)
    assert feat is not None and feat.side is HomeAway.HOME and feat.perfect is not away


@pytest.mark.parametrize(
    "movement",
    [
        {"originBase": None, "end": "2B", "isOut": False},  # a double
        {"originBase": None, "end": "score", "isOut": False},  # a home run
        {"originBase": None, "end": "1B"},  # safe at first, and not said to be out
    ],
)
def test_a_batter_who_ends_a_play_on_a_base_or_home_and_not_out_has_reached(movement: dict[str, Any]) -> None:
    log = _clean_log()
    log[TOP_OF_THE_3RD]["runners"] = [{"movement": movement}]
    assert _parse_game_state(_no_hit_feed(log)).away_reached_base is True


def test_a_reach_in_the_at_bat_still_in_progress_counts() -> None:
    log = _clean_log()
    log[-1]["runners"] = [_batter("1B", "strikeout")]  # the play is not yet marked complete
    assert _parse_game_state(_no_hit_feed(log)).away_reached_base is True


def _log(feed: dict[str, Any]) -> Any:
    return feed["liveData"]["plays"]["allPlays"]


def _drop(*path: Any) -> Callable[[dict[str, Any]], None]:
    """A change to a feed that deletes the node at ``path`` in its play log."""

    def change(feed: dict[str, Any]) -> None:
        node = _log(feed)
        for key in path[:-1]:
            node = node[key]
        del node[path[-1]]

    return change


def _put(*path: Any, value: Any) -> Callable[[dict[str, Any]], None]:
    """A change to a feed that sets the node at ``path`` in its play log to ``value``."""

    def change(feed: dict[str, Any]) -> None:
        node = _log(feed)
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = value

    return change


def _swap(feed: dict[str, Any]) -> None:
    log = _log(feed)
    log[20], log[21] = log[21], log[20]


@pytest.mark.parametrize(
    "change",
    [
        pytest.param(lambda feed: feed["liveData"]["plays"].pop("allPlays"), id="no allPlays"),
        pytest.param(lambda feed: feed["liveData"]["plays"].update(allPlays={}), id="allPlays not a list"),
        pytest.param(lambda feed: feed["liveData"].update(plays=[]), id="a malformed plays node"),
        pytest.param(lambda feed: feed["liveData"]["plays"].update(allPlays=[]), id="an empty log in the 7th"),
        pytest.param(_drop(20), id="an at-bat missing from the middle"),
        pytest.param(_drop(0), id="the first at-bat missing"),
        pytest.param(_swap, id="two at-bats out of order"),
        pytest.param(_drop(TOP_OF_THE_3RD, "runners"), id="a finished play with no runners"),
        pytest.param(_put(TOP_OF_THE_3RD, "runners", value=[]), id="a finished play with an empty runners list"),
        pytest.param(_put(TOP_OF_THE_3RD, "runners", 0, value={"details": {}}), id="a runner with no movement"),
        pytest.param(_drop(TOP_OF_THE_3RD, "about", "halfInning"), id="a play that names no half"),
        pytest.param(_put(TOP_OF_THE_3RD, value="not a play"), id="a play that is not an object"),
        pytest.param(
            _put(TOP_OF_THE_3RD, value=_play(TOP_OF_THE_3RD, 3, "top", complete=False)),
            id="an at-bat before the last still in progress",
        ),
    ],
)
def test_a_play_log_that_cant_rule_out_a_reach_leaves_a_plain_no_hitter(
    change: Callable[[dict[str, Any]], None],
) -> None:
    feed = _no_hit_feed(_clean_log())
    change(feed)
    state = _parse_game_state(feed)
    assert state.away_reached_base is None
    feat = pitching_feat_progress(state, min_completed_innings=6)
    assert feat is not None and feat.kind is PitchingFeatKind.NO_HITTER


def test_every_answer_reads_the_whole_play_log_afresh_so_a_rewritten_play_shows_at_once() -> None:
    # The top of the 3rd's leadoff man is logged out on strikes, then rewritten as safe at first on the wild pitch,
    # then back to out: each answer is read as it stands, with nothing kept from the one before.
    answers = [_no_hit_feed(_clean_log()), _no_hit_feed(_reached_at(TOP_OF_THE_3RD)), _no_hit_feed(_clean_log())]
    provider = MlbStatsApiProvider(MlbTeamRegistry({}), fetch_game=lambda _pk: answers.pop(0))
    reads = [provider.fetch_live_feed(_game(), now=NOW).state.away_reached_base for _ in range(3)]
    assert reads == [False, True, False]


def test_the_fields_whitelist_keeps_what_the_play_log_reads() -> None:
    read = {"allPlays", "about", "halfInning", "atBatIndex", "isComplete", "runners", "movement", "originBase", "end"}
    assert read | {"isOut"} <= set(_GAME_FIELDS.split(","))


def test_a_no_hit_side_is_no_perfect_game_without_every_count_that_could_have_put_a_man_on() -> None:
    feed = _no_hit_feed(_clean_log())  # a clean, complete play log
    del feed["liveData"]["linescore"]["teams"]["home"]["errors"]  # but the defense's errors are left out
    state = _parse_game_state(feed)
    assert state.home_errors == 0 and state.away_reached_base is None  # shown as 0, but no clean sheet claimed
    feat = pitching_feat_progress(state, min_completed_innings=6)
    assert feat is not None and feat.kind is PitchingFeatKind.NO_HITTER


# --- a linescore missing its numbers ---------------------------------------------------------------

_SIDE = {"runs": 1, "hits": 2, "errors": 0}


def _linescore(
    inning: int, inning_state: str, *, away: dict[str, int], home: dict[str, int], status: str = "In Progress"
) -> dict[str, Any]:
    """A bare game feed: its status, and its linescore at ``inning`` / ``inning_state`` with each side's counts."""
    teams = {"away": away, "home": home}
    line = {"currentInning": inning, "inningState": inning_state, "teams": teams}
    return {"gameData": {"status": {"detailedState": status}}, "liveData": {"linescore": line}}


@pytest.mark.parametrize(
    "inning, inning_state, side, missing",
    [
        (1, "Top", "away", "runs"),  # the away side bats first: it has batted once the game is under way
        (1, "Top", "away", "hits"),
        (1, "Bottom", "home", "runs"),  # the home side has from the bottom of the 1st
        (1, "End", "home", "hits"),
        (7, "Top", "home", "hits"),  # a hitless-looking home side in the 7th: no no-hitter from a missing count
        (9, "End", "away", "runs"),
    ],
)
def test_a_side_that_has_batted_without_its_runs_or_hits_is_a_failed_read(
    inning: int, inning_state: str, side: str, missing: str
) -> None:
    sides = {"away": dict(_SIDE), "home": dict(_SIDE)}
    del sides[side][missing]
    with pytest.raises(ProviderError, match=f"incomplete linescore: no {missing} for the {side} side"):
        _parse_game_state(_linescore(inning, inning_state, away=sides["away"], home=sides["home"]))


@pytest.mark.parametrize("inning_state", ["Top", "Middle"])
def test_a_home_side_yet_to_bat_without_its_runs_or_hits_has_a_true_zero(inning_state: str) -> None:
    state = _parse_game_state(_linescore(1, inning_state, away=_SIDE, home={}))
    assert (state.home_score, state.home_hits, state.home_errors) == (0, 0, 0)
    assert (state.away_score, state.away_hits) == (1, 2)


def test_a_final_without_both_sides_runs_is_a_failed_read_however_early_it_ended() -> None:
    with pytest.raises(ProviderError, match="no runs for the home side"):
        _parse_game_state(_linescore(1, "Top", away=_SIDE, home={"hits": 0}, status="Final"))
    state = _parse_game_state(_linescore(1, "Top", away=_SIDE, home={"runs": 0}, status="Final"))
    assert (state.home_score, state.home_hits) == (0, 0)  # the hits of a side yet to bat are still a true 0


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


def test_a_live_feed_carries_its_own_status_and_timestamp() -> None:
    # Only the feed's own word that a game is final lets its state stand as the result.
    feed = _feed()
    feed["gameData"]["status"] = {"detailedState": "Final"}
    feed["metaData"] = {"timeStamp": "20260617_233012"}
    provider = MlbStatsApiProvider(MlbTeamRegistry({}), fetch_game=lambda _pk: feed)
    live = provider.fetch_live_feed(_game(), now=NOW)
    assert live.status is GameStatus.FINAL and live.as_of == datetime(2026, 6, 17, 23, 30, 12, tzinfo=timezone.utc)
    assert {"gameData", "status", "detailedState", "metaData", "timeStamp"} <= set(_GAME_FIELDS.split(","))


@pytest.mark.parametrize(
    ("game_data", "meta_data"),
    [({}, {}), ({"status": {"detailedState": ""}}, {"timeStamp": "not a time"}), ({"status": None}, {"timeStamp": 7})],
)
def test_a_feed_that_doesnt_say_its_status_or_time_says_neither(game_data: Any, meta_data: Any) -> None:
    feed = _feed()
    feed["gameData"], feed["metaData"] = game_data, meta_data
    live = MlbStatsApiProvider(MlbTeamRegistry({}), fetch_game=lambda _pk: feed).fetch_live_feed(_game(), now=NOW)
    assert live.status is None and live.as_of is None


def test_the_fixture_feed_is_in_progress() -> None:
    provider = MlbStatsApiProvider(MlbTeamRegistry({}), fetch_game=lambda _pk: _feed())
    assert provider.fetch_live_feed(_game(), now=NOW).status is GameStatus.LIVE


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


def _break_feed(offense: dict[str, Any], *, inning_state: str = "Middle") -> dict[str, Any]:
    feed = _feed()
    feed["liveData"]["linescore"]["inningState"] = inning_state
    feed["liveData"]["linescore"]["offense"] = offense
    feed["liveData"]["boxscore"]["teams"]["home"]["players"].update(
        {
            "ID1": {"battingOrder": "100", "stats": {"batting": {"atBats": 3, "hits": 2, "homeRuns": 1, "rbi": 2}}},
            "ID2": {"battingOrder": "200", "stats": {"batting": {"atBats": 3, "hits": 1}}},
            "ID3": {"battingOrder": "300", "stats": {"batting": {"atBats": 3, "hits": 0}}},
        }
    )
    return feed


DUE_UP_OFFENSE = {
    "batter": {"id": 1, "fullName": "Shohei Ohtani"},
    "onDeck": {"id": 2, "fullName": "Mookie Betts"},
    "inHole": {"id": 3, "fullName": "Ken Griffey Jr."},
}


def test_due_up_reads_the_next_three_batters_on_a_break() -> None:
    due_up = _parse_game_state(_break_feed(DUE_UP_OFFENSE)).due_up
    assert due_up == DueUp(
        batters=(
            BatterGameLine(name="Ohtani", order=1, at_bats=3, hits=2, rbi=2, home_runs=1),
            BatterGameLine(name="Betts", order=2, at_bats=3, hits=1),
            BatterGameLine(name="Griffey", order=3, at_bats=3, hits=0),
        )
    )
    assert _parse_game_state(_break_feed(DUE_UP_OFFENSE, inning_state="End")).due_up is not None


def test_due_up_is_none_during_a_live_half() -> None:
    assert _parse_game_state(_break_feed(DUE_UP_OFFENSE, inning_state="Top")).due_up is None


def test_due_up_keeps_the_batters_the_feed_does_name() -> None:
    partial = {"batter": {"id": 1, "fullName": "Shohei Ohtani"}, "onDeck": {"id": 2}, "inHole": None}
    due_up = _parse_game_state(_break_feed(partial)).due_up
    assert due_up is not None and [b.name for b in due_up.batters] == ["Ohtani"]


def test_a_due_up_batter_off_every_roster_is_left_out() -> None:
    offense = {"batter": {"id": 99999, "fullName": "No Roster"}, "onDeck": {"id": 2, "fullName": "Mookie Betts"}}
    due_up = _parse_game_state(_break_feed(offense)).due_up
    assert due_up is not None and [b.name for b in due_up.batters] == ["Betts"]


def test_due_up_is_none_when_the_feed_names_no_one() -> None:
    assert _parse_game_state(_break_feed({})).due_up is None


def test_the_fields_whitelist_keeps_what_due_up_reads() -> None:
    # StatsAPI drops any key the whitelist omits, silently: a fixture would still pass.
    assert {"onDeck", "inHole", "fullName"} <= set(_GAME_FIELDS.split(","))


def _pregame_feed() -> dict[str, Any]:
    return {
        "gameData": {
            "teams": {"away": {"record": {"wins": 38, "losses": 44}}, "home": {"record": {"wins": 51, "losses": 31}}},
            "probablePitchers": {
                "away": {"id": 21, "fullName": "German Marquez"},
                "home": {"id": 22, "fullName": "Clayton Kershaw"},
            },
        },
        "liveData": {
            "boxscore": {
                "teams": {
                    "away": {
                        "players": {"ID21": {"seasonStats": {"pitching": {"wins": 5, "losses": 9, "era": "4.68"}}}}
                    },
                    "home": {
                        "players": {"ID22": {"seasonStats": {"pitching": {"wins": 12, "losses": 3, "era": "2.87"}}}}
                    },
                }
            }
        },
    }


def test_pregame_details_read_the_records_and_probable_starters() -> None:
    provider = MlbStatsApiProvider(MlbTeamRegistry({}), fetch_pregame=lambda _pk: _pregame_feed())
    details = provider.fetch_pregame_details(_game())
    assert details.away == PregameSide(
        record=WinLossRecord(wins=38, losses=44),
        starter=StartingPitcher(name="Marquez", record=WinLossRecord(wins=5, losses=9), era="4.68"),
    )
    assert details.home.starter is not None and details.home.starter.name == "Kershaw"


def test_a_starter_not_yet_announced_or_not_yet_rostered_comes_back_partial() -> None:
    feed = _pregame_feed()
    feed["gameData"]["probablePitchers"] = {"away": {"id": 99, "fullName": "New Guy"}}
    details = MlbStatsApiProvider(MlbTeamRegistry({}), fetch_pregame=lambda _pk: feed).fetch_pregame_details(_game())
    assert details.away.starter == StartingPitcher(name="Guy")  # named, with no season line to show
    assert details.home.starter is None and details.home.record == WinLossRecord(wins=51, losses=31)


def test_a_bare_pregame_feed_comes_back_blank() -> None:
    details = MlbStatsApiProvider(MlbTeamRegistry({}), fetch_pregame=lambda _pk: {}).fetch_pregame_details(_game())
    assert details == PregameDetails()


def test_a_failed_pregame_fetch_is_a_provider_error() -> None:
    def down(_pk: Any) -> dict[str, Any]:
        raise OSError("connection refused")

    with pytest.raises(ProviderError, match="pregame fetch failed"):
        MlbStatsApiProvider(MlbTeamRegistry({}), fetch_pregame=down).fetch_pregame_details(_game())


def _decision_feed() -> dict[str, Any]:
    feed = _feed()
    feed["liveData"]["decisions"] = {
        "winner": {"id": 11, "fullName": "Clayton Kershaw"},
        "loser": {"id": 12, "fullName": "German Marquez"},
        "save": {"id": 13, "fullName": "Tanner Scott"},
    }
    feed["liveData"]["boxscore"]["teams"]["home"]["players"].update(
        {
            "ID11": {"seasonStats": {"pitching": {"wins": 12, "losses": 3}}},
            "ID13": {"seasonStats": {"pitching": {"saves": 27}}},
        }
    )
    feed["liveData"]["boxscore"]["teams"]["away"]["players"]["ID12"] = {
        "seasonStats": {"pitching": {"wins": 5, "losses": 9}}
    }
    return feed


def test_decisions_carry_the_season_totals_from_the_boxscore() -> None:
    assert _parse_decisions(_decision_feed()) == PitchingDecisions(
        winner="Clayton Kershaw",
        loser="German Marquez",
        save="Tanner Scott",
        winner_record=WinLossRecord(wins=12, losses=3),
        loser_record=WinLossRecord(wins=5, losses=9),
        saves=27,
    )


def test_decisions_without_season_totals_still_name_the_pitchers() -> None:
    decisions = {"winner": {"fullName": "W"}, "loser": {"id": 404, "fullName": "L"}}
    parsed = _parse_decisions({"liveData": {"decisions": decisions}})
    assert parsed == PitchingDecisions(winner="W", loser="L")


def test_a_half_a_record_is_no_record() -> None:
    feed = _decision_feed()
    feed["liveData"]["boxscore"]["teams"]["home"]["players"]["ID11"] = {"seasonStats": {"pitching": {"wins": 12}}}
    parsed = _parse_decisions(feed)
    assert parsed is not None and parsed.winner_record is None


def test_the_fields_whitelist_keeps_what_the_season_totals_read() -> None:
    assert {"seasonStats", "wins", "losses", "saves"} <= set(_GAME_FIELDS.split(","))


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


# --- what each field of the state rests on ---------------------------------------------------------


def _read(feed: dict[str, Any]) -> BaseballGameState:
    """The state the provider reads from ``feed``, the way the board reads every live game."""
    provider = MlbStatsApiProvider(_REG, fetch_game=lambda game_pk: feed)
    return provider.fetch_live_feed(_game(), now=NOW).state


def _without(*keys: str) -> dict[str, Any]:
    """The fixture feed, the top of the 7th, with ``keys`` taken out of its linescore."""
    feed = _feed()
    for key in keys:
        del feed["liveData"]["linescore"][key]
    return feed


def test_a_full_linescore_is_reported_in_every_field() -> None:
    state = _read(_feed())
    assert state.coverage == CoverageMap()
    assert all(state.known(field) for field in BaseballField)


def test_a_linescore_without_its_outs_still_shows_none_out_but_says_that_is_a_stand_in() -> None:
    state = _read(_without("outs"))
    assert state.count == BaseballCount(balls=2, strikes=1, outs=0)
    assert state.coverage == CoverageMap.of({BaseballField.OUTS: FieldCoverage.DEFAULTED})
    assert not state.known(BaseballField.OUTS) and state.known(BaseballField.COUNT)


@pytest.mark.parametrize("missing", [("balls",), ("strikes",), ("balls", "strikes")])
def test_a_linescore_without_its_count_shows_0_for_what_it_left_out_as_a_stand_in(missing: tuple[str, ...]) -> None:
    state = _read(_without(*missing))
    shown = {"balls": 2, "strikes": 1, **{key: 0 for key in missing}}
    assert state.count == BaseballCount(outs=2, **shown)
    assert state.coverage == CoverageMap.of({BaseballField.COUNT: FieldCoverage.DEFAULTED})


def test_a_linescore_without_its_offense_shows_empty_bases_as_a_stand_in() -> None:
    state = _read(_without("offense"))
    assert state.bases == BaseballBaseState()
    assert state.coverage == CoverageMap.of({BaseballField.BASES: FieldCoverage.DEFAULTED})
    assert not state.known(BaseballField.BASES)


def test_an_offense_that_names_no_runner_reports_empty_bases() -> None:
    feed = _feed()
    feed["liveData"]["linescore"]["offense"] = {"batter": {"id": 1}}
    state = _read(feed)
    assert state.bases == BaseballBaseState()
    assert state.coverage == CoverageMap() and state.known(BaseballField.BASES)


@pytest.mark.parametrize("inning_state", ["Mid", None])
def test_an_inning_state_it_doesnt_know_shows_the_top_as_a_stand_in(inning_state: str | None) -> None:
    feed = _without("inningState")
    if inning_state is not None:
        feed["liveData"]["linescore"]["inningState"] = inning_state
    state = _read(feed)
    assert state.phase is InningPhase.TOP
    assert state.coverage == CoverageMap.of({BaseballField.HALF: FieldCoverage.DEFAULTED})
    assert not state.known(BaseballField.HALF)


def test_a_sides_missing_errors_show_as_0_marked_as_a_stand_in() -> None:
    state = _read(_linescore(7, "Top", away=_SIDE, home={"runs": 1, "hits": 2}))
    assert state.home_errors == 0
    assert state.coverage[BaseballField.ERRORS] is FieldCoverage.DEFAULTED and not state.known(BaseballField.ERRORS)
    assert state.known(BaseballField.SCORE) and state.known(BaseballField.HITS)


@pytest.mark.parametrize("inning_state", ["Top", "Middle"])
def test_a_side_yet_to_bat_has_a_true_0_inferred_from_the_half(inning_state: str) -> None:
    state = _read(_linescore(1, inning_state, away=_SIDE, home={}))
    assert (state.home_score, state.home_hits) == (0, 0)
    assert state.coverage[BaseballField.SCORE] is FieldCoverage.INFERRED
    assert state.coverage[BaseballField.HITS] is FieldCoverage.INFERRED
    assert state.known(BaseballField.SCORE) and state.known(BaseballField.HITS)


def test_no_stand_in_is_known_however_many_the_feed_needs() -> None:
    """A bare linescore in the 1st with a half it doesn't know: the home side's runs, hits and errors, the count,
    the outs and the bases are all left out, and the half that would vouch for the home side's zeros is a stand-in
    too."""
    state = _read(_linescore(1, "Mid", away=_SIDE, home={}))
    assert (state.phase, state.count, state.bases) == (
        InningPhase.TOP,
        BaseballCount(balls=0, strikes=0, outs=0),
        BaseballBaseState(),
    )
    assert (state.home_score, state.home_hits, state.home_errors) == (0, 0, 0)
    defaulted = {field for field in BaseballField if state.coverage[field] is FieldCoverage.DEFAULTED}
    assert defaulted == set(BaseballField) - {BaseballField.INNING}
    assert not any(state.known(field) for field in defaulted)
    assert state.known(BaseballField.INNING)
