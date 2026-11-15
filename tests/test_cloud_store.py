"""The table and the archive, against a fake client that behaves like DynamoDB where it matters."""

from __future__ import annotations

import gzip
import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from cloud.market_service.readings import Reading, ReadingKind
from cloud.market_service.store import MarketStore, ReadingArchive

NOW = datetime(2026, 9, 17, 2, 0, 0, tzinfo=timezone.utc)
TABLE = "omni-table"


class _ConditionFailed(Exception):
    response = {"Error": {"Code": "ConditionalCheckFailedException"}}


class _Throttled(Exception):
    response = {"Error": {"Code": "ProvisionedThroughputExceededException"}}


class _FakeDynamo:
    """Holds items by key, and fails conditions the way the real client does."""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}
        self.batches: list[int] = []
        self.send_back: list[int] = []  # how many items each batch write hands back unprocessed
        self.fail_next: Exception | None = None
        self.lease_blocked = False

    def _raise_if_told(self) -> None:
        if self.fail_next is not None:
            exc, self.fail_next = self.fail_next, None
            raise exc

    def batch_write_item(self, **kwargs: Any) -> dict[str, Any]:
        requests = kwargs["RequestItems"][TABLE]
        self.batches.append(len(requests))
        hand_back = self.send_back.pop(0) if self.send_back else 0
        kept = requests[hand_back:]
        for request in kept:
            item = request["PutRequest"]["Item"]
            self.items[(item["pk"]["S"], item["sk"]["N"])] = item
        return {"UnprocessedItems": {TABLE: requests[:hand_back]} if hand_back else {}}

    def query(self, **kwargs: Any) -> dict[str, Any]:
        assert kwargs["ScanIndexForward"] is False and kwargs["Limit"] == 1
        pk = kwargs["ExpressionAttributeValues"][":pk"]["S"]
        matches = [item for (item_pk, _), item in self.items.items() if item_pk == pk]
        matches.sort(key=lambda item: int(item["sk"]["N"]), reverse=True)
        return {"Items": matches[:1]}

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        item = self.items.get((kwargs["Key"]["pk"]["S"], kwargs["Key"]["sk"]["N"]))
        return {"Item": item} if item else {}

    def update_item(self, **kwargs: Any) -> dict[str, Any]:
        self._raise_if_told()
        key = (kwargs["Key"]["pk"]["S"], kwargs["Key"]["sk"]["N"])
        if "ConditionExpression" in kwargs and key not in self.items:
            raise _ConditionFailed
        item = self.items.setdefault(key, {"pk": kwargs["Key"]["pk"], "sk": kwargs["Key"]["sk"]})
        expression = kwargs["UpdateExpression"].removeprefix("SET ")
        for part in expression.split(", "):
            name, placeholder = part.split(" = ")
            item[name] = kwargs["ExpressionAttributeValues"][placeholder]
        return {}

    def put_item(self, **kwargs: Any) -> dict[str, Any]:
        self._raise_if_told()
        key = (kwargs["Item"]["pk"]["S"], kwargs["Item"]["sk"]["N"])
        existing = self.items.get(key)
        if existing and int(existing["lease_until"]["N"]) > int(kwargs["ExpressionAttributeValues"][":now"]["N"]):
            raise _ConditionFailed
        self.items[key] = kwargs["Item"]
        return {}

    def delete_item(self, **kwargs: Any) -> dict[str, Any]:
        self._raise_if_told()
        key = (kwargs["Key"]["pk"]["S"], kwargs["Key"]["sk"]["N"])
        item = self.items.get(key)
        if item is None or item["holder"] != kwargs["ExpressionAttributeValues"][":holder"]:
            raise _ConditionFailed
        del self.items[key]
        return {}


def _reading(game: str = "mlb:s:1", at: datetime = NOW, *, kind: ReadingKind = ReadingKind.WIN) -> Reading:
    return Reading(game_id=game, kind=kind, observed_at=at, body={"away_pct": 55.0})


@pytest.fixture
def dynamo() -> _FakeDynamo:
    return _FakeDynamo()


@pytest.fixture
def store(dynamo: _FakeDynamo) -> MarketStore:
    return MarketStore(dynamo, TABLE)


# --- readings ------------------------------------------------------------------------------------


def test_the_latest_is_the_newest_reading_of_that_kind_for_that_game(store: MarketStore) -> None:
    store.put_readings(
        [
            _reading(at=NOW),
            _reading(at=NOW + timedelta(seconds=10)),
            _reading("mlb:s:2", at=NOW + timedelta(seconds=20)),
            _reading(at=NOW + timedelta(seconds=30), kind=ReadingKind.LINES),
        ]
    )
    latest = store.latest("mlb:s:1", ReadingKind.WIN, NOW + timedelta(seconds=40))
    assert latest is not None
    assert latest.observed_at == NOW + timedelta(seconds=10)
    assert latest.body == {"away_pct": 55.0}


def test_a_game_with_no_readings_has_no_latest(store: MarketStore) -> None:
    assert store.latest("mlb:s:1", ReadingKind.WIN, NOW) is None


def test_an_expired_row_the_table_hasnt_deleted_yet_is_not_served(store: MarketStore) -> None:
    store.put_readings([_reading()])
    assert store.latest("mlb:s:1", ReadingKind.WIN, NOW + timedelta(minutes=59)) is not None
    assert store.latest("mlb:s:1", ReadingKind.WIN, NOW + timedelta(hours=1)) is None


def test_a_rows_expiry_is_an_hour_on_in_epoch_seconds(store: MarketStore, dynamo: _FakeDynamo) -> None:
    store.put_readings([_reading()])
    item = next(iter(dynamo.items.values()))
    assert int(item["expires_at"]["N"]) == int(NOW.timestamp()) + 3600


def test_large_batches_are_split_to_the_tables_limit(store: MarketStore, dynamo: _FakeDynamo) -> None:
    store.put_readings([_reading(at=NOW + timedelta(seconds=i)) for i in range(60)])
    assert dynamo.batches == [25, 25, 10]


def test_readings_the_table_hands_back_are_tried_again(store: MarketStore, dynamo: _FakeDynamo) -> None:
    dynamo.send_back = [2]
    store.put_readings([_reading(at=NOW + timedelta(seconds=i)) for i in range(3)])
    assert dynamo.batches == [3, 2]
    assert len(dynamo.items) == 3


def test_readings_that_never_land_are_an_error(store: MarketStore, dynamo: _FakeDynamo) -> None:
    dynamo.send_back = [1, 1, 1]
    with pytest.raises(RuntimeError, match="1 readings"):
        store.put_readings([_reading()])


def test_no_readings_is_no_write(store: MarketStore, dynamo: _FakeDynamo) -> None:
    store.put_readings([])
    assert dynamo.batches == []


# --- activity ------------------------------------------------------------------------------------


def test_activity_starts_unknown_and_holds_the_latest_heartbeat(store: MarketStore) -> None:
    assert store.active_until() is None
    store.mark_active(NOW)
    store.mark_active(NOW + timedelta(minutes=5))
    assert store.active_until() == NOW + timedelta(minutes=5)


# --- the lease -----------------------------------------------------------------------------------


def test_only_one_run_holds_the_lease(store: MarketStore) -> None:
    until = NOW + timedelta(seconds=58)
    assert store.acquire_lease("a", now=NOW, until=until)
    assert not store.acquire_lease("b", now=NOW + timedelta(seconds=30), until=until)


def test_a_dead_runs_lease_lapses_on_its_own(store: MarketStore) -> None:
    assert store.acquire_lease("a", now=NOW, until=NOW + timedelta(seconds=58))
    assert store.acquire_lease("b", now=NOW + timedelta(seconds=58), until=NOW + timedelta(seconds=116))


def test_a_released_lease_is_free_at_once(store: MarketStore) -> None:
    store.acquire_lease("a", now=NOW, until=NOW + timedelta(seconds=58))
    store.release_lease("a")
    assert store.acquire_lease("b", now=NOW + timedelta(seconds=1), until=NOW + timedelta(seconds=59))


def test_releasing_leaves_another_runs_lease_alone(store: MarketStore) -> None:
    store.acquire_lease("a", now=NOW, until=NOW + timedelta(seconds=58))
    store.release_lease("b")
    assert not store.acquire_lease("c", now=NOW + timedelta(seconds=1), until=NOW + timedelta(seconds=59))


def test_a_real_failure_taking_or_releasing_the_lease_is_not_swallowed(store: MarketStore, dynamo: _FakeDynamo) -> None:
    dynamo.fail_next = _Throttled()
    with pytest.raises(_Throttled):
        store.acquire_lease("a", now=NOW, until=NOW)
    dynamo.fail_next = _Throttled()
    with pytest.raises(_Throttled):
        store.release_lease("a")


def test_an_error_with_no_aws_shape_is_not_taken_for_a_failed_condition(
    store: MarketStore, dynamo: _FakeDynamo
) -> None:
    dynamo.fail_next = ConnectionError("network")
    with pytest.raises(ConnectionError):
        store.acquire_lease("a", now=NOW, until=NOW)


# --- boards --------------------------------------------------------------------------------------


def _add_board(dynamo: _FakeDynamo, board: str = "den", digest: str = "abc") -> None:
    dynamo.items[(f"board#{board}", "0")] = {
        "pk": {"S": f"board#{board}"},
        "sk": {"N": "0"},
        "token_sha256": {"S": digest},
    }


def test_a_boards_stored_hash_is_found(store: MarketStore, dynamo: _FakeDynamo) -> None:
    _add_board(dynamo)
    assert store.board_digest("den") == "abc"
    assert store.board_digest("elsewhere") is None


def test_touching_a_board_records_when_and_what_it_runs(store: MarketStore, dynamo: _FakeDynamo) -> None:
    _add_board(dynamo)
    store.touch_board("den", now=NOW, version="1.2")
    store_item = dynamo.items[("board#den", "0")]
    assert store_item["app_version"] == {"S": "1.2"}
    assert int(store_item["last_seen"]["N"]) == round(NOW.timestamp() * 1000)
    store.touch_board("den", now=NOW, version=None)
    assert store_item["app_version"] == {"S": "unknown"}


def test_touching_a_revoked_board_doesnt_bring_it_back(store: MarketStore, dynamo: _FakeDynamo) -> None:
    store.touch_board("gone", now=NOW, version=None)
    assert ("board#gone", "0") not in dynamo.items


def test_a_real_failure_touching_a_board_is_not_swallowed(store: MarketStore, dynamo: _FakeDynamo) -> None:
    dynamo.fail_next = _Throttled()
    with pytest.raises(_Throttled):
        store.touch_board("den", now=NOW, version=None)


# --- the archive ---------------------------------------------------------------------------------


class _FakeS3:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put_object(self, **kwargs: Any) -> dict[str, Any]:
        assert kwargs["Bucket"] == "omni-archive"
        self.objects[kwargs["Key"]] = kwargs["Body"]
        return {}


def test_a_runs_readings_are_filed_as_one_gzipped_file_of_json_lines() -> None:
    s3 = _FakeS3()
    archive = ReadingArchive(s3, "omni-archive", suffix=lambda: "abc")
    key = archive.write([_reading(), _reading("mlb:s:2")], NOW)
    assert key == "readings/dt=2026-09-17/hour=02/20260917T020000Z-abc.jsonl.gz"
    lines = gzip.decompress(s3.objects[key]).decode().splitlines()
    assert [json.loads(line)["game_id"] for line in lines] == ["mlb:s:1", "mlb:s:2"]
    assert json.loads(lines[0])["v"] == 1


def test_no_readings_writes_no_file() -> None:
    s3 = _FakeS3()
    assert ReadingArchive(s3, "omni-archive").write([], NOW) is None
    assert s3.objects == {}


def test_two_runs_in_one_second_dont_overwrite_each_other() -> None:
    s3 = _FakeS3()
    archive = ReadingArchive(s3, "omni-archive")
    archive.write([_reading()], NOW)
    archive.write([_reading()], NOW)
    assert len(s3.objects) == 2
