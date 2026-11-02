"""Check a Polymarket US key against the live market stream; prints no secrets.

    .venv/bin/python tools/polymarket_stream_check.py [--seconds 90] [--slug MARKET_SLUG]

Reads the key from the environment or a git-ignored ``.env``, connects, and watches up to three games
(live ones if there are any, from whichever league is playing). It reports whether the key was accepted,
what the server sends, and how often quotes change, beside the 30-second public REST quote.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))  # so `omni` resolves when this script is run directly, not just under pytest

from websockets.exceptions import InvalidStatus
from websockets.sync.client import connect

from omni.core.env import read_env_file
from omni.providers.polymarket_us_stream import (
    MARKET_STREAM_URL,
    MarketQuoteStream,
    PolymarketCredentials,
    signed_headers,
)

_GATEWAY = "https://gateway.polymarket.us"
_LEAGUES = ("mlb", "nfl", "nba", "nhl")
_MAX_MARKETS = 3
_SAMPLE_CHARS = 420
_CLIENT_CHECK_S = 5.0


def _get(path: str) -> dict[str, Any]:
    try:
        with urlopen(Request(_GATEWAY + path, headers={"User-Agent": "omni-scoreboard"}), timeout=15) as response:
            result: dict[str, Any] = json.load(response)
            return result
    except HTTPError:
        return {}


def _find_markets() -> list[str]:
    """Moneyline markets to watch: games in progress first, then upcoming ones."""
    live: list[str] = []
    upcoming: list[str] = []
    for league in _LEAGUES:
        for event in _get(f"/v2/leagues/{league}/events?limit=30").get("events", []):
            state = event.get("eventState") or {}
            for market in event.get("markets") or []:
                if market.get("marketType") != "moneyline" or market.get("closed"):
                    continue
                in_progress = state.get("live") and not state.get("ended")
                (live if in_progress else upcoming).append(market["slug"])
    return (live or upcoming)[:_MAX_MARKETS]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seconds", type=int, default=90)
    parser.add_argument("--slug", action="append", default=[])
    args = parser.parse_args()

    credentials = PolymarketCredentials.from_env({**read_env_file(_ROOT / ".env"), **os.environ})
    if credentials is None:
        print("No key found: set POLYMARKET_KEY_ID and POLYMARKET_SECRET_KEY in .env or the environment.")
        return 1
    slugs = args.slug or _find_markets()
    print(f"key id ending ...{credentials.key_id[-4:]}; watching {slugs}")
    if not slugs:
        print("No open markets found to watch.")
        return 1

    headers = signed_headers(credentials, timestamp_ms=int(time.time() * 1000))
    try:
        connection = connect(MARKET_STREAM_URL, additional_headers=headers, open_timeout=10)
    except InvalidStatus as exc:
        print(f"REFUSED: HTTP {exc.response.status_code} {bytes(exc.response.body or b'')[:160]!r}")
        return 1
    print("CONNECTED: the key was accepted")

    counts: dict[str, int] = {}
    samples: dict[str, str] = {}
    quotes: dict[str, tuple[object, object]] = {}
    change_times: dict[str, list[float]] = {slug: [] for slug in slugs}
    start = time.time()
    with connection:
        for slug in slugs:
            connection.send(
                json.dumps(
                    {
                        "subscribe": {
                            "requestId": f"check-{slug}",
                            "subscriptionType": "SUBSCRIPTION_TYPE_MARKET_DATA_LITE",
                            "marketSlugs": [slug],
                        }
                    }
                )
            )
        while time.time() - start < args.seconds:
            try:
                raw = connection.recv(timeout=1.0)
            except TimeoutError:
                continue
            message = json.loads(raw)
            kinds = [key for key in message if key not in ("requestId", "subscriptionType")] or ["(empty)"]
            kind = kinds[0]
            counts[kind] = counts.get(kind, 0) + 1
            samples.setdefault(kind, str(raw)[:_SAMPLE_CHARS])
            data = message.get("marketDataLite")
            if isinstance(data, dict):
                slug = data.get("marketSlug")
                quote = ((data.get("bestBid") or {}).get("value"), (data.get("bestAsk") or {}).get("value"))
                if slug in change_times and quotes.get(slug) != quote:
                    if slug in quotes:
                        change_times[slug].append(round(time.time() - start, 1))
                        print(f"{time.time() - start:6.1f}s {slug}: {quote[0]} / {quote[1]}", flush=True)
                    else:
                        print(f"{time.time() - start:6.1f}s {slug}: first quote {quote[0]} / {quote[1]}", flush=True)
                    quotes[slug] = quote

    print(f"\nmessage counts over {args.seconds}s: {counts}")
    for kind, sample in samples.items():
        print(f"sample {kind}: {sample}")
    for slug in slugs:
        times = change_times[slug]
        gaps = [round(b - a, 1) for a, b in zip(times, times[1:])]
        print(f"{slug}: first quote {'yes' if slug in quotes else 'NO'}, {len(times)} changes, gaps {gaps[:30]}")

    # The same handshake again, through the client the app actually runs.
    stream = MarketQuoteStream(credentials)
    for slug in slugs:
        stream.watch(slug)
    stream.start()
    time.sleep(_CLIENT_CHECK_S)
    stream.stop()
    print(f"\napp client after {_CLIENT_CHECK_S:.0f}s: {[stream.quote(slug) for slug in slugs]}")
    print(f"app client error: {stream.last_error}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
