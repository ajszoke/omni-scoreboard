"""What boards call: a heartbeat to say one is on, and the latest stored values for its games.

This runs behind a public Lambda function URL (payload format 2.0), so every route checks a board's
token before doing anything else. Raw JSON stays at this boundary.
"""

from __future__ import annotations

import base64
import json
import re
from datetime import datetime, timedelta
from typing import Any

from cloud.market_service.auth import authenticated_board
from cloud.market_service.readings import ReadingKind, format_instant
from cloud.market_service.store import MarketStore

__all__ = ["ACTIVE_AFTER_HEARTBEAT", "handle_request"]

ACTIVE_AFTER_HEARTBEAT = timedelta(minutes=10)
_HEARTBEAT = "/v1/heartbeat"
_LATEST = "/v1/latest"
_GAME_ID = re.compile(r"^[A-Za-z0-9:_.-]{1,96}$")
_MAX_GAMES = 30  # more than a full slate; each one costs two reads
_MAX_VERSION_LENGTH = 40


def _respond(status: int, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json", "cache-control": "no-store"},
        "body": json.dumps(payload, separators=(",", ":")),
    }


def _reported_version(event: dict[str, Any]) -> str | None:
    """The app version a heartbeat reports, if it reports a sensible one; it's only ever a note."""
    raw = event.get("body") or ""
    try:
        if event.get("isBase64Encoded"):
            raw = base64.b64decode(raw).decode()
        body = json.loads(raw) if raw else {}
    except ValueError:
        return None
    version = body.get("version") if isinstance(body, dict) else None
    return version[:_MAX_VERSION_LENGTH] if isinstance(version, str) and version else None


def _heartbeat(event: dict[str, Any], board_id: str, store: MarketStore, now: datetime) -> dict[str, Any]:
    until = now + ACTIVE_AFTER_HEARTBEAT
    store.mark_active(until)
    store.touch_board(board_id, now=now, version=_reported_version(event))
    return _respond(200, {"active_until": format_instant(until)})


def _latest(event: dict[str, Any], store: MarketStore, now: datetime) -> dict[str, Any]:
    raw_games = (event.get("queryStringParameters") or {}).get("games", "")
    game_ids = [game for game in raw_games.split(",") if game]
    if not game_ids or len(game_ids) > _MAX_GAMES or not all(_GAME_ID.match(game) for game in game_ids):
        return _respond(400, {"error": f"games must list 1-{_MAX_GAMES} game ids"})
    games: dict[str, dict[str, Any]] = {}
    for game_id in game_ids:
        games[game_id] = {}
        for kind in ReadingKind:
            stored = store.latest(game_id, kind, now)
            games[game_id][kind.value] = (
                {"observed_at": format_instant(stored.observed_at), **stored.body} if stored is not None else None
            )
    return _respond(200, {"server_time": format_instant(now), "games": games})


def handle_request(event: dict[str, Any], store: MarketStore, now: datetime) -> dict[str, Any]:
    """Answer one function-URL request."""
    headers = event.get("headers") or {}
    board_id = authenticated_board(headers.get("authorization"), store.board_digest)
    if board_id is None:
        return _respond(401, {"error": "unauthorized"})
    http = (event.get("requestContext") or {}).get("http") or {}
    route = (http.get("method"), event.get("rawPath"))
    if route == ("POST", _HEARTBEAT):
        return _heartbeat(event, board_id, store, now)
    if route == ("GET", _LATEST):
        return _latest(event, store, now)
    return _respond(404, {"error": "not found"})
