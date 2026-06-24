"""MLB provider backed by the MLB StatsAPI `schedule` endpoint.

Two calls, both confined to this module's raw dict shape: `refresh(now)` is one
cheap `statsapi.schedule(...)` call parsed into typed `TeamGame` contests
(matchup + status + start + venue); `fetch_live_feed(game, now=...)` pulls one
game's richer per-game feed into a typed `LiveBaseballFeed` — the current
`BaseballGameState` (score, inning, count, bases) *and* the typed
`BaseballGameEvent`s parsed from the same payload's play-by-play. One fetch yields
both, so callers never double-fetch. Everything it returns is typed.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

from omni.core.enum import DisplayPriority, GameStatus, League, UpdateUrgency
from omni.core.ids import LeagueScopedId, SourceRef
from omni.core.time import local_date
from omni.domain.baseball import BaseballBaseState, BaseballCount, BaseballGameState, InningPhase
from omni.domain.contest import TeamGame
from omni.events.base import EventImportance
from omni.events.baseball import (
    BaseballGameEvent,
    BaseballGameEventType,
    BaseballPlayPayload,
    LiveBaseballFeed,
)
from omni.providers.base import ProviderError, ProviderUpdate
from omni.providers.mlb_teams import MlbTeamRegistry

__all__ = ["MlbStatsApiProvider", "ScheduleFetcher", "GameFetcher", "map_game_status"]

# (game_date, sport_ids) -> the list of flat schedule rows.
ScheduleFetcher = Callable[[date, str], list[dict[str, Any]]]
# game_pk -> the raw nested game feed (statsapi.get("game", ...)).
GameFetcher = Callable[[Any], dict[str, Any]]

# StatsAPI `inningState` -> our typed phase; the breaks (Middle/End) are kept distinct.
_INNING_STATE_PHASE: dict[str, InningPhase] = {
    "Top": InningPhase.TOP,
    "Middle": InningPhase.MIDDLE,
    "Bottom": InningPhase.BOTTOM,
    "End": InningPhase.END,
}

# StatsAPI play `about.halfInning` -> our phase; a completed at-bat is only ever in a
# live half (top/bottom), never a break, so only those two are mapped (default TOP).
_HALF_INNING_PHASE: dict[str, InningPhase] = {
    "top": InningPhase.TOP,
    "bottom": InningPhase.BOTTOM,
}

# StatsAPI at-bat `result.eventType` -> our typed event. We map only the outcomes we
# can name unambiguously and might surface (hits, walks, multi-outs). Routine outs
# (`field_out`, `force_out`, `strikeout`, ...) have no entry and are *not* emitted as
# events — they are not big plays, and `strikeout` cannot be split into looking vs
# swinging from the at-bat result alone. Pitch-level granularity is a later concern.
_EVENT_TYPE_MAP: dict[str, BaseballGameEventType] = {
    "single": BaseballGameEventType.SINGLE,
    "double": BaseballGameEventType.DOUBLE,
    "triple": BaseballGameEventType.TRIPLE,
    "home_run": BaseballGameEventType.HOME_RUN,
    "walk": BaseballGameEventType.WALK,
    "intent_walk": BaseballGameEventType.WALK,
    "hit_by_pitch": BaseballGameEventType.HIT_BY_PITCH,
    "sac_fly": BaseballGameEventType.SAC_FLY,
    "sac_bunt": BaseballGameEventType.SAC_BUNT,
    "grounded_into_double_play": BaseballGameEventType.DOUBLE_PLAY,
    "double_play": BaseballGameEventType.DOUBLE_PLAY,
    "triple_play": BaseballGameEventType.TRIPLE_PLAY,
}

# Intrinsic importance per event type — what the play tells us on its own: its base
# `DisplayPriority` band and `rarity` (0..1). The *contextual* signals (leverage,
# favorite relevance) are 0 here; the `PriorityScorer` adds them later with game state
# and device favorites. So the provider says "this is a home run (rare)", not "this is
# the high-leverage favorite play of the night".
_EVENT_BAND_RARITY: dict[BaseballGameEventType, tuple[DisplayPriority, float]] = {
    BaseballGameEventType.HOME_RUN: (DisplayPriority.ALERT, 0.70),
    BaseballGameEventType.TRIPLE: (DisplayPriority.HIGH_LEVERAGE, 0.60),
    BaseballGameEventType.TRIPLE_PLAY: (DisplayPriority.ALERT, 0.95),
    BaseballGameEventType.DOUBLE_PLAY: (DisplayPriority.HIGH_LEVERAGE, 0.50),
    BaseballGameEventType.DOUBLE: (DisplayPriority.NORMAL, 0.35),
    BaseballGameEventType.SAC_FLY: (DisplayPriority.NORMAL, 0.25),
    BaseballGameEventType.HIT_BY_PITCH: (DisplayPriority.NORMAL, 0.20),
    BaseballGameEventType.SAC_BUNT: (DisplayPriority.NORMAL, 0.20),
    BaseballGameEventType.SINGLE: (DisplayPriority.NORMAL, 0.20),
    BaseballGameEventType.WALK: (DisplayPriority.NORMAL, 0.15),
}
# Higher bands carry more time-urgency in the live feed; derived, not stored twice.
_BAND_URGENCY: dict[DisplayPriority, UpdateUrgency] = {
    DisplayPriority.ALERT: UpdateUrgency.LIVE_CRITICAL,
    DisplayPriority.HIGH_LEVERAGE: UpdateUrgency.HIGH,
}

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

    def fetch_live_feed(self, game: TeamGame, *, now: datetime) -> LiveBaseballFeed:
        """Fetch one game's live feed and parse it into a typed `LiveBaseballFeed`.

        Separate from `refresh` (which is one cheap schedule call): the caller decides
        which live games are worth a per-game request. The single fetch yields both the
        current `BaseballGameState` and the typed play-by-play `events`; events are
        stamped `observed_at=now` and scoped to ``game`` so each carries stable lineage.
        Raises `ProviderError` if the fetch fails or the feed has no live state.
        """
        try:
            raw = self._fetch_game(game.id.raw)
        except Exception as exc:
            raise ProviderError(f"MLB game fetch failed for {game.id.raw}: {exc}") from exc
        state = _parse_game_state(raw)
        events = _parse_game_events(raw, contest=game, source=self.source, observed_at=now)
        return LiveBaseballFeed(state=state, events=events)

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


def _phase_from_inning_state(inning_state: str) -> InningPhase:
    # Keep Middle/End distinct from the active halves; default to TOP on an unknown label.
    return _INNING_STATE_PHASE.get(inning_state, InningPhase.TOP)


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
            away_hits=int(teams.get("away", {}).get("hits", 0) or 0),
            home_hits=int(teams.get("home", {}).get("hits", 0) or 0),
            inning=inning,
            phase=_phase_from_inning_state(str(line.get("inningState", "Top"))),
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


def _parse_iso(raw: Any) -> datetime | None:
    """Parse a StatsAPI ISO timestamp to an aware datetime, or None if absent/bad."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _safe_int(value: Any) -> int | None:
    """Coerce a StatsAPI numeric to int, or None if absent/non-numeric."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_count(count: Any) -> BaseballCount | None:
    """Build a `BaseballCount` from a play's count, or None if it is missing/impossible.

    The count is informational on a big-play card, so one malformed value drops the
    count rather than the whole event.
    """
    if not isinstance(count, dict):
        return None
    try:
        return BaseballCount(
            balls=int(count.get("balls", 0) or 0),
            strikes=int(count.get("strikes", 0) or 0),
            outs=int(count.get("outs", 0) or 0),
        )
    except (TypeError, ValueError):
        return None


def _event_importance(event_type: BaseballGameEventType) -> EventImportance:
    """Intrinsic importance of a play from its type alone (context added later)."""
    band, rarity = _EVENT_BAND_RARITY.get(event_type, (DisplayPriority.NORMAL, 0.1))
    return EventImportance(
        priority=band,
        urgency=_BAND_URGENCY.get(band, UpdateUrgency.NORMAL),
        leverage=0.0,
        rarity=rarity,
        favorite_relevance=0.0,
        reasons=(event_type.value,),
    )


def _parse_one_play(
    play: Any, *, contest: TeamGame, source: SourceRef, observed_at: datetime
) -> BaseballGameEvent | None:
    """Parse one `allPlays` entry into a typed event, or None to skip it.

    Returns None for a non-dict entry, an unmapped/routine outcome, or a play with no
    stable `atBatIndex` (without which the event could not be deduped across polls).
    """
    if not isinstance(play, dict):
        return None
    result = play.get("result", {})
    event_type = _EVENT_TYPE_MAP.get(str(result.get("eventType", "")))
    if event_type is None:
        return None  # routine / unmapped outcome — not surfaced as a typed event
    about = play.get("about", {})
    at_bat_index = play.get("atBatIndex", about.get("atBatIndex"))
    if at_bat_index is None:
        return None  # no stable lineage key — cannot dedupe across polls; skip
    # Anchor the TV-delay to when the play happened; fall back to receipt time.
    source_time = _parse_iso(about.get("endTime")) or _parse_iso(about.get("startTime")) or observed_at
    payload = BaseballPlayPayload(
        inning=int(about.get("inning") or 1),
        phase=_HALF_INNING_PHASE.get(str(about.get("halfInning", "")), InningPhase.TOP),
        description=str(result.get("description", "")),
        count=_safe_count(play.get("count")),
        rbi=int(result.get("rbi") or 0),
        away_score=_safe_int(result.get("awayScore")),
        home_score=_safe_int(result.get("homeScore")),
    )
    return BaseballGameEvent(
        id=LeagueScopedId(contest.league, source, f"{contest.id.raw}:ab:{at_bat_index}"),
        contest=contest,
        event_type=event_type,
        source=source,
        source_time=source_time,
        observed_at=observed_at,
        importance=_event_importance(event_type),
        payload=payload,
    )


def _parse_game_events(
    raw: dict[str, Any], *, contest: TeamGame, source: SourceRef, observed_at: datetime
) -> tuple[BaseballGameEvent, ...]:
    """Parse a game feed's `liveData.plays.allPlays` into typed, mapped events.

    Robust by design: a feed with no plays yields ``()``; routine and unmapped plays
    are skipped (see `_parse_one_play`) so one odd entry never sinks the whole parse.
    """
    plays = raw.get("liveData", {}).get("plays", {}).get("allPlays", [])
    if not isinstance(plays, list):
        return ()
    events = [_parse_one_play(p, contest=contest, source=source, observed_at=observed_at) for p in plays]
    return tuple(event for event in events if event is not None)


def _default_fetch_schedule(game_date: date, sport_ids: str) -> list[dict[str, Any]]:  # pragma: no cover - real network
    # Lazy import keeps the network library out of `omni` import time and tests.
    import statsapi

    result: list[dict[str, Any]] = statsapi.schedule(date=game_date.strftime("%Y-%m-%d"), sportId=sport_ids)
    return result


# StatsAPI `fields` is a hierarchical key-name whitelist. The first group keeps the
# linescore (-> BaseballGameState); the second keeps play-by-play (-> events). A name
# must be listed for its nested object to survive, so omitting one silently drops that
# data — keep this in sync with what `_parse_game_state` / `_parse_game_events` read.
_GAME_FIELDS = (
    "liveData,linescore,teams,home,away,runs,hits,currentInning,inningState,balls,strikes,outs,offense,first,second,third,"
    "plays,allPlays,result,eventType,description,rbi,awayScore,homeScore,about,inning,halfInning,atBatIndex,"
    "endTime,startTime,count"
)


def _default_fetch_game(game_pk: Any) -> dict[str, Any]:  # pragma: no cover - real network
    import statsapi

    result: dict[str, Any] = statsapi.get("game", {"gamePk": game_pk, "fields": _GAME_FIELDS})
    return result
