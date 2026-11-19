"""The quota ledger: what each provider account allows, what the board has spent of it, and what may go next.

Each provider account the board spends has limits (`AccountLimits`), any of which may be absent: requests a
minute, requests a month, rows a month, and asks running at once. One `QuotaLedger` counts every account,
whichever of the board's sources asks it, so two sources that hit one account share its limits.
- **A minute** is a sliding window of the last 60 seconds on the monotonic clock. A provider that counts a
  fixed UTC minute (NFLMeta does) never sees more in its minute than the window allowed.
- **A month** is the UTC calendar month (`BillingMonth`), which is NFLMeta's: its quota headers count "the
  current UTC month". Its requests and rows start at zero when a new month begins.

Two calls keep it:
- **`admit`**, before an ask goes out, with the ask's class (a `PollPriority`) and the rows it's expected to
  bill. A refusal says why (`Refusal`). An admitted ask holds its place in the minute until its request is
  charged, so asks handed out together can't overrun the minute between them.
- **`charge`**, after each HTTP response, with the rows it billed when the response says. Pages, retries and
  failed requests that reached the server cost what they cost. When a response carries the provider's own
  count of what's left (`ProviderBalance`), the ledger takes it if it shows more spent than its own count,
  never less, and a provider that says nothing is left has exhausted the account.

What is admitted as the month runs down is one declared table (`QuotaPolicy`, by default `SHEDDING`), read
against the tighter of the month's requests and rows, as a share of each left once the ask is spent:
backfill goes first, then background slows to half its pace and then stops, then normal, and focus runs
until the account is exhausted. The last fifth of each minute is kept for normal and focus asks, so a
burst of background work always leaves room for recovery.

The network code charges the ledger through a `Meter`, the seam each fetcher is wrapped in. While an ask
runs inside `tallying`, its charges are tallied too, so whoever asked learns what the ask cost.
"""

from __future__ import annotations

import functools
import threading
from collections import deque
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from fractions import Fraction
from types import MappingProxyType
from typing import Any, ParamSpec, Protocol, TypeVar

from omni.app.poll_plan import Demand, PollPriority
from omni.core.clock import Clock, ClockReading, MonotonicTime
from omni.core.enum import StrEnumMixin
from omni.providers.base import ProviderError

__all__ = [
    "BALLDONTLIE_FREE",
    "NFLMETA_BUILDER",
    "NFLMETA_FREE",
    "POLYMARKET_US_PUBLIC",
    "SHEDDING",
    "UNLIMITED",
    "AccountLimits",
    "Admission",
    "Billed",
    "BillingMonth",
    "ClassRule",
    "HttpAnswer",
    "Measure",
    "MeteredHttp",
    "Meter",
    "MonthUsage",
    "NflMetaPlan",
    "ProviderAccount",
    "ProviderBalance",
    "QuotaExhausted",
    "QuotaLedger",
    "QuotaPolicy",
    "Refusal",
    "Tally",
    "UsageStore",
    "account_limits",
    "nflmeta_billed",
    "spent_notice",
    "tallying",
    "usage_line",
]

P = ParamSpec("P")
R = TypeVar("R")

_MINUTE = timedelta(minutes=1)
SAVE_EVERY = timedelta(minutes=1)  # the usage is written at most this often, and at shutdown


class ProviderAccount(StrEnumMixin, str, Enum):
    """An account the board spends: one provider's key, or its public endpoints as this board reaches them."""

    MLB_STATSAPI = "mlb_statsapi"
    NFLMETA = "nflmeta"
    BALLDONTLIE = "balldontlie"
    POLYMARKET_US = "polymarket_us"
    BOVADA = "bovada"
    MARKET_SERVICE = "market_service"  # Omni's own market service (`cloud/`)

    @property
    def label(self) -> str:
        """The account's name, as a reason or a log line reads it."""
        return _LABELS[self]


_LABELS = {
    ProviderAccount.MLB_STATSAPI: "MLB StatsAPI",
    ProviderAccount.NFLMETA: "NFLMeta",
    ProviderAccount.BALLDONTLIE: "BALLDONTLIE",
    ProviderAccount.POLYMARKET_US: "Polymarket US",
    ProviderAccount.BOVADA: "Bovada",
    ProviderAccount.MARKET_SERVICE: "the market service",
}


@dataclass(frozen=True, slots=True)
class AccountLimits:
    """What one account allows; None for a limit it doesn't have."""

    per_minute: int | None = None
    requests_a_month: int | None = None
    rows_a_month: int | None = None
    at_once: int | None = None  # asks running at the same time, across the whole account

    def __post_init__(self) -> None:
        limits = (self.per_minute, self.requests_a_month, self.rows_a_month, self.at_once)
        if any(limit is not None and limit < 1 for limit in limits):
            raise ValueError("an account's limits must each allow at least one")


UNLIMITED = AccountLimits()  # counted, never held back
NFLMETA_FREE = AccountLimits(per_minute=20, requests_a_month=5_000, rows_a_month=25_000)
NFLMETA_BUILDER = AccountLimits(per_minute=120, requests_a_month=75_000, rows_a_month=250_000)
BALLDONTLIE_FREE = AccountLimits(per_minute=5)
POLYMARKET_US_PUBLIC = AccountLimits(per_minute=25 * 60)  # 25 a second per IP


class NflMetaPlan(StrEnumMixin, str, Enum):
    """The NFLMeta plan the board's key is on. The user chooses it; the board never assumes a paid one.

    Free comes first, and Builder once Free's month is spent: the user switches the account, and restarts
    the board with the new plan. The month's counts carry over, since they're what NFLMeta bills.
    """

    FREE = "free"
    BUILDER = "builder"  # $12 a month

    @property
    def limits(self) -> AccountLimits:
        return NFLMETA_BUILDER if self is NflMetaPlan.BUILDER else NFLMETA_FREE

    @property
    def label(self) -> str:
        """The plan's name, as NFLMeta writes it: "Free", "Builder"."""
        return str(self).capitalize()

    @property
    def upgrade(self) -> NflMetaPlan | None:
        """The plan to switch to once this one's month is spent, if there is one."""
        return NflMetaPlan.BUILDER if self is NflMetaPlan.FREE else None

    @classmethod
    def on(cls, limits: AccountLimits) -> NflMetaPlan | None:
        """The plan whose limits these are, if any is."""
        return next((plan for plan in cls if plan.limits == limits), None)

    def spent_notice(self, month: BillingMonth) -> str | None:
        """What a log says, once, when this plan's month is spent and a bigger plan is the next step."""
        upgrade = self.upgrade
        if upgrade is None:
            return None
        return (
            f"NFLMeta {self.label} is spent for {month}: switch the account to {upgrade.label}"
            f" and restart with --nflmeta-plan {upgrade.value}"
        )


def account_limits(nflmeta: NflMetaPlan = NflMetaPlan.FREE) -> dict[ProviderAccount, AccountLimits]:
    """Every account the board spends, with today's known limits: NFLMeta's by its plan, BALLDONTLIE's Free,
    Polymarket US's per-IP rate, and the rest unlimited."""
    limits = dict.fromkeys(ProviderAccount, UNLIMITED)
    limits[ProviderAccount.NFLMETA] = nflmeta.limits
    limits[ProviderAccount.BALLDONTLIE] = BALLDONTLIE_FREE
    limits[ProviderAccount.POLYMARKET_US] = POLYMARKET_US_PUBLIC
    return limits


class Measure(StrEnumMixin, str, Enum):
    """What a month counts."""

    REQUESTS = "requests"
    ROWS = "rows"


class Refusal(StrEnumMixin, str, Enum):
    """Why an ask was held back."""

    PER_MINUTE = "per_minute"  # the minute's share open to its class is used up
    REQUESTS_RESERVE = "requests_reserve"  # the month's requests left are held for more important asks
    ROWS_RESERVE = "rows_reserve"  # the month's rows left are held for more important asks
    EXHAUSTED = "exhausted"  # nothing is left this month


@dataclass(frozen=True, slots=True, kw_only=True)
class ClassRule:
    """When one class of ask is admitted, by the share of the month that would be left once it's spent."""

    above: int  # a percent: at its own cadence while more than this is left; 0 for as long as the ask fits
    slowed_above: int | None = None  # a percent: below `above` and above this, at a slower cadence
    uses_minute_reserve: bool = False  # whether it may use the last share of the minute

    def __post_init__(self) -> None:
        if not 0 <= self.above <= 100:
            raise ValueError("a class's threshold is a percent")
        if self.slowed_above is not None and not 0 <= self.slowed_above < self.above:
            raise ValueError("a class slows down above a lower threshold than the one it runs at")


@dataclass(frozen=True, slots=True, kw_only=True)
class QuotaPolicy:
    """The shedding order as the month runs down, and how much of each minute is kept back."""

    rules: Mapping[PollPriority, ClassRule]
    slowed_by: int = 2  # a slowed class asks this many times its cadence apart
    minute_reserve: int = 20  # a percent of the minute's capacity, kept for the classes that may use it

    def __post_init__(self) -> None:
        if set(self.rules) != set(PollPriority):
            raise ValueError("a quota policy has a rule for every class")
        if self.slowed_by < 2 or not 0 <= self.minute_reserve < 100:
            raise ValueError("a slowed class goes at least twice as slowly, and a minute's reserve is a percent")


# The month's shedding order: backfill (pregame details, odds, records) first, then background (finals settling,
# other dates), then normal; focus (a favorite's live game, a decisive or terminal read) runs until it's gone.
SHEDDING = QuotaPolicy(
    rules=MappingProxyType(
        {
            PollPriority.BACKFILL: ClassRule(above=50),
            PollPriority.BACKGROUND: ClassRule(above=25, slowed_above=10),
            PollPriority.NORMAL: ClassRule(above=5, uses_minute_reserve=True),
            PollPriority.FOCUS: ClassRule(above=0, uses_minute_reserve=True),
        }
    )
)


@dataclass(frozen=True, slots=True, kw_only=True)
class Admission:
    """The ledger's answer to an ask: admitted (perhaps at a slower cadence), or refused and why."""

    account: ProviderAccount
    priority: PollPriority
    refusal: Refusal | None = None
    slowed_by: int = 1  # admitted at this many times its cadence
    measure: Measure | None = None  # the tighter of the month's measures, when the month has a limit
    left: int | None = None  # what's left of that measure this month, as a whole percent
    used: int | None = None  # requests in the last minute, when the minute refused it
    per_minute: int | None = None
    retry_at: MonotonicTime | None = None  # when the minute has room again, when the minute refused it
    plan: NflMetaPlan | None = None  # NFLMeta's plan, when the month is spent on it
    month: BillingMonth | None = None  # the month that's spent

    @property
    def admitted(self) -> bool:
        return self.refusal is None

    @property
    def notice(self) -> str | None:
        """When the month is spent on a plan with a bigger one next, what a log says once, with the next step."""
        if self.refusal is not Refusal.EXHAUSTED or self.plan is None or self.month is None:
            return None
        return self.plan.spent_notice(self.month)

    @property
    def slowed(self) -> bool:
        return self.slowed_by > 1

    @property
    def held_back(self) -> bool:
        """Whether the ledger is holding this ask back: refused, or slowed."""
        return not self.admitted or self.slowed

    @property
    def reason(self) -> str:
        """A short phrase for a log or a health line, e.g. "quota: NFLMeta rows 8% left, background slowed"."""
        name, kind = self.account.label, self.priority.name.lower()
        if self.refusal is Refusal.PER_MINUTE:
            return f"quota: {name} {self.used} of {self.per_minute} a minute used, {kind} waiting"
        if self.refusal is Refusal.EXHAUSTED:
            upgrade = self.plan.upgrade if self.plan is not None else None
            if self.plan is not None and upgrade is not None:
                return f"quota: {name} {self.plan.label} spent for {self.month}: switch to {upgrade.label}"
            return f"quota: {name} {self.measure.value} exhausted" if self.measure else f"quota: {name} exhausted"
        if not self.held_back:
            return f"quota: {name} {kind} admitted"
        state = "slowed" if self.admitted else "held back"
        return f"quota: {name} {self.measure} {self.left}% left, {kind} {state}"


@dataclass(frozen=True, slots=True, order=True)
class BillingMonth:
    """A UTC calendar month, which a provider's monthly quota counts."""

    year: int
    month: int

    def __post_init__(self) -> None:
        if not 1 <= self.month <= 12:
            raise ValueError("a month is 1 to 12")

    @classmethod
    def of(cls, instant: datetime) -> BillingMonth:
        utc = instant if instant.tzinfo is timezone.utc else instant.astimezone(timezone.utc)
        return cls(utc.year, utc.month)

    @classmethod
    def parse(cls, text: str) -> BillingMonth:
        """A month written as ``2026-11``."""
        year, _, month = text.partition("-")
        return cls(int(year), int(month))

    def __str__(self) -> str:
        return f"{self.year:04d}-{self.month:02d}"


@dataclass(frozen=True, slots=True)
class MonthUsage:
    """What one account has spent in one billing month."""

    month: BillingMonth
    requests: int = 0
    rows: int = 0
    exhausted: bool = False  # the provider said nothing is left

    def __post_init__(self) -> None:
        if self.requests < 0 or self.rows < 0:
            raise ValueError("usage can't be negative")


@dataclass(frozen=True, slots=True)
class ProviderBalance:
    """What a provider's response says is left of the month; None for what it doesn't say."""

    requests_left: int | None = None
    rows_left: int | None = None


class UsageStore(Protocol):
    """Where the month's usage is kept between runs."""

    def save(self, usage: Mapping[ProviderAccount, MonthUsage]) -> bool:
        """Write ``usage``; whether it was written."""
        ...


@dataclass(slots=True)
class Tally:
    """What the charges made inside `tallying` added up to: requests, and rows if any response said."""

    requests: int = 0
    rows: int | None = None

    def add(self, requests: int, rows: int | None) -> None:
        self.requests += requests
        if rows is not None:
            self.rows = (self.rows or 0) + rows


_TALLY: ContextVar[Tally | None] = ContextVar("omni_quota_tally", default=None)


@contextmanager
def tallying() -> Iterator[Tally]:
    """Tally what's charged on this thread until the block ends: what one ask cost."""
    tally = Tally()
    token = _TALLY.set(tally)
    try:
        yield tally
    finally:
        _TALLY.reset(token)


@dataclass(slots=True)
class _Account:
    """One account's counts: the month's, and the last minute's requests on the monotonic clock."""

    limits: AccountLimits
    month: BillingMonth
    requests: int = 0
    rows: int = 0
    exhausted: bool = False
    minute: deque[MonotonicTime] = field(default_factory=deque)
    unspent: int = 0  # admitted asks whose request hasn't been charged yet: each already holds its place

    def usage(self) -> MonthUsage:
        return MonthUsage(self.month, requests=self.requests, rows=self.rows, exhausted=self.exhausted)


@dataclass(frozen=True, slots=True)
class _Left:
    """One measure's limit, and what's left of it before and after an ask."""

    measure: Measure
    limit: int
    before: int
    after: int

    @property
    def share(self) -> Fraction:
        return Fraction(self.after, self.limit)

    @property
    def percent(self) -> int:
        return max(0, self.before) * 100 // self.limit


class QuotaLedger:
    """Every account's counts, admissions and charges. Thread-safe."""

    def __init__(
        self,
        limits: Mapping[ProviderAccount, AccountLimits],
        *,
        policy: QuotaPolicy = SHEDDING,
        usage: Mapping[ProviderAccount, MonthUsage] | None = None,
        store: UsageStore | None = None,
        save_every: timedelta = SAVE_EVERY,
    ) -> None:
        self._limits = dict(limits)
        self._policy = policy
        self._usage = dict(usage or {})
        self._accounts: dict[ProviderAccount, _Account] = {}
        self._store = store
        self._save_every = save_every
        self._saved_at: MonotonicTime | None = None
        self._dirty = False
        self._lock = threading.Lock()
        self._writing = threading.Lock()

    @property
    def accounts(self) -> frozenset[ProviderAccount]:
        return frozenset(self._limits)

    def limits(self, account: ProviderAccount) -> AccountLimits:
        return self._limits[account]

    def usage(self, account: ProviderAccount, at: ClockReading) -> MonthUsage:
        """What ``account`` has spent in the month of ``at``."""
        with self._lock:
            return self._account(account, at).usage()

    def exhausted(self, account: ProviderAccount, at: ClockReading) -> bool:
        """Whether nothing is left of ``account`` this month: no ask of any class is admitted."""
        with self._lock:
            held = self._account(account, at)
            return _exhausted(held, self._left(held, Demand(PollPriority.FOCUS))) is not None

    def admit(self, account: ProviderAccount, demand: Demand, at: ClockReading) -> Admission:
        """Whether an ask of ``demand``'s class may go out now. An admitted one holds its place in the minute."""
        rule = self._policy.rules[demand.priority]
        with self._lock:
            held = self._account(account, at)
            lefts = self._left(held, demand)
            refused = _exhausted(held, lefts)
            if refused is not None:
                return Admission(
                    account=account,
                    priority=demand.priority,
                    refusal=Refusal.EXHAUSTED,
                    measure=refused.measure,
                    left=0,
                    plan=NflMetaPlan.on(held.limits) if account is ProviderAccount.NFLMETA else None,
                    month=held.month,
                )
            tighter = min(lefts, key=lambda left: left.share, default=None)
            slowed_by = 1
            if tighter is not None and not _keeps(tighter, rule.above):
                if rule.slowed_above is None or not _keeps(tighter, rule.slowed_above):
                    refusal = Refusal.ROWS_RESERVE if tighter.measure is Measure.ROWS else Refusal.REQUESTS_RESERVE
                    return Admission(
                        account=account,
                        priority=demand.priority,
                        refusal=refusal,
                        measure=tighter.measure,
                        left=tighter.percent,
                    )
                slowed_by = self._policy.slowed_by
            measure = tighter.measure if tighter is not None else None
            left = tighter.percent if tighter is not None else None
            per_minute = held.limits.per_minute
            if per_minute is not None:
                self._prune(held, at.monotonic)
                cap = (
                    per_minute
                    if rule.uses_minute_reserve
                    else per_minute - per_minute * self._policy.minute_reserve // 100
                )
                used = len(held.minute)
                if used >= cap:
                    return Admission(
                        account=account,
                        priority=demand.priority,
                        refusal=Refusal.PER_MINUTE,
                        measure=measure,
                        left=left,
                        used=used,
                        per_minute=per_minute,
                        retry_at=held.minute[used - cap] + _MINUTE,
                    )
                held.minute.append(at.monotonic)
                held.unspent += 1
            return Admission(account=account, priority=demand.priority, slowed_by=slowed_by, measure=measure, left=left)

    def charge(
        self,
        account: ProviderAccount,
        *,
        at: ClockReading,
        requests: int = 1,
        rows: int | None = None,
        balance: ProviderBalance | None = None,
    ) -> None:
        """Count ``requests`` that reached ``account``'s server, and the ``rows`` they billed when known. The
        provider's ``balance``, when its response gave one, raises the count to it, and never lowers it."""
        with self._lock:
            held = self._account(account, at)
            held.requests += requests
            held.rows += rows or 0
            if held.limits.per_minute is not None:
                placed = min(held.unspent, requests)
                held.unspent -= placed
                held.minute.extend([at.monotonic] * (requests - placed))
            if balance is not None:
                _reconcile(held, balance)
            self._dirty = True
        tally = _TALLY.get()
        if tally is not None:
            tally.add(requests, rows)
        self.save(at)

    def save(self, at: ClockReading, *, force: bool = False) -> None:
        """Write the month's usage to the store, if it changed: at most once a `save_every`, or now with
        ``force`` (at shutdown)."""
        if self._store is None:
            return
        with self._writing:
            with self._lock:
                saved = self._saved_at
                due = saved is None or at.monotonic.since(saved) >= self._save_every
                if not self._dirty or not (force or due):
                    return
                usage = {**self._usage, **{account: held.usage() for account, held in self._accounts.items()}}
                self._dirty, self._saved_at = False, at.monotonic
            if not self._store.save(usage):
                with self._lock:
                    self._dirty = True  # try again at the next chance

    def _account(self, account: ProviderAccount, at: ClockReading) -> _Account:
        """The account's counts for the month of ``at``. A new month starts from zero; a clock set back into
        an earlier month counts on into the later one, since nothing spent is ever given back.

        Usage carried over from an earlier run is judged against this run's limits: a provider's word that
        nothing was left already raised the counts to the limit it was given under, so an account the ledger
        limits by the month keeps only the counts, and a bigger plan since (Free, then Builder) has room
        again. An account with no monthly limit has only the word, and keeps it.
        """
        month = BillingMonth.of(at.utc)
        held = self._accounts.get(account)
        if held is None:
            limits = self._limits[account]  # an account the ledger doesn't know fails here, loudly
            usage = self._usage.pop(account, None)
            if usage is not None and usage.month >= month:
                counted = limits.requests_a_month is not None or limits.rows_a_month is not None
                exhausted = usage.exhausted and not counted
                held = _Account(limits, usage.month, usage.requests, usage.rows, exhausted)
            else:
                held = _Account(limits, month)
            self._accounts[account] = held
        elif month > held.month:
            held.month, held.requests, held.rows, held.exhausted = month, 0, 0, False
            self._dirty = True
        return held

    def _left(self, held: _Account, demand: Demand) -> list[_Left]:
        """What's left of each of the month's limited measures, before and after the ask."""
        lefts: list[_Left] = []
        limits = held.limits
        if limits.requests_a_month is not None:
            before = limits.requests_a_month - held.requests
            lefts.append(_Left(Measure.REQUESTS, limits.requests_a_month, before, before - 1))
        if limits.rows_a_month is not None:
            before = limits.rows_a_month - held.rows
            lefts.append(_Left(Measure.ROWS, limits.rows_a_month, before, before - demand.rows))
        return lefts

    @staticmethod
    def _prune(held: _Account, now: MonotonicTime) -> None:
        while held.minute and now.since(held.minute[0]) >= _MINUTE:
            held.minute.popleft()


@dataclass(frozen=True, slots=True)
class _Exhausted:
    measure: Measure | None  # None: the provider said so, of a measure the ledger has no limit for


def _exhausted(held: _Account, lefts: list[_Left]) -> _Exhausted | None:
    """Why nothing is left this month, if nothing is."""
    for left in lefts:
        if left.before <= 0:
            return _Exhausted(left.measure)
    return _Exhausted(None) if held.exhausted else None


def _keeps(left: _Left, percent: int) -> bool:
    """Whether what's left after the ask stays above ``percent`` of the month; at 0, whether the ask fits."""
    return left.after >= 0 if percent == 0 else left.after * 100 > left.limit * percent


def _reconcile(held: _Account, balance: ProviderBalance) -> None:
    """Raise the counts to what the provider says is spent; a provider saying none is left exhausts the month."""
    limits = held.limits
    if balance.requests_left is not None:
        if limits.requests_a_month is not None:
            held.requests = max(held.requests, limits.requests_a_month - balance.requests_left)
        held.exhausted = held.exhausted or balance.requests_left <= 0
    if balance.rows_left is not None:
        if limits.rows_a_month is not None:
            held.rows = max(held.rows, limits.rows_a_month - balance.rows_left)
        held.exhausted = held.exhausted or balance.rows_left <= 0


def spent_notice(ledger: QuotaLedger, at: ClockReading) -> str | None:
    """When NFLMeta's month is spent on a plan with a bigger one next, what a log says, with the next step."""
    account = ProviderAccount.NFLMETA
    plan = NflMetaPlan.on(ledger.limits(account))
    if plan is None or not ledger.exhausted(account, at):
        return None
    return plan.spent_notice(ledger.usage(account, at).month)


def usage_line(ledger: QuotaLedger, account: ProviderAccount, at: ClockReading) -> str:
    """One line for a log: the month's requests and rows so far, against the account's limits."""
    usage, limits = ledger.usage(account, at), ledger.limits(account)

    def against(spent: int, limit: int | None, what: str) -> str:
        return f"{spent:,} of {limit:,} {what}" if limit is not None else f"{spent:,} {what} (no limit)"

    minute = f", {limits.per_minute:,} a minute" if limits.per_minute is not None else ""
    return (
        f"quota: {account.label} in {usage.month}: {against(usage.requests, limits.requests_a_month, 'requests')},"
        f" {against(usage.rows, limits.rows_a_month, 'rows')}{minute}"
    )


# --- the metering seam -----------------------------------------------------------------------------------


class QuotaExhausted(ProviderError):
    """An account has nothing left this month, so the request was never made."""


@dataclass(frozen=True, slots=True)
class Billed:
    """What one response says it cost, and what it says is left."""

    rows: int | None = None
    balance: ProviderBalance | None = None


@dataclass(frozen=True, slots=True)
class HttpAnswer:
    """An HTTP response's parsed body, with its headers (names lowercased)."""

    body: Any
    headers: Mapping[str, str]


def nflmeta_billed(headers: Mapping[str, str]) -> Billed:
    """What an NFLMeta response billed (``x-nflmeta-response-rows``) and what it says is left of the UTC month
    (``x-monthly-quota-remaining`` for requests, ``x-monthly-row-remaining`` for rows, absent on a plan with no
    row ceiling). A header that's missing or unreadable says nothing."""
    requests_left = _count(headers.get("x-monthly-quota-remaining"))
    rows_left = _count(headers.get("x-monthly-row-remaining"))
    balance = ProviderBalance(requests_left, rows_left) if (requests_left, rows_left) != (None, None) else None
    return Billed(rows=_count(headers.get("x-nflmeta-response-rows")), balance=balance)


def _count(raw: str | None) -> int | None:
    try:
        return int(raw) if raw is not None else None
    except ValueError:
        return None


def _unbilled(headers: Mapping[str, str]) -> Billed:
    return Billed()


class Meter:
    """Charges one account of the ledger for each response its fetcher gets, from the response's headers."""

    def __init__(
        self,
        ledger: QuotaLedger,
        account: ProviderAccount,
        clock: Clock,
        *,
        billed: Callable[[Mapping[str, str]], Billed] = _unbilled,
    ) -> None:
        if account not in ledger.accounts:
            raise ValueError(f"the ledger has no {account.label} account to charge")
        self._ledger = ledger
        self._account = account
        self._clock = clock
        self._billed = billed

    @property
    def account(self) -> ProviderAccount:
        return self._account

    def check(self) -> None:
        """Raise `QuotaExhausted` when nothing is left this month, before a request is made."""
        if self._ledger.exhausted(self._account, self._clock.read()):
            raise QuotaExhausted(f"quota: {self._account.label} exhausted for the month; not asking")

    def charge(self, headers: Mapping[str, str] | None = None) -> None:
        """One response arrived: charge one request, and what its headers say it billed and left."""
        billed = self._billed(headers) if headers is not None else Billed()
        self._ledger.charge(self._account, at=self._clock.read(), rows=billed.rows, balance=billed.balance)

    def counted(self, fetch: Callable[P, R]) -> Callable[P, R]:
        """``fetch``, made one request a call with rows unknown. A fetcher this plain can't tell whether a
        failure reached the server, so each call counts, answered or not."""

        @functools.wraps(fetch)
        def call(*args: P.args, **kwargs: P.kwargs) -> R:
            self.check()
            try:
                return fetch(*args, **kwargs)
            finally:
                self.charge()

        return call


class MeteredHttp:
    """A fetcher that reads an `HttpAnswer` and returns its body, charging the meter for each response: an
    answer, or an HTTP error the server sent (it carries headers). A failure with no response (a timeout, no
    route) charges nothing here: the minute already counted the ask, and the provider's own count of what's
    left catches the month up at its next answer."""

    def __init__(self, get: Callable[[str, Mapping[str, str | int]], HttpAnswer], meter: Meter) -> None:
        self._get = get
        self._meter = meter

    def __call__(self, path: str, query: Mapping[str, str | int]) -> Any:
        self._meter.check()
        try:
            answer = self._get(path, query)
        except Exception as exc:
            headers = getattr(exc, "headers", None)
            if headers is not None:
                self._meter.charge(_lowered(headers))
            raise
        self._meter.charge(_lowered(answer.headers))
        return answer.body


def _lowered(headers: Any) -> dict[str, str]:
    """Headers with their names lowercased, from a mapping or an HTTP message."""
    return {str(name).lower(): str(value) for name, value in headers.items()}
