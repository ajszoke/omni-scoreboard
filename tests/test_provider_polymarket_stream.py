"""The keyed market stream: signing, subscriptions, quotes, and riding out a bad connection.

Nothing here touches the network: a scripted connection stands in for the socket.
"""

from __future__ import annotations

import base64
import json
import threading
from datetime import datetime, timedelta, timezone
from types import TracebackType
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519

from omni.core.enum import GameStatus, League
from omni.core.ids import LeagueScopedId, SourceRef
from omni.domain.contest import TeamGame
from omni.providers.mlb_teams import MlbTeamRegistry
from omni.providers.polymarket_us import MarketQuote, PolymarketUsProvider
from omni.providers.polymarket_us_stream import (
    MarketQuoteStream,
    PolymarketCredentials,
    signed_headers,
)

# RFC 8032's first Ed25519 test vector, so signing is checked against something independent of this code.
RFC_SEED = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
RFC_PUBLIC = bytes.fromhex("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
CREDENTIALS = PolymarketCredentials(key_id="key-1", secret_key=base64.b64encode(RFC_SEED + RFC_PUBLIC).decode())
SLUG = "aec-mlb-col-lad-2026-09-16"


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


class _Connection:
    """Hands out a script of messages; an Exception in the script is raised, and running dry stops the stream."""

    def __init__(self, stream_ref: list[MarketQuoteStream], script: list[Any]) -> None:
        self.stream_ref = stream_ref
        self.script = list(script)
        self.sent: list[dict[str, Any]] = []

    def send(self, message: str) -> None:
        self.sent.append(json.loads(message))

    def recv(self, timeout: float | None = None) -> str | bytes:
        if not self.script:
            self.stream_ref[0]._stop.set()
            raise TimeoutError
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item if isinstance(item, (str, bytes)) else json.dumps(item)

    def __enter__(self) -> _Connection:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        return None


def _quote_message(slug: str = SLUG, bid: str | None = "0.54", ask: str | None = "0.56") -> dict[str, Any]:
    data: dict[str, Any] = {"marketSlug": slug}
    if bid is not None:
        data["bestBid"] = {"value": bid, "currency": "USD"}
    if ask is not None:
        data["bestAsk"] = {"value": ask, "currency": "USD"}
    return {
        "requestId": f"omni-{slug}",
        "subscriptionType": "SUBSCRIPTION_TYPE_MARKET_DATA_LITE",
        "marketDataLite": data,
    }


def _serve(
    script: list[Any], *, watch: str | tuple[str, ...] = SLUG, clock: _Clock | None = None
) -> tuple[MarketQuoteStream, _Connection]:
    ref: list[MarketQuoteStream] = []
    connection = _Connection(ref, script)
    stream = MarketQuoteStream(CREDENTIALS, connect=lambda _headers: connection, clock=clock or _Clock())
    ref.append(stream)
    for slug in (watch,) if isinstance(watch, str) else watch:
        stream.watch(slug)
    stream._serve(connection, on_healthy=lambda: None)
    return stream, connection


# --- signing ---------------------------------------------------------------------------------


def test_the_handshake_signs_the_timestamp_method_and_path_with_the_seed() -> None:
    headers = signed_headers(CREDENTIALS, timestamp_ms=1_700_000_000_000)
    assert headers["X-PM-Access-Key"] == "key-1"
    assert headers["X-PM-Timestamp"] == "1700000000000"
    public = ed25519.Ed25519PublicKey.from_public_bytes(RFC_PUBLIC)
    # Raises if the signature isn't this key's over exactly this string.
    public.verify(base64.b64decode(headers["X-PM-Signature"]), b"1700000000000GET/v1/ws/markets")


def test_a_secret_holding_only_the_seed_signs_the_same() -> None:
    seed_only = PolymarketCredentials(key_id="key-1", secret_key=base64.b64encode(RFC_SEED).decode())
    assert signed_headers(seed_only, timestamp_ms=1) == signed_headers(CREDENTIALS, timestamp_ms=1)


def test_the_secret_stays_out_of_repr() -> None:
    assert CREDENTIALS.secret_key not in repr(CREDENTIALS)
    assert "key-1" in repr(CREDENTIALS)


@pytest.mark.parametrize(
    "env, found",
    [
        ({"POLYMARKET_KEY_ID": "a", "POLYMARKET_SECRET_KEY": "b"}, True),
        ({"POLYMARKET_KEY_ID": "a"}, False),
        ({"POLYMARKET_SECRET_KEY": "b"}, False),
        ({"POLYMARKET_KEY_ID": "", "POLYMARKET_SECRET_KEY": "b"}, False),
        ({}, False),
    ],
)
def test_credentials_need_both_halves(env: dict[str, str], found: bool) -> None:
    assert (PolymarketCredentials.from_env(env) is not None) is found


# --- subscriptions + quotes --------------------------------------------------------------------


def test_a_watched_market_is_subscribed_to_once_and_its_quote_held() -> None:
    stream, connection = _serve([_quote_message(), _quote_message(bid="0.60", ask="0.62")])
    assert connection.sent == [
        {
            "subscribe": {
                "requestId": f"omni-{SLUG}",
                "subscriptionType": "SUBSCRIPTION_TYPE_MARKET_DATA_LITE",
                "marketSlugs": [SLUG],
            }
        }
    ]
    assert stream.quote(SLUG) == MarketQuote(bid=0.60, ask=0.62)


def test_one_side_can_be_empty() -> None:
    stream, _connection = _serve([_quote_message(ask=None)])
    assert stream.quote(SLUG) == MarketQuote(bid=0.54, ask=None)


@pytest.mark.parametrize(
    "message",
    [
        {"heartbeat": {}},
        "not json",
        '["a", "list"]',
        {"marketDataLite": "not-a-dict"},
        {"marketDataLite": {"bestBid": {"value": "0.5"}}},  # no market named
        _quote_message(slug="some-other-market"),  # not one being watched
    ],
)
def test_anything_but_a_watched_markets_quote_changes_nothing(message: Any) -> None:
    stream, _connection = _serve([message])
    assert stream.quote(SLUG) is None
    assert stream.quote("some-other-market") is None


def test_a_market_nobody_asks_about_lapses_and_is_unsubscribed() -> None:
    clock = _Clock()
    ref: list[MarketQuoteStream] = []

    class _Lapsing(_Connection):
        def recv(self, timeout: float | None = None) -> str | bytes:
            if self.script:
                clock.now += 700  # the connection is alive, but nobody is asking about the market
            return super().recv(timeout)

    connection = _Lapsing(ref, [_quote_message(), {"heartbeat": {}}])
    stream = MarketQuoteStream(CREDENTIALS, connect=lambda _headers: connection, clock=clock)
    ref.append(stream)
    stream.watch(SLUG)
    stream._serve(connection, on_healthy=lambda: None)
    assert connection.sent[-1] == {"unsubscribe": {"requestId": f"omni-{SLUG}"}}
    assert stream.quote(SLUG) is None


def test_watching_a_market_keeps_it_from_lapsing() -> None:
    clock = _Clock()
    stream = MarketQuoteStream(CREDENTIALS, connect=lambda _headers: None, clock=clock)  # type: ignore[arg-type, return-value]
    stream.watch(SLUG)
    clock.now += 500
    stream.watch(SLUG)
    clock.now += 500
    assert SLUG in stream._watched


def test_a_quiet_market_is_not_mistaken_for_a_dead_connection() -> None:
    clock = _Clock()
    ref: list[MarketQuoteStream] = []

    class _Quiet(_Connection):
        def recv(self, timeout: float | None = None) -> str | bytes:
            clock.now += 120  # two quiet minutes, and well past when a short limit would have tripped
            if clock.now > 1_200.0:
                self.stream_ref[0]._stop.set()
            raise TimeoutError

    connection = _Quiet(ref, [])
    stream = MarketQuoteStream(CREDENTIALS, connect=lambda _headers: connection, clock=clock)
    ref.append(stream)
    stream._serve(connection, on_healthy=lambda: None)  # returns on the stop flag, with nothing raised


def test_too_long_without_hearing_anything_means_a_dead_connection() -> None:
    clock = _Clock()
    ref: list[MarketQuoteStream] = []

    class _Silent(_Connection):
        def recv(self, timeout: float | None = None) -> str | bytes:
            clock.now += 301
            raise TimeoutError

    connection = _Silent(ref, [])
    stream = MarketQuoteStream(CREDENTIALS, connect=lambda _headers: connection, clock=clock)
    ref.append(stream)
    with pytest.raises(ConnectionError, match="nothing heard"):
        stream._serve(connection, on_healthy=lambda: None)


# --- riding out a bad connection ---------------------------------------------------------------


class _RecordingStop(threading.Event):
    """A stop flag that records backoffs instead of sleeping them, and stops after enough of them."""

    def __init__(self, limit: int) -> None:
        super().__init__()
        self.limit = limit
        self.waits: list[float] = []

    def wait(self, timeout: float | None = None) -> bool:
        assert timeout is not None
        self.waits.append(timeout)
        if len(self.waits) >= self.limit:
            self.set()
        return self.is_set()


def _run_with(connect: Any, *, limit: int, clock: _Clock | None = None) -> tuple[MarketQuoteStream, _RecordingStop]:
    stream = MarketQuoteStream(CREDENTIALS, connect=connect, clock=clock or _Clock())
    stop = _RecordingStop(limit)
    stream._stop = stop
    stream.watch(SLUG)
    stream._run()
    return stream, stop


def test_a_failing_connection_backs_off_exponentially_to_a_ceiling_and_reports_why() -> None:
    def refuse(_headers: dict[str, str]) -> Any:
        raise OSError("connection refused")

    stream, stop = _run_with(refuse, limit=9)
    assert stop.waits == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0, 60.0]
    assert stream.last_error == "OSError: connection refused"


def test_each_connection_signs_afresh_with_the_current_time() -> None:
    clock = _Clock()
    seen: list[str] = []

    def refuse(headers: dict[str, str]) -> Any:
        seen.append(headers["X-PM-Timestamp"])
        clock.now += 5
        raise OSError("down")

    _run_with(refuse, limit=2, clock=clock)
    assert seen == ["1000000", "1005000"]


def test_losing_the_connection_clears_quotes_so_the_caller_goes_back_to_rest() -> None:
    ref: list[MarketQuoteStream] = []
    during: list[MarketQuote | None] = []

    class _Dropping(_Connection):
        def recv(self, timeout: float | None = None) -> str | bytes:
            if not self.script:
                during.append(self.stream_ref[0].quote(SLUG))
                raise ConnectionError("dropped")
            return super().recv(timeout)

    def connect(_headers: dict[str, str]) -> Any:
        return _Dropping(ref, [_quote_message()])

    stream = MarketQuoteStream(CREDENTIALS, connect=connect, clock=_Clock())
    ref.append(stream)
    stop = _RecordingStop(1)
    stream._stop = stop
    stream.watch(SLUG)
    stream._run()
    assert during == [MarketQuote(bid=0.54, ask=0.56)]
    assert stream.quote(SLUG) is None
    assert stream.last_error == "ConnectionError: dropped"


def test_a_connection_that_heard_something_resets_the_backoff() -> None:
    ref: list[MarketQuoteStream] = []
    attempts = {"n": 0}

    def connect(_headers: dict[str, str]) -> Any:
        attempts["n"] += 1
        if attempts["n"] == 3:
            return _Connection(ref, [{"heartbeat": {}}, ConnectionError("dropped")])
        raise OSError("refused")

    stream = MarketQuoteStream(CREDENTIALS, connect=connect, clock=_Clock())
    ref.append(stream)
    stop = _RecordingStop(4)
    stream._stop = stop
    stream.watch(SLUG)
    stream._run()
    assert stop.waits == [1.0, 2.0, 1.0, 2.0]  # back to the start after the one that worked


def test_it_runs_and_stops_on_a_real_thread() -> None:
    ready = threading.Event()

    def refuse(_headers: dict[str, str]) -> Any:
        ready.set()
        raise OSError("refused")

    stream = MarketQuoteStream(CREDENTIALS, connect=refuse, min_backoff_s=0.01)
    stream.start()
    assert ready.wait(timeout=5.0)
    stream.stop()
    assert stream._thread is not None and not stream._thread.is_alive()


def test_stopping_a_stream_that_never_started_is_harmless() -> None:
    MarketQuoteStream(CREDENTIALS).stop()


# --- sharpening REST readings ------------------------------------------------------------------


REG = MlbTeamRegistry.from_color_file()
START = datetime(2026, 9, 17, 1, 40, tzinfo=timezone.utc)
NOW = START + timedelta(hours=1)
GAME = TeamGame(
    id=LeagueScopedId(League.MLB, SourceRef("mlb_statsapi"), "g1"),
    league=League.MLB,
    status=GameStatus.LIVE,
    scheduled_start=START,
    away=REG.resolve(115),
    home=REG.resolve(119),
)
MARKET = {
    "market": {
        "gameStartTime": "2026-09-17T01:40:00Z",
        "marketSides": [{"long": True, "team": {"abbreviation": "col"}}],
    }
}


def _rest_quote(state: str = "MARKET_STATE_OPEN") -> dict[str, Any]:
    return {"marketData": {"state": state, "bestBid": {"value": "0.30"}, "bestAsk": {"value": "0.32"}}}


class _FakeStream:
    def __init__(self, quote: MarketQuote | None = None) -> None:
        self.current = quote
        self.watched: list[str] = []

    def watch(self, slug: str) -> None:
        self.watched.append(slug)

    def quote(self, slug: str) -> MarketQuote | None:
        return self.current


def _provider(stream: _FakeStream, *, rest: dict[str, Any] | None = None, market: Any = MARKET) -> PolymarketUsProvider:
    quote = rest if rest is not None else _rest_quote()
    return PolymarketUsProvider(fetch=lambda path: market if "/slug/" in path else quote, stream=stream)


def test_the_streams_price_beats_the_rest_quote() -> None:
    stream = _FakeStream(MarketQuote(bid=0.40, ask=0.42))
    reading = _provider(stream).win_probability(GAME, NOW)
    assert reading is not None and reading.away == pytest.approx(41.0)
    assert stream.watched == [SLUG]


def test_the_stream_is_read_on_every_call_though_rest_only_refreshes_on_its_interval() -> None:
    stream = _FakeStream(MarketQuote(bid=0.40, ask=0.42))
    provider = _provider(stream)
    provider.win_probability(GAME, NOW)
    stream.current = MarketQuote(bid=0.50, ask=0.52)
    reading = provider.win_probability(GAME, NOW + timedelta(seconds=12))
    assert reading is not None and reading.away == pytest.approx(51.0)


def test_with_nothing_from_the_stream_the_rest_reading_stands() -> None:
    reading = _provider(_FakeStream(None)).win_probability(GAME, NOW)
    assert reading is not None and reading.away == pytest.approx(31.0)


def test_a_stream_quote_too_wide_to_trust_leaves_the_rest_reading() -> None:
    reading = _provider(_FakeStream(MarketQuote(bid=0.20, ask=0.60))).win_probability(GAME, NOW)
    assert reading is not None and reading.away == pytest.approx(31.0)


def test_a_market_rest_says_is_closed_stays_closed_whatever_the_stream_says() -> None:
    stream = _FakeStream(MarketQuote(bid=0.40, ask=0.42))
    assert _provider(stream, rest=_rest_quote("MARKET_STATE_SUSPENDED")).win_probability(GAME, NOW) is None
    assert stream.watched == []


def test_no_market_means_nothing_to_watch() -> None:
    stream = _FakeStream(MarketQuote(bid=0.40, ask=0.42))
    assert _provider(stream, market={}).win_probability(GAME, NOW) is None
    assert stream.watched == []
