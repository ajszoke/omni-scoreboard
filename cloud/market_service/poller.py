"""The poller: one short run a minute that samples markets while a board is listening.

Lambda runs are capped and a self-invoking function gets stopped as a runaway, so "continually" is a
schedule firing every minute and a run lasting most of that minute. A crash costs at most one minute,
and the next run picks up with nothing to recover. A run ends early, having called no market, when no
board is listening or no game is in a window. A lease keeps two runs from overlapping.

Every successful sample lands a reading, even if the price hasn't moved, so a board can tell a fresh
reading from a frozen poller by its timestamp alone.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Protocol

from cloud.market_service.readings import Reading, lines_reading, win_reading
from cloud.market_service.slate import PollPlan, Slate, plan_poll
from cloud.market_service.store import MarketStore, ReadingArchive
from omni.core.enum import StrEnumMixin
from omni.domain.baseball import GameLines
from omni.domain.contest import TeamGame
from omni.providers.polymarket_us import MarketSample, QuoteStream

__all__ = [
    "LEASE_SPAN",
    "LinesSource",
    "ManagedStream",
    "MarketSource",
    "PollDependencies",
    "PollOutcome",
    "PollReport",
    "run_poll",
]

_SAMPLE_EVERY = timedelta(seconds=10)  # a board reads every 12 seconds, so sooner than this gains nothing
_SAMPLING_SPAN = timedelta(seconds=50)
_HARD_STOP = timedelta(seconds=52)  # past this a slow pass gives up, leaving time to finish before the timeout
LEASE_SPAN = timedelta(seconds=58)  # the function's timeout: past this a run holding the lease is surely dead
_LINES_EVERY_MINUTES = 5  # its source's CDN only refreshes every ten


class ManagedStream(QuoteStream, Protocol):
    """A quote stream that this run opens and closes."""

    @property
    def last_error(self) -> str | None: ...

    def start(self) -> None: ...

    def stop(self) -> None: ...


class MarketSource(Protocol):
    def sample(self, game: TeamGame, now: datetime) -> MarketSample | None: ...


class LinesSource(Protocol):
    def lines_for(self, game: TeamGame, now: datetime) -> GameLines | None: ...


class PollOutcome(StrEnumMixin, str, Enum):
    """How a run ended."""

    NO_BOARDS = "no_boards"
    NO_GAMES = "no_games"
    LEASE_HELD = "lease_held"
    POLLED = "polled"


@dataclass(frozen=True, slots=True, kw_only=True)
class PollReport:
    """What a run did, for its one log line."""

    outcome: PollOutcome
    market_games: int = 0
    lines_games: int = 0
    readings: int = 0
    archive_key: str | None = None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class PollDependencies:
    """Everything a run touches, so a test can stand in for all of it."""

    store: MarketStore
    archive: ReadingArchive
    slate: Slate
    markets: Callable[[QuoteStream | None], MarketSource]
    open_stream: Callable[[], ManagedStream | None]
    lines: LinesSource | None
    clock: Callable[[], datetime]
    sleep: Callable[[float], None]


@dataclass(slots=True)
class _Collected:
    """What a run has gathered so far."""

    readings: list[Reading] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def run_poll(deps: PollDependencies, *, holder: str) -> PollReport:
    """One run: check the two gates, take the lease, then read whatever is due."""
    started = deps.clock()
    active_until = deps.store.active_until()
    if active_until is None or active_until <= started:
        return PollReport(outcome=PollOutcome.NO_BOARDS)
    plan = plan_poll(deps.slate(started), started)
    if plan.is_idle:
        return PollReport(outcome=PollOutcome.NO_GAMES)
    if not deps.store.acquire_lease(holder, now=started, until=started + LEASE_SPAN):
        return PollReport(outcome=PollOutcome.LEASE_HELD)
    try:
        return _collect(deps, plan, started)
    finally:
        deps.store.release_lease(holder)


def _collect(deps: PollDependencies, plan: PollPlan, started: datetime) -> PollReport:
    collected = _Collected()
    if deps.lines is not None and plan.lines_games and started.minute % _LINES_EVERY_MINUTES == 0:
        _record(deps, _read_lines(deps.lines, plan.lines_games, started, collected), collected)
    if plan.market_games:
        _sample_markets(deps, plan.market_games, started, collected)
    archive_key = None
    try:
        archive_key = deps.archive.write(collected.readings, started)
    except Exception as exc:  # the table still has them; a board shouldn't suffer for the archive
        collected.warnings.append(f"archive: {type(exc).__name__}: {exc}")
    return PollReport(
        outcome=PollOutcome.POLLED,
        market_games=len(plan.market_games),
        lines_games=len(plan.lines_games),
        readings=len(collected.readings),
        archive_key=archive_key,
        warnings=tuple(collected.warnings),
    )


def _record(deps: PollDependencies, readings: Sequence[Reading], collected: _Collected) -> None:
    """Store a batch of readings; a failed write is reported, and the next sample tries again."""
    if not readings:
        return
    collected.readings.extend(readings)
    try:
        deps.store.put_readings(readings)
    except Exception as exc:
        collected.warnings.append(f"store: {type(exc).__name__}: {exc}")


def _read_lines(
    source: LinesSource, games: Sequence[TeamGame], now: datetime, collected: _Collected
) -> tuple[Reading, ...]:
    readings = []
    for game in games:
        try:
            lines = source.lines_for(game, now)
        except Exception as exc:
            collected.warnings.append(f"lines: {type(exc).__name__}: {exc}")
            continue
        if lines is not None and (lines.away is not None or lines.home is not None):
            readings.append(lines_reading(game, lines, now))
    return tuple(readings)


def _sample_markets(
    deps: PollDependencies, games: Sequence[TeamGame], started: datetime, collected: _Collected
) -> None:
    """Sample every game on a beat counted from the run's start, until the run's time is up.

    Counting from the start keeps the gap between one run's last sample and the next run's first about
    the same as any other, however long this run took to get going. A slow pass just skips a beat.
    """
    stream = deps.open_stream()
    if stream is not None:
        stream.start()
    markets = deps.markets(stream)
    next_sample = started
    try:
        while True:
            readings = _sample_pass(markets, games, deps.clock, started + _HARD_STOP, collected)
            _record(deps, readings, collected)
            now = deps.clock()
            while next_sample <= now:
                next_sample += _SAMPLE_EVERY
            if not readings or next_sample > started + _SAMPLING_SPAN:  # no open market just now, or time's up
                return
            deps.sleep((next_sample - now).total_seconds())
    finally:
        if stream is not None:
            if stream.last_error is not None:
                collected.warnings.append(f"stream: {stream.last_error}")
            stream.stop()


def _sample_pass(
    markets: MarketSource,
    games: Sequence[TeamGame],
    clock: Callable[[], datetime],
    hard_stop: datetime,
    collected: _Collected,
) -> tuple[Reading, ...]:
    readings = []
    for game in games:
        now = clock()
        if now >= hard_stop:
            collected.warnings.append("sampling ran out of time")
            break
        try:
            sample = markets.sample(game, now)
        except Exception as exc:
            collected.warnings.append(f"market: {type(exc).__name__}: {exc}")
            continue
        if sample is not None:
            readings.append(win_reading(game, sample, now))
    return tuple(readings)
