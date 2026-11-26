"""Pregame MLB and NBA odds from Bovada's public coupon endpoint.

The endpoint needs no key and costs nothing, but it is an undocumented site service rather than a
published API, so it may change or go away without notice. Everything about its shape stays in this
module, and the rest of the app treats odds as optional: a card without them just shows less.

Its responses sit behind a CDN for up to ten minutes. That's fine for a pregame line and far too old
for a live game, which is why the in-game meter reads a different source.

The coupon lists one event per game, each naming its home and away competitors, with a moneyline
market and a handicap market (MLB's run line, NBA's point spread) whose outcomes are tagged by side. Events are held
between fetches, so callers can ask about a game every pass while the endpoint is only hit every few minutes. Each
league has a coupon of its own (`BovadaCoupon`); NBA's is read the same way as MLB's, its point spread where MLB's run
line is, and `favorite_line` reads a game's two sides as the favorite's line an NBA pregame card shows. The live NBA
coupon's shape hasn't been seen: it's read as MLB's coupon is, with the handicap market its key names
(``Main Dynamic Asian Handicap``), and a game it doesn't carry just shows no line.

An answer of ``{}`` (seen for every sport at once, when the coupon has nothing to say) is a coupon with no games: no
lines, and no error.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

from omni.core.enum import HomeAway, StrEnumMixin
from omni.core.time import DurationSeconds
from omni.domain.baseball import BettingLine, GameLines
from omni.domain.basketball import FavoriteLine
from omni.domain.contest import TeamGame
from omni.domain.teams import Team
from omni.providers.base import ProviderError

__all__ = ["BovadaCoupon", "BovadaOddsProvider", "OddsFetcher", "favorite_line"]

_COUPON = "https://www.bovada.lv/services/sports/event/coupon/events/A/description/{path}"
_QUERY = "?marketFilterId=def&preMatchOnly=true&eventsLimit=50&lang=en"
_MONEYLINE_MARKET = "Head To Head"


class BovadaCoupon(StrEnumMixin, str, Enum):
    """A league's coupon: where it's read, and its handicap market's key."""

    MLB = "baseball/mlb"
    NBA = "basketball/nba"

    @property
    def url(self) -> str:
        return _COUPON.format(path=self.value) + _QUERY

    @property
    def handicap(self) -> str:
        """The key of the market whose outcomes give each side's handicap: MLB's run line, NBA's point spread."""
        return "Main Dynamic Asian Handicap" if self is BovadaCoupon.NBA else "Main Dynamic Asian Runline"


_AWAY, _HOME = "A", "H"  # how an outcome names its side
_EVEN = "EVEN"  # how the coupon writes +100
_OPEN = "O"  # any other market or outcome status means its price isn't being offered right now

# Clubs in a series meet again the next day, so an event only counts as this game near its start.
_MAX_START_SKEW = timedelta(hours=6)

# How the provider pulls the raw coupon.
OddsFetcher = Callable[[], Any]


@dataclass(frozen=True, slots=True)
class _Event:
    """One game on the board."""

    away_name: str
    home_name: str
    start: datetime
    lines: GameLines


class BovadaOddsProvider:
    """Answers "what are the odds on this game?" from a coupon it refreshes at its own pace."""

    def __init__(
        self,
        *,
        fetch: OddsFetcher | None = None,
        refresh_every: DurationSeconds = DurationSeconds(300),
        coupon: BovadaCoupon = BovadaCoupon.MLB,
    ) -> None:
        self._fetch = fetch if fetch is not None else _default_fetch
        self._coupon = coupon
        self._refresh_every = refresh_every.as_timedelta()
        self._events: tuple[_Event, ...] = ()
        self._next_fetch: datetime | None = None

    def lines_for(self, game: TeamGame, now: datetime) -> GameLines | None:
        """The odds on ``game``, or None if it isn't on the board.

        Raises `ProviderError` when a due refresh fails. The last good board is kept, and the next
        attempt waits out the full refresh interval, so an outage is reported once per interval and
        never hammers the endpoint.
        """
        self._refresh_if_due(now)
        candidates = [
            event
            for event in self._events
            if _names_team(event.away_name, game.away)
            and _names_team(event.home_name, game.home)
            and abs(event.start - game.scheduled_start) <= _MAX_START_SKEW
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda event: abs(event.start - game.scheduled_start)).lines  # a doubleheader

    def _refresh_if_due(self, now: datetime) -> None:
        if self._next_fetch is not None and now < self._next_fetch:
            return
        self._next_fetch = now + self._refresh_every
        try:
            self._events = _parse_coupon(self._fetch(), self._coupon)
        except Exception as exc:
            raise ProviderError(f"Bovada odds fetch failed: {exc}") from exc


def _names_team(competitor: str, team: Team) -> bool:
    """Whether a coupon's competitor name is ``team``: its full name, or failing that its nickname."""
    name = _normalized(competitor)
    return name == _normalized(team.display_name) or name.endswith(_normalized(team.short_name))


def _normalized(name: str) -> str:
    return "".join(ch for ch in name.casefold() if ch.isalnum())


def _parse_coupon(raw: Any, coupon: BovadaCoupon = BovadaCoupon.MLB) -> tuple[_Event, ...]:
    """Every usable game on the coupon; one that can't be read is left off rather than failing the rest. An empty
    object is a coupon with no games."""
    if raw == {}:
        return ()
    if not isinstance(raw, list):
        raise ValueError("expected a list of coupon groups")
    events = []
    for group in raw:
        for event in group.get("events") or []:
            parsed = _parse_event(event, coupon)
            if parsed is not None:
                events.append(parsed)
    return tuple(events)


def _parse_event(event: dict[str, Any], coupon: BovadaCoupon) -> _Event | None:
    competitors = event.get("competitors") or []
    away = next((c.get("name") for c in competitors if c.get("home") is False), None)
    home = next((c.get("name") for c in competitors if c.get("home") is True), None)
    start_ms = event.get("startTime")
    if not isinstance(away, str) or not isinstance(home, str) or not isinstance(start_ms, int):
        return None
    start = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc)
    groups = event.get("displayGroups") or []
    markets = (groups[0].get("markets") or []) if groups else []
    return _Event(
        away_name=away,
        home_name=home,
        start=start,
        lines=GameLines(
            away=_side_line(markets, _AWAY, coupon.handicap), home=_side_line(markets, _HOME, coupon.handicap)
        ),
    )


def _side_line(markets: list[dict[str, Any]], side: str, handicap: str) -> BettingLine | None:
    """One side's line from a game's markets; no moneyline means no line, and a handicap is a bonus."""
    moneyline = _american(_outcome_price(markets, _MONEYLINE_MARKET, side).get("american"))
    if moneyline is None:
        return None
    runline_price = _outcome_price(markets, handicap, side)
    runline = _handicap(runline_price.get("handicap"))
    price = _american(runline_price.get("american"))
    if runline is None or price is None:
        return BettingLine(moneyline=moneyline)
    return BettingLine(moneyline=moneyline, runline=runline, runline_price=price)


def _outcome_price(markets: list[dict[str, Any]], key: str, side: str) -> dict[str, Any]:
    for market in markets:
        if market.get("descriptionKey") == key and market.get("status", _OPEN) == _OPEN:
            for outcome in market.get("outcomes") or []:
                if outcome.get("type") == side and outcome.get("status", _OPEN) == _OPEN:
                    price: dict[str, Any] = outcome.get("price") or {}
                    return price
    return {}


def _american(raw: Any) -> int | None:
    """American odds from the coupon's string form, like ``-120``, ``+110`` or ``EVEN``."""
    if raw == _EVEN:
        return 100
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _handicap(raw: Any) -> float | None:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def favorite_line(lines: GameLines) -> FavoriteLine | None:
    """The favorite's line from a game's two sides: the side with the shorter moneyline, its moneyline and the points
    its handicap gives. None without both sides' lines, without the favorite's handicap, or with no favorite to tell
    (the same moneyline both ways, and no side giving points)."""
    away, home = lines.away, lines.home
    if away is None or home is None:
        return None
    sides = sorted(
        ((HomeAway.AWAY, away), (HomeAway.HOME, home)),
        key=lambda each: (each[1].moneyline, each[1].runline if each[1].runline is not None else 0.0),
    )
    side, line = sides[0]
    spread = line.runline
    if spread is None or spread > 0 or (line.moneyline == sides[1][1].moneyline and spread == 0):
        return None
    return FavoriteLine(favorite=side, moneyline=line.moneyline, spread=spread)


def _default_fetch(coupon: BovadaCoupon = BovadaCoupon.MLB) -> Any:  # pragma: no cover - real network
    from urllib.request import Request, urlopen

    request = Request(coupon.url, headers={"User-Agent": "omni-scoreboard"})
    with urlopen(request, timeout=10) as response:
        return json.load(response)
