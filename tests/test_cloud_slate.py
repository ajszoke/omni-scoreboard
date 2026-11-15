"""Which games get polled, and where the day's games come from."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

import pytest

from cloud.market_service.slate import combined_slate, mlb_slate, plan_poll, polymarket_slate
from omni.core.enum import GameStatus, League
from omni.core.ids import LeagueScopedId, SourceRef
from omni.domain.contest import GolfTournament, TeamGame
from omni.providers.base import ProviderUpdate
from omni.providers.mlb_statsapi import MlbStatsApiProvider
from omni.providers.mlb_teams import MlbTeamRegistry
from omni.providers.polymarket_us import PolymarketUsProvider

REG = MlbTeamRegistry.from_color_file()
SOURCE = SourceRef("mlb_statsapi")
START = datetime(2026, 9, 17, 1, 40, tzinfo=timezone.utc)


def _game(status: GameStatus = GameStatus.SCHEDULED, start: datetime = START, raw: str = "g1") -> TeamGame:
    return TeamGame(
        id=LeagueScopedId(League.MLB, SOURCE, raw),
        league=League.MLB,
        status=status,
        scheduled_start=start,
        away=REG.resolve(115),
        home=REG.resolve(119),
    )


# --- the window ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "before_start, market, lines",
    [
        (timedelta(hours=13), False, False),  # too early for anything
        (timedelta(hours=12), False, True),  # lines open
        (timedelta(minutes=11), False, True),
        (timedelta(minutes=10), True, True),  # market sampling starts ahead of first pitch
        (timedelta(0), True, False),  # lines stop at first pitch
        (timedelta(hours=-8), True, False),  # the backstop's last moment
        (timedelta(hours=-8, seconds=-1), False, False),
    ],
)
def test_windows_around_first_pitch(before_start: timedelta, market: bool, lines: bool) -> None:
    plan = plan_poll([_game()], START - before_start)
    assert bool(plan.market_games) is market
    assert bool(plan.lines_games) is lines


@pytest.mark.parametrize("status", [GameStatus.FINAL, GameStatus.POSTPONED, GameStatus.CANCELED, GameStatus.SUSPENDED])
def test_a_game_that_wont_be_played_now_is_never_polled(status: GameStatus) -> None:
    assert plan_poll([_game(status)], START + timedelta(hours=1)).is_idle
    assert plan_poll([_game(status)], START - timedelta(hours=1)).is_idle


def test_a_game_already_live_early_reads_no_pregame_lines() -> None:
    plan = plan_poll([_game(GameStatus.LIVE)], START - timedelta(minutes=5))
    assert plan.market_games and not plan.lines_games


def test_a_rain_delayed_game_keeps_being_sampled() -> None:
    assert plan_poll([_game(GameStatus.DELAYED)], START + timedelta(hours=3)).market_games


def test_an_empty_slate_is_idle() -> None:
    assert plan_poll([], START).is_idle


# --- MLB's slate ---------------------------------------------------------------------------------


def _row(game_id: int, *, status: str = "Scheduled") -> dict[str, Any]:
    return {
        "game_id": game_id,
        "away_id": 115,
        "home_id": 119,
        "status": status,
        "game_datetime": "2026-09-17T01:40:00Z",
    }


def _mlb_provider(by_date: dict[date, list[dict[str, Any]]], asked: list[date]) -> MlbStatsApiProvider:
    from cloud.market_service.slate import SCHEDULE_ZONE

    def fetch(game_date: date, _sports: str) -> list[dict[str, Any]]:
        asked.append(game_date)
        return by_date.get(game_date, [])

    return MlbStatsApiProvider(REG, fetch, schedule_timezone=SCHEDULE_ZONE)


def test_midday_reads_only_todays_slate() -> None:
    asked: list[date] = []
    slate = mlb_slate(_mlb_provider({date(2026, 9, 16): [_row(1)]}, asked))
    games = slate(datetime(2026, 9, 16, 20, 0, tzinfo=timezone.utc))  # 1 p.m. Pacific
    assert [game.id.raw for game in games] == ["1"]
    assert asked == [date(2026, 9, 16)]


def test_just_after_midnight_a_late_west_coast_game_is_still_on_the_slate() -> None:
    asked: list[date] = []
    slate = mlb_slate(_mlb_provider({date(2026, 9, 16): [_row(1)], date(2026, 9, 17): [_row(2)]}, asked))
    games = slate(datetime(2026, 9, 17, 8, 0, tzinfo=timezone.utc))  # 1 a.m. Pacific
    assert {game.id.raw for game in games} == {"1", "2"}
    assert set(asked) == {date(2026, 9, 16), date(2026, 9, 17)}


def test_a_game_seen_on_both_days_counts_once() -> None:
    both = {date(2026, 9, 16): [_row(1)], date(2026, 9, 17): [_row(1, status="In Progress")]}
    games = mlb_slate(_mlb_provider(both, []))(datetime(2026, 9, 17, 8, 0, tzinfo=timezone.utc))
    assert len(games) == 1


def test_a_contest_that_isnt_a_team_game_is_left_out() -> None:
    golf = GolfTournament(
        id=LeagueScopedId(League.PGA, SOURCE, "t1"),
        league=League.PGA,
        status=GameStatus.LIVE,
        scheduled_start=START,
        tournament_name="Open",
    )

    class _Provider(MlbStatsApiProvider):
        def refresh(self, now: datetime) -> ProviderUpdate:
            return ProviderUpdate(source=SOURCE, observed_at=now, contests=(golf, _game()))

    games = mlb_slate(_Provider(REG, lambda _d, _s: []))(START)
    assert [game.id.raw for game in games] == ["g1"]


# --- the exchange's listing ----------------------------------------------------------------------


def _market(slug: str = "aec-nba-pho-gs-2026-11-02", start: Any = "2026-11-03T03:00:00Z") -> dict[str, Any]:
    return {"slug": slug, "gameStartTime": start}


def _event(*markets: dict[str, Any], **flags: Any) -> dict[str, Any]:
    return {"markets": list(markets), **flags}


def _listing(*events: dict[str, Any]) -> Any:
    def fetch(path: str) -> Any:
        assert path == "/v2/leagues/nba/events?limit=50"
        return {"events": list(events)}

    return fetch


AT = datetime(2026, 11, 3, 3, 30, tzinfo=timezone.utc)


def test_a_listed_game_gets_its_teams_and_start_from_the_market() -> None:
    games = polymarket_slate(League.NBA, _listing(_event(_market(), live=True)))(AT)
    assert len(games) == 1
    game = games[0]
    assert (game.away.abbreviation, game.home.abbreviation) == ("PHO", "GS")
    assert game.league is League.NBA and game.status is GameStatus.LIVE
    assert game.scheduled_start == datetime(2026, 11, 3, 3, 0, tzinfo=timezone.utc)
    assert str(game.id) == "nba:polymarket_us:nba-pho-gs-2026-11-02"


@pytest.mark.parametrize(
    "flags, status",
    [
        ({"live": True}, GameStatus.LIVE),
        ({}, GameStatus.UNKNOWN),
        ({"ended": True, "live": True}, GameStatus.FINAL),
        ({"closed": True}, GameStatus.FINAL),
    ],
)
def test_a_listed_games_status_comes_from_its_event(flags: dict[str, Any], status: GameStatus) -> None:
    assert polymarket_slate(League.NBA, _listing(_event(_market(), **flags)))(AT)[0].status is status


@pytest.mark.parametrize(
    "market",
    [
        _market(slug="tec-nba-champion-2027"),
        _market(slug="aec-nfl-den-kc-2026-11-02"),  # another league's
        _market(slug="aec-nba-pho-gs-2026-11-02-spread"),
        {"gameStartTime": "2026-11-03T03:00:00Z"},
        _market(start=None),
        _market(start="not a time"),
        _market(start="2026-11-03T03:00:00"),  # no zone, so no way to place it
    ],
)
def test_a_listed_market_that_isnt_a_usable_game_is_skipped(market: dict[str, Any]) -> None:
    assert polymarket_slate(League.NBA, _listing(_event(market)))(AT) == ()


@pytest.mark.parametrize("raw", [None, "nope", {}, {"events": None}, {"events": [{"markets": None}]}])
def test_a_listing_with_nothing_usable_is_an_empty_slate(raw: Any) -> None:
    assert polymarket_slate(League.NBA, lambda _path: raw)(AT) == ()


def test_a_listed_game_finds_its_market_and_side_the_same_way_as_any_other() -> None:
    game = polymarket_slate(League.NBA, _listing(_event(_market(), live=True)))(AT)[0]
    market = {
        "market": {
            "gameStartTime": "2026-11-03T03:00:00Z",
            "marketSides": [{"long": True, "team": {"abbreviation": "gs"}}],
        }
    }
    quote = {"marketData": {"state": "MARKET_STATE_OPEN", "bestBid": {"value": "0.60"}, "bestAsk": {"value": "0.62"}}}
    asked: list[str] = []

    def fetch(path: str) -> Any:
        asked.append(path)
        return market if "/slug/" in path else quote

    reading = PolymarketUsProvider(fetch=fetch).win_probability(game, AT)
    assert asked[0] == "/v1/market/slug/aec-nba-pho-gs-2026-11-02"
    assert reading is not None and reading.home == pytest.approx(61.0)  # the price is for the home side here


def test_slates_combine() -> None:
    one, two = _game(raw="a"), _game(raw="b")
    games = combined_slate([lambda _now: (one,), lambda _now: (two,)])(START)
    assert [game.id.raw for game in games] == ["a", "b"]
