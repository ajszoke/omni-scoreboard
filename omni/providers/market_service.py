"""Live win probability from Omni's own market service, which reads the markets once for every board.

The service (``cloud/``) keeps the newest market price per game, and a board asks for those prices on
its own beat. It also sends a heartbeat every few minutes, because the service only polls markets
while some board is on.

A reading carries the time the service took it, and that is what makes a stopped poller visible: the
newest reading simply stops getting newer. So a reading older than a minute, by the service's own
clock, counts as none. The pipeline then holds the last reading for a while and falls back to MLB's
model, which is also what happens when the service can't be reached at all. A board never needs it.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol
from urllib.parse import quote

from omni.core.time import DurationSeconds
from omni.domain.baseball import WinProbability
from omni.domain.contest import TeamGame
from omni.providers.base import ProviderError

__all__ = [
    "HeartbeatSender",
    "MarketServiceClient",
    "MarketServiceConfig",
    "ServiceTransport",
    "UrllibTransport",
]

_URL_VAR = "OMNI_CLOUD_URL"
_TOKEN_VAR = "OMNI_CLOUD_TOKEN"
_MAX_AGE = timedelta(seconds=60)  # the poller writes every ~10 seconds; a minute of silence means it stopped
_REFRESH_EVERY = timedelta(seconds=10)
_RETRY_AFTER = timedelta(seconds=30)  # after a failure, so an outage doesn't cost every pass a timeout
_FORGET_AFTER = timedelta(minutes=10)  # a game nobody has asked about in this long has left the rotation
_REQUEST_TIMEOUT_S = 3.0
_HEARTBEAT_EVERY = DurationSeconds(300)
_HEARTBEAT_TIMEOUT_S = 5.0


@dataclass(frozen=True, slots=True)
class MarketServiceConfig:
    """Where the service is and this board's token; the token stays out of ``repr``."""

    url: str
    token: str = field(repr=False)

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> MarketServiceConfig | None:
        """The service settings in ``env``, or None unless both are set."""
        url, token = env.get(_URL_VAR), env.get(_TOKEN_VAR)
        return cls(url=url.rstrip("/"), token=token) if url and token else None


class ServiceTransport(Protocol):
    """How the client reaches the service: a parsed JSON answer, or an exception."""

    def get(self, path: str, *, timeout_s: float) -> Any: ...

    def post(self, path: str, body: Mapping[str, Any], *, timeout_s: float) -> Any: ...


class MarketServiceClient:
    """Answers "how does the market see this game?" from the service, one request per refresh for all games."""

    def __init__(
        self,
        transport: ServiceTransport,
        *,
        refresh_every: timedelta = _REFRESH_EVERY,
        max_age: timedelta = _MAX_AGE,
    ) -> None:
        self._transport = transport
        self._refresh_every = refresh_every
        self._max_age = max_age
        self._asked: dict[str, datetime] = {}  # game id -> when it was last asked about
        self._readings: dict[str, WinProbability | None] = {}  # what the last answer said, per game asked
        self._next_fetch: datetime | None = None

    def win_probability(self, game: TeamGame, now: datetime) -> WinProbability | None:
        """The service's current read on ``game``, or None if it has no fresh one.

        Raises `ProviderError` when a due fetch fails. What was known is dropped first, so a failure
        never leaves an old price looking current, and the next try waits a while.
        """
        game_id = str(game.id)
        self._asked[game_id] = now
        due = self._next_fetch is None or now >= self._next_fetch
        if due or game_id not in self._readings and not self._backing_off:
            self._fetch(now)
        return self._readings.get(game_id)

    @property
    def _backing_off(self) -> bool:
        return self._next_fetch is not None and not self._readings

    def _fetch(self, now: datetime) -> None:
        self._asked = {game: at for game, at in self._asked.items() if now - at < _FORGET_AFTER}
        games = sorted(self._asked)
        self._readings = {}
        try:
            answer = self._transport.get(
                f"/v1/latest?games={quote(','.join(games), safe=',:')}", timeout_s=_REQUEST_TIMEOUT_S
            )
            server_time = _parse_instant(answer["server_time"])
            listed = answer["games"]
            readings = {game: self._fresh_reading(listed.get(game), server_time) for game in games}
        except Exception as exc:
            self._next_fetch = now + _RETRY_AFTER
            raise ProviderError(f"market service fetch failed: {type(exc).__name__}: {exc}") from exc
        self._readings = readings
        self._next_fetch = now + self._refresh_every

    def _fresh_reading(self, entry: Any, server_time: datetime) -> WinProbability | None:
        """A game's reading, unless it's missing, too old to trust, or has no percentages to show."""
        win = entry.get("win") if isinstance(entry, dict) else None
        if not isinstance(win, dict):
            return None
        try:
            if server_time - _parse_instant(win["observed_at"]) > self._max_age:
                return None
            away, home = win["away_pct"], win["home_pct"]
            if not isinstance(away, (int, float)) or not isinstance(home, (int, float)):
                return None  # the market is too thin to read
            return WinProbability(home=float(home), away=float(away))
        except (KeyError, TypeError, ValueError):
            return None


def _parse_instant(raw: Any) -> datetime:
    """An instant from the service's UTC form; anything else is a `ValueError`."""
    parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"no zone on {raw!r}")
    return parsed


class HeartbeatSender:
    """Tells the service this board is on, every few minutes, from its own thread.

    The service only polls markets while some board's heartbeat is recent, so this runs whatever is on
    screen. A failed heartbeat is only noted; the next one tries again.
    """

    def __init__(
        self,
        transport: ServiceTransport,
        *,
        version: str,
        every: DurationSeconds = _HEARTBEAT_EVERY,
        wait: Callable[[threading.Event, float], bool] | None = None,
    ) -> None:
        self._transport = transport
        self._version = version
        self._every_s = float(every.as_timedelta().total_seconds())
        self._wait = wait if wait is not None else (lambda event, seconds: event.wait(seconds))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_error: str | None = None  # the latest failure, for a status line

    def beat(self) -> bool:
        """Send one heartbeat; whether the service took it."""
        try:
            self._transport.post("/v1/heartbeat", {"version": self._version}, timeout_s=_HEARTBEAT_TIMEOUT_S)
        except Exception as exc:  # never let a network failure take the thread down
            self.last_error = f"{type(exc).__name__}: {exc}"
            return False
        self.last_error = None
        return True

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="market-heartbeat", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=_HEARTBEAT_TIMEOUT_S + 1.0)

    def _run(self) -> None:
        while True:
            self.beat()
            if self._wait(self._stop, self._every_s):  # True once stopped
                return


class UrllibTransport:
    """The real transport: HTTPS with this board's bearer token."""

    def __init__(self, config: MarketServiceConfig) -> None:
        self._config = config

    def get(self, path: str, *, timeout_s: float) -> Any:  # pragma: no cover - real network
        return self._send("GET", path, None, timeout_s)

    def post(self, path: str, body: Mapping[str, Any], *, timeout_s: float) -> Any:  # pragma: no cover - real network
        return self._send("POST", path, json.dumps(body).encode(), timeout_s)

    def _send(self, method: str, path: str, data: bytes | None, timeout_s: float) -> Any:  # pragma: no cover
        from urllib.request import Request, urlopen

        headers = {
            "Authorization": f"Bearer {self._config.token}",
            "User-Agent": "omni-scoreboard",
            "Content-Type": "application/json",
        }
        request = Request(self._config.url + path, data=data, headers=headers, method=method)
        with urlopen(request, timeout=timeout_s) as response:
            return json.load(response)
