"""MLB provider backed by the MLB StatsAPI `schedule` endpoint.

Two calls, both confined to this module's raw dict shape: `refresh(now)` is one
cheap `schedule` call parsed into typed `TeamGame` contests (matchup, status,
start, venue, and the schedule's facts: stage, doubleheader game, national TV
and the like); `fetch_live_feed(game, now=...)` pulls one
game's richer per-game feed into a typed `LiveBaseballFeed` — the current
`BaseballGameState` (score, inning, count, bases) *and* the typed
`BaseballGameEvent`s parsed from the same payload's play-by-play. One fetch yields
both, so callers never double-fetch. Everything it returns is typed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo

from omni.core.enum import DisplayPriority, GameStatus, HomeAway, League, SeasonStage, UpdateUrgency, try_coerce_enum
from omni.core.ids import LeagueScopedId, SourceRef
from omni.core.time import local_date
from omni.domain.baseball import (
    BaseballAction,
    BaseballBaseState,
    BaseballBreakSignals,
    BaseballCount,
    BaseballField,
    BaseballFinishedPlay,
    BaseballGameState,
    BaseballReview,
    BaseballScoringImpact,
    BaseballStoppage,
    BaseballStoppageKind,
    BatterGameLine,
    DueUp,
    InningPhase,
    PitcherGameLine,
    PitchingDecisions,
    PitchSnapshot,
    PitchType,
    PlayLogCoverage,
    PregameDetails,
    PregameSide,
    StartingPitcher,
    WinLossRecord,
    WinProbability,
    scoring_impact,
)
from omni.domain.contest import TeamGame
from omni.domain.coverage import CoverageMap, FieldCoverage
from omni.events.base import EventImportance
from omni.events.baseball import (
    BaseballGameEvent,
    BaseballGameEventType,
    BaseballPlayPayload,
    LiveBaseballFeed,
)
from omni.providers.base import ProviderError, ProviderUpdate
from omni.providers.mlb_teams import MlbTeamRegistry

__all__ = ["MlbStatsApiProvider", "ScheduleFetcher", "GameFetcher", "ContextMetricsFetcher", "map_game_status"]

# (game_date, sport_ids) -> the list of flat schedule rows. A row has `statsapi.schedule`'s keys for what the two
# share (`game_id`, `game_datetime`, `game_date`, `game_type`, `status`, `away_id`, `away_name`, `home_id`,
# `home_name`, `venue_name`, `doubleheader`, `game_num`), and four of its own: `start_time_tbd`,
# `resumed_from_date`, `broadcasts` and `inning_break_length` (`_schedule_rows`).
ScheduleFetcher = Callable[[date, str], list[dict[str, Any]]]
# game_pk -> the raw nested game feed (statsapi.get("game", ...)).
GameFetcher = Callable[[Any], dict[str, Any]]
# game_pk -> the raw game_contextMetrics payload (carries win probability).
ContextMetricsFetcher = Callable[[Any], dict[str, Any]]

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

# StatsAPI `playEvents[].details.eventType` -> the pauses in play the board policy lends (BOARD_POLICY.md §5).
# `mound_visit` and `pitching_substitution` were seen in recorded games; `injury` is from StatsAPI's list of event
# types. The pauses that lend nothing (`batter_timeout`, a pickoff, a step-off, `offensive_substitution`) and the
# stoppages the feed has no event type for (equipment) are left out.
_STOPPAGE_EVENT_TYPES: dict[str, BaseballStoppageKind] = {
    "mound_visit": BaseballStoppageKind.MOUND_VISIT,
    "pitching_substitution": BaseballStoppageKind.PITCHING_CHANGE,
    "injury": BaseballStoppageKind.INJURY,
}

# StatsAPI `gameType` -> the part of the year it files the game under: spring training, the regular season, the
# four postseason rounds (wild card, division series, league championship, World Series), the All-Star Game, and
# exhibitions. A code it doesn't list is UNKNOWN, so no game is called a postseason one without saying so.
_GAME_TYPE_STAGE: dict[str, SeasonStage] = {
    "S": SeasonStage.PRESEASON,
    "R": SeasonStage.REGULAR,
    "F": SeasonStage.POSTSEASON,
    "D": SeasonStage.POSTSEASON,
    "L": SeasonStage.POSTSEASON,
    "W": SeasonStage.POSTSEASON,
    "A": SeasonStage.ALL_STAR,
    "E": SeasonStage.EXHIBITION,
}
# StatsAPI `doubleHeader`: `Y` a traditional doubleheader, `S` a split one (separate admissions), `N` neither.
_DOUBLEHEADERS = frozenset({"Y", "S"})

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
        fetch_pregame: GameFetcher | None = None,
        fetch_context_metrics: ContextMetricsFetcher | None = None,
        source: SourceRef | None = None,
        sport_ids: str = "1,51",  # 1 = MLB, 51 = WBC (mirrors upstream)
        schedule_timezone: ZoneInfo | None = None,
    ) -> None:
        self._registry = registry
        self._fetch = fetch_schedule if fetch_schedule is not None else _default_fetch_schedule
        self._fetch_game = fetch_game if fetch_game is not None else _default_fetch_game
        self._fetch_context_metrics = (
            fetch_context_metrics if fetch_context_metrics is not None else _default_fetch_context_metrics
        )
        self._fetch_pregame = fetch_pregame if fetch_pregame is not None else _default_fetch_pregame
        self.source = source if source is not None else _SOURCE
        self._sport_ids = sport_ids
        self._tz = schedule_timezone if schedule_timezone is not None else _DEFAULT_SCHEDULE_TZ
        # game_pk -> the game's scheduled inning break, as the schedule (or the game's context metrics) states it.
        self._inning_breaks: dict[str, timedelta] = {}

    @property
    def schedule_timezone(self) -> ZoneInfo:
        """The zone whose calendar decides which day's slate is "today"."""
        return self._tz

    def refresh(self, now: datetime) -> ProviderUpdate:
        # Localize to the configured zone, not UTC: in US evenings `now.date()`
        # (UTC) is already tomorrow while tonight's games are still on.
        return self.fetch_schedule(local_date(now, self._tz), now=now)

    def fetch_schedule(self, day: date, *, now: datetime) -> ProviderUpdate:
        """One local date's slate, as observed at ``now``."""
        try:
            rows = self._fetch(day, self._sport_ids)
        except Exception as exc:  # network/library failure -> whole-update error
            raise ProviderError(f"MLB schedule fetch failed: {exc}") from exc

        contests: list[TeamGame] = []
        warnings: list[str] = []
        for row in rows:
            try:
                contest = self._parse_game(row)
            except _SkipGame as skip:
                warnings.append(str(skip))
                continue
            contests.append(contest)
            self._note_inning_break(contest.id.raw, row.get("inning_break_length"))
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
        The feed's own status (the only word that its state is final) and its timestamp
        ride along. The state's break signals carry the game's scheduled inning break when
        the schedule (or the game's context metrics) has stated it. Raises `ProviderError`
        if the fetch fails or the feed has no live state.
        """
        try:
            raw = self._fetch_game(game.id.raw)
        except Exception as exc:
            raise ProviderError(f"MLB game fetch failed for {game.id.raw}: {exc}") from exc
        state = _parse_game_state(raw, inning_break_length=self._inning_breaks.get(game.id.raw))
        events, warnings = _parse_game_events(raw, contest=game, source=self.source, observed_at=now)
        return LiveBaseballFeed(
            state=state,
            events=events,
            decisions=_parse_decisions(raw),
            warnings=warnings,
            status=_feed_status(raw),
            as_of=_feed_as_of(raw),
        )

    def fetch_pregame_details(self, game: TeamGame) -> PregameDetails:
        """Fetch one upcoming game's records and probable starters.

        A separate, narrow request from the live feed, for a game with no live state to parse yet.
        Raises `ProviderError` if the fetch fails; a side the feed says nothing about comes back blank.
        """
        try:
            raw = self._fetch_pregame(game.id.raw)
        except Exception as exc:
            raise ProviderError(f"MLB pregame fetch failed for {game.id.raw}: {exc}") from exc
        return PregameDetails(away=_pregame_side(raw, "away"), home=_pregame_side(raw, "home"))

    def fetch_win_probability(self, game: TeamGame) -> WinProbability | None:
        """Fetch one game's live win probability, or None when none is available.

        A separate, cheap `game_contextMetrics` call — distinct from the feed fetch so the
        caller can poll it on its own (slower) cadence and only for games it is showing.
        Returns None when the feed carries no probability (a game not yet started, or a
        payload without the fields); raises `ProviderError` only if the fetch itself fails.
        """
        try:
            raw = self._fetch_context_metrics(game.id.raw)
        except Exception as exc:
            raise ProviderError(f"MLB win-probability fetch failed for {game.id.raw}: {exc}") from exc
        # The payload's game block is the schedule's, so it states the game's inning break too.
        block = _obj(raw.get("game"))
        self._note_inning_break(game.id.raw, block.get("inningBreakLength", raw.get("inningBreakLength")))
        return _parse_win_probability(raw)

    def _note_inning_break(self, game_pk: str, raw: Any) -> None:
        """Keep a game's scheduled inning break, when ``raw`` states one; one that doesn't changes nothing."""
        length = _inning_break_length(raw)
        if length is not None:
            self._inning_breaks[game_pk] = length

    def _parse_game(self, row: dict[str, Any]) -> TeamGame:
        """One schedule row as a typed game. StatsAPI's schedule has no neutral-site flag, so that stays unknown."""
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
            stage=_GAME_TYPE_STAGE.get(str(row.get("game_type")), SeasonStage.UNKNOWN),
            game_number=_doubleheader_game(row),
            start_tbd=row.get("start_time_tbd") is True,
            provider_date=_schedule_date(row.get("game_date"), self._tz),
            national_broadcast=_national_tv(row.get("broadcasts")),
            resumed_from=_schedule_date(row.get("resumed_from_date"), self._tz),
        )


def _inning_break_length(raw: Any) -> timedelta | None:
    """A game's scheduled inning break from StatsAPI's ``inningBreakLength`` (whole seconds), or None."""
    return timedelta(seconds=raw) if type(raw) is int and raw > 0 else None


def _doubleheader_game(row: dict[str, Any]) -> int | None:
    """Which game of a doubleheader the row is, 1 or 2; None when it isn't one."""
    kind, number = row.get("doubleheader"), row.get("game_num")
    if isinstance(kind, str) and kind in _DOUBLEHEADERS and type(number) is int and number in (1, 2):
        return number
    return None


def _schedule_date(raw: Any, zone: ZoneInfo) -> date | None:
    """A date the schedule gives as ``2026-06-17``, or as an instant read on ``zone``'s calendar; None otherwise."""
    if not isinstance(raw, str):
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        instant = _parse_iso(raw)
    return local_date(instant, zone) if instant is not None else None


def _objects(value: Any) -> list[dict[str, Any]]:
    """The JSON objects in ``value`` if it is a list, skipping anything else; empty if it isn't one."""
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _national_tv(broadcasts: Any) -> bool | None:
    """Whether a US national TV network carries the game; None when the schedule lists no broadcasts for it.

    StatsAPI flags a US national network ``isNational``. Canada's Sportsnet and TVA Sports come as a Toronto home
    broadcast, not a national one, so the flag alone picks out the US networks, in English or Spanish. Radio
    (``AM``, ``FM``) never counts. Listed broadcasts with no national TV among them say False: a local game.
    """
    listed = _objects(broadcasts)
    if not listed:
        return None
    return any(entry.get("isNational") is True and entry.get("type") == "TV" for entry in listed)


def _schedule_rows(response: Any) -> list[dict[str, Any]]:
    """A `schedule` response's games as flat rows, one a game, in the shape `_parse_game` reads (`ScheduleFetcher`).

    A row leaves out what its game doesn't give, for the parse to judge: a game without its id or clubs is
    skipped there with a warning, not dropped here. ``game_date`` is the league's own date for the game
    (``officialDate``), else the date the response files it under. A resumed game's original date is
    ``resumedFromDate``, or ``resumedFrom`` when only that is given.
    """
    rows: list[dict[str, Any]] = []
    for day in _objects(_obj(response).get("dates")):
        for game in _objects(day.get("games")):
            status = _obj(game.get("status"))
            away, home = (_obj(_obj(_obj(game.get("teams")).get(side)).get("team")) for side in ("away", "home"))
            row = {
                "game_id": game.get("gamePk"),
                "game_datetime": game.get("gameDate"),
                "game_date": game.get("officialDate") or day.get("date"),
                "game_type": game.get("gameType"),
                "status": status.get("detailedState"),
                "away_id": away.get("id"),
                "away_name": away.get("name"),
                "home_id": home.get("id"),
                "home_name": home.get("name"),
                "venue_name": _obj(game.get("venue")).get("name"),
                "doubleheader": game.get("doubleHeader"),
                "game_num": game.get("gameNumber"),
                "start_time_tbd": status.get("startTimeTBD"),
                "resumed_from_date": game.get("resumedFromDate") or game.get("resumedFrom"),
                "broadcasts": game.get("broadcasts"),
                "inning_break_length": game.get("inningBreakLength"),
            }
            rows.append({key: value for key, value in row.items() if value is not None})
    return rows


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


def _count(block: dict[str, Any], key: str) -> int | None:
    """A count from a linescore or boxscore block (``runs``, ``hits``, ``baseOnBalls``, ...), or None when the
    feed leaves it out: unknown, not zero."""
    value = block.get(key)
    return None if value is None else int(value)


def _batting_walks_hbp(raw: dict[str, Any], side: str) -> int | None:
    """Walks + hit-by-pitch for `side` from the boxscore batting line, or None if absent.

    These are the on-base events a no-hit line still allows — a walk is the classic blemish on a
    no-hitter — and the linescore omits them, so they come from the boxscore. ``or {}`` tolerates
    a null node at any level; None (no batting block, or either count missing from it) means the
    count is *unknown*, not zero.
    """
    boxscore = (raw.get("liveData") or {}).get("boxscore") or {}
    batting = ((boxscore.get("teams") or {}).get(side) or {}).get("teamStats") or {}
    line = batting.get("batting")
    if not isinstance(line, dict):
        return None
    walks, hit_by_pitch = _count(line, "baseOnBalls"), _count(line, "hitByPitch")
    if walks is None or hit_by_pitch is None:
        return None
    return walks + hit_by_pitch


def _obj(value: Any) -> dict[str, Any]:
    """``value`` if it is a JSON object, else an empty one, so a malformed node reads as absent."""
    return value if isinstance(value, dict) else {}


# Where a batter can end a play safely: on a base, or home.
_SAFE_ENDS = ("1B", "2B", "3B", "score")
# Where a runner can start a play: the batter starts from no base (None), anyone else from one of these.
_BASES = ("1B", "2B", "3B")

# A play's `about.halfInning` -> the side at bat: the away side bats in the top of each inning.
_BATTING_SIDE: dict[str, HomeAway] = {"top": HomeAway.AWAY, "bottom": HomeAway.HOME}
# A half's place within its inning, so the log's entries can be checked to run in game order.
_HALF_PLACE: dict[HomeAway, int] = {HomeAway.AWAY: 0, HomeAway.HOME: 1}

# The linescore's `inningState` -> the half of its inning that the log's last entry must be in, and whether that
# entry must be finished. A break comes after the half before it, so that half's last play ends the log, finished.
_LOG_END: dict[str, tuple[str, bool]] = {
    "Top": ("top", False),
    "Middle": ("top", True),
    "Bottom": ("bottom", False),
    "End": ("bottom", True),
}


@dataclass(frozen=True, slots=True)
class _ReachLedger:
    """What the play log says about each side's batters reaching base.

    `reached` holds the sides with a batter who reached safely on some play, whatever else the log shows. `coverage`
    says whether the log is on record through the point the linescore describes; only then does `complete` hold
    the sides with a finished play on record, so that finding no reach for them means none happened.
    """

    reached: frozenset[HomeAway]
    complete: frozenset[HomeAway]
    coverage: PlayLogCoverage

    def reached_base(self, side: HomeAway) -> bool | None:
        """True if a batter of ``side`` reached safely, False if its full record shows none did, else None."""
        if side in self.reached:
            return True
        return False if side in self.complete else None


def _batter_reached(movement: dict[str, Any]) -> bool:
    """Whether a runner's movement is the batter's (no ``originBase``), ending safe on a base or home and not out."""
    return (
        movement.get("originBase") is None and movement.get("end") in _SAFE_ENDS and movement.get("isOut") is not True
    )


def _readable_movement(movement: Any) -> bool:
    """Whether a runner's movement reads one way: from no base (the batter) or a base, and either out, with no
    ``end``, or not out and safe on a base or home."""
    if not isinstance(movement, dict):
        return False
    origin, end, out = movement.get("originBase"), movement.get("end"), movement.get("isOut")
    if (origin is not None and origin not in _BASES) or type(out) is not bool:
        return False
    return end is None if out else end in _SAFE_ENDS


def _log_reaches_the_linescore(last: dict[str, Any], *, inning: int, inning_state: Any, final: bool) -> bool:
    """Whether the log's last entry (its ``about``) is where the linescore says the game is: in its inning and half,
    and finished at a break or a final."""
    end = _LOG_END.get(inning_state) if isinstance(inning_state, str) else None
    if end is None:
        return False
    half, finished = end
    if last.get("inning") != inning or last.get("halfInning") != half:
        return False
    return last.get("isComplete") is True or not (finished or final)


def _reach_ledger(raw: dict[str, Any], *, inning: int, inning_state: Any, final: bool) -> _ReachLedger:
    """Each side's batters' safe reaches, from the play log (``liveData.plays.allPlays``), and whether the log covers
    the game through the linescore's ``inning`` and ``inning_state`` (``final``: the feed calls the game final).

    The log lists every plate appearance from the game's start. A play's batter is its runner with no
    ``originBase``; he reached safely if his ``end`` is a base or ``score`` and he was not put out. That catches what
    no count does: a batter who reached on a dropped third strike, say. A reach found anywhere counts.

    Finding none rules a reach out only over a log that is COMPLETE: a list whose ``atBatIndex`` values run 0, 1,
    2, ... and whose halves run in game order; every entry but the last finished, and every finished entry with a
    readable list of runners (only the at-bat in progress, the last entry, may still lack them); and a last entry in
    the linescore's current inning and half, finished at a break (after the top in the middle of an inning, after the
    bottom at its end) or a final. A prefix of the log, however tidy, stops short of that point, so it proves nothing
    about the plate appearances it leaves out. Then a side's record is complete once it has a finished play.

    It is read afresh from the whole log on every answer, so a scoring change that rewrites a play shows at once, with
    nothing carried over from an earlier read.
    """
    plays = _obj(_obj(raw.get("liveData")).get("plays")).get("allPlays")
    if not isinstance(plays, list) or not plays:
        return _ReachLedger(reached=frozenset(), complete=frozenset(), coverage=PlayLogCoverage.UNVERIFIED)
    reached: set[HomeAway] = set()
    on_record: set[HomeAway] = set()  # sides with a finished play in the log
    readable = True
    place = (0, 0)  # the inning and half of the entry before
    for position, entry in enumerate(plays):
        play = _obj(entry)
        about = _obj(play.get("about"))
        last = position == len(plays) - 1
        complete = about.get("isComplete")
        side = _BATTING_SIDE.get(str(about.get("halfInning")))
        played_in = about.get("inning")
        in_place = play.get("atBatIndex", about.get("atBatIndex")) == position  # none missing before it, nor misplaced
        under_way = last and complete is False  # only the last may still be under way
        if not in_place or side is None or type(played_in) is not int or not (complete is True or under_way):
            readable = False
        if side is None:
            continue  # a play without a half could be either side's
        if type(played_in) is int:
            readable = readable and (played_in, _HALF_PLACE[side]) >= place
            place = (played_in, _HALF_PLACE[side])
        runners = play.get("runners")
        if not isinstance(runners, list) or not runners:
            readable = readable and under_way
            continue
        if not under_way:
            on_record.add(side)
        for runner in runners:
            movement = _obj(runner).get("movement")
            readable = readable and _readable_movement(movement)
            if isinstance(movement, dict) and _batter_reached(movement):
                reached.add(side)
    through = readable and _log_reaches_the_linescore(
        _obj(_obj(plays[-1]).get("about")), inning=inning, inning_state=inning_state, final=final
    )
    coverage = PlayLogCoverage.COMPLETE if through else PlayLogCoverage.UNVERIFIED
    return _ReachLedger(reached=frozenset(reached), complete=frozenset(on_record if through else ()), coverage=coverage)


def _reached_base(
    raw: dict[str, Any], *, side: HomeAway, hits: int, fielding_errors: int | None, ledger: _ReachLedger
) -> bool | None:
    """Whether `side` has reached base at all this game: True / False / None (unknown).

    A hit, an error by the defense, a walk or HBP in the boxscore, or a batter reaching safely in
    the play log is a definite baserunner. A clean sheet (False) needs each of those counts present
    and zero, and the side's record complete in a play log that covers the game through the point
    the linescore describes, with no batter reaching: the counts alone miss a batter who reached on
    a dropped third strike, and a prefix of the log misses whatever it leaves out. Short of that it
    is None, so a perfect game is never claimed without the data to rule a baserunner out. Counting
    a fielding error as a baserunner can only ever err toward a plain no-hitter (a muffed foul that
    let no one reach), never toward a false perfect game.
    """
    walks_hbp = _batting_walks_hbp(raw, side.value)
    logged = ledger.reached_base(side)
    if hits > 0 or (fielding_errors or 0) > 0 or (walks_hbp or 0) > 0 or logged is True:
        return True
    if walks_hbp is None or fielding_errors is None or logged is None:
        return None
    return False


_NAME_SUFFIXES = frozenset({"jr", "sr", "ii", "iii", "iv", "v"})


def _last_name(full_name: str) -> str:
    """A panel-sized last name from a ``"First Last"`` string, skipping a Jr./Sr./III suffix."""
    parts = full_name.split()
    if len(parts) >= 2 and parts[-1].lower().strip(".") in _NAME_SUFFIXES:
        return parts[-2]
    return parts[-1] if parts else full_name


def _boxscore_player(raw: dict[str, Any], player_id: int) -> dict[str, Any] | None:
    """A player's boxscore record from either team, or None if not on a roster.

    The boxscore keys players as ``ID<personId>`` under each side; a player is on exactly one,
    so search both. Returns the whole player dict (the caller reads ``stats`` and ``battingOrder``).
    """
    teams = ((raw.get("liveData") or {}).get("boxscore") or {}).get("teams") or {}
    key = f"ID{player_id}"
    for side in ("away", "home"):
        player = ((teams.get(side) or {}).get("players") or {}).get(key)
        if isinstance(player, dict):
            return player
    return None


def _lineup_spot(player: dict[str, Any]) -> int | None:
    """The batting-order spot (1-9) from a boxscore ``battingOrder`` like ``"400"``, or None."""
    raw_order = player.get("battingOrder")
    if not isinstance(raw_order, str) or not raw_order.isdigit():
        return None
    spot = int(raw_order) // 100
    return spot if 1 <= spot <= 9 else None


def _batter_game_line(raw: dict[str, Any], person: dict[str, Any]) -> BatterGameLine | None:
    """A named batter with their game line, or None if the feed doesn't name them or carry the line."""
    player_id, name = person.get("id"), person.get("fullName")
    if not isinstance(player_id, int) or not isinstance(name, str) or not name:
        return None
    player = _boxscore_player(raw, player_id)
    if player is None:
        return None
    stats = (player.get("stats") or {}).get("batting") or {}
    return BatterGameLine(
        name=_last_name(name),
        order=_lineup_spot(player),
        at_bats=int(stats.get("atBats", 0) or 0),
        hits=int(stats.get("hits", 0) or 0),
        rbi=int(stats.get("rbi", 0) or 0),
        home_runs=int(stats.get("homeRuns", 0) or 0),
    )


def _current_batter(raw: dict[str, Any]) -> BatterGameLine | None:
    """The batter at the plate with their game line, or None if the feed names none."""
    batter = ((raw.get("liveData") or {}).get("linescore") or {}).get("offense", {}).get("batter") or {}
    return _batter_game_line(raw, batter)


def _current_pitcher(raw: dict[str, Any]) -> PitcherGameLine | None:
    """The pitcher on the mound with their game line, or None if the feed names none."""
    pitcher = ((raw.get("liveData") or {}).get("linescore") or {}).get("defense", {}).get("pitcher") or {}
    player_id, name = pitcher.get("id"), pitcher.get("fullName")
    if not isinstance(player_id, int) or not isinstance(name, str) or not name:
        return None
    player = _boxscore_player(raw, player_id)
    if player is None:
        return None
    stats = (player.get("stats") or {}).get("pitching") or {}
    return PitcherGameLine(
        name=_last_name(name),
        innings_pitched=str(stats.get("inningsPitched", "0.0") or "0.0"),
        pitches=int(stats.get("numberOfPitches", 0) or 0),
        strikeouts=int(stats.get("strikeOuts", 0) or 0),
    )


def _due_up(raw: dict[str, Any], offense: dict[str, Any]) -> DueUp | None:
    """The batters due up from a break's ``offense``, which already names the side coming to bat."""
    lines = (_batter_game_line(raw, offense.get(key) or {}) for key in ("batter", "onDeck", "inHole"))
    batters = tuple(line for line in lines if line is not None)
    return DueUp(batters=batters) if batters else None


def _has_batted(side: HomeAway, *, inning: int, phase: InningPhase) -> bool:
    """Whether ``side`` has come to bat yet, by the linescore's inning and phase. The away side bats first, so it
    has once the game is under way; the home side has from the bottom of the 1st on (not in the top of the 1st,
    nor the break after it)."""
    return side is HomeAway.AWAY or inning > 1 or phase in (InningPhase.BOTTOM, InningPhase.END)


def _runs_and_hits(team: dict[str, Any], side: HomeAway, *, batted: bool, final: bool) -> tuple[int, int]:
    """A side's runs and hits from the linescore.

    A side that has batted must have both, and an answer the feed calls final must have both sides' runs:
    without them the answer is a failed read, since a 0 put in their place could fake a tied final or a
    no-hitter. A side yet to bat that the feed leaves out has a true 0 of each.
    """
    runs, hits = _count(team, "runs"), _count(team, "hits")
    if runs is None and (batted or final):
        raise ProviderError(f"incomplete linescore: no runs for the {side.value} side")
    if hits is None and batted:
        raise ProviderError(f"incomplete linescore: no hits for the {side.value} side")
    return runs or 0, hits or 0


def _coverage_of(
    blocks: Iterable[dict[str, Any]], *keys: str, gap: FieldCoverage = FieldCoverage.DEFAULTED
) -> FieldCoverage:
    """REPORTED when every block gives every one of ``keys``; else ``gap``, for the 0 the parse shows in its place."""
    given = all(block.get(key) is not None for block in blocks for key in keys)
    return FieldCoverage.REPORTED if given else gap


def _parse_game_state(raw: dict[str, Any], *, inning_break_length: timedelta | None = None) -> BaseballGameState:
    """Parse a StatsAPI game feed's linescore into typed `BaseballGameState`.

    Base occupancy is read from `offense.{first,second,third}` (a key is present
    only when that base is occupied), mirroring upstream's `man_on`. A linescore
    missing a side's runs or hits once that side has batted (`_has_batted`), or
    either side's runs in a final answer, raises `ProviderError`: a failed read,
    so the last good answer stands. A missing error count shows as 0, but no
    perfect game is claimed without it, nor without a play log that covers the game through
    the linescore's point (`_reach_ledger`); the state's ``play_log`` says which it had.

    Other gaps show a stand-in, and the state's coverage marks each DEFAULTED: no
    ``outs`` (0), no ``balls`` or ``strikes`` (0), no ``offense`` object at all
    (empty bases; an ``offense`` naming no runner is reported empty bases), an
    ``inningState`` it doesn't know (the top), a side's missing errors (0). A side
    yet to bat has a true 0 of runs and hits, INFERRED from the half, unless the
    half is itself a stand-in.

    Its break signals come from the play log (`_break_signals`), with ``inning_break_length``
    the game's scheduled inning break when it's known.
    """
    try:
        line = raw["liveData"]["linescore"]
        inning = int(line.get("currentInning", 0) or 0)
        if inning < 1:
            raise ValueError("game feed has no current inning (not live yet?)")
        teams = line.get("teams", {})
        offense = line.get("offense", {})
        away_team = teams.get("away", {})
        home_team = teams.get("home", {})
        phase = _phase_from_inning_state(str(line.get("inningState", "Top")))
        inning_state = line.get("inningState")
        known_half = isinstance(inning_state, str) and inning_state in _INNING_STATE_PHASE
        half = FieldCoverage.REPORTED if known_half else FieldCoverage.DEFAULTED
        sides = (away_team, home_team)
        unbatted = FieldCoverage.INFERRED if half is FieldCoverage.REPORTED else FieldCoverage.DEFAULTED
        coverage = CoverageMap.of(
            {
                BaseballField.SCORE: _coverage_of(sides, "runs", gap=unbatted),
                BaseballField.HITS: _coverage_of(sides, "hits", gap=unbatted),
                BaseballField.ERRORS: _coverage_of(sides, "errors"),
                BaseballField.HALF: half,
                BaseballField.COUNT: _coverage_of((line,), "balls", "strikes"),
                BaseballField.OUTS: _coverage_of((line,), "outs"),
                BaseballField.BASES: (
                    FieldCoverage.REPORTED if isinstance(line.get("offense"), dict) else FieldCoverage.DEFAULTED
                ),
            }
        )
        final = _feed_status(raw) is GameStatus.FINAL
        away_runs, away_hits = _runs_and_hits(
            away_team, HomeAway.AWAY, batted=_has_batted(HomeAway.AWAY, inning=inning, phase=phase), final=final
        )
        home_runs, home_hits = _runs_and_hits(
            home_team, HomeAway.HOME, batted=_has_batted(HomeAway.HOME, inning=inning, phase=phase), final=final
        )
        away_errors, home_errors = _count(away_team, "errors"), _count(home_team, "errors")
        ledger = _reach_ledger(raw, inning=inning, inning_state=inning_state, final=final)
        return BaseballGameState(
            away_score=away_runs,
            home_score=home_runs,
            away_hits=away_hits,
            home_hits=home_hits,
            away_errors=away_errors or 0,
            home_errors=home_errors or 0,
            # A side's reached-base read needs the *fielding* side's errors: an away batter
            # reaching on error is charged to the home defense, and vice versa.
            away_reached_base=_reached_base(
                raw, side=HomeAway.AWAY, hits=away_hits, fielding_errors=home_errors, ledger=ledger
            ),
            home_reached_base=_reached_base(
                raw, side=HomeAway.HOME, hits=home_hits, fielding_errors=away_errors, ledger=ledger
            ),
            play_log=ledger.coverage,
            batter=_current_batter(raw),
            pitcher=_current_pitcher(raw),
            last_pitch=_last_pitch_snapshot(raw),
            inning=inning,
            phase=phase,
            due_up=_due_up(raw, offense) if phase.is_break else None,
            break_signals=_break_signals(raw, inning_break_length=inning_break_length),
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
            coverage=coverage,
        )
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        # AttributeError covers a null nested object (e.g. `"teams": null` -> None.get(...)),
        # so a malformed linescore becomes a typed error, never a raw escape.
        raise ProviderError(f"could not parse game state: {exc}") from exc


def _feed_status(raw: dict[str, Any]) -> GameStatus | None:
    """The game's status as the feed gives it (``gameData.status.detailedState``), or None when it says none."""
    detailed = ((raw.get("gameData") or {}).get("status") or {}).get("detailedState")
    return map_game_status(detailed) if isinstance(detailed, str) and detailed else None


def _feed_as_of(raw: dict[str, Any]) -> datetime | None:
    """When the feed says its answer stood (``metaData.timeStamp``, UTC as ``20260617_233012``), or None."""
    stamp = (raw.get("metaData") or {}).get("timeStamp")
    if not isinstance(stamp, str):
        return None
    try:
        return datetime.strptime(stamp, "%Y%m%d_%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


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


def _scoring_priority(impact: BaseballScoringImpact) -> tuple[DisplayPriority, float, str] | None:
    """The band, leverage, and reason a scoring play earns from context, or None if it did not score.

    A scoring play is at least big-play-eligible (HIGH_LEVERAGE) regardless of its bare type;
    a walk-off is an outright ALERT. Leverage grades the drama within that for ranking.
    """
    if not impact.scored:
        return None
    if impact.walk_off:
        return DisplayPriority.ALERT, 1.0, "walk_off"
    if impact.go_ahead:
        return DisplayPriority.HIGH_LEVERAGE, 0.8, "go_ahead"
    if impact.tying:
        return DisplayPriority.HIGH_LEVERAGE, 0.7, "tying"
    return DisplayPriority.HIGH_LEVERAGE, 0.4, "scored"


def _event_importance(event_type: BaseballGameEventType, impact: BaseballScoringImpact) -> EventImportance:
    """Importance of a play from its type *and* its scoring context.

    The intrinsic type sets a base band + rarity (a home run is rare on its own); the scoring
    context can only *raise* the band — so a walk-off single (intrinsically routine) clears the
    big-play bar, while a non-scoring single stays NORMAL and merely informs the live card.
    """
    band, rarity = _EVENT_BAND_RARITY.get(event_type, (DisplayPriority.NORMAL, 0.1))
    leverage = 0.0
    reasons: tuple[str, ...] = (event_type.value,)
    scoring = _scoring_priority(impact)
    if scoring is not None:
        scoring_band, leverage, reason = scoring
        band = max(band, scoring_band)
        reasons = reasons + (reason,)
    return EventImportance(
        priority=band,
        urgency=_BAND_URGENCY.get(band, UpdateUrgency.NORMAL),
        leverage=leverage,
        rarity=rarity,
        favorite_relevance=0.0,
        reasons=reasons,
    )


def _last_pitch_event(play: dict[str, Any]) -> dict[str, Any] | None:
    """The play's most recent *actual* pitch event, walking ``playEvents`` from the end.

    Skips non-pitch entries (a mound visit, a substitution); returns None when the feed
    carries no ``playEvents`` list or none of them is a pitch.
    """
    events = play.get("playEvents")
    if not isinstance(events, list):
        return None
    for event in reversed(events):
        if isinstance(event, dict) and event.get("isPitch"):
            return event
    return None


def _decisive_pitch_type(play: dict[str, Any]) -> PitchType | None:
    """The typed pitch type of the at-bat's last pitch — the one that ended it — or None.

    A typeless pitch, an unrecognized code, or a play with no pitch all yield None.
    """
    event = _last_pitch_event(play)
    if event is None:
        return None
    type_info = (event.get("details") or {}).get("type") or {}
    return try_coerce_enum(PitchType, type_info.get("code"))


def _last_pitch_snapshot(raw: dict[str, Any]) -> PitchSnapshot | None:
    """The current at-bat's most recent pitch — velocity + typed pitch — or None.

    Reads ``liveData.plays.currentPlay`` for the latest tracked pitch, pairing its release
    speed (``pitchData.startSpeed``, rounded to whole mph) with its typed pitch. A missing
    current play, an untyped or unrecognized pitch, or one without a positive speed all yield
    None — the board shows the token only for a real, measured delivery.
    """
    current = ((raw.get("liveData") or {}).get("plays") or {}).get("currentPlay")
    if not isinstance(current, dict):
        return None
    event = _last_pitch_event(current)
    if event is None:
        return None
    pitch_type = try_coerce_enum(PitchType, ((event.get("details") or {}).get("type") or {}).get("code"))
    speed = (event.get("pitchData") or {}).get("startSpeed")
    if pitch_type is None or not isinstance(speed, (int, float)) or isinstance(speed, bool):
        return None
    velocity = round(speed)
    if velocity <= 0:
        return None
    return PitchSnapshot(velocity_mph=velocity, pitch_type=pitch_type)


def _at_bat_index(play: dict[str, Any]) -> int | None:
    """A play's ``atBatIndex`` (on the play or under its ``about``), or None when it has no usable one."""
    index = play.get("atBatIndex", _obj(play.get("about")).get("atBatIndex"))
    return index if type(index) is int and index >= 0 else None


def _play_events(play: dict[str, Any]) -> list[tuple[int, dict[str, Any]]]:
    """A play's events, each with its place among them: the feed's ``index``, else its position in the list."""
    events = _objects(play.get("playEvents"))
    return [
        (index if type(index := event.get("index")) is int and index >= 0 else position, event)
        for position, event in enumerate(events)
    ]


def _newest_plays(raw: dict[str, Any]) -> list[tuple[int, dict[str, Any]]]:
    """The newest plate appearances, oldest first, each once with its index: the log's last two and the current
    play (which stands for its own entry in the log). The one before the current play still matters: a review of
    its result can run after the next plate appearance has begun."""
    plays = _obj(_obj(raw.get("liveData")).get("plays"))
    newest: dict[int, dict[str, Any]] = {}
    for play in [*_objects(plays.get("allPlays"))[-2:], _obj(plays.get("currentPlay"))]:
        index = _at_bat_index(play)
        if index is not None:
            newest[index] = play
    return sorted(newest.items())


def _finished(play: dict[str, Any]) -> bool:
    return _obj(play.get("about")).get("isComplete") is True


def _finished_play(at_bat: int, play: dict[str, Any]) -> BaseballFinishedPlay | None:
    """A finished plate appearance's place and end, or None when it doesn't say which half it was in."""
    about = _obj(play.get("about"))
    inning, phase = about.get("inning"), _HALF_INNING_PHASE.get(str(about.get("halfInning")))
    if type(inning) is not int or inning < 1 or phase is None:
        return None
    return BaseballFinishedPlay(at_bat=at_bat, inning=inning, phase=phase, ended=_parse_iso(about.get("endTime")))


def _last_action(plays: list[tuple[int, dict[str, Any]]]) -> BaseballAction | None:
    """The newest action: the newest plate appearance's last pitch while it's under way, else the newest finished
    plate appearance's end. None when the log shows neither."""
    if plays and not _finished(plays[-1][1]):
        at_bat, play = plays[-1]
        pitches = [(index, event) for index, event in _play_events(play) if event.get("isPitch") is True]
        if pitches:
            index, pitch = pitches[-1]
            return BaseballAction(at_bat=at_bat, pitch=index, at=_parse_iso(pitch.get("startTime")))
    finished = [(at_bat, play) for at_bat, play in plays if _finished(play)]
    if not finished:
        return None
    at_bat, play = finished[-1]
    return BaseballAction(at_bat=at_bat, at=_parse_iso(_obj(play.get("about")).get("endTime")))


def _stoppages(at_bat: int, play: dict[str, Any]) -> tuple[BaseballStoppage, ...]:
    """The pauses logged in a plate appearance's events, each marked resumed once a pitch follows it or the plate
    appearance has ended."""
    events = _play_events(play)
    ended = _finished(play)
    found: list[BaseballStoppage] = []
    for position, (index, event) in enumerate(events):
        kind = _STOPPAGE_EVENT_TYPES.get(str(_obj(event.get("details")).get("eventType")))
        if kind is None:
            continue
        pitched = any(later.get("isPitch") is True for _, later in events[position + 1 :])
        began = _parse_iso(event.get("startTime"))
        found.append(BaseballStoppage(kind=kind, at_bat=at_bat, event=index, began=began, resumed=ended or pitched))
    return tuple(found)


def _review(at_bat: int, details: Any, event: int | None = None) -> BaseballReview | None:
    in_progress = _obj(details).get("inProgress")
    return BaseballReview(at_bat=at_bat, event=event, in_progress=in_progress) if type(in_progress) is bool else None


def _reviews(at_bat: int, play: dict[str, Any]) -> list[BaseballReview]:
    """The replay reviews on a plate appearance: of its result (``reviewDetails`` on the play) and of its non-pitch
    events (a steal, a pickoff). A review on a pitch is left out: replay doesn't review balls and strikes, so one
    there is a ball-strike challenge, which lends nothing."""
    found = [_review(at_bat, play.get("reviewDetails"))]
    found += [
        _review(at_bat, event.get("reviewDetails"), index)
        for index, event in _play_events(play)
        if event.get("isPitch") is not True
    ]
    return [review for review in found if review is not None]


def _break_signals(raw: dict[str, Any], *, inning_break_length: timedelta | None) -> BaseballBreakSignals:
    """What the play log's newest plate appearances say about pauses in play.

    Nothing here sinks the state's parse: a play or an event the feed garbles is read as absent.
    """
    plays = _newest_plays(raw)
    finished = [(at_bat, play) for at_bat, play in plays if _finished(play)]
    return BaseballBreakSignals(
        inning_break_length=inning_break_length,
        last_finished=_finished_play(*finished[-1]) if finished else None,
        last_action=_last_action(plays),
        stoppages=_stoppages(*plays[-1]) if plays else (),
        reviews=tuple(review for at_bat, play in plays for review in _reviews(at_bat, play)),
    )


def _parse_one_play(
    play: Any, *, contest: TeamGame, source: SourceRef, observed_at: datetime, as_of: datetime | None = None
) -> BaseballGameEvent | None:
    """Parse one `allPlays` entry into a typed event, or None to skip it.

    Returns None for a non-dict entry, an unmapped/routine outcome, or a play with no
    stable `atBatIndex` (without which the event could not be deduped across polls).
    ``as_of`` is when the answer carrying the play stood, by the source's clock.
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
    # The TV delay is measured from when the play ended. Without that time, the play is dated by the answer
    # that reports it, which it can't postdate, so it waits the full delay from receipt. Its at-bat's start
    # would date it too early: a long at-bat would come out ahead of the broadcast. With no time at all, it
    # is dated by when we read it, and an answer without its own time waits the full delay too.
    source_time = _parse_iso(about.get("endTime")) or as_of or observed_at
    inning = int(about.get("inning") or 1)
    phase = _HALF_INNING_PHASE.get(str(about.get("halfInning", "")), InningPhase.TOP)
    rbi = int(result.get("rbi") or 0)
    away_score = _safe_int(result.get("awayScore"))
    home_score = _safe_int(result.get("homeScore"))
    impact = scoring_impact(phase=phase, inning=inning, rbi=rbi, away_score=away_score, home_score=home_score)
    payload = BaseballPlayPayload(
        inning=inning,
        phase=phase,
        description=str(result.get("description", "")),
        count=_safe_count(play.get("count")),
        rbi=rbi,
        away_score=away_score,
        home_score=home_score,
        pitch_type=_decisive_pitch_type(play),
        scoring_impact=impact,
    )
    return BaseballGameEvent(
        id=LeagueScopedId(contest.league, source, f"{contest.id.raw}:ab:{at_bat_index}"),
        contest=contest,
        event_type=event_type,
        source=source,
        source_time=source_time,
        observed_at=observed_at,
        importance=_event_importance(event_type, impact),
        payload=payload,
    )


def _parse_game_events(
    raw: dict[str, Any], *, contest: TeamGame, source: SourceRef, observed_at: datetime
) -> tuple[tuple[BaseballGameEvent, ...], tuple[str, ...]]:
    """Parse `liveData.plays.allPlays` into typed events, plus warnings for any it had to drop.

    Per-play isolation: a feed with no plays yields ``((), ())``; routine and unmapped plays
    are skipped silently (see `_parse_one_play`), and a *malformed* play — one that raises
    while parsing — is dropped with a warning instead of sinking the whole parse or escaping
    the provider boundary. So one odd entry never costs more than that one entry.
    """
    plays = _obj(_obj(raw.get("liveData")).get("plays")).get("allPlays")
    if not isinstance(plays, list):
        return (), ()
    events: list[BaseballGameEvent] = []
    warnings: list[str] = []
    as_of = _feed_as_of(raw)
    for index, play in enumerate(plays):
        try:
            event = _parse_one_play(play, contest=contest, source=source, observed_at=observed_at, as_of=as_of)
        except Exception as exc:
            warnings.append(f"play {index}: {type(exc).__name__}: {exc}")
            continue
        if event is not None:
            events.append(event)
    return tuple(events), tuple(warnings)


def _pitcher_name(person: Any) -> str | None:
    """The non-empty `fullName` of a `decisions` person block, or None if absent/blank."""
    if not isinstance(person, dict):
        return None
    name = person.get("fullName")
    return name if isinstance(name, str) and name else None


def _season_pitching(raw: dict[str, Any], person: Any) -> dict[str, Any]:
    """A `decisions` person's season pitching totals from the boxscore, or empty if not found."""
    player_id = person.get("id") if isinstance(person, dict) else None
    player = _boxscore_player(raw, player_id) if isinstance(player_id, int) else None
    return ((player or {}).get("seasonStats") or {}).get("pitching") or {}


def _season_record(pitching: dict[str, Any]) -> WinLossRecord | None:
    """A win-loss record from season pitching totals, or None unless both halves are present."""
    wins, losses = _safe_int(pitching.get("wins")), _safe_int(pitching.get("losses"))
    return WinLossRecord(wins=wins, losses=losses) if wins is not None and losses is not None else None


def _pregame_side(raw: dict[str, Any], side: str) -> PregameSide:
    """One club's record and probable starter from a pregame feed, each None if the feed lacks it."""
    game_data = raw.get("gameData") or {}
    team = (game_data.get("teams") or {}).get(side) or {}
    person = (game_data.get("probablePitchers") or {}).get(side)
    name = _pitcher_name(person)
    starter = None
    if name is not None:
        pitching = _season_pitching(raw, person)
        era = pitching.get("era")
        starter = StartingPitcher(
            name=_last_name(name),
            record=_season_record(pitching),
            era=era if isinstance(era, str) and era else None,
        )
    return PregameSide(record=_season_record(team.get("record") or {}), starter=starter)


def _parse_decisions(raw: dict[str, Any]) -> PitchingDecisions | None:
    """Parse `liveData.decisions` into the winning/losing/saving pitchers, or None.

    Returns None unless both a winner and a loser are named — a game still in progress, a
    tie, or a feed without the block; the save is optional (only a save situation has one).
    """
    decisions = raw.get("liveData", {}).get("decisions")
    if not isinstance(decisions, dict):
        return None
    winner = _pitcher_name(decisions.get("winner"))
    loser = _pitcher_name(decisions.get("loser"))
    if winner is None or loser is None:
        return None
    return PitchingDecisions(
        winner=winner,
        loser=loser,
        save=_pitcher_name(decisions.get("save")),
        winner_record=_season_record(_season_pitching(raw, decisions.get("winner"))),
        loser_record=_season_record(_season_pitching(raw, decisions.get("loser"))),
        saves=_safe_int(_season_pitching(raw, decisions.get("save")).get("saves")),
    )


def _parse_win_probability(raw: dict[str, Any]) -> WinProbability | None:
    """Parse `homeWinProbability`/`awayWinProbability` (0..100) into a typed value, or None.

    Returns None unless both are present and numeric (a game not yet started, or a payload
    without the block). `bool` is excluded — it is an `int` subclass and never a real percentage.
    """
    home = raw.get("homeWinProbability")
    away = raw.get("awayWinProbability")
    if isinstance(home, bool) or isinstance(away, bool):
        return None
    if not isinstance(home, (int, float)) or not isinstance(away, (int, float)):
        return None
    try:
        return WinProbability(home=float(home), away=float(away))
    except ValueError:
        return None  # out-of-range percentages from a malformed payload


HTTP_TIMEOUT_S = 15.0  # StatsAPI answers in well under a second; a request this slow has hung


class TimedRequests:
    """`requests` as `statsapi` calls it, with a timeout on every `get` that sets none.

    `statsapi` calls ``requests.get(url)`` with no timeout, so a stalled connection would hang its
    caller for good. Every other attribute passes through.
    """

    def __init__(self, requests: Any, timeout_s: float) -> None:
        self._requests = requests
        self._timeout_s = timeout_s

    def get(self, url: str, **kwargs: Any) -> Any:
        kwargs.setdefault("timeout", self._timeout_s)
        return self._requests.get(url, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._requests, name)


def _statsapi() -> Any:  # pragma: no cover - real network
    """The `statsapi` module, with its requests bounded by `HTTP_TIMEOUT_S`."""
    # Lazy import keeps the network library out of `omni` import time and tests.
    import statsapi

    if not isinstance(statsapi.requests, TimedRequests):
        statsapi.requests = TimedRequests(statsapi.requests, HTTP_TIMEOUT_S)
    return statsapi


# The schedule's one hydration: each game's TV and radio broadcasts, for its national-TV flag. Measured on a
# full 15-game slate (2026-09-27), it takes the response from about 2.7 KB to 8.3 KB gzipped, or 20 KB to 79 KB
# of JSON. `statsapi.schedule`, which this replaces, asked for more: it hydrates each game's broadcasts too, and
# its decisions, probable pitchers, linescore, series status and media listings besides.
_SCHEDULE_HYDRATE = "broadcasts"


def _default_fetch_schedule(game_date: date, sport_ids: str) -> list[dict[str, Any]]:  # pragma: no cover - real network
    statsapi = _statsapi()
    query = {"date": game_date.strftime("%Y-%m-%d"), "sportId": sport_ids, "hydrate": _SCHEDULE_HYDRATE}
    return _schedule_rows(statsapi.get("schedule", query))


# StatsAPI `fields` is a hierarchical key-name whitelist. The first group keeps the linescore
# and a slice of the boxscore (-> BaseballGameState, including reached-base); the second keeps
# play-by-play (allPlays -> events, and each play's runners -> the reach ledger) and the current play
# (-> the live last-pitch snapshot). A name must be listed for its nested object to survive, so
# omitting one silently drops that data — keep this in sync with what `_parse_game_state` /
# `_parse_game_events` / `_reach_ledger` read.
_GAME_FIELDS = (
    "liveData,linescore,teams,home,away,runs,hits,errors,currentInning,inningState,balls,strikes,outs,"
    "offense,defense,first,second,third,batter,onDeck,inHole,pitcher,id,"  # current batter/pitcher (lines below), due up
    "boxscore,teamStats,batting,baseOnBalls,hitByPitch,"  # per-side walks/HBP -> perfect-game detection
    "players,stats,pitching,battingOrder,atBats,homeRuns,strikeOuts,numberOfPitches,inningsPitched,"  # batter/pitcher lines
    "seasonStats,wins,losses,saves,"  # season totals for the final card's decisions
    "plays,allPlays,currentPlay,result,eventType,description,rbi,awayScore,homeScore,about,inning,halfInning,atBatIndex,"
    "endTime,startTime,count,playEvents,isPitch,pitchData,startSpeed,details,type,code,decisions,winner,loser,save,fullName,"
    # Each play's runners -> the reach ledger. `end` and `originBase` occur only under `movement`; `isOut` also
    # keeps a flag on each play's result and pitch events.
    "isComplete,runners,movement,originBase,end,isOut,"
    # Pauses in play -> the break signals: each event's place in its plate appearance (`index`; its type, times
    # and `isPitch` are kept above), and replay reviews on a play or an event.
    "index,reviewDetails,inProgress,"
    "gameData,status,detailedState,metaData,timeStamp"  # the feed's own status and timestamp
)


# The narrow whitelist for an upcoming game: its records and probable starters, and nothing else.
_PREGAME_FIELDS = (
    "gameData,teams,home,away,record,wins,losses,probablePitchers,id,fullName,"
    "liveData,boxscore,players,seasonStats,pitching,era"
)


def _default_fetch_pregame(game_pk: Any) -> dict[str, Any]:  # pragma: no cover - real network
    statsapi = _statsapi()
    result: dict[str, Any] = statsapi.get("game", {"gamePk": game_pk, "fields": _PREGAME_FIELDS})
    return result


def _default_fetch_game(game_pk: Any) -> dict[str, Any]:  # pragma: no cover - real network
    statsapi = _statsapi()
    result: dict[str, Any] = statsapi.get("game", {"gamePk": game_pk, "fields": _GAME_FIELDS})
    return result


def _default_fetch_context_metrics(game_pk: Any) -> dict[str, Any]:  # pragma: no cover - real network
    statsapi = _statsapi()
    result: dict[str, Any] = statsapi.get("game_contextMetrics", {"gamePk": game_pk})
    return result
