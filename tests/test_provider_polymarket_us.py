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
from omni.providers.polymarket_us import PolymarketUsProvider, _parse_quote

REG = MlbTeamRegistry.from_color_file()
SOURCE = SourceRef("mlb_statsapi")
START = datetime(2026, 9, 17, 1, 40, tzinfo=timezone.utc)  # a night game: still Sep 16 where it's played
NOW = START + timedelta(hours=1)
SLUG = "aec-mlb-col-lad-2026-09-16"


def _game(raw: str = "g1") -> TeamGame:
    return TeamGame(
        id=LeagueScopedId(League.MLB, SOURCE, raw),
        league=League.MLB,
        status=GameStatus.LIVE,
        scheduled_start=START,
        away=REG.resolve(115),  # Rockies
        home=REG.resolve(119),  # Dodgers
    )


def _market(*, long_team: str = "col", start: str = "2026-09-17T01:40:00Z") -> dict[str, Any]:
    other = "lad" if long_team == "col" else "col"
    return {
        "market": {
            "slug": SLUG,
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


def test_old_games_are_forgotten() -> None:
    api = _Api()
    provider = PolymarketUsProvider(fetch=api)
    provider.win_probability(_game("old"), NOW)
    provider.win_probability(_game("new"), NOW + timedelta(hours=13))
    assert set(provider._games) == {"new"}


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
