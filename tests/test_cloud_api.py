"""The board-facing API, end to end over a fake table."""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from cloud.market_service.api import handle_request
from cloud.market_service.auth import issue_credential
from cloud.market_service.readings import Reading, ReadingKind
from cloud.market_service.store import MarketStore
from tests.test_cloud_store import TABLE, _FakeDynamo

NOW = datetime(2026, 9, 17, 2, 0, tzinfo=timezone.utc)
GAME = "mlb:mlb_statsapi:745123"


@pytest.fixture
def dynamo() -> _FakeDynamo:
    return _FakeDynamo()


@pytest.fixture
def store(dynamo: _FakeDynamo) -> MarketStore:
    return MarketStore(dynamo, TABLE)


@pytest.fixture
def token(dynamo: _FakeDynamo) -> str:
    credential = issue_credential("den")
    dynamo.items[("board#den", "0")] = {
        "pk": {"S": "board#den"},
        "sk": {"N": "0"},
        "token_sha256": {"S": credential.digest},
    }
    return credential.token


def _event(
    method: str,
    path: str,
    token: str | None = None,
    *,
    query: dict[str, str] | None = None,
    body: str | None = None,
    encoded: bool = False,
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "rawPath": path,
        "headers": {"authorization": f"Bearer {token}"} if token else {},
        "requestContext": {"http": {"method": method}},
    }
    if query is not None:
        event["queryStringParameters"] = query
    if body is not None:
        event["body"] = base64.b64encode(body.encode()).decode() if encoded else body
        event["isBase64Encoded"] = encoded
    return event


def _call(event: dict[str, Any], store: MarketStore) -> tuple[int, dict[str, Any]]:
    response = handle_request(event, store, NOW)
    assert response["headers"]["cache-control"] == "no-store"
    return response["statusCode"], json.loads(response["body"])


# --- auth ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "event",
    [
        _event("GET", "/v1/latest", query={"games": GAME}),
        _event("POST", "/v1/heartbeat"),
        _event("GET", "/anything"),
        _event("POST", "/v1/heartbeat", "den.wrong"),
    ],
)
def test_nothing_is_answered_without_a_valid_token(event: dict[str, Any], store: MarketStore, token: str) -> None:
    status, body = _call(event, store)
    assert status == 401 and body == {"error": "unauthorized"}
    assert store.active_until() is None


def test_an_event_with_no_headers_at_all_is_turned_away(store: MarketStore) -> None:
    assert handle_request({}, store, NOW)["statusCode"] == 401


@pytest.mark.parametrize("method, path", [("GET", "/v1/heartbeat"), ("POST", "/v1/latest"), ("GET", "/nope")])
def test_anything_else_is_not_found(method: str, path: str, store: MarketStore, token: str) -> None:
    assert _call(_event(method, path, token), store)[0] == 404


def test_a_request_missing_its_http_context_is_not_found(store: MarketStore, token: str) -> None:
    event = {"headers": {"authorization": f"Bearer {token}"}}
    assert handle_request(event, store, NOW)["statusCode"] == 404


# --- heartbeat -----------------------------------------------------------------------------------


def test_a_heartbeat_keeps_the_poller_going_for_ten_minutes(store: MarketStore, token: str) -> None:
    status, body = _call(_event("POST", "/v1/heartbeat", token), store)
    assert status == 200
    assert body == {"active_until": "2026-09-17T02:10:00.000Z"}
    assert store.active_until() == NOW + timedelta(minutes=10)


def test_a_heartbeat_records_the_board_and_its_version(store: MarketStore, dynamo: _FakeDynamo, token: str) -> None:
    _call(_event("POST", "/v1/heartbeat", token, body='{"version": "1.2.3"}'), store)
    item = dynamo.items[("board#den", "0")]
    assert item["app_version"] == {"S": "1.2.3"}
    assert int(item["last_seen"]["N"]) == round(NOW.timestamp() * 1000)


def test_an_encoded_heartbeat_body_is_read(store: MarketStore, dynamo: _FakeDynamo, token: str) -> None:
    _call(_event("POST", "/v1/heartbeat", token, body='{"version": "9"}', encoded=True), store)
    assert dynamo.items[("board#den", "0")]["app_version"] == {"S": "9"}


def test_a_long_version_is_cut_short(store: MarketStore, dynamo: _FakeDynamo, token: str) -> None:
    _call(_event("POST", "/v1/heartbeat", token, body=json.dumps({"version": "v" * 500})), store)
    assert dynamo.items[("board#den", "0")]["app_version"] == {"S": "v" * 40}


@pytest.mark.parametrize("body", ["not json", "[1, 2]", '{"version": 7}', '{"version": ""}', ""])
def test_a_heartbeat_with_a_useless_body_still_counts(
    body: str, store: MarketStore, dynamo: _FakeDynamo, token: str
) -> None:
    assert _call(_event("POST", "/v1/heartbeat", token, body=body), store)[0] == 200
    assert dynamo.items[("board#den", "0")]["app_version"] == {"S": "unknown"}
    assert store.active_until() == NOW + timedelta(minutes=10)


def test_a_heartbeat_with_a_body_that_wont_decode_still_counts(store: MarketStore, token: str) -> None:
    event = _event("POST", "/v1/heartbeat", token, body="a")  # not valid Base64
    event["isBase64Encoded"] = True
    assert _call(event, store)[0] == 200


# --- latest --------------------------------------------------------------------------------------


def test_the_latest_values_come_back_for_each_game_with_when_they_were_taken(store: MarketStore, token: str) -> None:
    store.put_readings(
        [
            Reading(
                game_id=GAME, kind=ReadingKind.WIN, observed_at=NOW - timedelta(seconds=25), body={"away_pct": 40.0}
            ),
            Reading(
                game_id=GAME, kind=ReadingKind.WIN, observed_at=NOW - timedelta(seconds=5), body={"away_pct": 41.0}
            ),
            Reading(game_id=GAME, kind=ReadingKind.LINES, observed_at=NOW - timedelta(minutes=4), body={"lines": {}}),
        ]
    )
    status, body = _call(_event("GET", "/v1/latest", token, query={"games": f"{GAME},mlb:mlb_statsapi:9"}), store)
    assert status == 200
    assert body["server_time"] == "2026-09-17T02:00:00.000Z"
    assert body["games"][GAME]["win"] == {"observed_at": "2026-09-17T01:59:55.000Z", "away_pct": 41.0}
    assert body["games"][GAME]["lines"] == {"observed_at": "2026-09-17T01:56:00.000Z", "lines": {}}
    assert body["games"]["mlb:mlb_statsapi:9"] == {"win": None, "lines": None}


def test_a_frozen_poller_shows_as_an_old_timestamp_not_a_missing_row(store: MarketStore, token: str) -> None:
    store.put_readings([Reading(game_id=GAME, kind=ReadingKind.WIN, observed_at=NOW - timedelta(minutes=30), body={})])
    _, body = _call(_event("GET", "/v1/latest", token, query={"games": GAME}), store)
    assert body["games"][GAME]["win"]["observed_at"] == "2026-09-17T01:30:00.000Z"


@pytest.mark.parametrize(
    "query",
    [
        None,
        {},
        {"games": ""},
        {"games": ",,"},
        {"games": "has space"},
        {"games": "a#b"},
        {"games": "x" * 97},
        {"games": ",".join(f"g{i}" for i in range(31))},
    ],
)
def test_a_bad_game_list_is_refused_before_any_read(
    query: dict[str, str] | None, store: MarketStore, token: str
) -> None:
    status, body = _call(_event("GET", "/v1/latest", token, query=query), store)
    assert status == 400 and "games" not in body
