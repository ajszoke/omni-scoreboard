"""The MLB board's sources answered from a PollPlan: the slate by date, receipts, enrichment, and a gated meter."""

from __future__ import annotations

import dataclasses
import threading
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from omni.app.display import RecordingDisplaySink
from omni.app.loop import AppLoop, Refresh
from omni.app.pipeline import MeterReading
from omni.app.planned_mlb import FINAL_HOLD, MLB_POLICIES, NO_BACKOFF, PlannedMlb
from omni.app.poll_plan import PollKey, PollPlan, PollSource
from omni.app.poller import Poller
from omni.app.runner import build_loop, build_planned_loop, warm_up
from omni.cards.base import ScoreboardCard
from omni.core.clock import FakeClock
from omni.core.enum import GameStatus, PanelProfile
from omni.core.ids import SourceRef
from omni.core.time import DurationSeconds
from omni.domain.baseball import (
    BaseballGameState,
    BettingLine,
    GameLines,
    PregameDetails,
    PregameSide,
    WinLossRecord,
    WinProbability,
)
from omni.domain.contest import TeamGame
from omni.events.baseball import LiveBaseballFeed
from omni.providers.base import ProviderError, ProviderUpdate
from omni.renderers.canvas import Canvas
from omni.renderers.context import RenderContext
from omni.renderers.registry import RendererRegistry, default_registry
from omni.tower.board import FrameKind, SourceHealth
from tests.test_app_loop import SOURCE, _game, _state
from tests.test_poller import _SOON, _until

AFTER_MIDNIGHT = datetime(2026, 6, 18, 6, 30, tzinfo=timezone.utc)  # 00:30 in Denver, on the 18th
TODAY, YESTERDAY = date(2026, 6, 18), date(2026, 6, 17)
S = timedelta(seconds=1)
QUAD = PanelProfile.QUAD_128X64
EVEN = WinProbability(home=50.0, away=50.0)
DETAILS = PregameDetails(home=PregameSide(record=WinLossRecord(wins=51, losses=31)))
LINES = GameLines(away=BettingLine(moneyline=165), home=BettingLine(moneyline=-195))


class _FakeMlb:
    """StatsAPI, by date, with a market-like probability and pregame details; can be told to fail."""

    source: SourceRef = SOURCE
    schedule_timezone = ZoneInfo("America/Denver")

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.slates: dict[date, tuple[TeamGame, ...]] = {}
        self.failing: set[date] = set()
        self.asked: list[tuple[date, datetime]] = []  # each schedule request: its date, and when
        self.states: dict[str, BaseballGameState] = {}
        self.probability: WinProbability | None = EVEN
        self.details: PregameDetails | None = DETAILS
        self.hang: threading.Event | None = None  # when set, a details request waits for it

    def fetch_schedule(self, day: date, *, now: datetime) -> ProviderUpdate:
        self.asked.append((day, now))
        if day in self.failing:
            raise ProviderError("schedule down")
        return ProviderUpdate(source=SOURCE, observed_at=now, contests=self.slates.get(day, ()), warnings=(f"{day}",))

    def fetch_live_feed(self, game: TeamGame, *, now: datetime) -> LiveBaseballFeed:
        return LiveBaseballFeed(state=self.states.get(game.id.raw, _state()))

    def fetch_win_probability(self, game: TeamGame) -> WinProbability | None:
        return self.probability

    def fetch_pregame_details(self, game: TeamGame) -> PregameDetails:
        if self.hang is not None:
            self.hang.wait(_SOON)
        if self.details is None:
            raise ProviderError("pregame feed down")
        return self.details


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


def _asked_and_read(planned: PlannedMlb, poller: Poller, clock: FakeClock) -> ProviderUpdate:
    """A refresh that names what it wants, the asks, then the refresh that reads their answers."""
    try:
        planned.refresh(clock.now())
    except ProviderError:
        pass  # a new date with no answer yet
    poller.run_due()
    return planned.refresh(clock.now())


class _Drawn(RendererRegistry):
    """Today's renderers, remembering the last card drawn."""

    def __init__(self) -> None:
        super().__init__()
        self._real = default_registry()
        self.card: ScoreboardCard[Any] | None = None

    def render(self, card: ScoreboardCard[Any], context: RenderContext, canvas: Canvas) -> None:
        self.card = card
        self._real.render(card, context, canvas)


# --- the slate, date by date ---------------------------------------------------------------------


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


def test_without_an_answer_for_yesterday_the_slate_is_todays() -> None:
    planned, _, clock, provider, poller = _setup()
    provider.slates = {YESTERDAY: (_game("late"),), TODAY: (_game("tonight", GameStatus.SCHEDULED),)}
    provider.failing = {YESTERDAY}
    with pytest.raises(ProviderError):
        planned.refresh(clock.now())
    poller.run_due()
    assert _ids(planned.refresh(clock.now())) == ["tonight"]


def test_a_live_game_from_yesterday_outlasts_its_dates_failing_slate_and_ends_when_it_returns() -> None:
    planned, _, clock, provider, poller = _setup(market=False)
    late = _game("late")
    provider.slates = {YESTERDAY: (late,), TODAY: (_game("tonight", GameStatus.SCHEDULED),)}
    loop = build_planned_loop(planned, RecordingDisplaySink(QUAD), broadcast_lag=DurationSeconds(0))
    warm_up(loop, poller, clock)
    provider.failing = {YESTERDAY}
    refreshes: list[Refresh] = []
    for _ in range(14):  # seven minutes: yesterday's last good answer, from the start, stands only five
        clock.advance(30 * S)
        poller.run_due()
        refreshes.append(loop.refresh(clock.now()))
    removed = {game.raw for refresh in refreshes if refresh.reconciliation for game in refresh.reconciliation.removed}
    assert removed == set() and all(refresh.health.source is SourceHealth.FRESH for refresh in refreshes)
    assert _ids(planned.refresh(clock.now())) == ["tonight", "late"]  # still on the slate, its card still up
    provider.failing = set()
    provider.slates[YESTERDAY] = (dataclasses.replace(late, status=GameStatus.FINAL),)
    finals: list[str] = []
    for _ in range(3):  # the healthy answer lands, then the final is revealed
        clock.advance(30 * S)
        poller.run_due()
        finals.extend(card.raw for card in loop.refresh(clock.now()).pipeline.finals)
    assert finals == ["late:final"]


def test_a_date_that_leaves_the_window_retires_its_games_but_one_still_being_played() -> None:
    planned, plan, clock, provider, poller = _setup(market=False)
    late, marathon = _game("late"), _game("marathon")
    rained, early = _game("rained", GameStatus.POSTPONED), _game("early", GameStatus.FINAL)
    tonight = _game("tonight", GameStatus.SCHEDULED)
    provider.slates = {YESTERDAY: (late, marathon, rained, early), TODAY: (tonight,)}
    assert _ids(_asked_and_read(planned, poller, clock)) == ["tonight", "late", "marathon", "rained"]
    provider.slates[YESTERDAY] = (dataclasses.replace(late, status=GameStatus.FINAL), marathon, rained, early)
    clock.advance(60 * S)
    assert _ids(_asked_and_read(planned, poller, clock)) == ["tonight", "late", "marathon", "rained"]

    # A day on, the 17th leaves the window. Its finished game and its game never played retire; the one
    # still being played stays, and its date is still asked.
    clock.advance(timedelta(days=1))
    provider.slates[TODAY] = (dataclasses.replace(tonight, status=GameStatus.FINAL),)
    assert _ids(_asked_and_read(planned, poller, clock)) == ["tonight", "marathon"]
    ended = (dataclasses.replace(late, status=GameStatus.FINAL), dataclasses.replace(marathon, status=GameStatus.FINAL))
    provider.slates[YESTERDAY] = (*ended, rained, early)
    clock.advance(60 * S)
    update = _asked_and_read(planned, poller, clock)
    assert _ids(update) == ["tonight", "marathon"] and update.contests[1].status is GameStatus.FINAL

    # Once its final has had its time on the board, it retires too, and its date is no longer asked.
    clock.advance(FINAL_HOLD)
    assert _ids(_asked_and_read(planned, poller, clock)) == ["tonight"]
    retired = clock.now()
    for _ in range(4):
        clock.advance(61 * S)
        _asked_and_read(planned, poller, clock)
    assert [at for day, at in provider.asked if day == YESTERDAY and at > retired + 2 * 60 * S] == []
    assert plan.latest(PollKey(PollSource.SLATE, YESTERDAY), clock.read()) is None


# --- feeds -----------------------------------------------------------------------------------------


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


def test_warming_up_takes_the_board_from_nothing_to_a_live_card() -> None:
    planned, _, clock, provider, poller = _setup()
    provider.slates = {TODAY: (_game("g1"),)}
    sink = RecordingDisplaySink(QUAD)
    loop = build_loop(planned, planned.fetch_feed, sink, broadcast_lag=DurationSeconds(0), backoff=NO_BACKOFF)
    assert loop.refresh(clock.now()).health.source is SourceHealth.OFFLINE  # no schedule answered yet
    assert loop.paint(clock.read()).frame is FrameKind.OFFLINE
    warm_up(loop, poller, clock)
    assert loop.paint(clock.read()).shown is not None


# --- pregame details and odds ---------------------------------------------------------------------


def test_pregame_details_and_odds_are_read_from_the_plan_and_a_miss_only_shows_less() -> None:
    clock = FakeClock(AFTER_MIDNIGHT)
    provider = _FakeMlb(clock)
    plan = PollPlan(MLB_POLICIES)
    planned = PlannedMlb(plan, clock, provider, odds=lambda game, asked: LINES)
    game = _game("g2", GameStatus.SCHEDULED)
    assert planned.fetch_pregame_details(game) is None and planned.fetch_game_lines(game, clock.now()) is None
    Poller(plan, clock).run_due()
    assert planned.fetch_pregame_details(game) == DETAILS and planned.fetch_game_lines(game, clock.now()) == LINES
    provider.details = None  # the details request starts failing
    for _ in range(5):  # read every pass, so the plan keeps asking, every five minutes
        clock.advance(60 * S)
        planned.fetch_pregame_details(game), planned.fetch_game_lines(game, clock.now())
    assert Poller(plan, clock).run_due() == 2  # the details and the odds, both due again
    assert planned.fetch_pregame_details(game) == DETAILS  # the last good answer stands
    assert plan.last_error(PollKey(PollSource.PREGAME_DETAILS, game.id)) == "ProviderError: pregame feed down"
    assert PlannedMlb(plan, clock, provider).fetch_game_lines(game, clock.now()) is None  # no sportsbook


def test_a_hung_pregame_read_never_delays_an_on_time_score_release() -> None:
    clock = FakeClock(AFTER_MIDNIGHT)
    provider = _FakeMlb(clock)
    provider.hang = threading.Event()
    odds_asked = threading.Event()

    def odds(game: TeamGame, asked: datetime) -> GameLines | None:
        odds_asked.set()
        assert provider.hang is not None
        provider.hang.wait(_SOON)
        return None

    live, upcoming = _game("g1"), _game("g2", GameStatus.SCHEDULED)
    provider.slates = {TODAY: (live, upcoming)}
    plan = PollPlan(MLB_POLICIES)
    planned = PlannedMlb(plan, clock, provider, odds=odds)
    loop = build_planned_loop(planned, RecordingDisplaySink(QUAD), broadcast_lag=DurationSeconds(45))
    poller = Poller(plan, clock, check_every=timedelta(milliseconds=1))
    poller.start()
    try:
        loop.refresh(clock.now())  # wants the slates
        assert _until(lambda: plan.latest(PollKey(PollSource.SLATE, TODAY), clock.read()) is not None)
        loop.refresh(clock.now())  # wants g1's feed, and g2's details and odds
        feed: PollKey[LiveBaseballFeed] = PollKey(PollSource.GAME_FEED, live.id)
        assert _until(lambda: odds_asked.is_set() and plan.latest(feed, clock.read()) is not None)
        assert loop.refresh(clock.now()).pipeline.held == (live.id,)  # the score waits out the delay
        clock.advance(45 * S)
        released = loop.refresh(clock.now()).pipeline  # g2's details and odds are still hung
        assert [card.raw for card in released.ingested] == ["g1:live"]  # the score goes up on time
        assert [card.raw for card in released.pregames] == ["g2:pregame"]  # g2's card, just without details
    finally:
        provider.hang.set()
        poller.stop(_SOON)


# --- the meter --------------------------------------------------------------------------------------


def test_the_market_is_read_as_of_an_instant_and_carries_its_own_arrival() -> None:
    planned, _, clock, _, poller = _setup()
    game = _game("g1")
    assert planned.fetch_win_probability(game, clock.now()) is None  # nothing in yet
    poller.run_due()  # the market answers 300 ms after it's asked
    first = AFTER_MIDNIGHT + timedelta(milliseconds=300)
    assert planned.fetch_win_probability(game, AFTER_MIDNIGHT) is None  # nothing had arrived by then
    assert planned.fetch_win_probability(game, first) == MeterReading(WinProbability(home=60.0, away=40.0), first)
    clock.advance(15 * S)
    poller.run_due()
    assert planned.fetch_win_probability(game, first + S) == MeterReading(WinProbability(home=60.0, away=40.0), first)
    reading = planned.fetch_win_probability(game, clock.now())
    assert reading is not None and reading.value == WinProbability(home=61.0, away=39.0)


def test_the_model_is_read_the_same_way_and_a_market_with_nothing_to_say_reads_none() -> None:
    planned, _, clock, provider, poller = _setup()
    game = _game("g1")
    planned.fetch_fallback_win_probability(game, clock.now())
    poller.run_due()
    assert planned.fetch_fallback_win_probability(game, clock.now()) == MeterReading(EVEN, AFTER_MIDNIGHT)
    provider.probability = None
    clock.advance(20 * S)
    poller.run_due()
    assert planned.fetch_fallback_win_probability(game, clock.now()) is None
    assert planned.source == SOURCE


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


def _metered_board(*, meter: bool) -> tuple[AppLoop, PollPlan, FakeClock, _FakeMlb, Poller, _Drawn]:
    planned, plan, clock, provider, poller = _setup()
    provider.slates = {TODAY: (_game("g1"),)}
    drawn = _Drawn()
    loop = build_planned_loop(planned, RecordingDisplaySink(QUAD), meter=meter, registry=drawn)
    return loop, plan, clock, provider, poller, drawn


def test_the_running_board_shows_no_live_meter_unless_asked_for() -> None:
    for meter in (False, True):
        loop, plan, clock, _, poller, drawn = _metered_board(meter=meter)
        warm_up(loop, poller, clock)
        for _ in range(8):  # two minutes: the first states clear the delay, and the readings beside them
            clock.advance(15 * S)
            poller.run_due()
            loop.refresh(clock.now())
        loop.paint(clock.read())
        assert drawn.card is not None and drawn.card.id.raw == "g1:live"
        assert (drawn.card.payload.win_probability is not None) is meter
        market: PollKey[WinProbability | None] = PollKey(PollSource.MARKET_WIN_PROBABILITY, _game("g1").id)
        assert (plan.latest(market, clock.read()) is not None) is meter  # off, the market is never even asked


def test_a_quote_that_arrives_after_the_score_on_the_card_never_airs_beside_it() -> None:
    # A refresh starts as an old-score answer clears the delay, and another old-score answer arrives; it reads
    # only after a 99% quote and then a new score have arrived. The quote may stand beside the new score, never
    # beside an old one: not when that second old-score answer clears the delay 45 s on.
    loop, plan, clock, provider, poller, drawn = _metered_board(meter=True)
    game = _game("g1")
    old, new = _state(away=1, home=2), _state(away=1, home=3)
    provider.states["g1"] = old
    warm_up(loop, poller, clock)
    for _ in range(6):  # 90 s of old-score answers, and quotes beside them
        clock.advance(15 * S)
        poller.run_due()
        loop.refresh(clock.now())
    clock.advance(30 * S)  # an answer from 15 s before the last refresh clears the delay...
    started = clock.now()  # ...as the refresh starts, and another old-score answer arrives
    plan.answered(PollKey(PollSource.GAME_FEED, game.id), LiveBaseballFeed(state=old), clock.read())
    clock.advance(S)
    leading = WinProbability(home=99.0, away=1.0)
    plan.answered(PollKey(PollSource.MARKET_WIN_PROBABILITY, game.id), leading, clock.read())
    clock.advance(S)
    plan.answered(PollKey(PollSource.GAME_FEED, game.id), LiveBaseballFeed(state=new), clock.read())
    clock.advance(3 * S)
    loop.refresh(started)  # it reads only now
    clock.advance(timedelta(seconds=40))  # the second old-score answer clears the delay
    loop.refresh(clock.now())
    loop.paint(clock.read())
    assert drawn.card is not None and drawn.card.payload.home_line.runs == 2
    assert drawn.card.payload.win_probability not in (None, leading)
    clock.advance(2 * S)  # the new score clears it
    loop.refresh(clock.now())
    loop.paint(clock.read())
    assert drawn.card.payload.home_line.runs == 3 and drawn.card.payload.win_probability == leading
