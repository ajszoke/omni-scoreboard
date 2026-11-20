"""Polymarket US's win probability: market lookup, side mapping, pacing, and failure handling."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from omni.core.enum import GameStatus, League
from omni.core.ids import LeagueScopedId, SourceRef
from omni.core.time import DurationSeconds
from omni.domain.baseball import WinProbability
from omni.domain.contest import TeamGame
from omni.providers.base import ProviderError
from omni.providers.mlb_teams import MlbTeamRegistry
from omni.providers.nfl_teams import NflTeamRegistry
from omni.providers.nba_teams import NbaTeamRegistry
from omni.providers.polymarket_us import (
    MARKET_CODES,
    MarketLink,
    PolymarketUsProvider,
    _open_quote,
    _reading_from,
    market_code,
)

REG = MlbTeamRegistry.from_color_file()
SOURCE = SourceRef("mlb_statsapi")
START = datetime(2026, 9, 17, 1, 40, tzinfo=timezone.utc)  # a night game: still Sep 16 where it's played
NOW = START + timedelta(hours=1)
SLUG = "aec-mlb-col-lad-2026-09-16"


def _parse_quote(raw: Any, *, away_is_long: bool) -> WinProbability | None:
    quote = _open_quote(raw)
    return _reading_from(quote, away_is_long=away_is_long) if quote is not None else None


def _game(raw: str = "g1", *, start: datetime = START) -> TeamGame:
    return TeamGame(
        id=LeagueScopedId(League.MLB, SOURCE, raw),
        league=League.MLB,
        status=GameStatus.LIVE,
        scheduled_start=start,
        away=REG.resolve(115),  # Rockies
        home=REG.resolve(119),  # Dodgers
    )


def _market(
    *, long_team: str = "col", start: str = "2026-09-17T01:40:00Z", slug: str = SLUG, short_team: str | None = None
) -> dict[str, Any]:
    other = short_team if short_team is not None else ("lad" if long_team == "col" else "col")
    return {
        "market": {
            "slug": slug,
            "gameStartTime": start,
            "marketSides": [
                {"long": True, "team": {"abbreviation": long_team}},
                {"long": False, "team": {"abbreviation": other}},
            ],
        }
    }


def _quote(bid: str | None = "0.30", ask: str | None = "0.32", state: str = "MARKET_STATE_OPEN") -> dict[str, Any]:
    data: dict[str, Any] = {"state": state}
    if bid is not None:
        data["bestBid"] = {"value": bid, "currency": "USD"}
    if ask is not None:
        data["bestAsk"] = {"value": ask, "currency": "USD"}
    return {"marketData": data}


class _Api:
    """A fake API answering by path, and counting what it was asked."""

    def __init__(self, market: Any = None, quote: Any = None) -> None:
        self.market = _market() if market is None else market
        self.quote = _quote() if quote is None else quote
        self.calls: list[str] = []
        self.down = False

    def __call__(self, path: str) -> Any:
        self.calls.append(path)
        if self.down:
            raise OSError("connection refused")
        return self.market if path.startswith("/v1/market/slug/") else self.quote


def test_the_midpoint_is_the_win_probability_for_the_side_the_market_prices() -> None:
    reading = PolymarketUsProvider(fetch=_Api()).win_probability(_game(), NOW)
    assert reading is not None
    assert (reading.away, reading.home) == (pytest.approx(31.0), pytest.approx(69.0))


def test_the_market_is_named_for_the_matchup_and_the_date_where_the_game_is_played() -> None:
    api = _Api()
    PolymarketUsProvider(fetch=api).win_probability(_game(), NOW)
    assert api.calls == [f"/v1/market/slug/{SLUG}", f"/v1/markets/{SLUG}/bbo"]


def test_which_club_the_price_is_for_comes_from_the_market_not_the_name() -> None:
    reading = PolymarketUsProvider(fetch=_Api(market=_market(long_team="lad"))).win_probability(_game(), NOW)
    assert reading is not None
    assert (reading.home, reading.away) == (pytest.approx(31.0), pytest.approx(69.0))


def test_a_market_for_neither_club_is_refused_rather_than_guessed_at() -> None:
    assert PolymarketUsProvider(fetch=_Api(market=_market(long_team="nyy"))).win_probability(_game(), NOW) is None


@pytest.mark.parametrize("market", ["not-a-dict", {"market": "not-a-dict"}, {}])
def test_a_missing_or_malformed_market_is_no_reading(market: Any) -> None:
    api = _Api(market=market)
    assert PolymarketUsProvider(fetch=api).win_probability(_game(), NOW) is None
    assert len(api.calls) == 1  # never went on to ask for a quote


@pytest.mark.parametrize("start", ["2026-09-17T05:00:00Z", "not-a-time", "2026-09-17T01:40:00"])
def test_a_market_that_doesnt_start_with_this_game_isnt_this_game(start: str) -> None:
    # A doubleheader's other game, or an unreadable or zoneless time, can't stand in for this one.
    assert PolymarketUsProvider(fetch=_Api(market=_market(start=start))).win_probability(_game(), NOW) is None


def test_it_asks_at_most_once_per_interval_and_only_finds_the_market_once() -> None:
    api = _Api()
    provider = PolymarketUsProvider(fetch=api, refresh_every=DurationSeconds(30))
    first = provider.win_probability(_game(), NOW)
    assert provider.win_probability(_game(), NOW + timedelta(seconds=29)) == first
    assert len(api.calls) == 2
    provider.win_probability(_game(), NOW + timedelta(seconds=30))
    assert api.calls[2:] == [f"/v1/markets/{SLUG}/bbo"]


def test_a_failed_refresh_raises_clears_the_reading_and_waits_out_the_interval() -> None:
    api = _Api()
    provider = PolymarketUsProvider(fetch=api)
    assert provider.win_probability(_game(), NOW) is not None
    api.down = True
    with pytest.raises(ProviderError, match="Polymarket US fetch failed"):
        provider.win_probability(_game(), NOW + timedelta(seconds=30))
    asked = len(api.calls)
    assert provider.win_probability(_game(), NOW + timedelta(seconds=31)) is None  # never the old price
    assert len(api.calls) == asked


class _Exchange:
    """A fake API with several markets, each answering by its slug, and counting what it was asked."""

    def __init__(self, markets: dict[str, tuple[dict[str, Any], dict[str, Any]]]) -> None:
        self.markets = markets  # slug -> (its market, its quote)
        self.calls: list[str] = []

    def __call__(self, path: str) -> Any:
        self.calls.append(path)
        slug = path.removeprefix("/v1/market/slug/").removeprefix("/v1/markets/").removesuffix("/bbo")
        market, quote = self.markets.get(slug, (None, None))
        return market if path.startswith("/v1/market/slug/") else quote


def test_an_mlb_and_an_nfl_game_with_the_same_raw_id_each_read_their_own_market() -> None:
    clubs = NflTeamRegistry.default()
    nfl = TeamGame(
        id=LeagueScopedId(League.NFL, SourceRef("nflmeta"), "g1"),  # the MLB game's raw id too
        league=League.NFL,
        status=GameStatus.LIVE,
        scheduled_start=START,
        away=clubs.resolve("PIT"),
        home=clubs.resolve("CIN"),
    )
    nfl_slug = "aec-nfl-pit-cin-2026-09-16"
    nfl_market = _market(long_team="pit", short_team="cin", slug=nfl_slug)
    api = _Exchange({SLUG: (_market(), _quote()), nfl_slug: (nfl_market, _quote(bid="0.80", ask="0.82"))})
    provider = PolymarketUsProvider(fetch=api)
    baseball = provider.sample(_game("g1"), NOW)
    football = provider.sample(nfl, NOW)
    assert baseball is not None and football is not None
    assert (baseball.slug, football.slug) == (SLUG, nfl_slug)
    assert baseball.probability is not None and baseball.probability.away == pytest.approx(31.0)
    assert football.probability is not None and football.probability.away == pytest.approx(81.0)
    assert set(provider._games) == {_game("g1").id, nfl.id}  # one cache entry each
    assert [tracked.link for tracked in provider._games.values()] == [
        MarketLink(contest=_game("g1").id, slug=SLUG, away_is_long=True),
        MarketLink(contest=nfl.id, slug=nfl_slug, away_is_long=True),
    ]


def test_a_doubleheader_s_two_games_never_share_a_market() -> None:
    # Both games are COL at LAD on Sep 16, so both look for the same market name. The market is the night
    # game's: the day game, five and a half hours earlier, finds it and refuses it, and keeps no link.
    day = _game("g1", start=START - timedelta(hours=5, minutes=30))
    night = _game("g2")
    api = _Api()
    provider = PolymarketUsProvider(fetch=api)
    assert provider.sample(day, NOW) is None
    assert provider.sample(night, NOW) is not None
    assert provider.sample(day, NOW + timedelta(seconds=30)) is None  # asked again, still refused
    assert api.calls.count(f"/v1/market/slug/{SLUG}") == 3  # the night game's link never stood in for the day's
    assert provider._games[day.id].link is None
    assert provider._games[night.id].link == MarketLink(contest=night.id, slug=SLUG, away_is_long=True)


def test_old_games_are_forgotten() -> None:
    api = _Api()
    provider = PolymarketUsProvider(fetch=api)
    provider.win_probability(_game("old"), NOW)
    provider.win_probability(_game("new"), NOW + timedelta(hours=13))
    assert set(provider._games) == {_game("new").id}


@pytest.mark.parametrize(
    "quote",
    [
        _quote(state="MARKET_STATE_SUSPENDED"),
        _quote(state="MARKET_STATE_HALTED"),
        _quote(bid=None, ask=None),
        _quote(bid="0.20", ask="0.40"),  # too wide for a midpoint to mean much
        {"marketData": "not-a-dict"},
        "not-a-dict",
        None,
    ],
)
def test_a_quote_that_cant_be_trusted_is_no_reading(quote: Any) -> None:
    assert _parse_quote(quote, away_is_long=True) is None


@pytest.mark.parametrize("bid, ask, away", [("0.98", None, 98.0), (None, "0.02", 2.0)])
def test_a_lopsided_market_with_one_side_left_still_reads(bid: str | None, ask: str | None, away: float) -> None:
    reading = _parse_quote(_quote(bid=bid, ask=ask), away_is_long=True)
    assert reading is not None and reading.away == pytest.approx(away)


def test_a_price_is_unreadable_rather_than_fatal() -> None:
    assert (
        _parse_quote({"marketData": {"state": "MARKET_STATE_OPEN", "bestBid": {"value": "x"}}}, away_is_long=True)
        is None
    )


def test_prices_outside_the_range_are_clamped_to_a_valid_probability() -> None:
    assert _parse_quote(_quote(bid="1.02", ask="1.04"), away_is_long=True) == WinProbability(home=0.0, away=100.0)


_NBA = NbaTeamRegistry.default()


def _nba(away: str, home: str) -> TeamGame:
    return TeamGame(
        id=LeagueScopedId(League.NBA, SourceRef("balldontlie"), "21718141"),
        league=League.NBA,
        status=GameStatus.LIVE,
        scheduled_start=START,
        away=_NBA.resolve(away),
        home=_NBA.resolve(home),
    )


@pytest.mark.parametrize(
    ("away", "home", "slug", "long_team"),
    [
        ("GSW", "NOP", "aec-nba-gs-no-2026-09-16", "gs"),  # two clubs the exchange names its own way
        ("NYK", "PHX", "aec-nba-ny-pho-2026-09-16", "ny"),
        ("SAS", "UTA", "aec-nba-sa-uta-2026-09-16", "sa"),
        ("UTA", "LAL", "aec-nba-uta-lal-2026-09-16", "uta"),  # every other club by its abbreviation
    ],
)
def test_an_nba_market_names_each_club_by_the_exchange_s_own_code_and_its_sides_are_read_by_it(
    away: str, home: str, slug: str, long_team: str
) -> None:
    short = slug.split("-")[3]
    api = _Exchange({slug: (_market(long_team=long_team, short_team=short, slug=slug), _quote())})
    sample = PolymarketUsProvider(fetch=api).sample(_nba(away, home), NOW)
    assert sample is not None and sample.slug == slug
    assert sample.probability is not None and sample.probability.away == pytest.approx(31.0)


def test_the_codes_are_the_league_s_own_and_mlb_s_and_nfl_s_are_their_abbreviations() -> None:
    assert [market_code(_NBA.resolve(club)) for club in ("GSW", "NOP", "NYK", "PHX", "SAS", "CHA")] == [
        "gs",
        "no",
        "ny",
        "pho",
        "sa",
        "cha",
    ]
    assert market_code(REG.resolve(115)) == "col" and market_code(NflTeamRegistry.default().resolve("PIT")) == "pit"
    with pytest.raises(ValueError, match="isn't a nba club"):
        MARKET_CODES[League.NBA].code(REG.resolve(115))
