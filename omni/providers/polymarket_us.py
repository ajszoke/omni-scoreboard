"""Live win probability from Polymarket US's public market data.

MLB's own win probability is a model of the game state alone, so it knows nothing of how good the two
clubs are. A traded price does, which is why this is the meter's first choice. The exchange publishes
market data with no key; its public responses sit behind a 30-second cache, so asking more often than
that only re-reads the same answer. With a key, a `QuoteStream` sharpens those readings to real time.

Each game has one moneyline market, named for its matchup and its date. The market's price is for one
side of it, so which club that is gets read from the market itself rather than assumed from the name;
getting it backward would show every game's meter leaning the wrong way. A doubleheader's markets are
named some other way, so those games simply find no market here and the caller falls back.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Protocol
from urllib.error import HTTPError
from zoneinfo import ZoneInfo

from omni.core.time import DurationSeconds
from omni.domain.baseball import WinProbability
from omni.domain.contest import TeamGame
from omni.providers.base import ProviderError

__all__ = ["MarketFetcher", "MarketQuote", "PolymarketUsProvider", "QuoteStream", "parse_price"]

_BASE_URL = "https://gateway.polymarket.us"
_OPEN = "MARKET_STATE_OPEN"
_MAX_SPREAD = 0.10  # a wider market than this is too thin for its midpoint to mean much
_MAX_START_SKEW = timedelta(hours=2)  # closer than a doubleheader's two games ever start
_FORGET_AFTER = timedelta(hours=12)

# A US game's market carries its local date, which Pacific time always agrees with: no game starts
# early enough there to land on the day before, and a late one in Pacific time is still that date.
_MARKET_DATE_ZONE = ZoneInfo("America/Los_Angeles")

# How the provider reads a path off the API: the parsed response, or None if there is no such thing.
MarketFetcher = Callable[[str], Any]


@dataclass(frozen=True, slots=True)
class MarketQuote:
    """A market's best bid and ask; either side can be empty in a lopsided game."""

    bid: float | None
    ask: float | None


class QuoteStream(Protocol):
    """A live feed of quotes, consulted between REST polls."""

    def watch(self, slug: str) -> None: ...

    def quote(self, slug: str) -> MarketQuote | None: ...


@dataclass(slots=True)
class _TrackedGame:
    """What is known of one game's market: its name once confirmed, and the latest reading from it."""

    slug: str | None = None
    away_is_long: bool = True
    reading: WinProbability | None = None
    read_at: datetime | None = None


class PolymarketUsProvider:
    """Answers "how does the market see this game?", at most once per refresh interval per game."""

    def __init__(
        self,
        *,
        fetch: MarketFetcher | None = None,
        refresh_every: DurationSeconds = DurationSeconds(30),
        stream: QuoteStream | None = None,
    ) -> None:
        self._fetch = fetch if fetch is not None else _default_fetch
        self._stream = stream
        self._refresh_every = refresh_every.as_timedelta()
        self._games: dict[str, _TrackedGame] = {}

    def win_probability(self, game: TeamGame, now: datetime) -> WinProbability | None:
        """The market's current read on ``game``, or None if it has no open, liquid market.

        Raises `ProviderError` when a due refresh fails. The reading is cleared first, so a failure
        can never leave an old price looking current, and the next try waits out the full interval.
        """
        tracked = self._games.get(game.id.raw)
        if tracked is None:
            self._forget_old_games(now)
            tracked = self._games[game.id.raw] = _TrackedGame()
        if tracked.read_at is None or now - tracked.read_at >= self._refresh_every:
            self._refresh(game, tracked, now)
        return self._sharpened(tracked)

    def _refresh(self, game: TeamGame, tracked: _TrackedGame, now: datetime) -> None:
        tracked.read_at, tracked.reading = now, None
        try:
            if tracked.slug is None and not self._find_market(game, tracked):
                return
            raw = self._fetch(f"/v1/markets/{tracked.slug}/bbo")
        except Exception as exc:
            raise ProviderError(f"Polymarket US fetch failed for {game.id.raw}: {exc}") from exc
        tracked.reading = _parse_quote(raw, away_is_long=tracked.away_is_long)

    def _sharpened(self, tracked: _TrackedGame) -> WinProbability | None:
        """The latest reading, with the stream's newer price if it has one for a market REST calls open."""
        if self._stream is None or tracked.slug is None or tracked.reading is None:
            return tracked.reading
        self._stream.watch(tracked.slug)
        quote = self._stream.quote(tracked.slug)
        live = _reading_from(quote, away_is_long=tracked.away_is_long) if quote is not None else None
        return live if live is not None else tracked.reading

    def _find_market(self, game: TeamGame, tracked: _TrackedGame) -> bool:
        """Confirm this game's market and which club its price is for; False if it can't be confirmed."""
        slug = _market_slug(game)
        raw = self._fetch(f"/v1/market/slug/{slug}")
        market = (raw or {}).get("market") if isinstance(raw, dict) else None
        if not isinstance(market, dict) or not _starts_near(market, game):
            return False
        long_team = next(
            (
                (side.get("team") or {}).get("abbreviation")
                for side in market.get("marketSides") or []
                if side.get("long")
            ),
            None,
        )
        if long_team == game.away.abbreviation.lower():
            tracked.away_is_long = True
        elif long_team == game.home.abbreviation.lower():
            tracked.away_is_long = False
        else:
            return False
        tracked.slug = slug
        return True

    def _forget_old_games(self, now: datetime) -> None:
        self._games = {
            game_id: tracked
            for game_id, tracked in self._games.items()
            if tracked.read_at is None or now - tracked.read_at < _FORGET_AFTER
        }


def _market_date(game: TeamGame) -> date:
    return game.scheduled_start.astimezone(_MARKET_DATE_ZONE).date()


def _market_slug(game: TeamGame) -> str:
    away, home = game.away.abbreviation.lower(), game.home.abbreviation.lower()
    return f"aec-mlb-{away}-{home}-{_market_date(game).isoformat()}"


def _starts_near(market: dict[str, Any], game: TeamGame) -> bool:
    """Whether a market's first pitch is this game's, so one club's other game can't stand in for it."""
    raw = market.get("gameStartTime")
    try:
        start = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return False
    return start.tzinfo is not None and abs(start - game.scheduled_start) <= _MAX_START_SKEW


def _parse_quote(raw: Any, *, away_is_long: bool) -> WinProbability | None:
    """A win probability from an open market's REST quote."""
    data = (raw or {}).get("marketData") if isinstance(raw, dict) else None
    if not isinstance(data, dict) or data.get("state") != _OPEN:
        return None
    return _reading_from(
        MarketQuote(bid=parse_price(data.get("bestBid")), ask=parse_price(data.get("bestAsk"))),
        away_is_long=away_is_long,
    )


def _reading_from(quote: MarketQuote, *, away_is_long: bool) -> WinProbability | None:
    """A win probability from a quote: its midpoint, for the side the market prices."""
    bid, ask = quote.bid, quote.ask
    if bid is not None and ask is not None:
        if ask - bid > _MAX_SPREAD:
            return None
        long_price = (bid + ask) / 2
    else:  # a lopsided game can have nothing left on one side, and the side that remains still reads true
        one_side = bid if bid is not None else ask
        if one_side is None:
            return None
        long_price = one_side
    long_pct = min(max(long_price * 100.0, 0.0), 100.0)
    away, home = (long_pct, 100.0 - long_pct) if away_is_long else (100.0 - long_pct, long_pct)
    return WinProbability(home=home, away=away)


def parse_price(raw: Any) -> float | None:
    """A price from the API's ``{"value": "0.55", "currency": "USD"}`` form, or None."""
    try:
        return float(raw["value"])
    except (TypeError, KeyError, ValueError):
        return None


def _default_fetch(path: str) -> Any:  # pragma: no cover - real network
    from urllib.request import Request, urlopen

    request = Request(_BASE_URL + path, headers={"User-Agent": "omni-scoreboard"})
    try:
        with urlopen(request, timeout=10) as response:
            return json.load(response)
    except HTTPError as exc:
        if exc.code == 404:
            return None
        raise
