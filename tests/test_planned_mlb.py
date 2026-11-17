"""The MLB board's sources answered from a PollPlan: the slate by date, receipts, enrichment, and a gated meter."""

from __future__ import annotations

import copy
import dataclasses
import threading
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

import pytest

from omni.app.contest_store import FINAL_HOLD, ContestStore, ProviderStatus
from omni.app.display import RecordingDisplaySink
from omni.app.loop import AppLoop, Painted, Refresh
from omni.app.pipeline import MeterReading
from omni.app.planned_mlb import MLB_POLICIES, PlannedMlb
from omni.app.poll_plan import PollKey, PollPlan, PollSource
from omni.app.poller import Poller
from omni.app.runner import build_loop, build_planned_loop, warm_up
from omni.cards.base import ScoreboardCard
from omni.core.clock import ClockReading, FakeClock, MonotonicTime
from omni.core.enum import GameStatus, League, PanelProfile
from omni.core.ids import LeagueScopedId, SourceRef
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
from omni.domain.teams import Team
from omni.events.baseball import LiveBaseballFeed
from omni.preferences.favorites import Favorites
from omni.providers.base import LocalDate, PollScope, ProviderError, ProviderUpdate, ScopeOutcome, ScopeReport
from omni.providers.mlb_statsapi import MlbStatsApiProvider
from omni.providers.mlb_teams import MlbTeamRegistry
from omni.providers.nfl_teams import NflTeamRegistry
from omni.renderers.canvas import Canvas
from omni.renderers.context import RenderContext
from omni.renderers.registry import RendererRegistry, default_registry
from omni.tower.board import FrameKind, SourceHealth
from tests.test_app_loop import SOURCE, _game, _state
from tests.test_poller import _SOON, _until
from tests.test_provider_game_state import _clean_log, _dropped_third_strike_log

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
        self.skipped: dict[date, tuple[str, ...]] = {}  # rows a date's slate couldn't read, as its warnings
        self.failing: set[date] = set()
        self.asked: list[tuple[date, datetime]] = []  # each schedule request: its date, and when
        self.states: dict[str, BaseballGameState] = {}
        self.feed_status: dict[str, GameStatus] = {}  # a feed's own word on its game's status, if not the schedule's
        self.feed_failing: set[str] = set()  # games whose feed requests fail
        self.probability: WinProbability | None = EVEN
        self.details: PregameDetails | None = DETAILS
        self.hang: threading.Event | None = None  # when set, a details request waits for it

    def fetch_schedule(self, day: date, *, now: datetime) -> ProviderUpdate:
        self.asked.append((day, now))
        if day in self.failing:
            raise ProviderError("schedule down")
        contests, skipped = self.slates.get(day, ()), self.skipped.get(day, ())
        return ProviderUpdate(source=SOURCE, observed_at=now, contests=contests, warnings=skipped)

    def fetch_live_feed(self, game: TeamGame, *, now: datetime) -> LiveBaseballFeed:
        raw = game.id.raw
        if raw in self.feed_failing:
            raise ProviderError("game feed down")
        return LiveBaseballFeed(state=self.states.get(raw, _state()), status=self.feed_status.get(raw, game.status))

    def fetch_win_probability(self, game: TeamGame) -> WinProbability | None:
        return self.probability

    def fetch_pregame_details(self, game: TeamGame) -> PregameDetails:
        if self.hang is not None:
            self.hang.wait(_SOON)
        if self.details is None:
            raise ProviderError("pregame feed down")
        return self.details


def _setup(
    *, market: bool = True, favorites: Favorites = Favorites()
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


def _ids(store: ContestStore) -> list[str]:
    return [contest.id.raw for contest in store.contests]


def _scope(day: date) -> PollScope:
    return PollScope(SOURCE, League.MLB, LocalDate(day))


def _slate(day: date) -> PollKey[ProviderUpdate]:
    return PollKey(PollSource.SLATE, _scope(day))


def _pass(planned: PlannedMlb, store: ContestStore, clock: FakeClock) -> ScopeReport:
    """One refresh's pass over the slate: the dates asked, taken into ``store`` as observed, then reconciled."""
    report = planned.observe(clock.read(), store.scopes)
    store.take(report)
    store.reconcile(clock.monotonic())
    return report


def _asked_and_read(planned: PlannedMlb, store: ContestStore, poller: Poller, clock: FakeClock) -> list[str]:
    """A pass that names what it wants, the asks, then the pass that reads their answers: the board's games."""
    _pass(planned, store, clock)
    poller.run_due()
    _pass(planned, store, clock)
    return _ids(store)


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
    store = ContestStore()
    late, done, tonight = _game("late"), _game("done", GameStatus.FINAL), _game("tonight", GameStatus.SCHEDULED)
    provider.slates = {YESTERDAY: (late, done), TODAY: (tonight,)}
    first = _pass(planned, store, clock)
    assert first.observations == () and first.coverage.scopes == (_scope(TODAY), _scope(YESTERDAY))  # asked
    poller.run_due()
    report = _pass(planned, store, clock)
    assert _ids(store) == ["tonight", "late"]  # yesterday's finished game stays away on a fresh start
    assert [(seen.scope, seen.outcome, seen.received.utc) for seen in report.observations] == [
        (_scope(TODAY), ScopeOutcome.COMPLETE, AFTER_MIDNIGHT),
        (_scope(YESTERDAY), ScopeOutcome.COMPLETE, AFTER_MIDNIGHT),
    ]
    provider.slates[YESTERDAY] = (dataclasses.replace(late, status=GameStatus.FINAL), done)
    clock.advance(60 * S)  # yesterday's slate is asked once a minute
    poller.run_due()
    _pass(planned, store, clock)
    assert _ids(store) == ["tonight", "late"]  # the late game stays through its final
    assert store.contests[1].status is GameStatus.FINAL


def test_a_slate_that_skipped_a_row_keeps_the_game_it_couldnt_read() -> None:
    planned, _, clock, provider, poller = _setup()
    store = ContestStore()
    first, second = _game("g1", GameStatus.SCHEDULED), _game("g2", GameStatus.SCHEDULED)
    provider.slates = {TODAY: (first, second)}
    assert _asked_and_read(planned, store, poller, clock) == ["g1", "g2"]
    provider.slates[TODAY], provider.skipped[TODAY] = (first,), ("game g2: unknown team 999",)
    clock.advance(15 * S)
    poller.run_due()
    assert [seen.outcome for seen in _pass(planned, store, clock).observations][0] is ScopeOutcome.PARTIAL
    assert _ids(store) == ["g1", "g2"]  # its row was skipped, not refuted
    provider.skipped.clear()
    clock.advance(15 * S)
    poller.run_due()
    _pass(planned, store, clock)
    assert _ids(store) == ["g1"]  # a complete answer without it: it has left


def test_a_game_on_both_dates_shows_todays_entry() -> None:
    planned, _, clock, provider, poller = _setup()
    store = ContestStore()
    resumed = _game("resumed", GameStatus.SUSPENDED)
    provider.slates = {YESTERDAY: (resumed,), TODAY: (dataclasses.replace(resumed, status=GameStatus.LIVE),)}
    _asked_and_read(planned, store, poller, clock)  # both answered at once: the later window's entry
    [game] = store.contests
    assert game.status is GameStatus.LIVE


def test_without_a_slate_answer_the_schedule_says_why() -> None:
    planned, _, clock, provider, poller = _setup()
    provider.failing = {TODAY}
    loop = build_planned_loop(planned, RecordingDisplaySink(QUAD))
    assert loop.refresh(clock.read()).health.reason == "no schedule yet: no answer"  # asked, not answered yet
    poller.run_due()
    assert loop.refresh(clock.read()).health.reason == "no schedule yet: ProviderError: schedule down"


def test_without_an_answer_for_yesterday_the_slate_is_todays() -> None:
    planned, _, clock, provider, poller = _setup()
    provider.slates = {YESTERDAY: (_game("late"),), TODAY: (_game("tonight", GameStatus.SCHEDULED),)}
    provider.failing = {YESTERDAY}
    assert _asked_and_read(planned, ContestStore(), poller, clock) == ["tonight"]


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
        refreshes.append(loop.refresh(clock.read()))
    removed = {game.raw for refresh in refreshes for game in refresh.reconciliation.removed}
    assert removed == set() and all(refresh.health.source is SourceHealth.FRESH for refresh in refreshes)
    assert _ids(loop._store) == ["tonight", "late"]  # still on the slate, its card still up
    yesterday = loop._store.health(_scope(YESTERDAY), clock.monotonic())  # stale on its own clock
    assert yesterday is not None and yesterday.status is ProviderStatus.STALE and yesterday.error is not None
    provider.failing = set()
    provider.slates[YESTERDAY] = (dataclasses.replace(late, status=GameStatus.FINAL),)
    finals: list[str] = []
    for _ in range(3):  # the healthy answer lands, then the final is revealed
        clock.advance(30 * S)
        poller.run_due()
        finals.extend(card.raw for card in loop.refresh(clock.read()).pipeline.finals)
    assert finals == ["mlb:mlb_statsapi:late:final"]


def test_a_live_game_from_a_date_whose_slate_keeps_failing_gets_its_final_from_its_own_feed() -> None:
    planned, _, clock, provider, poller = _setup(market=False)
    late = _game("late")
    provider.slates = {YESTERDAY: (late,), TODAY: (_game("tonight", GameStatus.SCHEDULED),)}
    loop = build_planned_loop(planned, RecordingDisplaySink(QUAD), broadcast_lag=DurationSeconds(0))
    warm_up(loop, poller, clock)
    provider.failing = {YESTERDAY}  # yesterday's slate keeps saying the game is live, as last heard
    provider.states["late"] = _state(away=3, home=4)
    provider.feed_status["late"] = GameStatus.FINAL  # but its feed says it's over
    finals: list[str] = []
    for _ in range(4):
        clock.advance(15 * S)
        poller.run_due()
        finals.extend(card.raw for card in loop.refresh(clock.read()).pipeline.finals)
    assert finals == ["mlb:mlb_statsapi:late:final"]
    assert [contest.status for contest in loop._store.contests][1] is GameStatus.LIVE


def test_a_date_that_leaves_the_window_retires_its_games_but_one_still_being_played() -> None:
    planned, plan, clock, provider, poller = _setup(market=False)
    store = ContestStore()
    late, marathon = _game("late"), _game("marathon")
    rained, early = _game("rained", GameStatus.POSTPONED), _game("early", GameStatus.FINAL)
    tonight = _game("tonight", GameStatus.SCHEDULED)
    provider.slates = {YESTERDAY: (late, marathon, rained, early), TODAY: (tonight,)}
    assert _asked_and_read(planned, store, poller, clock) == ["tonight", "late", "marathon", "rained"]
    provider.slates[YESTERDAY] = (dataclasses.replace(late, status=GameStatus.FINAL), marathon, rained, early)
    clock.advance(60 * S)
    assert _asked_and_read(planned, store, poller, clock) == ["tonight", "late", "marathon", "rained"]

    # A day on, the 17th leaves the window. Its finished game and its game never played retire; the one
    # still being played stays, and its date is still asked.
    clock.advance(timedelta(days=1))
    provider.slates[TODAY] = (dataclasses.replace(tonight, status=GameStatus.FINAL),)
    assert _asked_and_read(planned, store, poller, clock) == ["tonight", "marathon"]
    ended = (dataclasses.replace(late, status=GameStatus.FINAL), dataclasses.replace(marathon, status=GameStatus.FINAL))
    provider.slates[YESTERDAY] = (*ended, rained, early)
    clock.advance(60 * S)
    assert _asked_and_read(planned, store, poller, clock) == ["tonight", "marathon"]
    assert store.contests[1].status is GameStatus.FINAL

    # Once its final has had its time on the board, it retires too, and its date is no longer asked.
    clock.advance(FINAL_HOLD)
    assert _asked_and_read(planned, store, poller, clock) == ["tonight"]
    retired = clock.now()
    for _ in range(4):
        clock.advance(61 * S)
        _asked_and_read(planned, store, poller, clock)
    assert [at for day, at in provider.asked if day == YESTERDAY and at > retired + 2 * 60 * S] == []
    assert plan.latest(_slate(YESTERDAY), clock.read()) is None


# --- feeds -----------------------------------------------------------------------------------------


def test_a_feed_is_dated_by_its_arrival_and_a_favorite_is_asked_sooner() -> None:
    planned, plan, clock, _, poller = _setup(favorites=Favorites({League.MLB: ("COL",)}))
    game = _game("g1")
    with pytest.raises(ProviderError, match="no answer yet"):
        planned.fetch_feed(game, clock.now())
    poller.run_due()
    feed = planned.fetch_feed(game, clock.now())
    assert feed.received == ClockReading(AFTER_MIDNIGHT, MonotonicTime(0))
    assert feed.current_for == 16 * S and feed.failing is None  # current for two of its cadences
    due = plan.next_due()
    assert due is not None and due.seconds == 8.0  # a favorite's live game: the focus cadence


def _minnesota(league: League) -> TeamGame:
    """Minnesota at home, in MLB (the Twins) or the NFL (the Vikings): one abbreviation, and one raw id, for both."""
    away: Team
    home: Team
    if league is League.MLB:
        registry = MlbTeamRegistry.from_color_file()
        away, home = registry.resolve(147), registry.resolve(142)  # NYY at MIN
    else:
        nfl = NflTeamRegistry.default()
        away, home = nfl.resolve("GB"), nfl.resolve("MIN")
    return TeamGame(
        id=LeagueScopedId(league, SOURCE, "g1"),
        league=league,
        status=GameStatus.LIVE,
        scheduled_start=AFTER_MIDNIGHT,
        away=away,
        home=home,
    )


@pytest.mark.parametrize(
    ("favorite", "game", "cadence"),
    [
        (League.MLB, League.MLB, 8.0),
        (League.NFL, League.MLB, 15.0),  # a Vikings favorite never hurries the Twins
        (League.MLB, League.NFL, 15.0),  # nor a Twins favorite the Vikings
        (League.NFL, League.NFL, 8.0),
    ],
)
def test_a_favorite_s_game_is_asked_sooner_only_in_the_favorite_s_own_league(
    favorite: League, game: League, cadence: float
) -> None:
    planned, plan, clock, _, poller = _setup(favorites=Favorites({favorite: ("MIN",)}))
    with pytest.raises(ProviderError):
        planned.fetch_feed(_minnesota(game), clock.now())
    poller.run_due()
    due = plan.next_due()
    assert due is not None and due.seconds == cadence


def test_another_live_game_is_asked_on_its_cadence() -> None:
    planned, plan, clock, _, poller = _setup()
    with pytest.raises(ProviderError):
        planned.fetch_feed(_game("g1"), clock.now())
    poller.run_due()
    due = plan.next_due()
    assert due is not None and due.seconds == 15.0


def test_a_final_is_asked_as_if_live_until_its_feed_says_final_then_in_the_background() -> None:
    planned, plan, clock, provider, poller = _setup(favorites=Favorites({League.MLB: ("COL",)}))
    game = _game("g1", GameStatus.FINAL)
    provider.feed_status["g1"] = GameStatus.LIVE  # the feed hasn't caught up with the schedule
    with pytest.raises(ProviderError):
        planned.fetch_feed(game, clock.now())
    poller.run_due()
    assert planned.fetch_feed(game, clock.now()).current_for == 16 * S  # still a favorite's live game to ask
    due = plan.next_due()
    assert due is not None and due.seconds == 8.0
    provider.feed_status.clear()  # it says final now
    clock.advance(8 * S)
    poller.run_due()
    assert planned.fetch_feed(game, clock.now()).current_for == 60 * S  # a final: two background cadences
    clock.advance(8 * S)
    poller.run_due()
    due = plan.next_due()
    assert due is not None and due.seconds == 46.0  # asked again only in the background, for corrections


def test_warming_up_takes_the_board_from_nothing_to_a_live_card() -> None:
    planned, _, clock, provider, poller = _setup()
    provider.slates = {TODAY: (_game("g1"),)}
    sink = RecordingDisplaySink(QUAD)
    loop = build_loop(planned, planned.fetch_feed, sink, broadcast_lag=DurationSeconds(0))
    assert loop.refresh(clock.read()).health.source is SourceHealth.OFFLINE  # no schedule answered yet
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
        loop.refresh(clock.read())  # wants the slates
        assert _until(lambda: plan.latest(_slate(TODAY), clock.read()) is not None)
        loop.refresh(clock.read())  # wants g1's feed, and g2's details and odds
        feed: PollKey[LiveBaseballFeed] = PollKey(PollSource.GAME_FEED, live.id)
        assert _until(lambda: odds_asked.is_set() and plan.latest(feed, clock.read()) is not None)
        assert loop.refresh(clock.read()).pipeline.held == (live.id,)  # the score waits out the delay
        clock.advance(45 * S)
        released = loop.refresh(clock.read()).pipeline  # g2's details and odds are still hung
        assert [card.raw for card in released.ingested] == ["mlb:mlb_statsapi:g1:live"]  # the score goes up on time
        assert [card.raw for card in released.pregames] == [
            "mlb:mlb_statsapi:g2:pregame"
        ]  # g2's card, just without details
    finally:
        provider.hang.set()
        poller.stop(_SOON)


# --- the meter --------------------------------------------------------------------------------------


def test_the_market_is_read_as_of_an_instant_and_carries_its_own_arrival() -> None:
    planned, _, clock, _, poller = _setup()
    game = _game("g1")
    start = clock.read()
    assert planned.fetch_win_probability(game, start) is None  # nothing in yet
    poller.run_due()  # the market answers 300 ms after it's asked
    first = clock.read()
    assert first.utc == AFTER_MIDNIGHT + timedelta(milliseconds=300)
    assert planned.fetch_win_probability(game, start) is None  # nothing had arrived by then
    assert planned.fetch_win_probability(game, first) == MeterReading(WinProbability(home=60.0, away=40.0), first)
    clock.advance(15 * S)
    poller.run_due()
    just_after = ClockReading(first.utc + S, first.monotonic + S)
    assert planned.fetch_win_probability(game, just_after) == MeterReading(WinProbability(home=60.0, away=40.0), first)
    reading = planned.fetch_win_probability(game, clock.read())
    assert reading is not None and reading.value == WinProbability(home=61.0, away=39.0)


def test_the_model_is_read_the_same_way_and_a_market_with_nothing_to_say_reads_none() -> None:
    planned, _, clock, provider, poller = _setup()
    game = _game("g1")
    planned.fetch_fallback_win_probability(game, clock.read())
    poller.run_due()
    assert planned.fetch_fallback_win_probability(game, clock.read()) == MeterReading(EVEN, clock.read())
    provider.probability = None
    clock.advance(20 * S)
    poller.run_due()
    assert planned.fetch_fallback_win_probability(game, clock.read()) is None
    assert planned.source == SOURCE


def test_a_failing_market_is_reported_and_no_market_means_no_meter() -> None:
    planned, plan, clock, _, poller = _setup()
    game = _game("g1")

    def broken(game: TeamGame, asked: datetime) -> WinProbability:
        raise ProviderError("market down")

    failing = PlannedMlb(plan, clock, _FakeMlb(clock), market=broken)
    failing.fetch_win_probability(game, clock.read())
    poller.run_due()
    with pytest.raises(ProviderError, match="market down"):
        failing.fetch_win_probability(game, clock.read())
    unpriced, _, _, _, _ = _setup(market=False)
    assert unpriced.fetch_win_probability(game, clock.read()) is None


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
            loop.refresh(clock.read())
        loop.paint(clock.read())
        assert drawn.card is not None and drawn.card.id.raw == "mlb:mlb_statsapi:g1:live"
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
        loop.refresh(clock.read())
    clock.advance(30 * S)  # an answer from 15 s before the last refresh clears the delay...
    started = clock.read()  # ...as the refresh starts, and another old-score answer arrives
    plan.answered(PollKey(PollSource.GAME_FEED, game.id), LiveBaseballFeed(state=old), clock.read())
    clock.advance(S)
    leading = WinProbability(home=99.0, away=1.0)
    plan.answered(PollKey(PollSource.MARKET_WIN_PROBABILITY, game.id), leading, clock.read())
    clock.advance(S)
    plan.answered(PollKey(PollSource.GAME_FEED, game.id), LiveBaseballFeed(state=new), clock.read())
    clock.advance(3 * S)
    loop.refresh(started)  # it reads only now
    clock.advance(timedelta(seconds=40))  # the second old-score answer clears the delay
    loop.refresh(clock.read())
    loop.paint(clock.read())
    assert drawn.card is not None and drawn.card.payload.home_line.runs == 2
    assert drawn.card.payload.win_probability not in (None, leading)
    clock.advance(2 * S)  # the new score clears it
    loop.refresh(clock.read())
    loop.paint(clock.read())
    assert drawn.card.payload.home_line.runs == 3 and drawn.card.payload.win_probability == leading


# --- finals the feed confirms, and staleness from the last good answer ---------------------------------


class _Board:
    """The running board's wiring over the fake StatsAPI, stepped a second at a time: the poller asks what is
    due, then a refresh and a paint."""

    def __init__(self, *, lag: int, favorites: Favorites = Favorites()) -> None:
        self.planned, self.plan, self.clock, self.provider, self.poller = _setup(market=False, favorites=favorites)
        self.drawn = _Drawn()
        self.loop = build_planned_loop(
            self.planned, RecordingDisplaySink(QUAD), broadcast_lag=DurationSeconds(lag), registry=self.drawn
        )
        self.start = self.clock.monotonic()
        self.refreshes: list[tuple[float, Refresh]] = []
        self.paints: list[tuple[float, Painted]] = []

    @property
    def elapsed(self) -> float:
        return self.clock.monotonic().since(self.start).total_seconds()

    def run_until(self, seconds: float) -> None:
        while self.elapsed < seconds:
            self.clock.advance(S)
            self.poller.run_due()
            self.refreshes.append((self.elapsed, self.loop.refresh(self.clock.read())))
            self.paints.append((self.elapsed, self.loop.paint(self.clock.read())))

    def finals(self) -> list[tuple[float, str]]:
        return [(at, card.raw) for at, refresh in self.refreshes for card in refresh.pipeline.finals]

    def shown(self, at: float) -> tuple[str | None, bool]:
        """What the paint at ``at`` showed, and whether it was dimmed."""
        [painted] = [painted for when, painted in self.paints if when == at]
        return (painted.shown.raw if painted.shown is not None else None), painted.stale


def test_a_schedule_final_never_puts_the_last_live_score_on_a_final_and_the_feed_s_own_final_does() -> None:
    # The schedule calls a tied game final at +15 s, and its feed fails from then on: the last good answer is
    # the live 2-2, still standing for 90 s. It never airs as a final. Once the feed answers again, with a
    # final 2-3, that result shows when it clears the 45 s delay.
    board = _Board(lag=45)
    game = _game("g1")
    board.provider.slates = {TODAY: (game,)}
    board.provider.states["g1"] = _state(away=2, home=2)
    warm_up(board.loop, board.poller, board.clock)  # the 2-2 answer arrives at 0
    board.run_until(14)
    board.provider.slates = {TODAY: (dataclasses.replace(game, status=GameStatus.FINAL),)}
    board.provider.feed_failing = {"g1"}
    board.run_until(89)
    assert board.finals() == []  # no FINAL 2-2 at +60, when the schedule's word would have cleared the delay
    unconfirmed = [refresh.pipeline.unconfirmed for at, refresh in board.refreshes if at >= 15]
    assert set(unconfirmed) == {(game.id,)}  # traced: final by the schedule, not yet by its feed
    # Its last live frame airs as it clears the delay, dimmed:
    assert board.shown(45) == ("mlb:mlb_statsapi:g1:live", True)

    board.provider.feed_failing.clear()
    board.provider.states["g1"] = _state(away=2, home=3)
    board.run_until(160)
    # The feed's ask failed at 15 s and its retries at 17, 22, 37, then every 15 s: the one at 97 s answers.
    assert board.finals() == [(97 + 45, "mlb:mlb_statsapi:g1:final")]
    assert board.drawn.card is not None and board.drawn.card.id.raw == "mlb:mlb_statsapi:g1:final"
    assert (board.drawn.card.payload.away_score, board.drawn.card.payload.home_score) == (2, 3)


def test_a_live_card_dims_two_cadences_after_its_last_good_answer_and_comes_down_two_minutes_later() -> None:
    # The feed's last good answer arrives at 0. The next is due 15 s later and fails, and so do its retries: at
    # 30 s, a whole cadence past when it was due, the card is drawn dimmed, however long the plan keeps that
    # answer standing; at 150 s, two minutes on, it comes down.
    board = _Board(lag=0)
    board.provider.slates = {TODAY: (_game("g1"),)}
    warm_up(board.loop, board.poller, board.clock)
    board.provider.feed_failing = {"g1"}
    board.run_until(160)
    assert board.shown(29) == ("mlb:mlb_statsapi:g1:live", False)
    assert board.shown(30) == ("mlb:mlb_statsapi:g1:live", True)
    assert board.shown(149) == ("mlb:mlb_statsapi:g1:live", True)
    assert board.shown(150) == (None, False)  # down: the board says it has no feed for the game
    stale = [(at, refresh.pipeline.warnings) for at, refresh in board.refreshes if refresh.pipeline.warnings]
    assert stale[0] == (
        30,
        (
            "mlb:mlb_statsapi:g1: stale: its feed's newest answer is out of date (its last ask: "
            "ProviderError: game feed down)",
        ),
    )


def test_a_final_its_feed_keeps_calling_live_is_held_dimmed_then_taken_down() -> None:
    # The schedule says final at +20 s, but the feed keeps answering, live: its live card carries on as the feed
    # has it, and 30 s after the schedule's word it is marked stale; two minutes later it comes down. No result
    # is shown, and the feed is still asked.
    board = _Board(lag=0)
    game = _game("g1")
    board.provider.slates = {TODAY: (game,)}
    board.provider.feed_status["g1"] = GameStatus.LIVE
    warm_up(board.loop, board.poller, board.clock)
    board.run_until(19)
    board.provider.slates = {TODAY: (dataclasses.replace(game, status=GameStatus.FINAL),)}
    board.run_until(200)
    said_final = min(at for at, refresh in board.refreshes if refresh.pipeline.unconfirmed)
    assert said_final == 30  # the slate is asked every 15 s
    assert board.shown(59) == ("mlb:mlb_statsapi:g1:live", False)
    assert board.shown(60) == ("mlb:mlb_statsapi:g1:live", True)
    assert board.shown(179) == ("mlb:mlb_statsapi:g1:live", True) and board.shown(180) == (None, False)
    assert board.finals() == []
    feed: PollKey[LiveBaseballFeed] = PollKey(PollSource.GAME_FEED, game.id)
    asked = board.plan.latest(feed, board.clock.read())
    assert asked is not None and asked.received.monotonic.since(board.start) > 185 * S  # still asked


# --- a feed answer missing its numbers, read by the real provider ----------------------------------------

_ROW = {
    "game_id": 700001,
    "away_id": 115,
    "home_id": 119,
    "status": "In Progress",
    "game_datetime": "2026-06-18T01:10Z",
}


def _statsapi(
    status: str, inning: int, inning_state: str, away: dict[str, int], home: dict[str, int]
) -> dict[str, Any]:
    """StatsAPI's game feed: its status, and its linescore with each side's counts as given."""
    line = {"currentInning": inning, "inningState": inning_state, "teams": {"away": away, "home": home}}
    return {"gameData": {"status": {"detailedState": status}}, "liveData": {"linescore": line}}


class _StatsApiBoard:
    """The running board over the real StatsAPI provider, whose one game's feed answers with ``payload`` as the
    test sets it, stepped a second at a time: the poller asks what is due, then a refresh and a paint, each paint
    remembered with the card drawn."""

    def __init__(self, payload: dict[str, Any], *, lag: int) -> None:
        self.payload = payload
        self.clock = FakeClock(AFTER_MIDNIGHT)
        provider = MlbStatsApiProvider(
            MlbTeamRegistry.from_color_file(),
            fetch_schedule=lambda day, sport_ids: [_ROW] if day == TODAY else [],
            fetch_game=lambda game_pk: copy.deepcopy(self.payload),
        )
        self.plan = PollPlan(MLB_POLICIES)
        self.planned = PlannedMlb(self.plan, self.clock, provider)
        self.poller = Poller(self.plan, self.clock)
        self.drawn = _Drawn()
        sink = RecordingDisplaySink(QUAD)
        self.loop = build_planned_loop(self.planned, sink, broadcast_lag=DurationSeconds(lag), registry=self.drawn)
        self.start = self.clock.monotonic()
        self.refreshes: list[tuple[float, Refresh]] = []
        self.paints: list[tuple[float, ScoreboardCard[Any] | None]] = []
        warm_up(self.loop, self.poller, self.clock)

    def run_until(self, seconds: float) -> None:
        while (elapsed := self.clock.monotonic().since(self.start).total_seconds()) < seconds:
            self.clock.advance(S)
            self.poller.run_due()
            self.refreshes.append((elapsed + 1, self.loop.refresh(self.clock.read())))
            painted = self.loop.paint(self.clock.read())
            self.paints.append((elapsed + 1, self.drawn.card if painted.shown is not None else None))

    def shown(self, kind: str) -> list[tuple[float, Any]]:
        """Each paint of a card whose id ends with ``kind``: when, and its payload."""
        return [(at, card.payload) for at, card in self.paints if card is not None and card.id.raw.endswith(kind)]


def test_a_final_missing_its_runs_never_airs_and_the_complete_final_airs_once() -> None:
    # A live 3-2 answer, then from 10 s a final whose linescore leaves out both sides' runs and hits, then from 40 s
    # the complete final. The partial final is a failed read: the live answer stands, its failure is reported, and
    # once that answer is out of date the live card is drawn stale. No FINAL 0-0 is ever painted. The complete
    # final, asked at 52 s (the retries after 15 s fall at 17, 22 and 37), airs 3-2 once the delay has passed, with
    # no correction after it.
    board = _StatsApiBoard(
        _statsapi("In Progress", 8, "Bottom", {"runs": 3, "hits": 7}, {"runs": 2, "hits": 5}), lag=45
    )
    board.run_until(10)
    board.payload = _statsapi("Final", 9, "End", {"errors": 0}, {"errors": 1})
    board.run_until(40)
    feed = board.planned.fetch_feed(_game("700001"), board.clock.now())
    assert (feed.status, feed.state.away_score, feed.state.home_score) == (GameStatus.LIVE, 3, 2)  # the last good
    assert feed.failing == "ProviderError: incomplete linescore: no runs for the away side"
    warnings = [(at, refresh.pipeline.warnings) for at, refresh in board.refreshes if refresh.pipeline.warnings]
    assert warnings == [
        (
            30,
            (
                "mlb:mlb_statsapi:700001: stale: its feed's newest answer is out of date "
                "(its last ask: ProviderError: incomplete linescore: no runs for the away side)",
            ),
        )
    ]
    board.payload = _statsapi("Final", 9, "End", {"runs": 3, "hits": 7, "errors": 0}, {"runs": 2, "hits": 5})
    board.run_until(200)
    finals = [(at, card.raw) for at, refresh in board.refreshes for card in refresh.pipeline.finals]
    assert finals == [(52 + 45, "mlb:mlb_statsapi:700001:final")]
    assert [refresh.pipeline.corrections for _, refresh in board.refreshes if refresh.pipeline.corrections] == []
    shown = board.shown(":final")
    assert shown[0][0] == 97 and {(final.away_score, final.home_score) for _, final in shown} == {(3, 2)}


def test_a_live_answer_missing_its_hits_in_the_7th_raises_no_no_hitter() -> None:
    # Both sides have hit by the 6th; an answer from the top of the 7th leaves out their hits. Read as zeros, it
    # would put a no-hitter on the board; it is a failed read instead, and the 6th's answer stands.
    board = _StatsApiBoard(_statsapi("In Progress", 6, "End", {"runs": 1, "hits": 4}, {"runs": 0, "hits": 2}), lag=45)
    board.run_until(10)
    board.payload = _statsapi("In Progress", 7, "Top", {"runs": 1, "errors": 0}, {"runs": 0, "errors": 0})
    board.run_until(120)
    assert [refresh.pipeline.no_hitters for _, refresh in board.refreshes if refresh.pipeline.no_hitters] == []
    assert board.shown(":nohitter") == []
    assert board.shown(":live")  # the live card airs, from the 6th's answer


@pytest.mark.parametrize(("log", "perfect"), [(_clean_log, True), (_dropped_third_strike_log, False)])
def test_a_bid_in_the_7th_airs_as_a_perfect_game_only_when_its_play_log_shows_no_one_reached(
    log: Callable[[], list[dict[str, Any]]], perfect: bool
) -> None:
    # Every count is zero both ways in the top of the 7th. With every batter in the play log put out, the board
    # calls it a perfect game; with one who reached first on a strikeout's wild pitch, a no-hitter.
    payload = _statsapi(
        "In Progress", 7, "Top", {"runs": 0, "hits": 0, "errors": 0}, {"runs": 0, "hits": 0, "errors": 0}
    )
    batting = {"teamStats": {"batting": {"baseOnBalls": 0, "hitByPitch": 0}}}
    payload["liveData"]["boxscore"] = {"teams": {"away": batting, "home": batting}}
    payload["liveData"]["plays"] = {"allPlays": log()}
    board = _StatsApiBoard(payload, lag=0)
    board.run_until(60)
    shown = board.shown(":nohitter")
    assert shown and {(bid.perfect, bid.through_inning) for _, bid in shown} == {(perfect, 6)}


def test_an_answer_from_the_top_of_the_1st_with_nothing_yet_for_the_home_side_shows_its_true_zero() -> None:
    board = _StatsApiBoard(_statsapi("In Progress", 1, "Top", {"runs": 1, "hits": 1}, {}), lag=0)
    board.run_until(5)
    [(_, live), *_] = board.shown(":live")
    assert (live.away_line.runs, live.home_line.runs, live.home_line.hits) == (1, 0, 0)
