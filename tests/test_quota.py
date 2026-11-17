"""The quota ledger: each account's minute and month, its reserves by class, reconciling with the provider,
and the metering seam that charges it per response."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from omni.app.poll_plan import Demand, PollPolicy, PollPriority
from omni.app.quota import (
    BALLDONTLIE_FREE,
    NFLMETA_BUILDER,
    NFLMETA_FREE,
    POLYMARKET_US_PUBLIC,
    SHEDDING,
    UNLIMITED,
    AccountLimits,
    Admission,
    Billed,
    BillingMonth,
    ClassRule,
    HttpAnswer,
    Measure,
    MeteredHttp,
    Meter,
    MonthUsage,
    NflMetaPlan,
    ProviderAccount,
    ProviderBalance,
    QuotaExhausted,
    QuotaLedger,
    QuotaPolicy,
    Refusal,
    account_limits,
    nflmeta_billed,
    tallying,
    usage_line,
)
from omni.core.clock import FakeClock

T0 = datetime(2026, 11, 15, 18, 0, tzinfo=timezone.utc)
S = timedelta(seconds=1)
NFL = ProviderAccount.NFLMETA
TEST = ProviderAccount.BALLDONTLIE  # an account whose limits each test declares
BACKFILL, BACKGROUND, NORMAL, FOCUS = (
    PollPriority.BACKFILL,
    PollPriority.BACKGROUND,
    PollPriority.NORMAL,
    PollPriority.FOCUS,
)


def _ledger(limits: AccountLimits, **kwargs: Any) -> tuple[QuotaLedger, FakeClock]:
    return QuotaLedger({TEST: limits}, **kwargs), FakeClock(T0)


def _spend(ledger: QuotaLedger, clock: FakeClock, *, requests: int = 0, rows: int = 0) -> None:
    ledger.charge(TEST, at=clock.read(), requests=requests, rows=rows)


def _admit(ledger: QuotaLedger, clock: FakeClock, priority: PollPriority, rows: int = 1) -> Admission:
    return ledger.admit(TEST, Demand(priority, rows), clock.read())


# --- accounts and limits ------------------------------------------------------------------------------


def test_every_account_is_declared_with_today_s_known_limits_and_nflmeta_s_plan_is_the_user_s() -> None:
    free = account_limits()
    assert set(free) == set(ProviderAccount)
    assert free[NFL] == NFLMETA_FREE == AccountLimits(per_minute=20, requests_a_month=5_000, rows_a_month=25_000)
    assert account_limits(NflMetaPlan.BUILDER)[NFL] == NFLMETA_BUILDER
    assert NFLMETA_BUILDER == AccountLimits(per_minute=120, requests_a_month=75_000, rows_a_month=250_000)
    assert free[ProviderAccount.BALLDONTLIE] == BALLDONTLIE_FREE == AccountLimits(per_minute=5)
    assert free[ProviderAccount.POLYMARKET_US] == POLYMARKET_US_PUBLIC == AccountLimits(per_minute=1_500)
    for account in (ProviderAccount.MLB_STATSAPI, ProviderAccount.BOVADA, ProviderAccount.MARKET_SERVICE):
        assert free[account] == UNLIMITED
    assert NflMetaPlan.FREE.limits == NFLMETA_FREE
    assert [account.label for account in ProviderAccount] == [
        "MLB StatsAPI",
        "NFLMeta",
        "BALLDONTLIE",
        "Polymarket US",
        "Bovada",
        "the market service",
    ]


@pytest.mark.parametrize(
    "build",
    [
        lambda: AccountLimits(per_minute=0),
        lambda: AccountLimits(rows_a_month=-1),
        lambda: ClassRule(above=101),
        lambda: ClassRule(above=25, slowed_above=25),
        lambda: QuotaPolicy(rules={PollPriority.FOCUS: ClassRule(above=0)}),
        lambda: QuotaPolicy(rules=SHEDDING.rules, slowed_by=1),
        lambda: QuotaPolicy(rules=SHEDDING.rules, minute_reserve=100),
        lambda: Demand(PollPriority.FOCUS, rows=-1),
        lambda: PollPolicy(every=S, keep_for=S, rows=-1),
        lambda: MonthUsage(BillingMonth(2026, 11), requests=-1),
        lambda: BillingMonth(2026, 13),
    ],
)
def test_nonsense_limits_rules_and_counts_are_refused(build: Any) -> None:
    with pytest.raises(ValueError):
        build()


def test_the_shedding_table_is_as_declared() -> None:
    rules = SHEDDING.rules
    assert rules[BACKFILL] == ClassRule(above=50)
    assert rules[BACKGROUND] == ClassRule(above=25, slowed_above=10)
    assert rules[NORMAL] == ClassRule(above=5, uses_minute_reserve=True)
    assert rules[FOCUS] == ClassRule(above=0, uses_minute_reserve=True)
    assert (SHEDDING.slowed_by, SHEDDING.minute_reserve) == (2, 20)


# --- the minute -----------------------------------------------------------------------------------------


def test_the_minute_is_a_sliding_window_and_a_refusal_says_when_it_has_room() -> None:
    ledger, clock = _ledger(AccountLimits(per_minute=3))
    for _ in range(3):
        assert _admit(ledger, clock, FOCUS).admitted
        clock.advance(10 * S)
    refused = _admit(ledger, clock, FOCUS)
    assert refused.refusal is Refusal.PER_MINUTE and (refused.used, refused.per_minute) == (3, 3)
    assert refused.retry_at == clock.monotonic() + 30 * S  # the first ask, at 0 s, leaves the window at 60 s
    assert refused.reason == "quota: BALLDONTLIE 3 of 3 a minute used, focus waiting"
    clock.advance(29 * S)
    assert not _admit(ledger, clock, FOCUS).admitted
    clock.advance(S)
    assert _admit(ledger, clock, FOCUS).admitted


def test_an_admitted_ask_holds_its_place_until_charged_and_extra_requests_take_more() -> None:
    ledger, clock = _ledger(AccountLimits(per_minute=4))
    assert _admit(ledger, clock, FOCUS).admitted
    assert _admit(ledger, clock, FOCUS).admitted  # two asks handed out together, neither charged yet
    _spend(ledger, clock, requests=3)  # the first ask paged: the two held places, and one more
    assert ledger.usage(TEST, clock.read()).requests == 3
    _spend(ledger, clock, requests=1)  # the second ask's request: one more
    assert not _admit(ledger, clock, FOCUS).admitted  # four requests made, four in the minute


def test_the_last_fifth_of_the_minute_is_kept_for_normal_and_focus() -> None:
    ledger, clock = _ledger(BALLDONTLIE_FREE)  # five a minute: one kept for recovery
    for _ in range(4):
        assert _admit(ledger, clock, BACKGROUND).admitted
    assert _admit(ledger, clock, BACKGROUND).refusal is Refusal.PER_MINUTE
    assert _admit(ledger, clock, BACKFILL).refusal is Refusal.PER_MINUTE
    assert _admit(ledger, clock, NORMAL).admitted
    assert _admit(ledger, clock, FOCUS).refusal is Refusal.PER_MINUTE


def test_a_request_that_was_never_admitted_still_counts_in_the_minute() -> None:
    ledger, clock = _ledger(AccountLimits(per_minute=2))
    _spend(ledger, clock, requests=2)  # a lookup made outside any plan
    assert _admit(ledger, clock, FOCUS).refusal is Refusal.PER_MINUTE


# --- the month ------------------------------------------------------------------------------------------


def test_a_month_counts_requests_and_rows_and_unknown_rows_count_as_none() -> None:
    ledger, clock = _ledger(AccountLimits(requests_a_month=100, rows_a_month=1_000))
    ledger.charge(TEST, at=clock.read(), rows=14)
    ledger.charge(TEST, at=clock.read(), requests=2, rows=200)
    ledger.charge(TEST, at=clock.read())  # a response that didn't say what it billed
    assert ledger.usage(TEST, clock.read()) == MonthUsage(BillingMonth(2026, 11), requests=4, rows=214)


def test_the_billing_month_is_the_utc_calendar_month_and_a_new_one_starts_from_zero() -> None:
    ledger = QuotaLedger({TEST: AccountLimits(requests_a_month=10)})
    clock = FakeClock(datetime(2026, 11, 30, 23, 59, tzinfo=timezone.utc))
    _spend(ledger, clock, requests=10)
    assert ledger.exhausted(TEST, clock.read())
    clock.advance(timedelta(minutes=1))  # midnight UTC
    assert ledger.usage(TEST, clock.read()) == MonthUsage(BillingMonth(2026, 12))
    assert _admit(ledger, clock, BACKFILL).admitted
    clock.jump(-timedelta(hours=1))  # a clock set back into November gives nothing back
    assert ledger.usage(TEST, clock.read()).month == BillingMonth(2026, 12)
    assert str(BillingMonth.of(datetime(2026, 11, 30, 20, 15, tzinfo=timezone(-timedelta(hours=5))))) == "2026-12"
    assert BillingMonth.parse("2026-11") == BillingMonth(2026, 11)


def test_usage_carried_over_counts_on_in_its_own_month_and_is_dropped_in_a_later_one() -> None:
    november = MonthUsage(BillingMonth(2026, 11), requests=4_000, rows=10)
    ledger, clock = _ledger(AccountLimits(requests_a_month=5_000), usage={TEST: november})
    assert ledger.usage(TEST, clock.read()) == november
    ledger, clock = _ledger(AccountLimits(requests_a_month=5_000), usage={TEST: november})
    clock.advance(timedelta(days=20))
    assert ledger.usage(TEST, clock.read()) == MonthUsage(BillingMonth(2026, 12))


# --- reconciling with the provider ----------------------------------------------------------------------


def test_the_provider_s_remaining_raises_the_count_but_never_lowers_it() -> None:
    ledger, clock = _ledger(AccountLimits(requests_a_month=5_000, rows_a_month=25_000))
    _spend(ledger, clock, requests=100, rows=500)
    ledger.charge(TEST, at=clock.read(), rows=3, balance=ProviderBalance(requests_left=4_000, rows_left=24_000))
    assert ledger.usage(TEST, clock.read()) == MonthUsage(BillingMonth(2026, 11), requests=1_000, rows=1_000)
    ledger.charge(TEST, at=clock.read(), rows=3, balance=ProviderBalance(requests_left=4_990, rows_left=24_990))
    assert ledger.usage(TEST, clock.read()) == MonthUsage(BillingMonth(2026, 11), requests=1_001, rows=1_003)


def test_a_provider_that_says_nothing_is_left_exhausts_the_account_whatever_its_limits() -> None:
    ledger, clock = _ledger(UNLIMITED)
    ledger.charge(TEST, at=clock.read(), balance=ProviderBalance(requests_left=9, rows_left=9))
    assert _admit(ledger, clock, FOCUS).admitted and ledger.usage(TEST, clock.read()).requests == 1
    ledger.charge(TEST, at=clock.read(), balance=ProviderBalance(rows_left=0))
    refused = _admit(ledger, clock, FOCUS)
    assert refused.refusal is Refusal.EXHAUSTED and refused.reason == "quota: BALLDONTLIE exhausted"
    ledger, clock = _ledger(AccountLimits(requests_a_month=5_000))
    ledger.charge(TEST, at=clock.read(), balance=ProviderBalance(requests_left=0))
    refused = _admit(ledger, clock, FOCUS)
    assert refused.reason == "quota: BALLDONTLIE requests exhausted" and refused.measure is Measure.REQUESTS


# --- each class's reserve -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("priority", "above"), [(BACKFILL, 50), (BACKGROUND, 10), (NORMAL, 5)]
)  # background's last threshold: it runs slowed above it
def test_a_class_is_admitted_just_past_its_threshold_and_refused_at_it(priority: PollPriority, above: int) -> None:
    limit = 1_000
    ledger, clock = _ledger(AccountLimits(requests_a_month=limit))
    _spend(ledger, clock, requests=limit - limit * above // 100 - 2)  # after the ask, one more than the threshold
    assert _admit(ledger, clock, priority).admitted
    _spend(ledger, clock, requests=1)  # after the ask, exactly the threshold left
    refused = _admit(ledger, clock, priority)
    assert refused.refusal is Refusal.REQUESTS_RESERVE and refused.measure is Measure.REQUESTS


def test_focus_runs_until_the_last_request_and_then_the_account_is_exhausted() -> None:
    ledger, clock = _ledger(AccountLimits(requests_a_month=100))
    _spend(ledger, clock, requests=99)
    assert not _admit(ledger, clock, NORMAL).admitted
    assert _admit(ledger, clock, FOCUS).admitted  # the last one fits
    _spend(ledger, clock, requests=1)
    exhausted = _admit(ledger, clock, FOCUS)
    assert exhausted.refusal is Refusal.EXHAUSTED and exhausted.left == 0
    assert ledger.exhausted(TEST, clock.read())


def test_an_ask_expected_to_bill_more_rows_than_are_left_doesnt_fit_even_as_focus() -> None:
    ledger, clock = _ledger(AccountLimits(rows_a_month=1_000))
    _spend(ledger, clock, rows=990)
    assert _admit(ledger, clock, FOCUS, rows=10).admitted
    refused = _admit(ledger, clock, FOCUS, rows=11)
    assert (
        refused.refusal is Refusal.ROWS_RESERVE and refused.reason == "quota: BALLDONTLIE rows 1% left, focus held back"
    )


def test_background_slows_to_double_its_cadence_between_a_quarter_and_a_tenth_left() -> None:
    ledger, clock = _ledger(AccountLimits(requests_a_month=1_000, rows_a_month=10_000))
    _spend(ledger, clock, requests=100, rows=7_400)  # rows are the tighter: 26% left
    assert _admit(ledger, clock, BACKGROUND, rows=10).slowed_by == 1
    _spend(ledger, clock, rows=100)  # 25% left: what's left after the ask is no longer above it
    slowed = _admit(ledger, clock, BACKGROUND, rows=10)
    assert slowed.admitted and slowed.slowed and slowed.held_back and slowed.slowed_by == 2
    assert slowed.reason == "quota: BALLDONTLIE rows 25% left, background slowed"
    _spend(ledger, clock, rows=1_490)  # 10.1% left
    assert _admit(ledger, clock, BACKGROUND, rows=10).refusal is Refusal.ROWS_RESERVE
    assert _admit(ledger, clock, NORMAL, rows=10).slowed_by == 1  # normal isn't slowed, only shed below 5%


def test_backfill_goes_first_and_focus_last_as_the_month_runs_down() -> None:
    ledger, clock = _ledger(AccountLimits(rows_a_month=100))
    admitted: list[tuple[int, list[str]]] = []
    for spent in (40, 60, 80, 92, 97, 100):
        _spend(ledger, clock, rows=spent - ledger.usage(TEST, clock.read()).rows)
        kinds = [p.name.lower() for p in PollPriority if _admit(ledger, clock, p, rows=0).admitted]
        admitted.append((spent, kinds))
    assert admitted == [
        (40, ["backfill", "background", "normal", "focus"]),
        (60, ["background", "normal", "focus"]),
        (80, ["background", "normal", "focus"]),  # background slowed
        (92, ["normal", "focus"]),
        (97, ["focus"]),
        (100, []),
    ]


def test_an_account_with_no_month_limits_is_counted_and_never_held_back_for_the_month() -> None:
    ledger, clock = _ledger(UNLIMITED)
    _spend(ledger, clock, requests=1_000_000, rows=1_000_000)
    admitted = _admit(ledger, clock, BACKFILL)
    assert admitted.admitted and (admitted.measure, admitted.left) == (None, None)
    assert not admitted.held_back and admitted.reason == "quota: BALLDONTLIE backfill admitted"


def test_a_ledger_charged_for_an_account_it_doesn_t_know_fails_loudly() -> None:
    ledger, clock = _ledger(UNLIMITED)
    with pytest.raises(KeyError):
        ledger.charge(NFL, at=clock.read())


# --- the line at shutdown -------------------------------------------------------------------------------


def test_the_usage_line_gives_the_month_s_requests_and_rows_against_the_limits() -> None:
    ledger, clock = QuotaLedger(account_limits()), FakeClock(T0)
    ledger.charge(NFL, at=clock.read(), requests=1_234, rows=5_678)
    assert usage_line(ledger, NFL, clock.read()) == (
        "quota: NFLMeta in 2026-11: 1,234 of 5,000 requests, 5,678 of 25,000 rows, 20 a minute"
    )
    assert usage_line(ledger, ProviderAccount.BOVADA, clock.read()) == (
        "quota: Bovada in 2026-11: 0 requests (no limit), 0 rows (no limit)"
    )


# --- the metering seam ----------------------------------------------------------------------------------


def _headers(rows: int | None = None, requests_left: int | None = None, rows_left: int | None = None) -> dict[str, str]:
    headers = {"content-type": "application/json"}
    if rows is not None:
        headers["X-NFLMeta-Response-Rows"] = str(rows)
    if requests_left is not None:
        headers["X-Monthly-Quota-Remaining"] = str(requests_left)
    if rows_left is not None:
        headers["X-Monthly-Row-Remaining"] = str(rows_left)
    return headers


class _HttpError(Exception):
    """An HTTP error response, as urllib raises it: a status and the server's headers."""

    def __init__(self, code: int, headers: Mapping[str, str]) -> None:
        super().__init__(f"HTTP Error {code}")
        self.code = code
        self.headers = headers


def test_nflmeta_s_headers_say_the_rows_billed_and_what_is_left() -> None:
    lowered = {name.lower(): value for name, value in _headers(14, 4_321, 20_000).items()}
    assert nflmeta_billed(lowered) == Billed(rows=14, balance=ProviderBalance(4_321, 20_000))
    assert nflmeta_billed({"x-nflmeta-response-rows": "lots"}) == Billed()
    assert nflmeta_billed({}) == Billed()


def test_a_metered_fetcher_charges_each_response_and_each_error_the_server_sent() -> None:
    ledger, clock = QuotaLedger(account_limits()), FakeClock(T0)
    meter = Meter(ledger, NFL, clock, billed=nflmeta_billed)
    responses: list[Any] = [
        HttpAnswer({"data": []}, {k.lower(): v for k, v in _headers(14, 4_990).items()}),
        _HttpError(410, _headers(0, 4_989)),
        TimeoutError("timed out"),
    ]

    def get(path: str, query: Mapping[str, str | int]) -> HttpAnswer:
        answer = responses.pop(0)
        if isinstance(answer, Exception):
            raise answer
        assert isinstance(answer, HttpAnswer)
        return answer

    fetch = MeteredHttp(get, meter)
    with tallying() as billed:
        assert fetch("/api/v1/live-scores", {}) == {"data": []}
        with pytest.raises(_HttpError):
            fetch("/api/v1/games/1/play-changes", {"cursor": "gone"})
        with pytest.raises(TimeoutError):
            fetch("/api/v1/live-scores", {})  # no response: nothing to charge
    assert (billed.requests, billed.rows) == (2, 14)
    assert ledger.usage(NFL, clock.read()) == MonthUsage(BillingMonth(2026, 11), requests=11, rows=14)  # reconciled


def test_a_meter_that_reads_no_billing_charges_one_request_a_response() -> None:
    ledger, clock = QuotaLedger(account_limits()), FakeClock(T0)
    fetch = MeteredHttp(lambda path, query: HttpAnswer("ok", _headers(14, 0)), Meter(ledger, NFL, clock))
    assert fetch("/api/v1/live-scores", {}) == "ok"
    assert ledger.usage(NFL, clock.read()) == MonthUsage(BillingMonth(2026, 11), requests=1)


def test_an_exhausted_account_makes_no_request_at_all() -> None:
    ledger, clock = QuotaLedger(account_limits()), FakeClock(T0)
    ledger.charge(NFL, at=clock.read(), balance=ProviderBalance(requests_left=0))
    asked: list[str] = []

    def get(path: str, query: Mapping[str, str | int]) -> HttpAnswer:
        asked.append(path)
        return HttpAnswer({}, {})

    fetch = MeteredHttp(get, Meter(ledger, NFL, clock))
    with pytest.raises(QuotaExhausted, match="quota: NFLMeta exhausted for the month"):
        fetch("/api/v1/live-scores", {})
    assert asked == []


def test_a_counted_fetcher_is_one_request_a_call_answered_or_not() -> None:
    ledger, clock = QuotaLedger(account_limits()), FakeClock(T0)
    meter = Meter(ledger, ProviderAccount.MLB_STATSAPI, clock)
    assert meter.account is ProviderAccount.MLB_STATSAPI

    def schedule(day: str, *, sport: int) -> str:
        if day == "bad":
            raise ConnectionError("refused")
        return f"{day}:{sport}"

    counted = meter.counted(schedule)
    with tallying() as billed:
        assert counted("2026-11-15", sport=1) == "2026-11-15:1"
        with pytest.raises(ConnectionError):
            counted("bad", sport=1)
    assert (billed.requests, billed.rows) == (2, None)
    assert ledger.usage(ProviderAccount.MLB_STATSAPI, clock.read()).requests == 2


def test_a_meter_for_an_account_the_ledger_doesn_t_count_fails_when_it_s_built() -> None:
    ledger = QuotaLedger({ProviderAccount.MLB_STATSAPI: UNLIMITED})
    with pytest.raises(ValueError, match="no NFLMeta account"):
        Meter(ledger, NFL, FakeClock(T0))


def test_charges_outside_a_tally_go_untallied_and_a_tally_ends_with_its_block() -> None:
    ledger, clock = QuotaLedger(account_limits()), FakeClock(T0)
    with tallying() as outer:
        ledger.charge(NFL, at=clock.read(), rows=2)
        with tallying() as inner:
            ledger.charge(NFL, at=clock.read(), rows=3)
        ledger.charge(NFL, at=clock.read())
    ledger.charge(NFL, at=clock.read(), rows=5)
    assert (outer.requests, outer.rows) == (2, 2)
    assert (inner.requests, inner.rows) == (1, 3)
