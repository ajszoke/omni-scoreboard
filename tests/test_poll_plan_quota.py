"""The plan under a quota ledger: one account shared by its sources, refusals that stay due, shedding by class,
asks expected to cost what they last cost, and the reason a held-back slate gives its league's health."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

import pytest

from omni.app.display import RecordingDisplaySink
from omni.app.planned_mlb import MLB_POLICIES, PlannedMlb, mlb_accounts
from omni.app.poll_plan import AccountMap, PollKey, PollPlan, PollPolicy, PollPriority, PollSource
from omni.app.poller import Poller
from omni.app.quota import (
    AccountLimits,
    Billed,
    HttpAnswer,
    MeteredHttp,
    Meter,
    ProviderAccount,
    QuotaLedger,
    Refusal,
    account_limits,
    nflmeta_billed,
)
from omni.app.runner import build_planned_loop
from omni.core.clock import FakeClock
from omni.core.enum import GameStatus, League, PanelProfile
from omni.core.ids import LeagueScopedId, SourceRef
from omni.domain.contest import TeamGame
from omni.events.baseball import LiveBaseballFeed
from omni.providers.base import LocalDate, PollScope, ProviderError, ProviderUpdate
from omni.providers.nflmeta import NFLMETA_SOURCE
from omni.tower.board import SourceHealth
from tests.test_app_loop import SOURCE, _game
from tests.test_planned_mlb import AFTER_MIDNIGHT, TODAY, _FakeMlb

S = timedelta(seconds=1)
T0 = datetime(2026, 11, 15, 18, 0, tzinfo=timezone.utc)
STATSAPI, NFL, TEST = ProviderAccount.MLB_STATSAPI, ProviderAccount.NFLMETA, ProviderAccount.BALLDONTLIE
TEST_SOURCE = SourceRef("test")
_POLICY = PollPolicy(every=15 * S, background_every=30 * S, keep_for=90 * S)


def _ledger(**limits: AccountLimits) -> QuotaLedger:
    declared = account_limits()
    declared.update({ProviderAccount(name): limit for name, limit in limits.items()})
    return QuotaLedger(declared)


def _test_plan(ledger: QuotaLedger, *sources: PollSource) -> tuple[PollPlan, FakeClock]:
    accounts = AccountMap(sources={TEST_SOURCE: TEST, NFLMETA_SOURCE: NFL})
    plan = PollPlan(
        {source: _POLICY for source in sources or (PollSource.GAME_FEED,)}, ledger=ledger, accounts=accounts
    )
    return plan, FakeClock(T0)


def _game_key(raw: str, source: SourceRef = TEST_SOURCE) -> PollKey[str]:
    return PollKey(PollSource.GAME_FEED, LeagueScopedId(League.NFL, source, raw))


def _want(plan: PollPlan, clock: FakeClock, key: PollKey[str], priority: PollPriority = PollPriority.NORMAL) -> None:
    plan.want(key, lambda asked: "answer", priority=priority, at=clock.read())


def _handed(plan: PollPlan, clock: FakeClock) -> list[PollKey[Any]]:
    """The keys handed out now, each answered at once."""
    asks = plan.take_due(clock.read())
    for ask in asks:
        plan.answered(ask.key, "answer", clock.read(), ask=ask.number)
    return [ask.key for ask in asks]


class _Timed(_FakeMlb):
    """StatsAPI that notes when each request went out, on the monotonic clock."""

    def __init__(self, clock: FakeClock) -> None:
        super().__init__(clock)
        self.sent: list[tuple[float, str]] = []

    def fetch_schedule(self, day: date, *, now: datetime) -> ProviderUpdate:
        self.sent.append((self.clock.monotonic().seconds, "slate"))
        return super().fetch_schedule(day, now=now)

    def fetch_live_feed(self, game: TeamGame, *, now: datetime) -> LiveBaseballFeed:
        self.sent.append((self.clock.monotonic().seconds, "feed"))
        return super().fetch_live_feed(game, now=now)


def _mlb_board(ledger: QuotaLedger) -> tuple[PlannedMlb, PollPlan, FakeClock, _Timed, Poller]:
    clock = FakeClock(AFTER_MIDNIGHT)
    provider = _Timed(clock)
    plan = PollPlan(MLB_POLICIES, ledger=ledger, accounts=mlb_accounts(SOURCE))
    planned = PlannedMlb(plan, clock, provider, meter=Meter(ledger, STATSAPI, clock))
    return planned, plan, clock, provider, Poller(plan, clock)


def _pass(planned: PlannedMlb, clock: FakeClock, games: list[TeamGame]) -> None:
    """One decision pass's reads: the slate, and each live game's feed."""
    planned.observe(clock.read(), ())
    for game in games:
        try:
            planned.fetch_feed(game, clock.now())
        except ProviderError:
            pass  # no answer yet


# --- one account, shared ----------------------------------------------------------------------------------


def test_the_slate_and_the_game_feeds_share_statsapi_s_minute_and_never_exceed_it_together() -> None:
    ledger = _ledger(mlb_statsapi=AccountLimits(per_minute=6))
    planned, plan, clock, provider, poller = _mlb_board(ledger)
    games = [_game(f"g{n}") for n in range(1, 4)]
    provider.slates = {TODAY: tuple(games)}
    for _ in range(240):  # four minutes, a pass a second: unlimited, they'd ask about 17 times a minute
        _pass(planned, clock, games)
        poller.run_due()
        clock.advance(S)
    times = [at for at, _ in provider.sent]
    busiest = max(sum(1 for other in times if at <= other < at + 60) for at in times)
    assert busiest == 6
    assert {kind for _, kind in provider.sent} == {"slate", "feed"}
    assert ledger.usage(STATSAPI, clock.read()).requests == len(provider.sent)  # each request charged once


def test_with_statsapi_unlimited_nothing_is_ever_held_back() -> None:
    planned, plan, clock, provider, poller = _mlb_board(_ledger())
    games = [_game(f"g{n}") for n in range(1, 4)]
    provider.slates = {TODAY: tuple(games)}
    for _ in range(120):
        _pass(planned, clock, games)
        poller.run_due()
        clock.advance(S)
    today = PollScope(SOURCE, League.MLB, LocalDate(TODAY))
    keys: list[PollKey[Any]] = [PollKey(PollSource.SLATE, today)]
    keys += [PollKey(PollSource.GAME_FEED, game.id) for game in games]
    assert all(plan.held_back(key) is None for key in keys)
    assert planned.held_back(today) is None


def test_every_statsapi_request_the_mlb_board_makes_is_charged_to_statsapi() -> None:
    ledger = _ledger()
    planned, plan, clock, provider, poller = _mlb_board(ledger)
    live, upcoming = _game("g1"), _game("g2", GameStatus.PREGAME)
    provider.slates = {TODAY: (live, upcoming)}
    _pass(planned, clock, [live])
    planned.fetch_fallback_win_probability(live, clock.read())
    planned.fetch_pregame_details(upcoming)
    assert poller.run_due() == 5  # today's slate, yesterday's, the feed, the model, the pregame details
    assert ledger.usage(STATSAPI, clock.read()).requests == 5
    assert planned.fetch_pregame_details(upcoming) is not None


def test_an_nfl_slate_in_the_same_plan_spends_nflmeta_not_statsapi() -> None:
    ledger = _ledger()
    clock = FakeClock(T0)
    accounts = AccountMap(sources={SOURCE: STATSAPI, NFLMETA_SOURCE: NFL}, kinds=dict(mlb_accounts(SOURCE).kinds))
    plan = PollPlan(MLB_POLICIES, ledger=ledger, accounts=accounts)
    nflmeta = MeteredHttp(
        lambda path, query: HttpAnswer({"data": []}, {"x-nflmeta-response-rows": "14"}),
        Meter(ledger, NFL, clock, billed=nflmeta_billed),
    )
    statsapi = Meter(ledger, STATSAPI, clock).counted(lambda: "slate")
    day = LocalDate(date(2026, 11, 15))
    nfl_slate: PollKey[Any] = PollKey(PollSource.SLATE, PollScope(NFLMETA_SOURCE, League.NFL, day))
    mlb_slate: PollKey[Any] = PollKey(PollSource.SLATE, PollScope(SOURCE, League.MLB, day))
    plan.want(
        nfl_slate, lambda asked: nflmeta("/api/v1/live-scores", {}), priority=PollPriority.NORMAL, at=clock.read()
    )
    plan.want(mlb_slate, lambda asked: statsapi(), priority=PollPriority.NORMAL, at=clock.read())
    assert Poller(plan, clock).run_due() == 2
    assert (ledger.usage(NFL, clock.read()).requests, ledger.usage(NFL, clock.read()).rows) == (1, 14)
    assert ledger.usage(STATSAPI, clock.read()).requests == 1
    ledger.charge(NFL, at=clock.read(), requests=4_999)  # NFLMeta's month is spent; StatsAPI's isn't
    clock.advance(15 * S)
    assert Poller(plan, clock).run_due() == 1
    held = plan.held_back(nfl_slate)
    assert held is not None and held.reason == "quota: NFLMeta Free spent for 2026-11: switch to Builder"
    assert plan.held_back(mlb_slate) is None


# --- refusals, shedding and pace ------------------------------------------------------------------------


def test_a_refused_ask_stays_due_and_goes_out_first_once_admitted() -> None:
    plan, clock = _test_plan(_ledger(balldontlie=AccountLimits(per_minute=2)))
    first, second, third = _game_key("1"), _game_key("2"), _game_key("3")
    for key in (first, second, third):
        _want(plan, clock, key)
        clock.advance(S / 10)  # wanted in this order, so due in this order
    assert _handed(plan, clock) == [first, second]
    held = plan.held_back(third)
    assert held is not None and held.refusal is Refusal.PER_MINUTE
    assert held.reason == "quota: BALLDONTLIE 2 of 2 a minute used, normal waiting"
    clock.advance(30 * S)
    for key in (first, second, third):
        _want(plan, clock, key)
    assert _handed(plan, clock) == []  # the first two are due again too, and the minute is still full
    clock.advance(30 * S)
    assert _handed(plan, clock)[0] == third  # due the longest
    assert plan.held_back(third) is None


def test_focus_goes_out_while_background_is_shed() -> None:
    ledger = _ledger()
    plan, clock = _test_plan(ledger, PollSource.SLATE, PollSource.GAME_FEED)
    ledger.charge(NFL, at=clock.read(), rows=23_000)  # 8% of the month's rows left
    slate: PollKey[str] = PollKey(PollSource.SLATE, PollScope(NFLMETA_SOURCE, League.NFL, LocalDate(TODAY)))
    favorite = _game_key("22920", NFLMETA_SOURCE)
    _want(plan, clock, slate, PollPriority.BACKGROUND)
    _want(plan, clock, favorite, PollPriority.FOCUS)
    assert _handed(plan, clock) == [favorite]
    held = plan.held_back(slate)
    assert held is not None and held.refusal is Refusal.ROWS_RESERVE
    assert held.reason == "quota: NFLMeta rows 8% left, background held back"


def test_a_slowed_admission_puts_the_key_s_next_ask_twice_its_cadence_out() -> None:
    ledger = _ledger(balldontlie=AccountLimits(requests_a_month=100))
    plan, clock = _test_plan(ledger)
    key = _game_key("1")
    ledger.charge(TEST, at=clock.read(), requests=80)  # a fifth left: background slows
    _want(plan, clock, key, PollPriority.BACKGROUND)
    assert _handed(plan, clock) == [key]
    held = plan.held_back(key)
    assert held is not None and held.slowed and held.reason == "quota: BALLDONTLIE requests 20% left, background slowed"
    clock.advance(59 * S)
    _want(plan, clock, key, PollPriority.BACKGROUND)
    assert _handed(plan, clock) == []  # its cadence is 30 s
    clock.advance(S)
    assert _handed(plan, clock) == [key]


def test_an_account_s_asks_at_once_are_counted_across_its_sources() -> None:
    plan, clock = _test_plan(_ledger(balldontlie=AccountLimits(at_once=1)), PollSource.SLATE, PollSource.GAME_FEED)
    slate: PollKey[str] = PollKey(PollSource.SLATE, PollScope(TEST_SOURCE, League.NBA, LocalDate(TODAY)))
    game = _game_key("1")
    _want(plan, clock, slate)
    _want(plan, clock, game)
    [ask] = plan.take_due(clock.read())
    assert plan.take_due(clock.read()) == []  # one out on the account: the other waits
    plan.answered(ask.key, "answer", clock.read(), ask=ask.number)
    assert [later.key for later in plan.take_due(clock.read())] == [game if ask.key == slate else slate]


def test_the_next_ask_of_a_key_is_expected_to_bill_what_its_last_ask_billed() -> None:
    ledger = _ledger(balldontlie=AccountLimits(rows_a_month=100))
    plan, clock = _test_plan(ledger)
    meter = Meter(ledger, TEST, clock, billed=lambda headers: Billed(rows=int(headers["rows"])))
    key = _game_key("1")
    outcomes: list[str] = ["30", "fail 40", "30"]

    def fetch(asked: datetime) -> str:
        outcome = outcomes.pop(0)
        meter.charge({"rows": outcome.split()[-1]})
        if outcome.startswith("fail"):
            raise ProviderError("the server answered, then the parse failed")
        return outcome

    poller = Poller(plan, clock)
    plan.want(key, fetch, priority=PollPriority.FOCUS, at=clock.read())
    assert poller.run_due() == 1  # expected to bill one row, the policy's estimate; it billed 30
    clock.advance(15 * S)
    plan.want(key, fetch, priority=PollPriority.FOCUS, at=clock.read())
    assert poller.run_due() == 1  # it failed, having billed 40: 70 spent, 30 left
    clock.advance(15 * S)
    plan.want(key, fetch, priority=PollPriority.FOCUS, at=clock.read())
    assert poller.run_due() == 0
    held = plan.held_back(key)
    assert held is not None and held.refusal is Refusal.ROWS_RESERVE  # 40 expected, 30 left


# --- a plan's accounts are named, or it isn't built ---------------------------------------------------------


def test_a_plan_whose_ledger_and_accounts_dont_match_isn_t_built() -> None:
    ledger = QuotaLedger({STATSAPI: AccountLimits()})
    with pytest.raises(ValueError, match="come together"):
        PollPlan(MLB_POLICIES, ledger=ledger)
    with pytest.raises(ValueError, match="no account for Bovada, Polymarket US"):
        PollPlan(MLB_POLICIES, ledger=ledger, accounts=mlb_accounts(SOURCE))


def test_a_key_whose_account_isn_t_named_fails_when_wanted_never_uncounted() -> None:
    plan, clock = _test_plan(_ledger())
    with pytest.raises(ValueError, match="no provider account is named for game_feed asks about elsewhere"):
        _want(plan, clock, _game_key("1", SourceRef("elsewhere")))


def test_an_mlb_board_on_a_plan_that_doesnt_name_its_provider_s_account_isn_t_built() -> None:
    ledger = _ledger()
    plan = PollPlan(MLB_POLICIES, ledger=ledger, accounts=mlb_accounts(NFLMETA_SOURCE))
    clock = FakeClock(AFTER_MIDNIGHT)
    with pytest.raises(ValueError, match="no provider account is named for slate asks about mlb_statsapi"):
        PlannedMlb(plan, clock, _FakeMlb(clock))


# --- the league's health says why -----------------------------------------------------------------------


def test_a_slate_the_quota_holds_back_gives_its_league_the_reason_fresh_or_stale() -> None:
    ledger = _ledger(mlb_statsapi=AccountLimits(requests_a_month=100))
    planned, plan, clock, provider, poller = _mlb_board(ledger)
    loop = build_planned_loop(planned, RecordingDisplaySink(PanelProfile.QUAD_128X64))
    loop.refresh(clock.read())
    poller.run_due()
    [league] = loop.refresh(clock.read()).leagues
    assert (league.source, league.reason) == (SourceHealth.FRESH, "")
    spent = ledger.usage(STATSAPI, clock.read()).requests
    ledger.charge(STATSAPI, at=clock.read(), requests=96 - spent)  # 4% left: today's slate is normal, shed below 5%
    clock.advance(15 * S)
    loop.refresh(clock.read())
    poller.run_due()
    [league] = loop.refresh(clock.read()).leagues
    assert league.source is SourceHealth.FRESH  # the last answer is still fresh
    assert league.reason == "quota: MLB StatsAPI requests 4% left, normal held back"
    clock.advance(2 * timedelta(minutes=1))
    refresh = loop.refresh(clock.read())
    [league] = refresh.leagues
    assert league.source is SourceHealth.STALE
    assert league.reason == "no schedule for over 2m: quota: MLB StatsAPI requests 4% left, normal held back"
    assert refresh.health.reason == league.reason


def test_a_slate_held_back_before_its_first_answer_says_so_offline() -> None:
    ledger = _ledger(mlb_statsapi=AccountLimits(requests_a_month=100))
    planned, plan, clock, provider, poller = _mlb_board(ledger)
    ledger.charge(STATSAPI, at=clock.read(), requests=96)
    loop = build_planned_loop(planned, RecordingDisplaySink(PanelProfile.QUAD_128X64))
    loop.refresh(clock.read())
    poller.run_due()
    [league] = loop.refresh(clock.read()).leagues
    assert league.source is SourceHealth.OFFLINE
    assert league.reason == "no schedule yet: quota: MLB StatsAPI requests 4% left, normal held back"
    assert provider.sent == []
