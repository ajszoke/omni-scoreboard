"""Real-time quotes from Polymarket US's keyed market data WebSocket.

The exchange's public REST quotes sit behind a 30-second cache; this feed pushes a market's best bid
and ask as they change. It runs on its own thread, so the render loop never waits on the network: the
loop just reads whichever quote arrived last.

The feed only carries prices. Whether a market is open at all still comes from REST, so this only ever
sharpens a reading REST already vouches for. Losing the connection clears every quote, so nothing here
can go stale without the caller seeing that: it simply goes back to REST until the feed returns.
"""

from __future__ import annotations

import base64
import json
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import TracebackType
from typing import Protocol

from cryptography.hazmat.primitives.asymmetric import ed25519

from omni.providers.polymarket_us import MarketQuote, parse_price

__all__ = ["MARKET_STREAM_URL", "MarketQuoteStream", "PolymarketCredentials", "signed_headers"]

MARKET_STREAM_URL = "wss://api.polymarket.us/v1/ws/markets"
_WS_PATH = "/v1/ws/markets"
_KEY_ID_VAR = "POLYMARKET_KEY_ID"
_SECRET_KEY_VAR = "POLYMARKET_SECRET_KEY"
_ED25519_SEED_BYTES = 32  # the Base64 secret holds the seed first; only that part signs

_RECV_POLL_S = 1.0  # how long a read may block before checking on subscriptions and the stop flag
# A backstop only: the socket library's own pings catch a dead connection much sooner. The server's
# documented heartbeats didn't appear in a 90-second run, so a quiet market can't be read as a dead
# link on any short timescale; reconnecting after this long costs little, since quotes return at once.
_SILENCE_LIMIT_S = 300.0
_UNWATCH_AFTER_S = 600.0  # a market nobody has asked about in this long has probably finished
_MIN_BACKOFF_S = 1.0
_MAX_BACKOFF_S = 60.0


@dataclass(frozen=True, slots=True)
class PolymarketCredentials:
    """An API key; the secret stays out of ``repr`` so it can't reach a log by accident."""

    key_id: str
    secret_key: str = field(repr=False)

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> PolymarketCredentials | None:
        """The key in ``env``, or None unless both halves are set."""
        key_id, secret_key = env.get(_KEY_ID_VAR), env.get(_SECRET_KEY_VAR)
        return cls(key_id=key_id, secret_key=secret_key) if key_id and secret_key else None


def signed_headers(credentials: PolymarketCredentials, *, timestamp_ms: int) -> dict[str, str]:
    """The handshake's auth headers: an Ed25519 signature over the timestamp, method and path."""
    seed = base64.b64decode(credentials.secret_key)[:_ED25519_SEED_BYTES]
    signature = ed25519.Ed25519PrivateKey.from_private_bytes(seed).sign(f"{timestamp_ms}GET{_WS_PATH}".encode())
    return {
        "X-PM-Access-Key": credentials.key_id,
        "X-PM-Timestamp": str(timestamp_ms),
        "X-PM-Signature": base64.b64encode(signature).decode(),
    }


class _Connection(Protocol):
    def send(self, message: str) -> None: ...

    def recv(self, timeout: float | None = None) -> str | bytes: ...

    def __enter__(self) -> _Connection: ...

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None: ...


# How the stream opens a connection, given the signed headers for it.
Connector = Callable[[dict[str, str]], _Connection]


def _request_id(slug: str) -> str:
    return f"omni-{slug}"


class MarketQuoteStream:
    """Keeps one connection open on a background thread, holding the latest quote per watched market."""

    def __init__(
        self,
        credentials: PolymarketCredentials,
        *,
        connect: Connector | None = None,
        clock: Callable[[], float] = time.time,
        min_backoff_s: float = _MIN_BACKOFF_S,
    ) -> None:
        self._credentials = credentials
        self._connect = connect if connect is not None else _default_connect
        self._clock = clock
        self._min_backoff_s = min_backoff_s
        self._lock = threading.Lock()
        self._watched: dict[str, float] = {}  # market -> when it was last asked about
        self._quotes: dict[str, MarketQuote] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_error: str | None = None  # the latest connection failure, for a status line

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="polymarket-stream", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=_RECV_POLL_S + 1.0)

    def watch(self, slug: str) -> None:
        """Ask for quotes on a market, or keep asking; markets not asked about lapse on their own."""
        with self._lock:
            self._watched[slug] = self._clock()

    def quote(self, slug: str) -> MarketQuote | None:
        """The latest quote on a market, or None if there isn't one or the connection is down."""
        with self._lock:
            return self._quotes.get(slug)

    def _run(self) -> None:
        backoff = self._min_backoff_s

        def reset_backoff() -> None:
            nonlocal backoff
            backoff = self._min_backoff_s  # only a connection that actually heard something earns this

        while not self._stop.is_set():
            try:
                headers = signed_headers(self._credentials, timestamp_ms=int(self._clock() * 1000))
                with self._connect(headers) as connection:
                    self.last_error = None
                    self._serve(connection, on_healthy=reset_backoff)
            except Exception as exc:  # never let a network failure take the thread down
                self.last_error = f"{type(exc).__name__}: {exc}"
            with self._lock:
                self._quotes.clear()
            self._stop.wait(backoff)
            backoff = min(backoff * 2, _MAX_BACKOFF_S)

    def _serve(self, connection: _Connection, *, on_healthy: Callable[[], None]) -> None:
        """Run one connection until it dies or the stream stops."""
        subscribed: set[str] = set()
        last_heard = self._clock()
        while not self._stop.is_set():
            self._sync_subscriptions(connection, subscribed)
            try:
                raw = connection.recv(timeout=_RECV_POLL_S)
            except TimeoutError:
                if self._clock() - last_heard > _SILENCE_LIMIT_S:
                    raise ConnectionError(f"nothing heard for {_SILENCE_LIMIT_S:.0f}s") from None
                continue
            last_heard = self._clock()
            on_healthy()
            self._handle(raw)

    def _sync_subscriptions(self, connection: _Connection, subscribed: set[str]) -> None:
        """Bring the connection's subscriptions in line with what's being watched right now."""
        now = self._clock()
        with self._lock:
            lapsed = [slug for slug, seen in self._watched.items() if now - seen >= _UNWATCH_AFTER_S]
            for slug in lapsed:
                del self._watched[slug]
                self._quotes.pop(slug, None)
            wanted = set(self._watched)
        for slug in sorted(wanted - subscribed):
            connection.send(
                json.dumps(
                    {
                        "subscribe": {
                            "requestId": _request_id(slug),
                            "subscriptionType": "SUBSCRIPTION_TYPE_MARKET_DATA_LITE",
                            "marketSlugs": [slug],
                        }
                    }
                )
            )
            subscribed.add(slug)
        for slug in sorted(subscribed - wanted):
            connection.send(json.dumps({"unsubscribe": {"requestId": _request_id(slug)}}))
            subscribed.discard(slug)

    def _handle(self, raw: str | bytes) -> None:
        """Take in one message; anything but a market's quote (a heartbeat, say) needs nothing."""
        try:
            message = json.loads(raw)
        except ValueError:
            return
        data = message.get("marketDataLite") if isinstance(message, dict) else None
        if not isinstance(data, dict):
            return
        slug = data.get("marketSlug")
        if not isinstance(slug, str):
            return
        with self._lock:
            if slug in self._watched:
                self._quotes[slug] = MarketQuote(
                    bid=parse_price(data.get("bestBid")), ask=parse_price(data.get("bestAsk"))
                )


def _default_connect(headers: dict[str, str]) -> _Connection:  # pragma: no cover - real network
    from websockets.sync.client import connect

    return connect(MARKET_STREAM_URL, additional_headers=headers, open_timeout=10)
