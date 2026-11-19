"""Minimal synthetic StatsAPI live-feed answers for the MLB break tests, read through the provider as a board would."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from omni.core.clock import ClockReading, MonotonicTime
from omni.core.enum import GameStatus, League
from omni.core.ids import LeagueScopedId, SourceRef
from omni.domain.contest import TeamGame
from omni.events.baseball import LiveBaseballFeed
from omni.providers.mlb_statsapi import ContextMetricsFetcher, MlbStatsApiProvider
from omni.providers.mlb_teams import MlbTeamRegistry

T = datetime(2026, 6, 17, 23, 30, tzinfo=timezone.utc)  # when the answer stood, by StatsAPI's clock
RECEIVED = ClockReading(T + timedelta(seconds=2), MonotonicTime(500.0))  # when it reached the board
SOURCE = SourceRef("mlb_statsapi", "https://statsapi.mlb.com")
GAME_PK = 700001
_REGISTRY = MlbTeamRegistry.from_color_file()
_SIDE = {"runs": 1, "hits": 3, "errors": 0}


def game(status: GameStatus = GameStatus.LIVE) -> TeamGame:
    return TeamGame(
        id=LeagueScopedId(League.MLB, SOURCE, str(GAME_PK)),
        league=League.MLB,
        status=status,
        scheduled_start=T - timedelta(hours=2),
        away=_REGISTRY.resolve(115),
        home=_REGISTRY.resolve(119),
    )


def stamp(at: datetime) -> str:
    return at.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def pitch(*, at: datetime | None = None, index: int | None = None) -> dict[str, Any]:
    event: dict[str, Any] = {"isPitch": True, "type": "pitch", "details": {"type": {"code": "FF"}}}
    if at is not None:
        event["startTime"] = stamp(at)
    if index is not None:
        event["index"] = index
    return event


def action(
    event_type: str, *, at: datetime | None = None, index: int | None = None, review: Any = None
) -> dict[str, Any]:
    """A non-pitch event: a mound visit, a substitution, a steal."""
    event: dict[str, Any] = {"isPitch": False, "type": "action", "details": {"eventType": event_type}}
    if at is not None:
        event["startTime"] = stamp(at)
    if index is not None:
        event["index"] = index
    if review is not None:
        event["reviewDetails"] = review
    return event


def play(
    at_bat: int,
    *,
    inning: int = 5,
    half: str = "top",
    complete: bool = True,
    ended: datetime | None = None,
    events: tuple[dict[str, Any], ...] = (),
    review: Any = None,
) -> dict[str, Any]:
    about: dict[str, Any] = {"atBatIndex": at_bat, "inning": inning, "halfInning": half, "isComplete": complete}
    if ended is not None:
        about["endTime"] = stamp(ended)
    entry: dict[str, Any] = {"atBatIndex": at_bat, "about": about, "result": {}, "playEvents": list(events)}
    if review is not None:
        entry["reviewDetails"] = review
    return entry


def answer(
    *,
    inning: int = 5,
    inning_state: str = "Top",
    plays: tuple[dict[str, Any], ...] = (),
    status: str = "In Progress",
    as_of: datetime | None = T,
) -> dict[str, Any]:
    """A live-feed answer: the linescore at ``inning`` / ``inning_state``, and the play log, whose last entry is the
    current play."""
    raw: dict[str, Any] = {
        "gameData": {"status": {"detailedState": status}},
        "liveData": {
            "linescore": {
                "currentInning": inning,
                "inningState": inning_state,
                "teams": {"away": dict(_SIDE), "home": dict(_SIDE)},
            },
            "plays": {"allPlays": list(plays), "currentPlay": plays[-1] if plays else {}},
        },
    }
    if as_of is not None:
        raw["metaData"] = {"timeStamp": as_of.strftime("%Y%m%d_%H%M%S")}
    return raw


def provider(
    raw: dict[str, Any], *, inning_break_length: Any = None, context: ContextMetricsFetcher | None = None
) -> MlbStatsApiProvider:
    """A provider whose schedule lists the game (stating its inning break, if given), whose live feed is ``raw``,
    and whose context metrics come from ``context``."""
    row: dict[str, Any] = {
        "game_id": GAME_PK,
        "game_datetime": stamp(T - timedelta(hours=2)),
        "status": "In Progress",
        "away_id": 115,
        "home_id": 119,
    }
    if inning_break_length is not None:
        row["inning_break_length"] = inning_break_length

    def fetch_schedule(day: date, sport_ids: str) -> list[dict[str, Any]]:
        return [row]

    def fetch_game(game_pk: Any) -> dict[str, Any]:
        return raw

    mlb = MlbStatsApiProvider(_REGISTRY, fetch_schedule, fetch_game=fetch_game, fetch_context_metrics=context)
    mlb.fetch_schedule(T.date(), now=T)
    return mlb


def read(
    raw: dict[str, Any], *, inning_break_length: Any = None, status: GameStatus = GameStatus.LIVE
) -> LiveBaseballFeed:
    """``raw`` read through the provider, as the board reads a game's live feed."""
    return provider(raw, inning_break_length=inning_break_length).fetch_live_feed(game(status), now=T)
