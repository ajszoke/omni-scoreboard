"""Tests for the MLB StatsAPI schedule provider (fixture-driven, no network)."""

from __future__ import annotations

import ast
import io
import json
import tokenize
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from omni.core.enum import GameStatus, League, SeasonStage
from omni.core.ids import SourceRef
from omni.domain.contest import TeamGame
from omni.providers.base import ProviderError, ProviderUpdate
from omni.providers.mlb_statsapi import (
    HTTP_TIMEOUT_S,
    MlbStatsApiProvider,
    TimedRequests,
    _parse_start,
    _schedule_rows,
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
    assert provider.schedule_timezone == ZoneInfo("America/New_York")
    provider.fetch_schedule(date(2026, 6, 16), now=datetime(2026, 6, 18, 2, 0, tzinfo=timezone.utc))
    assert calls[-1] == date(2026, 6, 16)  # any one date, for a schedule window


def test_refresh_parses_known_games_and_warns_on_unknown_team() -> None:
    provider, calls = _provider()
    update = provider.refresh(NOW)

    assert calls == [(NOW.date(), "1,51")]  # 21:00Z == 17:00 ET same day, so local == UTC date here
    assert update.observed_at == NOW
    assert update.source.name == "mlb_statsapi"

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
        # StatsAPI writes a manager's challenge and an umpire's review with a lowercase second word, an umpire's
        # ball-strike challenge with a capital.
        ("Manager challenge", GameStatus.LIVE),
        ("Manager challenge: Tag play", GameStatus.LIVE),
        ("Umpire review: Home run", GameStatus.LIVE),
        ("Instant Replay", GameStatus.LIVE),
        ("Umpire Challenge: Pitch Result", GameStatus.LIVE),
        ("Player challenge: Pitch Result", GameStatus.LIVE),
        ("Final", GameStatus.FINAL),
        ("Final: Tied", GameStatus.FINAL),
        ("Game Over", GameStatus.FINAL),
        ("Forfeit: Rule", GameStatus.FINAL),
        ("Postponed", GameStatus.POSTPONED),
        ("Postponed: Rain", GameStatus.POSTPONED),
        ("Cancelled", GameStatus.CANCELED),
        ("Canceled", GameStatus.CANCELED),
        ("Cancelled: Wet Grounds", GameStatus.CANCELED),
        ("Delayed: Rain", GameStatus.DELAYED),
        ("Delayed Start: Rain", GameStatus.DELAYED),
        ("Suspended: Inclement Weather", GameStatus.SUSPENDED),
        ("Frobnicated", GameStatus.UNKNOWN),
        ("", GameStatus.UNKNOWN),
    ],
)
def test_map_game_status(raw: str, expected: GameStatus) -> None:
    assert map_game_status(raw) is expected
    assert map_game_status(raw.upper()) is expected and map_game_status(raw.lower()) is expected  # in any case


# Upstream's copy of StatsAPI's game statuses (`statsapi.meta("gameStatus")`), each constant with its coarse state.
_UPSTREAM_STATUSES = Path(__file__).resolve().parents[1] / "data" / "status.py"
# What upstream marks a status of its own, not StatsAPI's.
_NOT_STATSAPIS = "Not in json"


def _official_statuses() -> dict[str, str]:
    """Each status upstream's `data/status.py` names, with the coarse state (`abstractGameState`) its trailing
    comment gives: read from the module's source, so reading it imports nothing. Upstream's own statuses are left
    out."""
    source = _UPSTREAM_STATUSES.read_text()
    comments = {
        token.start[0]: token.string.removeprefix("#").strip()
        for token in tokenize.generate_tokens(io.StringIO(source).readline)
        if token.type == tokenize.COMMENT
    }
    statuses: dict[str, str] = {}
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            statuses[node.value.value] = comments[node.lineno]
    return {status: coarse for status, coarse in statuses.items() if coarse != _NOT_STATSAPIS}


# The families of StatsAPI's statuses: what each status's text begins with, the coarse state StatsAPI gives every
# status in it, and the typed status it maps to.
_FAMILIES: tuple[tuple[str, str, GameStatus], ...] = (
    ("Scheduled", "Preview", GameStatus.SCHEDULED),
    ("Pre-Game", "Preview", GameStatus.PREGAME),
    ("Warmup", "Live", GameStatus.PREGAME),
    ("In Progress", "Live", GameStatus.LIVE),
    ("Manager challenge", "Live", GameStatus.LIVE),
    ("Umpire review", "Live", GameStatus.LIVE),
    ("Instant Replay", "Live", GameStatus.LIVE),
    ("Umpire Challenge", "Live", GameStatus.LIVE),
    ("Player challenge", "Live", GameStatus.LIVE),
    ("Delayed Start", "Preview", GameStatus.DELAYED),
    ("Delayed", "Live", GameStatus.DELAYED),
    ("Suspended", "Live", GameStatus.SUSPENDED),
    ("Final", "Final", GameStatus.FINAL),
    ("Game Over", "Final", GameStatus.FINAL),
    ("Completed Early", "Final", GameStatus.FINAL),
    ("Forfeit", "Final", GameStatus.FINAL),
    ("Postponed", "Final", GameStatus.POSTPONED),
    ("Cancelled", "Final", GameStatus.CANCELED),
    ("Unknown", "Other", GameStatus.UNKNOWN),
    ("Writing", "Other", GameStatus.UNKNOWN),
)


def _family(status: str) -> tuple[str, str, GameStatus]:
    """The family ``status`` belongs to: the longest of their beginnings it starts with."""
    return max((family for family in _FAMILIES if status.startswith(family[0])), key=lambda family: len(family[0]))


def test_every_official_status_maps_by_its_family() -> None:
    official = _official_statuses()
    assert set(official.values()) == {"Preview", "Live", "Final", "Other"}
    members: dict[str, list[str]] = {beginning: [] for beginning, _, _ in _FAMILIES}
    for status, coarse in official.items():
        beginning, family_coarse, expected = _family(status)
        members[beginning].append(status)
        assert coarse == family_coarse, status
        assert map_game_status(status) is expected, status
        assert map_game_status(status, coarse) is expected, status
    assert all(members.values())  # no family is a beginning no status has


def test_only_the_two_official_statuses_that_say_nothing_map_to_unknown() -> None:
    official = _official_statuses()
    unknown = {status for status in official if map_game_status(status) is GameStatus.UNKNOWN}
    assert unknown == {status for status, coarse in official.items() if coarse == "Other"} == {"Unknown", "Writing"}


@pytest.mark.parametrize(
    ("abstract", "expected"),
    [
        ("Live", GameStatus.LIVE),
        ("live", GameStatus.LIVE),
        ("Final", GameStatus.FINAL),
        ("Preview", GameStatus.SCHEDULED),
        ("Other", GameStatus.UNKNOWN),
        ("", GameStatus.UNKNOWN),
        (None, GameStatus.UNKNOWN),
    ],
)
def test_a_detailed_state_no_family_names_falls_back_to_its_coarse_state(
    abstract: str | None, expected: GameStatus
) -> None:
    assert map_game_status("Crew chief review: Tag play", abstract) is expected


def test_a_family_outranks_the_coarse_state() -> None:
    assert map_game_status("Delayed: Rain", "Live") is GameStatus.DELAYED
    assert map_game_status("Postponed: Rain", "Final") is GameStatus.POSTPONED
    assert map_game_status("Warmup", "Live") is GameStatus.PREGAME


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


def test_statsapi_requests_get_a_timeout_unless_they_set_one() -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    class _Requests:
        codes = "passed through"

        def get(self, url: str, **kwargs: object) -> str:
            calls.append((url, kwargs))
            return "response"

    timed = TimedRequests(_Requests(), HTTP_TIMEOUT_S)
    assert timed.get("https://statsapi.mlb.com/a") == "response"
    timed.get("https://statsapi.mlb.com/b", timeout=3)
    assert calls == [("https://statsapi.mlb.com/a", {"timeout": 15.0}), ("https://statsapi.mlb.com/b", {"timeout": 3})]
    assert timed.codes == "passed through"


# --- the schedule's facts about each game ----------------------------------------------------------------


def _broadcast(kind: str, *, national: bool = False, language: str = "en", side: str = "home") -> dict[str, Any]:
    """One of a game's listed broadcasts: TV or radio (``AM``/``FM``), its language, and whether it is national."""
    return {"type": kind, "language": language, "isNational": national, "homeAway": side}


def _scheduled(game_pk: int, **changes: Any) -> dict[str, Any]:
    """One game as the `schedule` call gives it: COL at LAD, a regular-season night game, on local TV and radio."""
    game: dict[str, Any] = {
        "gamePk": game_pk,
        "gameDate": "2026-06-18T02:10:00Z",
        "officialDate": "2026-06-17",
        "gameType": "R",
        "doubleHeader": "N",
        "gameNumber": 1,
        "status": {"detailedState": "Scheduled", "startTimeTBD": False},
        "teams": {
            "away": {"team": {"id": 115, "name": "Colorado Rockies"}},
            "home": {"team": {"id": 119, "name": "Los Angeles Dodgers"}},
        },
        "venue": {"id": 22, "name": "Dodger Stadium"},
        "broadcasts": [_broadcast("TV"), _broadcast("TV", side="away"), _broadcast("AM")],
    }
    game.update(changes)
    return game


def _response(*games: Any, day: str = "2026-06-17") -> dict[str, Any]:
    return {"totalItems": len(games), "dates": [{"date": day, "games": list(games)}]}


def _slate(response: Any) -> ProviderUpdate:
    """``response`` read as the provider reads its schedule call, for the day it was asked for."""
    provider = MlbStatsApiProvider(MlbTeamRegistry.from_color_file(), lambda day, sport_ids: _schedule_rows(response))
    return provider.fetch_schedule(date(2026, 6, 17), now=NOW)


def _scheduled_game(**changes: Any) -> TeamGame:
    (game,) = _slate(_response(_scheduled(1, **changes))).contests
    assert isinstance(game, TeamGame)
    return game


def test_a_schedule_game_flattens_into_the_row_the_parse_reads() -> None:
    status = {"abstractGameState": "Preview", "detailedState": "Scheduled", "startTimeTBD": True}
    assert _schedule_rows(_response(_scheduled(1, status=status))) == [
        {
            "game_id": 1,
            "game_datetime": "2026-06-18T02:10:00Z",
            "game_date": "2026-06-17",
            "game_type": "R",
            "status": "Scheduled",
            "abstract_game_state": "Preview",
            "away_id": 115,
            "away_name": "Colorado Rockies",
            "home_id": 119,
            "home_name": "Los Angeles Dodgers",
            "venue_name": "Dodger Stadium",
            "doubleheader": "N",
            "game_num": 1,
            "start_time_tbd": True,
            "broadcasts": [_broadcast("TV"), _broadcast("TV", side="away"), _broadcast("AM")],
        }
    ]


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ({"abstractGameState": "Live", "detailedState": "Manager challenge: Tag play"}, GameStatus.LIVE),
        ({"abstractGameState": "Live", "detailedState": "Crew chief review"}, GameStatus.LIVE),  # one added later
        ({"abstractGameState": "Final", "detailedState": "Called"}, GameStatus.FINAL),
        ({"abstractGameState": 7, "detailedState": "Crew chief review"}, GameStatus.UNKNOWN),  # not a state
        ({"detailedState": "Crew chief review"}, GameStatus.UNKNOWN),
    ],
)
def test_a_schedule_game_whose_status_no_family_names_takes_its_coarse_state(
    status: dict[str, Any], expected: GameStatus
) -> None:
    assert _scheduled_game(status=status).status is expected


def test_a_scheduled_game_is_typed_with_the_schedules_facts() -> None:
    game = _scheduled_game()
    assert (game.id.raw, game.away.abbreviation, game.home.abbreviation) == ("1", "COL", "LAD")
    assert game.status is GameStatus.SCHEDULED and game.venue_name == "Dodger Stadium"
    assert game.stage is SeasonStage.REGULAR
    assert (game.game_number, game.start_tbd, game.national_broadcast, game.resumed_from) == (None, False, False, None)
    assert game.neutral_site is None  # the schedule has no neutral-site flag


def test_the_schedules_own_date_for_a_late_game_is_kept_apart_from_its_utc_start() -> None:
    game = _scheduled_game()  # a 7:10 p.m. Pacific start is the next day in UTC
    assert game.scheduled_start.date() == date(2026, 6, 18)
    assert game.provider_date == date(2026, 6, 17)
    (filed,) = _slate(_response(_scheduled(1, officialDate=None), day="2026-06-16")).contests
    assert filed.provider_date == date(2026, 6, 16)  # without its own date, the date the response files it under


def test_a_doubleheaders_two_games_between_the_same_clubs_on_the_same_date_stay_two_contests() -> None:
    first = _scheduled(745001, doubleHeader="Y", gameNumber=1, gameDate="2026-06-17T17:10:00Z")
    second = _scheduled(745002, doubleHeader="Y", gameNumber=2, gameDate="2026-06-17T21:10:00Z")
    update = _slate(_response(first, second))
    one, two = update.contests
    assert isinstance(one, TeamGame) and isinstance(two, TeamGame)
    assert one.id != two.id and (one.id.raw, two.id.raw) == ("745001", "745002")
    assert (one.away, one.home, one.provider_date) == (two.away, two.home, two.provider_date)
    assert (one.game_number, two.game_number) == (1, 2)


@pytest.mark.parametrize(
    ("double_header", "number", "expected"),
    [
        ("S", 2, 2),  # a split doubleheader's second game
        ("Y", 1, 1),
        ("N", 1, None),  # every game is game 1 of its day; only a doubleheader's number counts
        ("Y", None, None),
        ("Y", 3, None),
        ("Y", True, None),
        (["Y"], 1, None),
    ],
)
def test_only_a_doubleheaders_game_carries_its_number(double_header: Any, number: Any, expected: int | None) -> None:
    assert _scheduled_game(doubleHeader=double_header, gameNumber=number).game_number == expected


@pytest.mark.parametrize(
    ("game_type", "stage"),
    [
        ("S", SeasonStage.PRESEASON),
        ("R", SeasonStage.REGULAR),
        ("F", SeasonStage.POSTSEASON),
        ("D", SeasonStage.POSTSEASON),
        ("L", SeasonStage.POSTSEASON),
        ("W", SeasonStage.POSTSEASON),
        ("A", SeasonStage.ALL_STAR),
        ("E", SeasonStage.EXHIBITION),
        ("X", SeasonStage.UNKNOWN),
        (None, SeasonStage.UNKNOWN),
    ],
)
def test_a_games_type_sets_its_stage(game_type: str | None, stage: SeasonStage) -> None:
    assert _scheduled_game(gameType=game_type).stage is stage


def test_a_start_time_still_to_be_set_says_so() -> None:
    game = _scheduled_game(status={"detailedState": "Scheduled", "startTimeTBD": True})
    assert game.start_tbd is True
    assert _scheduled_game(status={"detailedState": "Scheduled"}).start_tbd is False


@pytest.mark.parametrize(
    ("broadcasts", "national"),
    [
        ([_broadcast("TV"), _broadcast("TV", national=True)], True),
        ([_broadcast("TV", national=True, language="es")], True),  # a Spanish-language US network
        ([_broadcast("TV"), _broadcast("TV", side="away"), _broadcast("AM", national=True)], False),  # radio
        ([_broadcast("TV", language="fr")], False),  # Toronto's French-language TV, a local broadcast
        ([], None),
        (["junk"], None),
        (None, None),
    ],
)
def test_national_tv_is_said_only_by_a_us_national_tv_broadcast(broadcasts: Any, national: bool | None) -> None:
    assert _scheduled_game(broadcasts=broadcasts).national_broadcast is national


@pytest.mark.parametrize(
    ("changes", "resumed_from"),
    [
        ({"resumedFromDate": "2026-06-16", "resumedFrom": "2026-06-16T23:05:00Z"}, date(2026, 6, 16)),
        ({"resumedFrom": "2026-06-17T02:10:00Z"}, date(2026, 6, 16)),  # 10:10 p.m. Eastern on the 16th
        ({"resumedFrom": "the 16th"}, None),
        ({}, None),
    ],
)
def test_a_resumed_game_carries_the_day_it_began(changes: dict[str, Any], resumed_from: date | None) -> None:
    assert _scheduled_game(**changes).resumed_from == resumed_from


def test_a_schedule_response_keeps_every_game_it_can_read() -> None:
    clubless = {"gamePk": 3, "status": None, "teams": {"away": None}}
    response = {"dates": [_response(_scheduled(1), "junk", clubless)["dates"][0], "junk", {"games": None}]}
    update = _slate(response)
    assert [game.id.raw for game in update.contests] == ["1"]
    assert len(update.warnings) == 1  # the game without its clubs is skipped, and says so
    assert _schedule_rows(None) == [] and _schedule_rows({"dates": "none"}) == []


def test_a_statsapi_schedule_row_without_the_newer_facts_leaves_them_unknown() -> None:
    game = _by_id(_provider()[0].refresh(NOW), "700001")
    assert game.stage is SeasonStage.REGULAR and game.provider_date == date(2026, 6, 17)
    assert (game.game_number, game.national_broadcast, game.resumed_from, game.start_tbd) == (None, None, None, False)
