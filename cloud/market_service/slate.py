"""Which games are worth polling right now, and where the day's games come from.

A board that stays on keeps its heartbeat going all night and all winter, so heartbeats alone would mean
polling forever. The schedule is the second gate: with no game in a window, a run ends without calling
any market at all.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from omni.core.colors import RGBColor
from omni.core.enum import GameStatus, League
from omni.core.ids import LeagueScopedId, SourceRef
from omni.core.time import local_date
from omni.domain.base import LogoAsset
from omni.domain.contest import TeamGame
from omni.domain.teams import Team
from omni.providers.mlb_statsapi import MlbStatsApiProvider
from omni.providers.polymarket_us import MarketFetcher

__all__ = ["PollPlan", "SCHEDULE_ZONE", "Slate", "combined_slate", "mlb_slate", "plan_poll", "polymarket_slate"]

# Pacific time, so the "day" doesn't turn over while a West Coast night game is still on.
SCHEDULE_ZONE = ZoneInfo("America/Los_Angeles")

_MARKET_LEAD = timedelta(minutes=10)  # so a reading is waiting at first pitch
_MARKET_SPAN = timedelta(hours=8)  # a backstop for a game whose status never says it ended
_LINES_LEAD = timedelta(hours=12)
_LATE_GAME_SPAN = timedelta(hours=6)  # how far past midnight last night's slate can still matter
_DONE = frozenset({GameStatus.FINAL, GameStatus.POSTPONED, GameStatus.CANCELED, GameStatus.SUSPENDED})

_LISTING_PAGE = 50
_LISTING_SOURCE = SourceRef("polymarket_us", "https://gateway.polymarket.us")
_SLUG_PARTS = 7  # aec-{league}-{away}-{home}-{yyyy}-{mm}-{dd}
_NEUTRAL = RGBColor(0, 0, 0)

# The day's games at a given moment.
Slate = Callable[[datetime], Sequence[TeamGame]]


@dataclass(frozen=True, slots=True)
class PollPlan:
    """What a run has reason to read."""

    market_games: tuple[TeamGame, ...]
    lines_games: tuple[TeamGame, ...]

    @property
    def is_idle(self) -> bool:
        return not self.market_games and not self.lines_games


def plan_poll(games: Sequence[TeamGame], now: datetime) -> PollPlan:
    """Sort a slate into games whose market to sample and games whose pregame lines to read."""
    open_games = [game for game in games if game.status not in _DONE]
    return PollPlan(
        market_games=tuple(
            game
            for game in open_games
            if game.scheduled_start - _MARKET_LEAD <= now <= game.scheduled_start + _MARKET_SPAN
        ),
        lines_games=tuple(
            game
            for game in open_games
            if game.status is not GameStatus.LIVE and game.scheduled_start - _LINES_LEAD <= now < game.scheduled_start
        ),
    )


def mlb_slate(provider: MlbStatsApiProvider) -> Slate:
    """MLB's games from its schedule, including last night's while one of them could still be going."""

    def slate(now: datetime) -> Sequence[TeamGame]:
        moments = [now]
        earlier = now - _LATE_GAME_SPAN
        if local_date(earlier, SCHEDULE_ZONE) != local_date(now, SCHEDULE_ZONE):
            moments.append(earlier)
        games: dict[str, TeamGame] = {}
        for moment in moments:
            for contest in provider.refresh(moment).contests:
                if isinstance(contest, TeamGame):
                    games[contest.id.raw] = contest
        return tuple(games.values())

    return slate


def polymarket_slate(league: League, fetch: MarketFetcher) -> Slate:
    """A league's games read off the exchange's own listing.

    This serves any league without a schedule provider yet. Its teams are bare names with no colors or
    logos, which is all a poller needs. Which side a price is for still gets confirmed from the market.
    """

    def slate(now: datetime) -> Sequence[TeamGame]:
        raw = fetch(f"/v2/leagues/{league.value}/events?limit={_LISTING_PAGE}")
        events = raw.get("events") if isinstance(raw, dict) else None
        games = []
        for event in events or []:
            for market in event.get("markets") or []:
                game = _listed_game(league, event, market)
                if game is not None:
                    games.append(game)
        return tuple(games)

    return slate


def _listed_game(league: League, event: dict[str, Any], market: dict[str, Any]) -> TeamGame | None:
    """A game from one listed market, if that market is a game's moneyline with a usable start time."""
    slug = market.get("slug")
    parts = slug.split("-") if isinstance(slug, str) else []
    if len(parts) != _SLUG_PARTS or parts[:2] != ["aec", league.value]:
        return None
    try:
        start = datetime.fromisoformat(str(market.get("gameStartTime")).replace("Z", "+00:00"))
    except ValueError:
        return None
    if start.tzinfo is None:
        return None
    status = GameStatus.FINAL if event.get("ended") or event.get("closed") else GameStatus.UNKNOWN
    if status is GameStatus.UNKNOWN and event.get("live"):
        status = GameStatus.LIVE
    return TeamGame(
        id=LeagueScopedId(league, _LISTING_SOURCE, str(slug).removeprefix("aec-")),
        league=league,
        status=status,
        scheduled_start=start,
        away=_bare_team(league, parts[2]),
        home=_bare_team(league, parts[3]),
    )


def _bare_team(league: League, abbreviation: str) -> Team:
    name = abbreviation.upper()
    return Team(
        id=LeagueScopedId(league, _LISTING_SOURCE, abbreviation),
        league=league,
        display_name=name,
        short_name=name,
        abbreviation=name,
        primary_color=_NEUTRAL,
        secondary_color=_NEUTRAL,
        logo=LogoAsset(key=abbreviation, path=""),
    )


def combined_slate(slates: Sequence[Slate]) -> Slate:
    """Several leagues' slates read as one."""

    def slate(now: datetime) -> Sequence[TeamGame]:
        return tuple(game for one in slates for game in one(now))

    return slate
