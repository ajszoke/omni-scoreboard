"""The DynamoDB table and the S3 archive; everything about their shapes stays in this module.

One table holds it all, since boards only ever ask for one thing at a time. A game's readings sit under
one key, newest last, so the latest is a one-row query however many have piled up. Rows expire after an
hour, and that's generous: boards only want the newest, and rows cost almost nothing to keep. DynamoDB
deletes expired rows within days rather than on the dot, so a read must check the expiry itself.

Each run's readings also go to S3 as gzipped JSON lines, under ``readings/``, to diagnose the poller by. They
expire a day after they're written (the bucket's lifecycle rule in ``cloud/template.yaml``): a board may show a
market's prices only from a copy that expires quickly (`omni.providers.rights`).
"""

from __future__ import annotations

import gzip
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol
from uuid import uuid4

from cloud.market_service.readings import Reading, ReadingKind

__all__ = ["DynamoClient", "MarketStore", "ReadingArchive", "S3Client", "StoredReading"]

_READING_TTL = timedelta(hours=1)
_ACTIVITY_PK = "fleet#activity"
_LEASE_PK = "poller#lease"
_SINGLETON_SK = {"N": "0"}  # rows that aren't a series still need a sort key
_MAX_BATCH = 25  # DynamoDB's limit on one batch write
_BATCH_ATTEMPTS = 3
_CONDITION_FAILED = "ConditionalCheckFailedException"


class DynamoClient(Protocol):
    """The low-level client calls this module makes."""

    def batch_write_item(self, **kwargs: Any) -> dict[str, Any]: ...

    def delete_item(self, **kwargs: Any) -> dict[str, Any]: ...

    def get_item(self, **kwargs: Any) -> dict[str, Any]: ...

    def put_item(self, **kwargs: Any) -> dict[str, Any]: ...

    def query(self, **kwargs: Any) -> dict[str, Any]: ...

    def update_item(self, **kwargs: Any) -> dict[str, Any]: ...


class S3Client(Protocol):
    def put_object(self, **kwargs: Any) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class StoredReading:
    """A reading as a board gets it back: when it was taken, and what it said."""

    observed_at: datetime
    body: dict[str, Any]


def _epoch_ms(moment: datetime) -> int:
    return round(moment.timestamp() * 1000)


def _from_epoch_ms(raw: str) -> datetime:
    return datetime.fromtimestamp(int(raw) / 1000, tz=timezone.utc)


def _reading_pk(game_id: str, kind: ReadingKind) -> str:
    return f"reading#{game_id}#{kind.value}"


def _board_pk(board_id: str) -> str:
    return f"board#{board_id}"


def _is_condition_failure(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    return isinstance(response, dict) and response.get("Error", {}).get("Code") == _CONDITION_FAILED


class MarketStore:
    """Readings, the fleet's activity, the poller's lease, and the boards."""

    def __init__(self, client: DynamoClient, table: str) -> None:
        self._client = client
        self._table = table

    def _item(self, reading: Reading) -> dict[str, Any]:
        return {
            "pk": {"S": _reading_pk(reading.game_id, reading.kind)},
            "sk": {"N": str(_epoch_ms(reading.observed_at))},
            "expires_at": {"N": str(int((reading.observed_at + _READING_TTL).timestamp()))},
            "body": {"S": json.dumps(reading.body, separators=(",", ":"))},
        }

    def put_readings(self, readings: Sequence[Reading]) -> None:
        """Store readings, retrying any the table sends back. Raises `RuntimeError` if some never land."""
        for start in range(0, len(readings), _MAX_BATCH):
            requests = [{"PutRequest": {"Item": self._item(r)}} for r in readings[start : start + _MAX_BATCH]]
            for _ in range(_BATCH_ATTEMPTS):
                response = self._client.batch_write_item(RequestItems={self._table: requests})
                requests = response.get("UnprocessedItems", {}).get(self._table, [])
                if not requests:
                    break
            else:
                raise RuntimeError(f"{len(requests)} readings were never accepted")

    def latest(self, game_id: str, kind: ReadingKind, now: datetime) -> StoredReading | None:
        """The newest reading of this kind for a game, unless it's past its expiry."""
        response = self._client.query(
            TableName=self._table,
            KeyConditionExpression="pk = :pk",
            ExpressionAttributeValues={":pk": {"S": _reading_pk(game_id, kind)}},
            ScanIndexForward=False,
            Limit=1,
        )
        items = response.get("Items") or []
        if not items or int(items[0]["expires_at"]["N"]) <= now.timestamp():
            return None
        return StoredReading(observed_at=_from_epoch_ms(items[0]["sk"]["N"]), body=json.loads(items[0]["body"]["S"]))

    def mark_active(self, until: datetime) -> None:
        """Note that a board wants data until ``until``."""
        self._client.update_item(
            TableName=self._table,
            Key={"pk": {"S": _ACTIVITY_PK}, "sk": _SINGLETON_SK},
            UpdateExpression="SET active_until = :until",
            ExpressionAttributeValues={":until": {"N": str(_epoch_ms(until))}},
        )

    def active_until(self) -> datetime | None:
        """How long the latest heartbeat asked for data, or None if no board has ever asked."""
        response = self._client.get_item(TableName=self._table, Key={"pk": {"S": _ACTIVITY_PK}, "sk": _SINGLETON_SK})
        item = response.get("Item")
        return _from_epoch_ms(item["active_until"]["N"]) if item else None

    def acquire_lease(self, holder: str, *, now: datetime, until: datetime) -> bool:
        """Claim the poller's lease unless another run still holds it.

        This is what keeps runs from overlapping. A run that dies holding it blocks nobody for long,
        since the lease lapses on its own.
        """
        try:
            self._client.put_item(
                TableName=self._table,
                Item={
                    "pk": {"S": _LEASE_PK},
                    "sk": _SINGLETON_SK,
                    "holder": {"S": holder},
                    "lease_until": {"N": str(_epoch_ms(until))},
                },
                ConditionExpression="attribute_not_exists(pk) OR lease_until <= :now",
                ExpressionAttributeValues={":now": {"N": str(_epoch_ms(now))}},
            )
        except Exception as exc:
            if _is_condition_failure(exc):
                return False
            raise
        return True

    def release_lease(self, holder: str) -> None:
        """Give the lease back, so the next run needn't wait out its time. Someone else's is left alone."""
        try:
            self._client.delete_item(
                TableName=self._table,
                Key={"pk": {"S": _LEASE_PK}, "sk": _SINGLETON_SK},
                ConditionExpression="holder = :holder",
                ExpressionAttributeValues={":holder": {"S": holder}},
            )
        except Exception as exc:
            if not _is_condition_failure(exc):
                raise

    def board_digest(self, board_id: str) -> str | None:
        """A board's stored token hash, or None if it has no row (never issued, or revoked)."""
        response = self._client.get_item(
            TableName=self._table, Key={"pk": {"S": _board_pk(board_id)}, "sk": _SINGLETON_SK}
        )
        item = response.get("Item")
        return item["token_sha256"]["S"] if item else None

    def touch_board(self, board_id: str, *, now: datetime, version: str | None) -> None:
        """Record that a board was just heard from. A board revoked a moment ago stays gone."""
        try:
            self._client.update_item(
                TableName=self._table,
                Key={"pk": {"S": _board_pk(board_id)}, "sk": _SINGLETON_SK},
                UpdateExpression="SET last_seen = :now, app_version = :version",
                ConditionExpression="attribute_exists(pk)",
                ExpressionAttributeValues={":now": {"N": str(_epoch_ms(now))}, ":version": {"S": version or "unknown"}},
            )
        except Exception as exc:
            if not _is_condition_failure(exc):
                raise


class ReadingArchive:
    """Writes each run's readings to S3 as one gzipped JSON-lines file, filed by date and hour."""

    def __init__(self, client: S3Client, bucket: str, *, suffix: Callable[[], str] = lambda: uuid4().hex[:12]) -> None:
        self._client = client
        self._bucket = bucket
        self._suffix = suffix

    def write(self, readings: Sequence[Reading], now: datetime) -> str | None:
        """Archive readings and return the key, or None if there was nothing to archive."""
        if not readings:
            return None
        stamp = now.astimezone(timezone.utc)
        key = f"readings/dt={stamp:%Y-%m-%d}/hour={stamp:%H}/{stamp:%Y%m%dT%H%M%SZ}-{self._suffix()}.jsonl.gz"
        body = gzip.compress("".join(f"{r.archive_line()}\n" for r in readings).encode())
        self._client.put_object(Bucket=self._bucket, Key=key, Body=body, ContentType="application/gzip")
        return key
