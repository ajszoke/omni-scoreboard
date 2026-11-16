"""The MLB board's sources answered from a PollPlan: the two-day slate, receipts, and a gated meter."""

from __future__ import annotations

import dataclasses
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from omni.core.clock import FakeClock
from omni.app.display import RecordingDisplaySink
from omni.app.planned_mlb import MLB_POLICIES, PlannedMlb
from omni.app.poll_plan import PollPlan
from omni.app.poller import Poller
from omni.app.planned_mlb import NO_BACKOFF
from omni.tower.board import FrameKind, SourceHealth
from omni.app.runner import build_loop, warm_up
from omni.core.enum import GameStatus, PanelProfile
from omni.core.ids import SourceRef
from omni.core.time import DurationSeconds
from omni.domain.baseball import WinProbability
from omni.domain.contest import TeamGame
from omni.events.baseball import LiveBaseballFeed
from omni.providers.base import ProviderError, ProviderUpdate
from tests.test_app_loop import SOURCE, _game, _state

AFTER_MIDNIGHT = datetime(2026, 6, 18, 6, 30, tzinfo=timezone.utc)  # 00:30 in Denver, on the 18th
TODAY, YESTERDAY = date(2026, 6, 18), date(2026, 6, 17)
S = timedelta(seconds=1)
EVEN = WinProbability(home=50.0, away=50.0)


class _FakeMlb:
    """StatsAPI, by date, with a market-like probability; can be told to fail."""

    source: SourceRef = SOURCE
    schedule_timezone = ZoneInfo("America/Denver")

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.slates: dict[date, tuple[TeamGame, ...]] = {}
        self.failing: set[date] = set()
        self.probability = EVEN

    def fetch_schedule(self, day: date, *, now: datetime) -> ProviderUpdate:
        if day in self.failing:
            raise ProviderError("schedule down")
        return ProviderUpdate(source=SOURCE, observed_at=now, contests=self.slates.get(day, ()), warnings=(f"{day}",))

    def fetch_live_feed(self, game: TeamGame, *, now: datetime) -> LiveBaseballFeed:
        return LiveBaseballFeed(state=_state())

    def fetch_win_probability(self, game: TeamGame) -> WinProbability | None:
        return self.probability


def _setup(
    *, market: bool = True, favorites: frozenset[str] = frozenset()
) -> tuple[PlannedMlb, PollPlan, FakeClock, _FakeMlb, Poller]:
    clock = FakeClock(AFTER_MIDNIGHT)
    provider = _FakeMlb(clock)
    plan = PollPlan(MLB_POLICIES)
    prices: list[WinProbability] = []

    def quote(game: TeamGame, asked: datetime) -> WinProbability:
        clock.advance(timedelta(milliseconds=300))  # the market answers a beat after the feed
        prices.append(WinProbability(home=float(60 + len(prices)), away=float(40 - len(prices))))
        return prices[-1]

    planned = PlannedMlb(plan, clock, provider, market=quote if market else None, favorites=favorites)
    return planned, plan, clock, provider, Poller(plan, clock)


def _ids(update: ProviderUpdate) -> list[str]:
    return [contest.id.raw for contest in update.contests]


def test_the_slate_spans_yesterday_and_today_and_keeps_a_game_through_its_final() -> None:
    planned, _, clock, provider, poller = _setup()
    late, done, tonight = _game("late"), _game("done", GameStatus.FINAL), _game("tonight", GameStatus.SCHEDULED)
    provider.slates = {YESTERDAY: (late, done), TODAY: (tonight,)}
    with pytest.raises(ProviderError, match="no answer yet"):
        planned.refresh(clock.now())
    poller.run_due()
    update = planned.refresh(clock.now())
    assert _ids(update) == ["tonight", "late"]  # yesterday's finished game stays away on a fresh start
    assert update.observed_at == AFTER_MIDNIGHT and update.warnings == ("2026-06-18", "2026-06-17")
    provider.slates[YESTERDAY] = (dataclasses.replace(late, status=GameStatus.FINAL), done)
    clock.advance(60 * S)  # yesterday's slate is asked once a minute
    poller.run_due()
    finished = planned.refresh(clock.now())
    assert _ids(finished) == ["tonight", "late"]  # the late game stays through its final
    assert finished.contests[1].status is GameStatus.FINAL


def test_a_game_on_both_dates_shows_todays_entry() -> None:
    planned, _, clock, provider, poller = _setup()
    resumed = _game("resumed", GameStatus.SUSPENDED)
    provider.slates = {YESTERDAY: (resumed,), TODAY: (dataclasses.replace(resumed, status=GameStatus.LIVE),)}
    with pytest.raises(ProviderError):
        planned.refresh(clock.now())
    poller.run_due()
    [game] = planned.refresh(clock.now()).contests
    assert game.status is GameStatus.LIVE


def test_without_a_slate_answer_the_schedule_says_why() -> None:
    planned, _, clock, provider, poller = _setup()
    provider.failing = {TODAY}
    with pytest.raises(ProviderError):
        planned.refresh(clock.now())
    poller.run_due()
    with pytest.raises(ProviderError, match="ProviderError: schedule down"):
        planned.refresh(clock.now())


def test_a_feed_is_dated_by_its_arrival_and_a_favorite_is_asked_sooner() -> None:
    planned, plan, clock, _, poller = _setup(favorites=frozenset({"COL"}))
    game = _game("g1")
    with pytest.raises(ProviderError, match="no answer yet"):
        planned.fetch_feed(game, clock.now())
    poller.run_due()
    feed = planned.fetch_feed(game, clock.now())
    assert feed.received_at == AFTER_MIDNIGHT
    due = plan.next_due()
    assert due is not None and due.seconds == 8.0  # a favorite's live game: the focus cadence


@pytest.mark.parametrize(("status", "cadence"), [(GameStatus.LIVE, 15.0), (GameStatus.FINAL, 30.0)])
def test_other_games_are_asked_on_their_cadence(status: GameStatus, cadence: float) -> None:
    planned, plan, clock, _, poller = _setup()
    with pytest.raises(ProviderError):
        planned.fetch_feed(_game("g1", status), clock.now())
    poller.run_due()
    due = plan.next_due()
    assert due is not None and due.seconds == cadence


def test_the_meter_never_knows_more_than_the_score_beside_it() -> None:
    planned, _, clock, _, poller = _setup()
    game = _game("g1")
    with pytest.raises(ProviderError):
        planned.fetch_feed(game, clock.now())
    assert planned.fetch_win_probability(game, clock.now()) is None  # nothing in yet
    poller.run_due()  # the feed answers at once; the market 300 ms later
    assert planned.fetch_win_probability(game, clock.now()) is None  # the price arrived after the score
    clock.advance(15 * S)
    planned.fetch_feed(game, clock.now())
    poller.run_due()  # a new feed answer, after the first price
    assert planned.fetch_win_probability(game, clock.now()) == WinProbability(home=60.0, away=40.0)


def test_a_failing_market_is_reported_and_no_market_means_no_meter() -> None:
    planned, plan, clock, _, poller = _setup()
    game = _game("g1")

    def broken(game: TeamGame, asked: datetime) -> WinProbability:
        raise ProviderError("market down")

    failing = PlannedMlb(plan, clock, _FakeMlb(clock), market=broken)
    failing.fetch_win_probability(game, clock.now())
    poller.run_due()
    with pytest.raises(ProviderError, match="market down"):
        failing.fetch_win_probability(game, clock.now())
    unpriced, _, _, _, _ = _setup(market=False)
    assert unpriced.fetch_win_probability(game, clock.now()) is None


def test_the_model_probability_is_gated_the_same_way() -> None:
    planned, _, clock, _, poller = _setup()
    game = _game("g1")
    planned.fetch_fallback_win_probability(game, clock.now())
    with pytest.raises(ProviderError):
        planned.fetch_feed(game, clock.now())
    poller.run_due()
    assert planned.fetch_fallback_win_probability(game, clock.now()) == EVEN
    assert planned.source == SOURCE


def test_warming_up_takes_the_board_from_nothing_to_a_live_card() -> None:
    planned, _, clock, provider, poller = _setup()
    provider.slates = {TODAY: (_game("g1"),)}
    sink = RecordingDisplaySink(PanelProfile.QUAD_128X64)
    loop = build_loop(planned, planned.fetch_feed, sink, broadcast_lag=DurationSeconds(0), backoff=NO_BACKOFF)
    assert loop.refresh(clock.now()).health.source is SourceHealth.OFFLINE  # no schedule answered yet
    assert loop.paint(clock.read()).frame is FrameKind.OFFLINE
    warm_up(loop, poller, clock)
    assert loop.paint(clock.read()).shown is not None


def test_without_an_answer_for_yesterday_the_slate_is_todays() -> None:
    planned, _, clock, provider, poller = _setup()
    provider.slates = {YESTERDAY: (_game("late"),), TODAY: (_game("tonight", GameStatus.SCHEDULED),)}
    provider.failing = {YESTERDAY}
    with pytest.raises(ProviderError):
        planned.refresh(clock.now())
    poller.run_due()
    assert _ids(planned.refresh(clock.now())) == ["tonight"]
