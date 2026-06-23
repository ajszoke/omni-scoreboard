"""MLB provider backed by the MLB StatsAPI `schedule` endpoint.

Two calls, both confined to this module's raw dict shape: `refresh(now)` is one
cheap `statsapi.schedule(...)` call parsed into typed `TeamGame` contests
(matchup + status + start + venue); `fetch_game_state(game_pk)` pulls one game's
richer per-game feed into a typed `BaseballGameState` (score, inning, count,
bases). Everything either returns is typed.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

from omni.core.enum import GameStatus, League
from omni.core.ids import LeagueScopedId, SourceRef
from omni.core.time import local_date
from omni.domain.baseball import BaseballBaseState, BaseballCount, BaseballGameState, HalfInning
from omni.domain.contest import TeamGame
from omni.providers.base import ProviderError, ProviderUpdate
from omni.providers.mlb_teams import MlbTeamRegistry

__all__ = ["MlbStatsApiProvider", "ScheduleFetcher", "GameFetcher", "map_game_status"]

# (game_date, sport_ids) -> the list of flat schedule rows.
ScheduleFetcher = Callable[[date, str], list[dict[str, Any]]]
# game_pk -> the raw nested game feed (statsapi.get("game", ...)).
GameFetcher = Callable[[Any], dict[str, Any]]

# StatsAPI `inningState` values that mean the bottom half is current/next.
_BOTTOM_INNING_STATES = frozenset({"Bottom", "End"})

_SOURCE = SourceRef("mlb_statsapi", "https://statsapi.mlb.com")
# MLB's league-office zone; a sensible default "baseball day" until a device sets its own.
_DEFAULT_SCHEDULE_TZ = ZoneInfo("America/New_York")

# StatsAPI `detailedState` strings -> our typed status. Variants that carry a
# suffix ("Delayed: Rain", "Suspended: Rain") are matched by prefix below.
_STATUS_MAP: dict[str, GameStatus] = {
    "Scheduled": GameStatus.SCHEDULED,
    "Pre-Game": GameStatus.PREGAME,
    "Warmup": GameStatus.PREGAME,
    "In Progress": GameStatus.LIVE,
    "Manager Challenge": GameStatus.LIVE,
    "Final": GameStatus.FINAL,
    "Game Over": GameStatus.FINAL,
    "Completed Early": GameStatus.FINAL,
    "Postponed": GameStatus.POSTPONED,
    "Cancelled": GameStatus.CANCELED,
    "Canceled": GameStatus.CANCELED,
}
_STATUS_PREFIXES: tuple[tuple[str, GameStatus], ...] = (
    ("Delayed", GameStatus.DELAYED),
    ("Suspended", GameStatus.SUSPENDED),
    ("Completed Early", GameStatus.FINAL),
)


def map_game_status(detailed_state: str) -> GameStatus:
    """Map a StatsAPI `detailedState` to a typed `GameStatus` (UNKNOWN if unrecognized)."""
    if detailed_state in _STATUS_MAP:
        return _STATUS_MAP[detailed_state]
    for prefix, status in _STATUS_PREFIXES:
        if detailed_state.startswith(prefix):
            return status
    return GameStatus.UNKNOWN


class _SkipGame(Exception):
    """A single schedule row could not be parsed; recorded as a warning and skipped."""


class MlbStatsApiProvider:
    """Fetches today's MLB schedule and returns typed `TeamGame` contests."""

    league = League.MLB

    def __init__(
        self,
        registry: MlbTeamRegistry,
        fetch_schedule: ScheduleFetcher | None = None,
        *,
        fetch_game: GameFetcher | None = None,
        source: SourceRef | None = None,
        sport_ids: str = "1,51",  # 1 = MLB, 51 = WBC (mirrors upstream)
        schedule_timezone: ZoneInfo | None = None,
    ) -> None:
        self._registry = registry
        self._fetch = fetch_schedule if fetch_schedule is not None else _default_fetch_schedule
        self._fetch_game = fetch_game if fetch_game is not None else _default_fetch_game
        self.source = source if source is not None else _SOURCE
        self._sport_ids = sport_ids
        self._tz = schedule_timezone if schedule_timezone is not None else _DEFAULT_SCHEDULE_TZ

    def refresh(self, now: datetime) -> ProviderUpdate:
        try:
            # Localize to the configured zone, not UTC: in US evenings `now.date()`
            # (UTC) is already tomorrow while tonight's games are still on.
            rows = self._fetch(local_date(now, self._tz), self._sport_ids)
        except Exception as exc:  # network/library failure -> whole-update error
            raise ProviderError(f"MLB schedule fetch failed: {exc}") from exc

        contests: list[TeamGame] = []
        warnings: list[str] = []
        for row in rows:
            try:
                contests.append(self._parse_game(row))
            except _SkipGame as skip:
                warnings.append(str(skip))
        return ProviderUpdate(
            source=self.source,
            observed_at=now,
            contests=tuple(contests),
            warnings=tuple(warnings),
        )

    def fetch_game_state(self, game_pk: int | str) -> BaseballGameState:
        """Fetch one game's live feed and parse it into typed `BaseballGameState`.

        Separate from `refresh` (which is one cheap schedule call): the caller
        decides which live games are worth a per-game request. Raises
        `ProviderError` if the fetch fails or the feed has no live state.
        """
        try:
            raw = self._fetch_game(game_pk)
        except Exception as exc:
            raise ProviderError(f"MLB game fetch failed for {game_pk}: {exc}") from exc
        return _parse_game_state(raw)

    def _parse_game(self, row: dict[str, Any]) -> TeamGame:
        try:
            game_pk = row["game_id"]
            away_id = int(row["away_id"])
            home_id = int(row["home_id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise _SkipGame(f"schedule row missing/invalid team ids: {exc}") from exc

        try:
            away = self._registry.resolve(away_id, full_name=row.get("away_name"))
            home = self._registry.resolve(home_id, full_name=row.get("home_name"))
        except KeyError as exc:
            raise _SkipGame(f"unknown team id {exc} in game {game_pk}") from exc

        return TeamGame(
            id=LeagueScopedId(League.MLB, self.source, str(game_pk)),
            league=League.MLB,
            status=map_game_status(str(row.get("status", ""))),
            scheduled_start=_parse_start(row.get("game_datetime"), game_pk),
            away=away,
            home=home,
            venue_name=row.get("venue_name") or None,
        )


def _parse_start(raw: Any, game_pk: Any) -> datetime:
    if not isinstance(raw, str) or not raw:
        raise _SkipGame(f"game {game_pk} has no start time")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise _SkipGame(f"game {game_pk} has unparseable start time {raw!r}") from exc
    # Treat a naive timestamp as UTC so downstream timing math always has a tz.
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _half_from_inning_state(inning_state: str) -> HalfInning:
    # "Middle"/"End" are the breaks after the top/bottom; collapse to our two halves.
    return HalfInning.BOTTOM if inning_state in _BOTTOM_INNING_STATES else HalfInning.TOP


def _parse_game_state(raw: dict[str, Any]) -> BaseballGameState:
    """Parse a StatsAPI game feed's linescore into typed `BaseballGameState`.

    Base occupancy is read from `offense.{first,second,third}` (a key is present
    only when that base is occupied), mirroring upstream's `man_on`.
    """
    try:
        line = raw["liveData"]["linescore"]
        inning = int(line.get("currentInning", 0) or 0)
        if inning < 1:
            raise ValueError("game feed has no current inning (not live yet?)")
        teams = line.get("teams", {})
        offense = line.get("offense", {})
        return BaseballGameState(
            away_score=int(teams.get("away", {}).get("runs", 0) or 0),
            home_score=int(teams.get("home", {}).get("runs", 0) or 0),
            inning=inning,
            half=_half_from_inning_state(str(line.get("inningState", "Top"))),
            count=BaseballCount(
                balls=int(line.get("balls", 0) or 0),
                strikes=int(line.get("strikes", 0) or 0),
                outs=int(line.get("outs", 0) or 0),
            ),
            bases=BaseballBaseState(
                first="first" in offense,
                second="second" in offense,
                third="third" in offense,
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ProviderError(f"could not parse game state: {exc}") from exc


def _default_fetch_schedule(game_date: date, sport_ids: str) -> list[dict[str, Any]]:  # pragma: no cover - real network
    # Lazy import keeps the network library out of `omni` import time and tests.
    import statsapi

    result: list[dict[str, Any]] = statsapi.schedule(date=game_date.strftime("%Y-%m-%d"), sportId=sport_ids)
    return result


_GAME_FIELDS = (
    "liveData,linescore,teams,home,away,runs,currentInning,inningState,balls,strikes,outs,offense,first,second,third"
)


def _default_fetch_game(game_pk: Any) -> dict[str, Any]:  # pragma: no cover - real network
    import statsapi

    result: dict[str, Any] = statsapi.get("game", {"gamePk": game_pk, "fields": _GAME_FIELDS})
    return result
