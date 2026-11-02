"""Pregame MLB odds from Bovada's public coupon endpoint.

The endpoint needs no key and costs nothing, but it is an undocumented site service rather than a
published API, so it may change or go away without notice. Everything about its shape stays in this
module, and the rest of the app treats odds as optional: a card without them just shows less.

Its responses sit behind a CDN for up to ten minutes. That's fine for a pregame line and far too old
for a live game, which is why the in-game meter reads a different source.

The coupon lists one event per game, each naming its home and away competitors, with a moneyline
market and a run line market whose outcomes are tagged by side. Events are held between fetches, so
callers can ask about a game every pass while the endpoint is only hit every few minutes.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from omni.core.time import DurationSeconds
from omni.domain.baseball import BettingLine, GameLines
from omni.domain.contest import TeamGame
from omni.domain.teams import Team
from omni.providers.base import ProviderError

__all__ = ["BovadaOddsProvider", "OddsFetcher"]

_URL = (
    "https://www.bovada.lv/services/sports/event/coupon/events/A/description/baseball/mlb"
    "?marketFilterId=def&preMatchOnly=true&eventsLimit=50&lang=en"
)
_MONEYLINE_MARKET = "Head To Head"
_RUNLINE_MARKET = "Main Dynamic Asian Runline"
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
    ) -> None:
        self._fetch = fetch if fetch is not None else _default_fetch
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
            self._events = _parse_coupon(self._fetch())
        except Exception as exc:
            raise ProviderError(f"Bovada odds fetch failed: {exc}") from exc


def _names_team(competitor: str, team: Team) -> bool:
    """Whether a coupon's competitor name is ``team``: its full name, or failing that its nickname."""
    name = _normalized(competitor)
    return name == _normalized(team.display_name) or name.endswith(_normalized(team.short_name))


def _normalized(name: str) -> str:
    return "".join(ch for ch in name.casefold() if ch.isalnum())


def _parse_coupon(raw: Any) -> tuple[_Event, ...]:
    """Every usable game on the coupon; one that can't be read is left off rather than failing the rest."""
    if not isinstance(raw, list):
        raise ValueError("expected a list of coupon groups")
    events = []
    for group in raw:
        for event in group.get("events") or []:
            parsed = _parse_event(event)
            if parsed is not None:
                events.append(parsed)
    return tuple(events)


def _parse_event(event: dict[str, Any]) -> _Event | None:
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
        lines=GameLines(away=_side_line(markets, _AWAY), home=_side_line(markets, _HOME)),
    )


def _side_line(markets: list[dict[str, Any]], side: str) -> BettingLine | None:
    """One side's line from a game's markets; no moneyline means no line, and a run line is a bonus."""
    moneyline = _american(_outcome_price(markets, _MONEYLINE_MARKET, side).get("american"))
    if moneyline is None:
        return None
    runline_price = _outcome_price(markets, _RUNLINE_MARKET, side)
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


def _default_fetch() -> Any:  # pragma: no cover - real network
    from urllib.request import Request, urlopen

    request = Request(_URL, headers={"User-Agent": "omni-scoreboard"})
    with urlopen(request, timeout=10) as response:
        return json.load(response)
