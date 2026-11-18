"""NFLMeta budget replay: a synthetic November of NFL games, read through the real quota ledger at chosen read
strategies, and what each costs on each plan.

**The schedule** is declared here, not recorded:
- A typical week has 16 games: Thursday 1 (20:15 ET); Sunday 9 early (13:00), 4 late (two at 16:05, two at
  16:25) and 1 at night (20:20); Monday 1 (20:15). That is already a full 16-game week, so it's the heavy
  week too.
- A bye week has 13: three of Sunday's early games are off.
- Thanksgiving week has 16, three of them on Thursday (12:30, 16:30, 20:20), so Sunday has two fewer.
- November 2026 is the UTC billing month: Sunday Nov 1 and Monday Nov 2 (a bye week whose Thursday falls in
  October), a bye week, two typical weeks, and Thanksgiving week. Monday Nov 30's game kicks off at 01:15 UTC
  on December 1, so it bills to December and isn't counted. That's 5 Sundays (11, 11, 14, 14 and 12 games),
  4 Thursdays (1, 1, 1 and 3) and 4 Mondays: 72 games.
- The favorite (CAR) plays one of the 13:00 games every Sunday: 5 games.
- A game lasts 3 h 10 min, with the halftime hold (13 minutes) in its middle. Its injury entries grow
  linearly to 4 by the final, and stay listed after it.

**The billing model**, as measured on PIT@CIN (2026-11-15):
- A live-scores read bills one row per game it lists plus one per injury entry those games carry. Read
  unfiltered, it lists the whole day's slate, finals and games not yet started included. Filtered to one
  game (``game_id``), it bills that game and its injuries. NFLMeta's docs also offer ``phase=in`` (games in
  progress, halftime included) and ``game_ids`` (several games, intersected with the phase).
- A game's play feed bills one row per change: about 280 over a game, read "on change" in 274 reads spread
  over the time in play, an empty read billing none. Any read costs one request.
- A followed game is read every 10 s (NFLMeta's recommended pace), not at all through the halftime hold, and
  through the final's drain (`FINAL_DRAIN` at the same pace, its boundary read included).

**The strategies** (`STRATEGIES`): A, today's solo favorite; B, the shared board with one unfiltered slate
read every 30 s or 45 s while any game is live, the favorite filtered at 10 s with its plays, and a drain
for each final; C, B with each live game read filtered instead; D, B with plays for every live game; E,
the cheapest found that keeps every live game's score within 45 s and the favorite's within 10 s.

Each read is admitted, or not, by the real `QuotaLedger` under a `QuotaPolicy`, as the plan would, and
charged what the model bills: a refused read is skipped (its plays come with the next read of that game),
one the minute holds waits for room, and a slowed one stretches its reader's cadence. `budget_report`
writes the result as markdown, deterministically.
"""

from __future__ import annotations

import heapq
import math
from collections import Counter, deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from enum import Enum
from zoneinfo import ZoneInfo

from omni.app.poll_plan import Demand, PollPriority
from omni.app.quota import (
    SHEDDING,
    UNLIMITED,
    AccountLimits,
    BillingMonth,
    NflMetaPlan,
    ProviderAccount,
    QuotaLedger,
    QuotaPolicy,
    Refusal,
)
from omni.core.clock import ClockReading, MonotonicTime
from omni.core.enum import StrEnumMixin
from omni.providers.nflmeta import DEFAULT_POLL, FINAL_DRAIN, HALFTIME_HOLD

__all__ = [
    "GAME_LENGTH",
    "INJURIES_BY_FINAL",
    "MONTH",
    "NOVEMBER_2026",
    "PLAY_READS",
    "PLAY_ROWS",
    "STRATEGIES",
    "TYPICAL_SUNDAY",
    "Drain",
    "Game",
    "PlayScope",
    "ReadKind",
    "Run",
    "SlateRead",
    "Strategy",
    "WeekKind",
    "budget_report",
    "month_games",
    "replay",
    "restart_cost",
]

EASTERN = ZoneInfo("America/New_York")
GAME_LENGTH = timedelta(hours=3, minutes=10)
FIRST_HALF = (GAME_LENGTH - HALFTIME_HOLD) / 2  # the halftime hold sits in the middle of the game
INJURIES_BY_FINAL = 4
PLAY_READS = 274  # reads of a game's play feed "on change", measured
PLAY_ROWS = 280  # a game's change rows, measured (277 read whole at the end)
PLAY_PAGE = 100  # change rows a page, as the board reads them
LIVE_PACE = DEFAULT_POLL.as_timedelta()
MONTH = BillingMonth(2026, 11)
TYPICAL_SUNDAY = date(2026, 11, 15)
_MINUTE = timedelta(minutes=1)
_NFL = ProviderAccount.NFLMETA


class WeekKind(StrEnumMixin, str, Enum):
    TYPICAL = "typical"
    BYE = "bye"
    THANKSGIVING = "thanksgiving"


# Each week's kickoffs, from its Thursday: (days after Thursday, Eastern time, games).
_WEEKS: Mapping[WeekKind, tuple[tuple[int, time, int], ...]] = {
    WeekKind.TYPICAL: (
        (0, time(20, 15), 1),
        (3, time(13, 0), 9),
        (3, time(16, 5), 2),
        (3, time(16, 25), 2),
        (3, time(20, 20), 1),
        (4, time(20, 15), 1),
    ),
    WeekKind.BYE: (
        (0, time(20, 15), 1),
        (3, time(13, 0), 6),
        (3, time(16, 5), 2),
        (3, time(16, 25), 2),
        (3, time(20, 20), 1),
        (4, time(20, 15), 1),
    ),
    WeekKind.THANKSGIVING: (
        (0, time(12, 30), 1),
        (0, time(16, 30), 1),
        (0, time(20, 20), 1),
        (3, time(13, 0), 7),
        (3, time(16, 5), 2),
        (3, time(16, 25), 2),
        (3, time(20, 20), 1),
        (4, time(20, 15), 1),
    ),
}

# November 2026's weeks, by their Thursdays.
NOVEMBER_2026: tuple[tuple[date, WeekKind], ...] = (
    (date(2026, 10, 29), WeekKind.BYE),
    (date(2026, 11, 5), WeekKind.BYE),
    (date(2026, 11, 12), WeekKind.TYPICAL),
    (date(2026, 11, 19), WeekKind.TYPICAL),
    (date(2026, 11, 26), WeekKind.THANKSGIVING),
)


@dataclass(frozen=True, slots=True)
class Game:
    """One synthetic game: the Eastern date whose slate lists it, its kickoff, and whether it's the favorite's."""

    day: date
    kickoff: datetime
    favorite: bool = False

    @property
    def final(self) -> datetime:
        return self.kickoff + GAME_LENGTH

    @property
    def halftime(self) -> tuple[datetime, datetime]:
        """When the halftime hold begins and ends."""
        start = self.kickoff + FIRST_HALF
        return start, start + HALFTIME_HOLD

    def live(self, at: datetime) -> bool:
        """In progress (halftime included): what ``phase=in`` lists."""
        return self.kickoff <= at < self.final

    def injuries(self, at: datetime) -> int:
        """The injury entries its row carries at ``at``: none before kickoff, growing to four by the final."""
        if at < self.kickoff:
            return 0
        if at >= self.final:
            return INJURIES_BY_FINAL
        return (at - self.kickoff) * INJURIES_BY_FINAL // GAME_LENGTH

    def row(self, at: datetime) -> int:
        """The rows its scoreboard row bills at ``at``: the game, and its injuries."""
        return 1 + self.injuries(at)

    def plays(self) -> list[tuple[datetime, int]]:
        """Its play-feed reads on change, and the rows each bills: `PLAY_READS` spread over the time in play,
        `PLAY_ROWS` among them."""
        playing = GAME_LENGTH - HALFTIME_HOLD
        reads: list[tuple[datetime, int]] = []
        for k in range(PLAY_READS):
            offset = playing * (k + 1) / PLAY_READS
            held = HALFTIME_HOLD if offset > FIRST_HALF else timedelta(0)
            rows = (k + 1) * PLAY_ROWS // PLAY_READS - k * PLAY_ROWS // PLAY_READS
            reads.append((self.kickoff + offset + held, rows))
        return reads

    def rows_by(self, at: datetime) -> int:
        """Its play rows written by ``at``: what reading its whole history then would bill."""
        return sum(rows for when, rows in self.plays() if when <= at)


def month_games(weeks: Sequence[tuple[date, WeekKind]] = NOVEMBER_2026, month: BillingMonth = MONTH) -> list[Game]:
    """Every game of ``weeks`` that kicks off in the UTC billing ``month``, in kickoff order; the first 13:00
    game of each Sunday is the favorite's."""
    games: list[Game] = []
    for thursday, kind in weeks:
        for days, kickoff, count in _WEEKS[kind]:
            day = thursday + timedelta(days=days)
            at = datetime.combine(day, kickoff, EASTERN).astimezone(timezone.utc)
            for n in range(count):
                favorite = day.weekday() == 6 and kickoff == time(13, 0) and n == 0
                games.append(Game(day=day, kickoff=at, favorite=favorite))
    return sorted((game for game in games if BillingMonth.of(game.kickoff) == month), key=lambda game: game.kickoff)


class SlateRead(StrEnumMixin, str, Enum):
    """How the other games' scores are read."""

    UNFILTERED = "unfiltered"  # one read lists the day's games, finals and all, with their injuries
    EACH_GAME = "each_game"  # each live game read filtered, the favorite aside (it's read on its own)
    LIVE_ONLY = "live_only"  # one read of the games in progress (phase=in), the favorite left out by id


class PlayScope(StrEnumMixin, str, Enum):
    """Whose play feeds are read."""

    FAVORITE = "favorite"
    EVERY_LIVE = "every_live"


class Drain(StrEnumMixin, str, Enum):
    """How another game's final is read once it ends."""

    NONE = "none"
    FULL = "full"  # the provider's drain: at the live pace for `FINAL_DRAIN`, its boundary read included
    ONE = "one"  # a single filtered read, once it has left phase=in, for its final score


@dataclass(frozen=True, slots=True)
class Strategy:
    """How a board reads NFLMeta. The favorite's game is always read filtered at the live pace, with its plays."""

    key: str
    summary: str
    slate: SlateRead | None = None
    slate_every: timedelta = timedelta(seconds=30)
    plays: PlayScope = PlayScope.FAVORITE
    drain: Drain = Drain.NONE
    day_list: bool = False  # one unfiltered read before the day's first kickoff, for the day's game ids


_S = timedelta(seconds=1)
STRATEGIES: tuple[Strategy, ...] = (
    Strategy("A", "today's solo favorite: CAR filtered at 10 s, its plays on change"),
    Strategy(
        "B30",
        "an unfiltered slate read every 30 s while any game is live; the favorite at 10 s with its plays; a"
        " drain for each final",
        SlateRead.UNFILTERED,
        30 * _S,
        drain=Drain.FULL,
    ),
    Strategy("B45", "B with the slate read every 45 s", SlateRead.UNFILTERED, 45 * _S, drain=Drain.FULL),
    Strategy(
        "C30",
        "B with each live game read filtered every 30 s instead of the slate",
        SlateRead.EACH_GAME,
        30 * _S,
        drain=Drain.FULL,
    ),
    Strategy("C45", "C with each game read every 45 s", SlateRead.EACH_GAME, 45 * _S, drain=Drain.FULL),
    Strategy(
        "D30",
        "B30 with plays on change for every live game",
        SlateRead.UNFILTERED,
        30 * _S,
        PlayScope.EVERY_LIVE,
        Drain.FULL,
    ),
    Strategy(
        "E",
        "one phase=in read every 45 s while another game is live, the favorite left out by id; the day's game"
        " ids from one unfiltered read before its first kickoff; the favorite at 10 s with its plays; one"
        " filtered read for each other final",
        SlateRead.LIVE_ONLY,
        45 * _S,
        drain=Drain.ONE,
        day_list=True,
    ),
)


class ReadKind(StrEnumMixin, str, Enum):
    """What a read is for, and the class the board asks it at."""

    FAVORITE = "favorite score reads"
    FAVORITE_PLAYS = "favorite play reads"
    DRAIN = "final drain reads"
    OTHER_PLAYS = "other games' play reads"
    SLATE = "slate reads"
    GAME_SCORES = "other games' score reads"
    LIVE_GAMES = "live-games reads"
    DAY_LIST = "day's game list reads"

    @property
    def priority(self) -> PollPriority:
        return _CLASSES[self]


_CLASSES = {
    ReadKind.FAVORITE: PollPriority.FOCUS,
    ReadKind.FAVORITE_PLAYS: PollPriority.FOCUS,
    ReadKind.DRAIN: PollPriority.FOCUS,
    ReadKind.OTHER_PLAYS: PollPriority.NORMAL,
    ReadKind.SLATE: PollPriority.BACKGROUND,
    ReadKind.GAME_SCORES: PollPriority.BACKGROUND,
    ReadKind.LIVE_GAMES: PollPriority.BACKGROUND,
    ReadKind.DAY_LIST: PollPriority.BACKFILL,
}


class _Periodic:
    """Reads every ``every`` within each of its windows, billing ``bill(at)``."""

    def __init__(
        self,
        kind: ReadKind,
        windows: Sequence[tuple[datetime, datetime]],
        every: timedelta,
        bill: Callable[[datetime], int],
    ) -> None:
        self.kind = kind
        self.expected = 1  # the rows its next read is expected to bill: its last read's
        self._windows = sorted(windows)
        self._every = every
        self._bill = bill
        self._window = 0
        self.due: datetime | None = self._windows[0][0]

    def rows(self, at: datetime) -> int:
        return self._bill(at)

    def read(self, at: datetime, slowed_by: int) -> None:
        self._place(at + self._every * slowed_by)

    def skip(self, at: datetime) -> None:
        self._place(at + self._every)

    def wait(self, until: datetime) -> None:
        self._place(until)

    def _place(self, at: datetime) -> None:
        """Due at ``at``, or at a later window's start once ``at`` is past its window; done past the last."""
        while self._window < len(self._windows) and at >= self._windows[self._window][1]:
            self._window += 1
            if self._window < len(self._windows):
                at = max(at, self._windows[self._window][0])
        self.due = at if self._window < len(self._windows) else None


class _Scheduled:
    """Reads at set times, each billing set rows; a refused read's rows come with the next one."""

    def __init__(self, kind: ReadKind, reads: Sequence[tuple[datetime, int]]) -> None:
        self.kind = kind
        self.expected = 1
        self._reads = list(reads)
        self._next = 0
        self._carried = 0
        self.due: datetime | None = self._reads[0][0]

    def rows(self, at: datetime) -> int:
        return self._reads[self._next][1] + self._carried

    def read(self, at: datetime, slowed_by: int) -> None:
        self._carried = 0
        self._step(at)

    def skip(self, at: datetime) -> None:
        self._carried += self._reads[self._next][1]
        self._step(at)

    def wait(self, until: datetime) -> None:
        self.due = until

    def _step(self, at: datetime) -> None:
        self._next += 1
        self.due = max(self._reads[self._next][0], at) if self._next < len(self._reads) else None


_Reader = _Periodic | _Scheduled


def _merged(windows: Iterable[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    """Overlapping windows joined."""
    joined: list[tuple[datetime, datetime]] = []
    for start, end in sorted(windows):
        if joined and start <= joined[-1][1]:
            joined[-1] = (joined[-1][0], max(joined[-1][1], end))
        else:
            joined.append((start, end))
    return joined


def _readers(games: Sequence[Game], strategy: Strategy) -> list[_Reader]:
    """Every reader the strategy runs over ``games``."""
    readers: list[_Reader] = []
    drain_end = FINAL_DRAIN + timedelta(milliseconds=1)  # the drain's boundary read is at its end
    for game in games:
        if game.favorite:
            start, end = game.halftime
            windows = [(game.kickoff, start), (end, game.final + drain_end)]
            readers.append(_Periodic(ReadKind.FAVORITE, windows, LIVE_PACE, game.row))
            readers.append(_Scheduled(ReadKind.FAVORITE_PLAYS, game.plays()))
            continue
        if strategy.plays is PlayScope.EVERY_LIVE:
            readers.append(_Scheduled(ReadKind.OTHER_PLAYS, game.plays()))
        if strategy.drain is Drain.FULL:
            readers.append(_Periodic(ReadKind.DRAIN, [(game.final, game.final + drain_end)], LIVE_PACE, game.row))
        elif strategy.drain is Drain.ONE:
            readers.append(_Scheduled(ReadKind.DRAIN, [(game.final, game.row(game.final))]))
        if strategy.slate is SlateRead.EACH_GAME:
            windows = [(game.kickoff, game.final)]
            readers.append(_Periodic(ReadKind.GAME_SCORES, windows, strategy.slate_every, game.row))
    days: dict[date, list[Game]] = {}
    for game in games:
        days.setdefault(game.day, []).append(game)
    for day_games in days.values():
        if strategy.slate is SlateRead.UNFILTERED:
            windows = _merged((game.kickoff, game.final) for game in day_games)
            readers.append(_Periodic(ReadKind.SLATE, windows, strategy.slate_every, _listing(day_games)))
        elif strategy.slate is SlateRead.LIVE_ONLY:
            others = [game for game in day_games if not game.favorite]
            windows = _merged((game.kickoff, game.final) for game in others)
            readers.append(_Periodic(ReadKind.LIVE_GAMES, windows, strategy.slate_every, _live(others)))
        if strategy.day_list:
            first = min(game.kickoff for game in day_games) - timedelta(minutes=10)
            readers.append(_Scheduled(ReadKind.DAY_LIST, [(first, len(day_games))]))
    return readers


def _listing(games: Sequence[Game]) -> Callable[[datetime], int]:
    """An unfiltered read's rows: every game of the day, and their injuries."""
    return lambda at: sum(game.row(at) for game in games)


def _live(games: Sequence[Game]) -> Callable[[datetime], int]:
    """A phase=in read's rows: the games in progress, and their injuries."""
    return lambda at: sum(game.row(at) for game in games if game.live(at))


@dataclass(frozen=True, slots=True, kw_only=True)
class Run:
    """What one replay spent, and what the ledger held back."""

    requests: int
    rows: int
    peak_minute: int  # the most requests sent in any 60 s
    waits: int  # reads the minute made wait
    asked: Mapping[ReadKind, int]  # reads sent, by kind
    lost: Mapping[ReadKind, int]  # reads refused for the month, by kind
    slowed: Mapping[ReadKind, datetime]  # when each kind was first slowed
    shed: Mapping[ReadKind, datetime]  # when each kind was first refused for the month
    exhausted: datetime | None  # when a focus read was first refused: the month is spent, or can't fit it


def replay(
    games: Sequence[Game],
    strategy: Strategy,
    limits: AccountLimits,
    *,
    policy: QuotaPolicy = SHEDDING,
    start: datetime | None = None,
    end: datetime | None = None,
) -> Run:
    """Run ``strategy`` over ``games`` through a fresh ledger at ``limits``, from ``start`` until ``end``."""
    readers = _readers(games, strategy)
    begin = start if start is not None else min(game.kickoff for game in games) - timedelta(hours=1)
    stop = end if end is not None else max(game.final for game in games) + timedelta(hours=1)
    ledger = QuotaLedger({_NFL: limits}, policy=policy)
    queue = [(reader.due, n, reader) for n, reader in enumerate(readers) if reader.due is not None]
    heapq.heapify(queue)
    minute: deque[datetime] = deque()
    peak = waits = requests = rows = 0
    asked: Counter[ReadKind] = Counter()
    lost: Counter[ReadKind] = Counter()
    slowed: dict[ReadKind, datetime] = {}
    shed: dict[ReadKind, datetime] = {}
    exhausted: datetime | None = None
    while queue and queue[0][0] < stop:
        at, n, reader = heapq.heappop(queue)
        reading = ClockReading(at, MonotonicTime((at - begin).total_seconds()))
        admission = ledger.admit(_NFL, Demand(reader.kind.priority, reader.expected), reading)
        if admission.admitted:
            billed = reader.rows(at)
            ledger.charge(_NFL, at=reading, rows=billed)
            reader.expected = billed
            requests, rows = requests + 1, rows + billed
            asked[reader.kind] += 1
            minute.append(at)
            while at - minute[0] >= _MINUTE:
                minute.popleft()
            peak = max(peak, len(minute))
            if admission.slowed:
                slowed.setdefault(reader.kind, at)
            reader.read(at, admission.slowed_by)
        elif admission.refusal is Refusal.PER_MINUTE:
            assert admission.retry_at is not None
            waits += 1
            reader.wait(begin + timedelta(seconds=admission.retry_at.seconds))
        else:
            lost[reader.kind] += 1
            shed.setdefault(reader.kind, at)
            if reader.kind.priority is PollPriority.FOCUS and exhausted is None:
                exhausted = at
            reader.skip(at)
        if reader.due is not None:
            heapq.heappush(queue, (reader.due, n, reader))
    return Run(
        requests=requests,
        rows=rows,
        peak_minute=peak,
        waits=waits,
        asked=dict(asked),
        lost=dict(lost),
        slowed=slowed,
        shed=shed,
        exhausted=exhausted,
    )


@dataclass(frozen=True, slots=True)
class _Outcome:
    """One strategy's demand, and how each plan carries it."""

    strategy: Strategy
    sunday: Run
    month: Run
    plans: Mapping[NflMetaPlan, Run]


def _month_bounds(month: BillingMonth) -> tuple[datetime, datetime]:
    start = datetime(month.year, month.month, 1, tzinfo=timezone.utc)
    following = datetime(month.year + month.month // 12, month.month % 12 + 1, 1, tzinfo=timezone.utc)
    return start, following


def _outcomes(games: Sequence[Game], policy: QuotaPolicy) -> list[_Outcome]:
    start, end = _month_bounds(MONTH)
    sunday = [game for game in games if game.day == TYPICAL_SUNDAY]
    outcomes = []
    for strategy in STRATEGIES:
        plans = {
            plan: replay(games, strategy, plan.limits, policy=policy, start=start, end=end) for plan in NflMetaPlan
        }
        outcomes.append(
            _Outcome(
                strategy=strategy,
                sunday=replay(sunday, strategy, UNLIMITED, policy=policy),
                month=replay(games, strategy, UNLIMITED, policy=policy, start=start, end=end),
                plans=plans,
            )
        )
    return outcomes


def restart_cost(game: Game, at: datetime) -> tuple[int, int]:
    """What a board restarting at ``at`` re-reads of ``game``'s plays from the start: (requests, rows)."""
    rows = game.rows_by(at)
    return max(1, math.ceil(rows / PLAY_PAGE)), rows


# --- the report ------------------------------------------------------------------------------------------


def _day(at: datetime) -> str:
    local = at.astimezone(EASTERN)
    return f"{local:%b} {local.day}"


def _limits(plan: NflMetaPlan) -> tuple[int, int, int]:
    """A plan's requests a minute, requests a month and rows a month: every NFLMeta plan has all three."""
    limits = plan.limits
    assert limits.per_minute and limits.requests_a_month and limits.rows_a_month
    return limits.per_minute, limits.requests_a_month, limits.rows_a_month


def _fits(demand: Run, plan: NflMetaPlan) -> bool:
    per_minute, requests, rows = _limits(plan)
    return demand.requests <= requests and demand.rows <= rows and demand.peak_minute <= per_minute


def _verdict(outcome: _Outcome, plan: NflMetaPlan) -> str:
    """One cell: whether the plan carries the strategy's month, and when the ledger starts holding reads back
    and runs out."""
    run, demand = outcome.plans[plan], outcome.month
    _, requests, rows = _limits(plan)
    verdict = "no"
    if _fits(demand, plan):
        verdict = f"fits ({demand.requests * 100 // requests}% of requests, {demand.rows * 100 // rows}% of rows)"
    notes = []
    held = sorted(at for at in (*run.slowed.values(), *run.shed.values()) if not _spent(run, at))
    if held:
        notes.append(f"sheds from {_day(held[0])}")
    if run.exhausted is not None:
        notes.append(f"exhausted {_day(run.exhausted)}")
    if run.waits:
        notes.append(f"{run.waits:,} reads wait for the minute")
    if not notes:
        return verdict
    return f"{verdict}{': ' if verdict == 'no' else '; '}{', '.join(notes)}"


def _coverage(outcome: _Outcome, plan: NflMetaPlan, policy: QuotaPolicy) -> list[str]:
    """What the rest of the month gets once the ledger starts holding reads back, in order."""
    run = outcome.plans[plan]
    slowed = int((outcome.strategy.slate_every * policy.slowed_by).total_seconds())
    events: list[tuple[datetime, str]] = []
    for kind, at in run.slowed.items():
        events.append((at, f"{kind.value} every {slowed} s (background slowed)"))
    for kind, at in run.shed.items():
        if not _spent(run, at):
            events.append((at, f"no {kind.value} ({kind.priority.name.lower()} shed)"))
    if run.exhausted is not None:
        events.append((run.exhausted, "the month is spent: nothing more is read, the favorite included"))
    lines = [f"from {_day(at)}, {what}" for at, what in sorted(events)]
    if run.lost:
        lost = ", ".join(f"{count:,} {kind.value}" for kind, count in sorted(run.lost.items()))
        lines.append(f"refused for the month: {lost}")
    return lines


def _spent(run: Run, at: datetime) -> bool:
    """Whether the month was already spent by ``at``."""
    return run.exhausted is not None and at >= run.exhausted


def _row(cells: Iterable[object]) -> str:
    return "| " + " | ".join(str(cell) for cell in cells) + " |"


def budget_report(policy: QuotaPolicy = SHEDDING) -> str:
    """The budget replay's report, as markdown."""
    games = month_games()
    outcomes = _outcomes(games, policy)
    sundays = Counter(game.day for game in games if game.day.weekday() == 6)
    free, builder = NflMetaPlan.FREE.limits, NflMetaPlan.BUILDER.limits
    lines = [
        "# NFLMeta budget replay: November 2026",
        "",
        "A synthetic month of NFL games read through the board's quota ledger (`omni.app.quota`) at each read"
        " strategy, from `omni/replay/nfl_budget.py`. Regenerate with `tools/nflmeta_budget.py`.",
        "",
        f"- **Plans:** Free is {free.per_minute} requests a minute, {free.requests_a_month:,} requests and"
        f" {free.rows_a_month:,} rows a month; Builder ($12 a month) is {builder.per_minute}, "
        f"{builder.requests_a_month:,} and {builder.rows_a_month:,}. The month is the UTC calendar month.",
        f"- **Schedule:** {len(games)} games kick off in November (UTC): Sundays of "
        + ", ".join(f"{count}" for _, count in sorted(sundays.items()))
        + " games, four Thursdays (Thanksgiving's three games included), and four Monday nights (Nov 30's"
        " bills to December). The favorite (CAR) plays at 13:00 ET every Sunday. The typical Sunday is"
        f" {TYPICAL_SUNDAY:%b} {TYPICAL_SUNDAY.day}: {sum(1 for game in games if game.day == TYPICAL_SUNDAY)}"
        " games.",
        f"- **Billing model:** one row per game a live-scores read lists plus one per injury (up to"
        f" {INJURIES_BY_FINAL} a game by its final); an unfiltered read lists the whole day, finals included."
        f" A followed game's plays bill {PLAY_ROWS} rows over {PLAY_READS} reads on change. A followed game is"
        f" read every {int(LIVE_PACE.total_seconds())} s outside its {int(HALFTIME_HOLD.total_seconds()) // 60}"
        f"-minute halftime hold, through a {int(FINAL_DRAIN.total_seconds())} s drain after its final.",
        "- **Classes:** the favorite's score and plays and every final's drain are focus; other games' plays"
        " normal; the slate, the per-game and live-games reads background; the day's game list backfill.",
        "- **NFLMeta's docs** (https://nflmeta.org/api-docs/core-endpoints, https://nflmeta.org/openapi.yaml):"
        " live-scores takes `phase` (pre, in, post), `game_id`, and `game_ids` (up to 100), and has no way to"
        " leave injuries out. They say each game listed bills a row; on PIT@CIN each injury entry billed one too,"
        " which this model follows. Responses carry `X-Monthly-Quota-Remaining` and, on plans with a row"
        " ceiling, `X-Monthly-Row-Remaining`, both for the current UTC month.",
        "",
        "## Strategies",
        "",
        *(f"- **{outcome.strategy.key}:** {outcome.strategy.summary}." for outcome in outcomes),
        "",
        "## Headline",
        "",
        "Demand is what the strategy asks with nothing held back. A plan carries a strategy when the month's"
        " requests, rows and busiest minute all fit; the cells say when the ledger starts holding reads back"
        " on that plan, and when the month runs out. Games in one window kick off together and, in this model,"
        " end together, so their final drains bunch into one minute: that sets B, C and D's busiest minute.",
        "",
        _row(
            (
                "Strategy",
                "Peak a minute",
                "Sunday requests",
                "Sunday rows",
                "Month requests",
                "Month rows",
                "Free",
                "Builder",
            )
        ),
        _row(("---",) * 8),
    ]
    for outcome in outcomes:
        lines.append(
            _row(
                (
                    outcome.strategy.key,
                    outcome.month.peak_minute,
                    f"{outcome.sunday.requests:,}",
                    f"{outcome.sunday.rows:,}",
                    f"{outcome.month.requests:,}",
                    f"{outcome.month.rows:,}",
                    _verdict(outcome, NflMetaPlan.FREE),
                    _verdict(outcome, NflMetaPlan.BUILDER),
                )
            )
        )
    lines += ["", "## Which plan carries which strategy", ""]
    for plan in NflMetaPlan:
        carried = [outcome.strategy.key for outcome in outcomes if _fits(outcome.month, plan)]
        lines.append(f"- **{plan.value.title()}** carries " + (", ".join(carried) if carried else "none of them") + ".")
    lines += ["", "## What each plan does to the rest of the month", ""]
    for plan in NflMetaPlan:
        lines += [f"### {plan.value.title()}", ""]
        for outcome in outcomes:
            run = outcome.plans[plan]
            coverage = _coverage(outcome, plan, policy)
            spent = f"{run.requests:,} requests and {run.rows:,} rows read"
            lines.append(
                f"- **{outcome.strategy.key}** ({spent}): " + ("; ".join(coverage) if coverage else "all of it") + "."
            )
        lines.append("")
    lines += _restart_section(games)
    return "\n".join(lines) + "\n"


def _restart_section(games: Sequence[Game]) -> list[str]:
    favorite = next(game for game in games if game.day == TYPICAL_SUNDAY and game.favorite)
    start, end = favorite.halftime
    moments = (
        ("30 min after kickoff", favorite.kickoff + timedelta(minutes=30)),
        ("at halftime", start),
        ("an hour after halftime", end + timedelta(hours=1)),
        ("at the final", favorite.final),
    )
    lines = [
        "## A board restart",
        "",
        "The play cursor lives only in the process, so a restarted board reads each followed game's play feed"
        f" again from its start, in pages of {PLAY_PAGE}. On PIT@CIN, 19 restarts were most of the night's 4,737"
        " play rows. A persisted cursor (with the play ledger it feeds) resumes where the board stopped: one"
        " request, billing only the changes since.",
        "",
        _row(("Restart", "Requests", "Rows", "With a persisted cursor")),
        _row(("---",) * 4),
    ]
    for label, at in moments:
        requests, rows = restart_cost(favorite, at)
        lines.append(_row((f"one followed game, {label}", requests, f"{rows:,}", "1 request, new changes only")))
    afternoon = datetime.combine(TYPICAL_SUNDAY, time(15, 0), EASTERN).astimezone(timezone.utc)
    live = [game for game in games if game.day == TYPICAL_SUNDAY and game.live(afternoon)]
    costs = [restart_cost(game, afternoon) for game in live]
    lines.append(
        _row(
            (
                f"D's {len(live)} followed games, 15:00 ET on {TYPICAL_SUNDAY:%b} {TYPICAL_SUNDAY.day}",
                sum(requests for requests, _ in costs),
                f"{sum(rows for _, rows in costs):,}",
                f"{len(live)} requests, new changes only",
            )
        )
    )
    return lines
