"""One poller run: its two gates, its lease, its beat, and riding out failures."""

from __future__ import annotations

import gzip
import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from cloud.market_service.poller import (
    LEASE_SPAN,
    PollDependencies,
    PollOutcome,
    PollReport,
    run_poll,
)
from cloud.market_service.readings import ReadingKind
from cloud.market_service.store import MarketStore, ReadingArchive
from omni.core.enum import GameStatus, League
from omni.core.ids import LeagueScopedId, SourceRef
from omni.domain.baseball import BettingLine, GameLines, WinProbability
from omni.domain.contest import TeamGame
from omni.providers.mlb_teams import MlbTeamRegistry
from omni.providers.polymarket_us import MarketQuote, MarketSample, QuoteStream
from tests.test_cloud_store import TABLE, _FakeDynamo, _FakeS3

REG = MlbTeamRegistry.from_color_file()
SOURCE = SourceRef("mlb_statsapi")
START = datetime(2026, 9, 17, 1, 40, tzinfo=timezone.utc)
RUN = START + timedelta(hours=1, seconds=0.5)  # a live game, on a minute that isn't a multiple of five...
LINES_MINUTE = START - timedelta(hours=1)  # ...and a pregame moment on one that is


def _game(raw: str = "g1", start: datetime = START, *, status: GameStatus = GameStatus.LIVE) -> TeamGame:
    return TeamGame(
        id=LeagueScopedId(League.MLB, SOURCE, raw),
        league=League.MLB,
        status=status,
        scheduled_start=start,
        away=REG.resolve(115),
        home=REG.resolve(119),
    )


SAMPLE = MarketSample(
    slug="aec-mlb-col-lad-2026-09-16",
    quote=MarketQuote(bid=0.30, ask=0.32),
    probability=WinProbability(home=69.0, away=31.0),
    streamed=True,
)


class _Time:
    """A clock that only moves when the run sleeps, or when a fake says some work took a while."""

    def __init__(self, start: datetime = RUN) -> None:
        self.now = start
        self.slept: list[float] = []

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += timedelta(seconds=seconds)


class _Markets:
    """A market source with a scripted answer per game."""

    def __init__(
        self, answer: MarketSample | Exception | None = SAMPLE, *, time: _Time | None = None, takes: float = 0.0
    ) -> None:
        self.answer = answer
        self.time = time
        self.takes = takes
        self.asked: list[tuple[str, datetime]] = []

    def sample(self, game: TeamGame, now: datetime) -> MarketSample | None:
        self.asked.append((game.id.raw, now))
        if self.time is not None:
            self.time.now += timedelta(seconds=self.takes)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


class _Stream:
    def __init__(self, *, error: str | None = None) -> None:
        self.events: list[str] = []
        self._error = error

    @property
    def last_error(self) -> str | None:
        return self._error

    def start(self) -> None:
        self.events.append("start")

    def stop(self) -> None:
        self.events.append("stop")

    def watch(self, slug: str) -> None:
        raise AssertionError("the fake markets don't watch")

    def quote(self, slug: str) -> MarketQuote | None:
        raise AssertionError("the fake markets don't read quotes")


class _Lines:
    def __init__(self, answer: GameLines | Exception | None) -> None:
        self.answer = answer
        self.asked: list[str] = []

    def lines_for(self, game: TeamGame, now: datetime) -> GameLines | None:
        self.asked.append(game.id.raw)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


class _Rig:
    """A full set of fakes for a run."""

    def __init__(
        self,
        *,
        games: tuple[TeamGame, ...] = (_game(),),
        time: _Time | None = None,
        markets: _Markets | None = None,
        stream: _Stream | None = None,
        lines: _Lines | None = None,
        active: bool = True,
    ) -> None:
        self.dynamo = _FakeDynamo()
        self.store = MarketStore(self.dynamo, TABLE)
        self.s3 = _FakeS3()
        self.time = time if time is not None else _Time()
        self.markets = markets if markets is not None else _Markets()
        self.stream = stream
        self.streams_given: list[QuoteStream | None] = []
        self.slate_calls = 0
        self.games = games
        self.lines = lines
        if active:
            self.store.mark_active(self.time.now + timedelta(minutes=5))

    def _slate(self, _now: datetime) -> tuple[TeamGame, ...]:
        self.slate_calls += 1
        return self.games

    def _market_source(self, stream: QuoteStream | None) -> _Markets:
        self.streams_given.append(stream)
        return self.markets

    def run(self, holder: str = "run-1") -> PollReport:
        deps = PollDependencies(
            store=self.store,
            archive=ReadingArchive(self.s3, "omni-archive", suffix=lambda: "x"),
            slate=self._slate,
            markets=self._market_source,
            open_stream=lambda: self.stream,
            lines=self.lines,
            clock=self.time,
            sleep=self.time.sleep,
        )
        return run_poll(deps, holder=holder)

    def stored_kinds(self) -> list[str]:
        return [key[0].rsplit("#", 1)[1] for key in self.dynamo.items if key[0].startswith("reading#")]


# --- the two gates -------------------------------------------------------------------------------


def test_with_no_board_ever_heard_from_nothing_is_called() -> None:
    rig = _Rig(active=False)
    assert rig.run().outcome is PollOutcome.NO_BOARDS
    assert rig.slate_calls == 0 and rig.markets.asked == []


def test_ten_minutes_after_the_last_heartbeat_nothing_is_called() -> None:
    rig = _Rig(active=False)
    rig.store.mark_active(rig.time.now)
    assert rig.run().outcome is PollOutcome.NO_BOARDS
    assert rig.slate_calls == 0


def test_an_always_on_board_with_no_game_in_a_window_calls_no_market() -> None:
    rig = _Rig(games=())
    assert rig.run().outcome is PollOutcome.NO_GAMES
    assert rig.markets.asked == [] and rig.streams_given == []
    assert rig.s3.objects == {}


# --- the lease -----------------------------------------------------------------------------------


def test_a_run_finding_another_in_progress_steps_aside() -> None:
    rig = _Rig()
    rig.store.acquire_lease("other", now=rig.time.now, until=rig.time.now + LEASE_SPAN)
    assert rig.run().outcome is PollOutcome.LEASE_HELD
    assert rig.markets.asked == []


def test_a_finished_run_gives_the_lease_back() -> None:
    rig = _Rig()
    rig.run()
    assert rig.store.acquire_lease("next", now=rig.time.now, until=rig.time.now + LEASE_SPAN)


def test_a_run_that_blows_up_still_gives_the_lease_back() -> None:
    rig = _Rig()

    def broken_stream() -> Any:
        raise RuntimeError("no stream today")

    deps = PollDependencies(
        store=rig.store,
        archive=ReadingArchive(rig.s3, "omni-archive"),
        slate=rig._slate,
        markets=rig._market_source,
        open_stream=broken_stream,
        lines=None,
        clock=rig.time,
        sleep=rig.time.sleep,
    )
    with pytest.raises(RuntimeError):
        run_poll(deps, holder="run-1")
    assert rig.store.acquire_lease("next", now=rig.time.now, until=rig.time.now + LEASE_SPAN)


# --- the beat ------------------------------------------------------------------------------------


def test_a_run_samples_every_ten_seconds_for_most_of_a_minute() -> None:
    rig = _Rig()
    report = rig.run()
    offsets = [round((at - RUN).total_seconds()) for _, at in rig.markets.asked]
    assert offsets == [0, 10, 20, 30, 40, 50]
    assert report.outcome is PollOutcome.POLLED and report.readings == 6


def test_a_slow_start_doesnt_shift_the_beat_or_cut_the_run_short() -> None:
    time = _Time()

    class _SlowSlate(_Rig):
        def _slate(self, _now: datetime) -> tuple[TeamGame, ...]:
            time.now += timedelta(seconds=7)
            return self.games

    rig = _SlowSlate(time=time)
    rig.run()
    offsets = [round((at - RUN).total_seconds()) for _, at in rig.markets.asked]
    assert offsets == [7, 10, 20, 30, 40, 50]


def test_a_slow_pass_skips_a_beat_instead_of_falling_behind() -> None:
    time = _Time()
    rig = _Rig(time=time, markets=_Markets(time=time, takes=13.0))
    rig.run()
    offsets = [round((at - RUN).total_seconds()) for _, at in rig.markets.asked]
    assert offsets == [0, 20, 40]


def test_a_pass_too_slow_to_finish_starts_no_more_fetches_past_the_deadline_and_says_so() -> None:
    time = _Time()
    games = tuple(_game(f"g{i}") for i in range(4))
    rig = _Rig(games=games, time=time, markets=_Markets(time=time, takes=20.0))
    report = rig.run()
    assert [raw for raw, _ in rig.markets.asked] == ["g0", "g1", "g2"]  # at 0, 20 and 40 seconds; 60 is too late
    assert report.warnings == ("sampling ran out of time",)
    assert report.readings == 3 and rig.time.slept == []


def test_when_no_game_has_an_open_market_the_run_ends_early() -> None:
    rig = _Rig(markets=_Markets(answer=None))
    report = rig.run()
    assert len(rig.markets.asked) == 1 and rig.time.slept == []
    assert report.readings == 0 and report.outcome is PollOutcome.POLLED
    assert rig.s3.objects == {}


# --- what lands where ----------------------------------------------------------------------------


def test_every_successful_sample_lands_a_row_and_the_archive_keeps_them_all() -> None:
    rig = _Rig(games=(_game("a"), _game("b")))
    report = rig.run()
    assert report.readings == 12
    assert rig.stored_kinds() == ["win"] * 12
    assert report.archive_key is not None
    lines = gzip.decompress(rig.s3.objects[report.archive_key]).decode().splitlines()
    assert len(lines) == 12
    first = json.loads(lines[0])
    assert first["kind"] == ReadingKind.WIN.value and first["source"] == "stream"


def test_a_game_with_no_market_doesnt_stop_the_others() -> None:
    class _OneOfTwo(_Markets):
        def sample(self, game: TeamGame, now: datetime) -> MarketSample | None:
            return SAMPLE if game.id.raw == "a" else None

    rig = _Rig(games=(_game("a"), _game("b")), markets=_OneOfTwo())
    assert rig.run().readings == 6


def test_a_market_failure_is_reported_and_doesnt_stop_the_others() -> None:
    class _OneFails(_Markets):
        def sample(self, game: TeamGame, now: datetime) -> MarketSample | None:
            if game.id.raw == "b":
                raise RuntimeError("boom")
            return SAMPLE

    report = _Rig(games=(_game("a"), _game("b")), markets=_OneFails()).run()
    assert report.readings == 6
    assert report.warnings[0] == "market: RuntimeError: boom"


def test_a_table_failure_is_reported_and_the_archive_still_gets_the_readings() -> None:
    rig = _Rig()
    rig.dynamo.send_back = [1] * 30
    report = rig.run()
    assert any(warning.startswith("store: RuntimeError") for warning in report.warnings)
    assert report.archive_key is not None
    assert len(gzip.decompress(rig.s3.objects[report.archive_key]).decode().splitlines()) == 6


def test_an_archive_failure_is_reported_and_the_table_still_has_the_readings() -> None:
    rig = _Rig()

    def fail(**_kwargs: Any) -> dict[str, Any]:
        raise OSError("s3 is down")

    rig.s3.put_object = fail  # type: ignore[method-assign]
    report = rig.run()
    assert report.warnings == ("archive: OSError: s3 is down",)
    assert report.archive_key is None and len(rig.stored_kinds()) == 6


# --- the stream ----------------------------------------------------------------------------------


def test_a_stream_opens_for_the_run_and_closes_before_it_ends() -> None:
    stream = _Stream()
    rig = _Rig(stream=stream)
    rig.run()
    assert stream.events == ["start", "stop"]
    assert rig.streams_given == [stream]


def test_with_no_key_the_run_still_samples_on_public_quotes() -> None:
    rig = _Rig(stream=None)
    assert rig.run().readings == 6
    assert rig.streams_given == [None]


def test_a_stream_problem_is_reported_for_the_log() -> None:
    report = _Rig(stream=_Stream(error="ConnectionError: refused")).run()
    assert "stream: ConnectionError: refused" in report.warnings


def test_a_run_with_only_pregame_games_opens_no_stream() -> None:
    stream = _Stream()
    rig = _Rig(games=(_game(start=RUN + timedelta(hours=2), status=GameStatus.SCHEDULED),), stream=stream, lines=None)
    rig.run()
    assert stream.events == [] and rig.markets.asked == []


# --- pregame lines -------------------------------------------------------------------------------


LINES = GameLines(away=BettingLine(moneyline=120), home=BettingLine(moneyline=-140))
PREGAME = _game(status=GameStatus.SCHEDULED)


def _pregame_rig(answer: GameLines | Exception | None, *, at: datetime = LINES_MINUTE) -> _Rig:
    return _Rig(games=(PREGAME,), time=_Time(at), lines=_Lines(answer))


def test_lines_are_read_on_every_fifth_minute() -> None:
    rig = _pregame_rig(LINES)
    report = rig.run()
    assert rig.stored_kinds() == ["lines"] and report.lines_games == 1 and report.market_games == 0
    assert report.archive_key is not None


def test_lines_are_left_alone_on_other_minutes() -> None:
    rig = _pregame_rig(LINES, at=LINES_MINUTE + timedelta(minutes=1))
    assert rig.run().readings == 0
    assert rig.lines is not None and rig.lines.asked == []


def test_with_lines_switched_off_the_source_isnt_there_to_ask() -> None:
    rig = _Rig(games=(PREGAME,), time=_Time(LINES_MINUTE), lines=None)
    assert rig.run().readings == 0


def test_a_lines_failure_is_reported_and_the_run_goes_on() -> None:
    report = _pregame_rig(RuntimeError("coupon")).run()
    assert report.warnings == ("lines: RuntimeError: coupon",)


@pytest.mark.parametrize("answer", [None, GameLines()])
def test_a_game_off_the_board_lands_no_row(answer: GameLines | None) -> None:
    rig = _pregame_rig(answer)
    assert rig.run().readings == 0 and rig.stored_kinds() == []


def test_a_game_near_first_pitch_gets_both_its_lines_and_its_market() -> None:
    at = datetime(2026, 9, 17, 1, 35, tzinfo=timezone.utc)
    rig = _Rig(games=(PREGAME,), time=_Time(at), lines=_Lines(LINES))
    report = rig.run()
    assert report.market_games == 1 and report.lines_games == 1
    assert sorted(set(rig.stored_kinds())) == ["lines", "win"]
