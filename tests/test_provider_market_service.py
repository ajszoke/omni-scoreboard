"""The board's side of the market service: fresh readings only, one request for all games, and heartbeats."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

import pytest

from omni.core.enum import GameStatus, League
from omni.core.ids import LeagueScopedId, SourceRef
from omni.core.time import DurationSeconds
from omni.domain.baseball import WinProbability
from omni.domain.contest import TeamGame
from omni.providers.base import ProviderError
from omni.providers.market_service import (
    HeartbeatSender,
    MarketServiceClient,
    MarketServiceConfig,
    UrllibTransport,
)
from omni.providers.mlb_teams import MlbTeamRegistry

REG = MlbTeamRegistry.from_color_file()
NOW = datetime(2026, 9, 17, 2, 0, tzinfo=timezone.utc)
SERVER = "2026-09-17T02:00:00.000Z"


def _game(raw: str = "745123") -> TeamGame:
    return TeamGame(
        id=LeagueScopedId(League.MLB, SourceRef("mlb_statsapi"), raw),
        league=League.MLB,
        status=GameStatus.LIVE,
        scheduled_start=NOW - timedelta(hours=1),
        away=REG.resolve(115),
        home=REG.resolve(119),
    )


GAME_ID = "mlb:mlb_statsapi:745123"


def _win(observed_at: str = "2026-09-17T01:59:55.000Z", away: Any = 41.0, home: Any = 59.0) -> dict[str, Any]:
    return {"observed_at": observed_at, "away_pct": away, "home_pct": home, "source": "stream"}


class _Service:
    """A fake service answering every requested game from a table of entries, and recording requests."""

    def __init__(self, entries: dict[str, Any] | None = None, *, server_time: str = SERVER) -> None:
        self.entries = entries if entries is not None else {GAME_ID: {"win": _win(), "lines": None}}
        self.server_time = server_time
        self.gets: list[str] = []
        self.posts: list[tuple[str, Mapping[str, Any], float]] = []
        self.fail: Exception | None = None
        self.answer: Any = None  # when set, returned as-is instead of a well-formed answer

    def get(self, path: str, *, timeout_s: float) -> Any:
        self.gets.append(path)
        if self.fail is not None:
            raise self.fail
        if self.answer is not None:
            return self.answer
        requested = path.split("games=", 1)[1].split(",")
        return {
            "server_time": self.server_time,
            "games": {game: self.entries.get(game, {"win": None, "lines": None}) for game in requested},
        }

    def post(self, path: str, body: Mapping[str, Any], *, timeout_s: float) -> Any:
        self.posts.append((path, body, timeout_s))
        if self.fail is not None:
            raise self.fail
        return {"active_until": "2026-09-17T02:10:00.000Z"}


# --- config --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "env, found",
    [
        ({"OMNI_CLOUD_URL": "https://x.on.aws/", "OMNI_CLOUD_TOKEN": "den.s"}, True),
        ({"OMNI_CLOUD_URL": "https://x.on.aws/"}, False),
        ({"OMNI_CLOUD_TOKEN": "den.s"}, False),
        ({"OMNI_CLOUD_URL": "", "OMNI_CLOUD_TOKEN": "den.s"}, False),
        ({}, False),
    ],
)
def test_the_service_is_used_only_when_both_settings_are_present(env: dict[str, str], found: bool) -> None:
    assert (MarketServiceConfig.from_env(env) is not None) is found


def test_the_url_loses_its_trailing_slash_and_the_token_stays_out_of_repr() -> None:
    config = MarketServiceConfig.from_env({"OMNI_CLOUD_URL": "https://x.on.aws/", "OMNI_CLOUD_TOKEN": "den.s3cret"})
    assert config is not None and config.url == "https://x.on.aws"
    assert "s3cret" not in repr(config) and "s3cret" not in repr(UrllibTransport(config).__dict__)


# --- readings ------------------------------------------------------------------------------------


def test_a_fresh_reading_comes_back_as_a_win_probability() -> None:
    service = _Service()
    assert MarketServiceClient(service).win_probability(_game(), NOW) == WinProbability(home=59.0, away=41.0)
    assert service.gets == [f"/v1/latest?games={GAME_ID}"]


def test_a_reading_over_a_minute_old_by_the_services_clock_counts_as_none() -> None:
    old = _Service({GAME_ID: {"win": _win(observed_at="2026-09-17T01:58:59.000Z")}})
    assert MarketServiceClient(old).win_probability(_game(), NOW) is None
    edge = _Service({GAME_ID: {"win": _win(observed_at="2026-09-17T01:59:00.000Z")}})
    assert MarketServiceClient(edge).win_probability(_game(), NOW) is not None


def test_freshness_is_judged_on_the_services_clock_not_the_boards() -> None:
    service = _Service()
    board_clock_far_ahead = NOW + timedelta(hours=3)
    assert MarketServiceClient(service).win_probability(_game(), board_clock_far_ahead) is not None


@pytest.mark.parametrize(
    "entry",
    [
        {"win": None, "lines": None},  # no reading yet, or the game is out of its window
        {"win": _win(away=None, home=None)},  # too thin a market to read
        {"win": _win(away="41", home=59.0)},
        {"win": _win(observed_at="whenever")},
        {"win": _win(observed_at="2026-09-17T01:59:55")},  # no zone
        {"win": {"away_pct": 41.0, "home_pct": 59.0}},  # no timestamp
        {"win": _win(away=141.0, home=-41.0)},  # out of range
        {"win": "nope"},
        "nope",
    ],
)
def test_anything_short_of_a_fresh_readable_reading_is_none(entry: Any) -> None:
    assert MarketServiceClient(_Service({GAME_ID: entry})).win_probability(_game(), NOW) is None


def test_a_game_the_answer_leaves_out_is_none() -> None:
    service = _Service()
    service.answer = {"server_time": SERVER, "games": {}}
    assert MarketServiceClient(service).win_probability(_game(), NOW) is None


# --- pacing --------------------------------------------------------------------------------------


def test_one_request_per_refresh_covers_every_game_on_the_board() -> None:
    service = _Service()
    client = MarketServiceClient(service)
    client.win_probability(_game("1"), NOW)
    client.win_probability(_game("2"), NOW)  # new this pass: it can't wait for the next refresh
    client.win_probability(_game("1"), NOW + timedelta(seconds=1))
    client.win_probability(_game("2"), NOW + timedelta(seconds=1))
    assert service.gets == [
        "/v1/latest?games=mlb:mlb_statsapi:1",
        "/v1/latest?games=mlb:mlb_statsapi:1,mlb:mlb_statsapi:2",
    ]
    client.win_probability(_game("1"), NOW + timedelta(seconds=10))
    client.win_probability(_game("2"), NOW + timedelta(seconds=10))
    assert len(service.gets) == 3 and service.gets[-1].endswith("1,mlb:mlb_statsapi:2")


def test_a_game_nobody_asks_about_for_ten_minutes_drops_out_of_the_request() -> None:
    service = _Service()
    client = MarketServiceClient(service)
    client.win_probability(_game("1"), NOW)
    client.win_probability(_game("2"), NOW + timedelta(minutes=10))
    assert service.gets[-1] == "/v1/latest?games=mlb:mlb_statsapi:2"


# --- failures ------------------------------------------------------------------------------------


def test_a_failed_fetch_is_reported_once_and_then_waits_before_trying_again() -> None:
    service = _Service()
    client = MarketServiceClient(service)
    assert client.win_probability(_game(), NOW) is not None
    service.fail = TimeoutError("timed out")
    later = NOW + timedelta(seconds=10)
    with pytest.raises(ProviderError, match="TimeoutError"):
        client.win_probability(_game(), later)
    asked = len(service.gets)
    # Inside the wait, the old reading is gone and no game sets off another request, not even a new one.
    assert client.win_probability(_game(), later + timedelta(seconds=12)) is None
    assert client.win_probability(_game("2"), later + timedelta(seconds=12)) is None
    assert len(service.gets) == asked
    service.fail = None
    assert client.win_probability(_game(), later + timedelta(seconds=30)) is not None


@pytest.mark.parametrize("answer", [{"games": {}}, {"server_time": "nope", "games": {}}, {"server_time": SERVER}])
def test_a_malformed_answer_is_a_failed_fetch(answer: Any) -> None:
    service = _Service()
    service.answer = answer
    with pytest.raises(ProviderError):
        MarketServiceClient(service).win_probability(_game(), NOW)


# --- callers on several threads -------------------------------------------------------------------


class _HeldFirst(_Service):
    """A service whose first request is held open until released, so a later one can finish first."""

    def __init__(self, entries: dict[str, Any]) -> None:
        super().__init__(entries)
        self.first_out = threading.Event()
        self.release_first = threading.Event()

    def get(self, path: str, *, timeout_s: float) -> Any:
        first = not self.gets
        answer = super().get(path, timeout_s=timeout_s)
        if first:
            self.first_out.set()
            self.release_first.wait(5.0)
        return answer


def test_an_older_answer_finishing_last_never_overwrites_a_newer_batch() -> None:
    second = "mlb:mlb_statsapi:2"
    service = _HeldFirst({GAME_ID: {"win": _win()}, second: {"win": _win(away=30.0, home=70.0)}})
    client = MarketServiceClient(service)
    older: list[WinProbability | None] = []
    caller = threading.Thread(target=lambda: older.append(client.win_probability(_game(), NOW)))
    caller.start()
    assert service.first_out.wait(5.0)  # the first request, for one game, is out
    newer = client.win_probability(_game("2"), NOW)  # a game new to the client asks for both, and finishes first
    service.release_first.set()  # then the older answer lands
    caller.join(5.0)
    assert newer == WinProbability(home=70.0, away=30.0) and older == [WinProbability(home=59.0, away=41.0)]
    assert service.gets[1] == f"/v1/latest?games={second},{GAME_ID}"
    later = NOW + timedelta(seconds=1)
    assert client.win_probability(_game("2"), later) == newer  # the newer batch stands, both games in it
    assert client.win_probability(_game(), later) == older[0]
    assert len(service.gets) == 2  # so neither prompts another request


# --- heartbeats ----------------------------------------------------------------------------------


def test_a_heartbeat_reports_the_boards_version() -> None:
    service = _Service()
    sender = HeartbeatSender(service, version="abc1234")
    assert sender.beat()
    assert service.posts == [("/v1/heartbeat", {"version": "abc1234"}, 5.0)]
    assert sender.last_error is None


def test_a_failed_heartbeat_is_noted_and_cleared_by_the_next_good_one() -> None:
    service = _Service()
    sender = HeartbeatSender(service, version="v")
    service.fail = ConnectionError("refused")
    assert not sender.beat()
    assert sender.last_error == "ConnectionError: refused"
    service.fail = None
    assert sender.beat() and sender.last_error is None


def test_heartbeats_repeat_on_their_interval_until_stopped() -> None:
    service = _Service()
    waits: list[float] = []

    def wait(event: threading.Event, seconds: float) -> bool:
        waits.append(seconds)
        if len(waits) == 3:
            event.set()
        return event.is_set()

    sender = HeartbeatSender(service, version="v", every=DurationSeconds(300), wait=wait)
    sender._run()
    assert len(service.posts) == 3 and waits == [300.0, 300.0, 300.0]


def test_heartbeats_run_and_stop_on_a_real_thread() -> None:
    service = _Service()
    sender = HeartbeatSender(service, version="v", every=DurationSeconds(300))
    sender.start()
    deadline = time.monotonic() + 2.0
    while not service.posts and time.monotonic() < deadline:
        time.sleep(0.01)
    sender.stop()
    assert len(service.posts) == 1


def test_stopping_a_sender_that_never_started_is_harmless() -> None:
    HeartbeatSender(_Service(), version="v").stop()
