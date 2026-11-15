"""The poller's Lambda entry point: real AWS clients and network sources around `run_poll`.

The market key is read once per container from SSM. With no key stored, the poller still runs on
Polymarket's public quotes, which are 30 seconds behind.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict
from datetime import datetime, timezone
from functools import lru_cache
from typing import TYPE_CHECKING, Any

from cloud.market_service.poller import ManagedStream, MarketSource, PollDependencies, run_poll
from cloud.market_service.slate import SCHEDULE_ZONE, Slate, combined_slate, mlb_slate, polymarket_slate
from cloud.market_service.store import MarketStore, ReadingArchive
from omni.core.enum import League
from omni.core.time import DurationSeconds
from omni.providers.bovada_odds import BovadaOddsProvider
from omni.providers.polymarket_us import PolymarketUsProvider, QuoteStream, fetch_market_data

if TYPE_CHECKING:
    from cloud.market_service.quote_stream import PolymarketCredentials

_FETCH_TIMEOUT_S = 4.0  # short enough that one slow market can't eat the run
_LINES_REFRESH = DurationSeconds(60)  # the poller's own beat decides when these are due


def _fetch(path: str) -> Any:  # pragma: no cover - real network
    return fetch_market_data(path, timeout_s=_FETCH_TIMEOUT_S)


def _slate(leagues: str) -> Slate:  # pragma: no cover - real network
    from omni.providers.mlb_statsapi import MlbStatsApiProvider
    from omni.providers.mlb_teams import MlbTeamRegistry

    slates = []
    for name in leagues.split(","):
        league = League(name.strip())
        if league is League.MLB:
            provider = MlbStatsApiProvider(MlbTeamRegistry.from_color_file(), schedule_timezone=SCHEDULE_ZONE)
            slates.append(mlb_slate(provider))
        else:
            slates.append(polymarket_slate(league, _fetch))
    return combined_slate(slates)


_credentials: PolymarketCredentials | None = None  # kept once found; a key stored later gets picked up


def _load_credentials() -> PolymarketCredentials | None:  # pragma: no cover - real AWS client
    global _credentials
    if _credentials is not None:
        return _credentials
    import boto3

    from cloud.market_service.quote_stream import PolymarketCredentials

    prefix = os.environ["POLYMARKET_KEY_PREFIX"]
    key_id_name, secret_key_name = f"{prefix}/key-id", f"{prefix}/secret-key"
    response = boto3.client("ssm").get_parameters(Names=[key_id_name, secret_key_name], WithDecryption=True)
    values = {parameter["Name"]: parameter["Value"] for parameter in response["Parameters"]}
    key_id, secret_key = values.get(key_id_name), values.get(secret_key_name)
    if key_id and secret_key:
        _credentials = PolymarketCredentials(key_id=key_id, secret_key=secret_key)
    return _credentials


def _open_stream() -> ManagedStream | None:  # pragma: no cover - real network
    credentials = _load_credentials()
    if credentials is None:
        return None
    from cloud.market_service.quote_stream import MarketQuoteStream

    return MarketQuoteStream(credentials)


def _markets(stream: QuoteStream | None) -> MarketSource:  # pragma: no cover - real network
    return PolymarketUsProvider(fetch=_fetch, stream=stream)


@lru_cache(maxsize=1)
def _dependencies() -> PollDependencies:  # pragma: no cover - real AWS client
    import boto3

    return PollDependencies(
        store=MarketStore(boto3.client("dynamodb"), os.environ["TABLE_NAME"]),
        archive=ReadingArchive(boto3.client("s3"), os.environ["ARCHIVE_BUCKET"]),
        slate=_slate(os.environ["SLATE_LEAGUES"]),
        markets=_markets,
        open_stream=_open_stream,
        lines=BovadaOddsProvider(refresh_every=_LINES_REFRESH) if os.environ.get("BOVADA_ENABLED") == "true" else None,
        clock=lambda: datetime.now(timezone.utc),
        sleep=time.sleep,
    )


def lambda_handler(_event: Any, context: Any) -> dict[str, Any]:  # pragma: no cover - real AWS client
    report = run_poll(_dependencies(), holder=context.aws_request_id)
    summary = {**asdict(report), "outcome": report.outcome.value}
    print(json.dumps(summary))
    return summary
