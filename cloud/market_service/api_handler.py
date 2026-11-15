"""The function URL's Lambda entry point: real AWS clients around `handle_request`."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any

from cloud.market_service.api import handle_request
from cloud.market_service.store import MarketStore


@lru_cache(maxsize=1)
def _store() -> MarketStore:  # pragma: no cover - real AWS client
    import boto3

    return MarketStore(boto3.client("dynamodb"), os.environ["TABLE_NAME"])


def lambda_handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:  # pragma: no cover - real AWS client
    try:
        return handle_request(event, _store(), datetime.now(timezone.utc))
    except Exception as exc:  # the type alone: a message could echo back part of a request
        print(json.dumps({"error": type(exc).__name__}))
        return {
            "statusCode": 500,
            "headers": {"content-type": "application/json", "cache-control": "no-store"},
            "body": '{"error":"internal"}',
        }
