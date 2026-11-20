"""NBA on the shared board: BALLDONTLIE's slate, players' lines and season, asked through a `PollPlan` and its quota
ledger.

`PlannedNba` stands between the board's decision pass and BALLDONTLIE. It is NBA's schedule (a `ScopeSource`) and NBA's
feeds (`NbaFeeds`). Each pass (`observe`) it decides what BALLDONTLIE should be asked, and the poller asks it, so nothing
here waits on the network. BALLDONTLIE limits requests by the minute (`omni.app.quota.BalldontliePlan`): 5 on Free, 60
on ALL-STAR, which the ledger holds to 55. Its reads:

- **The day's games** (`PollSource.SLATE_STATES`), one read a date (``/v1/games?dates[]=``, US Eastern, the dates the
  feed files its games under): every game's tip and status, and for a game under way or over its state, the slate read
  the deriver takes its timeouts from. Today's is read while the board runs: every `nba_policies` live cadence (5 s on
  ALL-STAR, 12 s on Free) while a game is under way or within `NbaSchedulePolicy.tip_lead` of its tip, and every
  minute otherwise: before the first tip, between windows, after the last final. Yesterday's is read at the live pace
  while one of its games is still being played (a late tip past midnight), and once, for a board started within
  `NbaSchedulePolicy.late_span` of midnight, to find them.
- **The players' lines** (`PollSource.PLAYER_LINES`, ALL-STAR only), one paged read of every game under way and of each
  game for `NbaSchedulePolicy.lines_after_final` after its final (``/v1/stats?game_ids[]=``), every 5 s. A 401 there
  (a key whose tier doesn't read them) turns the lines off for the run, said once in the log, and NBA's cards run on
  the slate alone: no captions, and the line score in the bottom panel. It's never asked again.
- **The season** (`PollSource.SEASON_RESULTS`), once a day, a page at a time (``/v1/games?seasons[]=``, a request a
  page, every 10 s), for the records and ranks: the finals of games filed before the day it's read on, so none of the
  night's own finals reaches a record before the board has shown it. The pipeline folds each final in as it reveals
  it. A page that fails is asked again from where the read stood.

**The quota arithmetic.** On ALL-STAR, with eight games under way (about 210 rows, three pages of lines): 12 slate
reads a minute and 36 pages of lines, 48 of the 55; a season of 13 pages fits the minutes before the first tip. On
Free, the 12 s slate takes the whole minute while a game is under way; before the first tip and between windows the
slate is read once a minute, and the season's pages fill the rest, four a minute at most (the ledger keeps the minute's
last fifth for the slate), so 13 pages take about four minutes.

**The provider's word on its minute.** Each request goes through `MeteredBdl`: admitted and charged on the ledger, and
when an answer says the minute is spent (a 429, or a window with nothing left) the account waits it out, by the
``retry-after`` when the 429 names one, else until the window's reset: no ask of it is admitted until then.

**Scopes and freshness.** NBA's scope is (BALLDONTLIE, NBA, the Eastern date). Each pass hands the board today's scope
and yesterday's: the date's games as of its slate's newest answer, partial when the slate skipped a row it couldn't
read, and its failure while the slate's reads are failing and nothing has answered since. A game a later read leaves
out before it has started has left the date. An answer counts as fresh for `NbaSchedulePolicy.fresh_for` past the
slate's next read.

**Feeds.** Each read of a game, of either kind, is kept as it arrived: `take` hands each over once, in arrival order,
and `latest` gives the newest. Each counts as current for two of its own read's cadence.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, TypeVar
from zoneinfo import ZoneInfo

from omni.app.nba_feeds import NbaReading, SeasonResults
from omni.app.poll_plan import AccountMap, PollKey, PollPlan, PollPolicy, PollPriority, PollSource
from omni.app.quota import BalldontliePlan, Meter, ProviderAccount
from omni.core.clock import Clock, ClockReading, MonotonicTime
from omni.core.enum import League
from omni.core.ids import LeagueScopedId, SourceRef
from omni.core.time import local_date
from omni.domain.basketball import BasketballPhase
from omni.domain.contest import TeamGame
from omni.providers.balldontlie import (
    BALLDONTLIE_SOURCE,
    BalldontlieProvider,
    BdlAnswer,
    BdlFetch,
    BdlQuery,
    FetchOutcome,
    LinesPoll,
    Poll,
    RateLimit,
    SeasonPage,
    SlateGame,
    SlatePoll,
)
from omni.providers.base import (
    LocalDate,
    PollScope,
    ProviderError,
    ProviderUpdate,
    ScopeObservation,
    ScopeReport,
    ScopeSpan,
)
from omni.providers.nba_standings import GameResult
from omni.tower.interruption import Interruption

__all__ = [
    "EASTERN",
    "LINES_EVERY",
    "MeteredBdl",
    "NbaSchedulePolicy",
    "PlannedNba",
    "QUIET_EVERY",
    "nba_accounts",
    "nba_policies",
    "season_of",
]

EASTERN = ZoneInfo("America/New_York")  # BALLDONTLIE files each game under its US Eastern date
LINES_EVERY = timedelta(seconds=5)
QUIET_EVERY = timedelta(minutes=1)  # the slate before the first tip, between windows and after the last final
_LIVE_EVERY = {BalldontliePlan.FREE: timedelta(seconds=12), BalldontliePlan.ALL_STAR: timedelta(seconds=5)}
_SEASON_PAGE_EVERY = timedelta(seconds=10)
_CURRENT_CADENCES = 2  # a read counts as current for this many of its read's cadence
_KEEP = 2048  # reads kept per game until taken: hours of a game's
_LONGEST_WAIT = timedelta(minutes=1)  # the provider's minute: no wait it asks for runs longer
_READS = (PollSource.SLATE_STATES, PollSource.PLAYER_LINES, PollSource.SEASON_RESULTS)
_OVER = frozenset({BasketballPhase.FINAL, BasketballPhase.POSTPONED})

PollT = TypeVar("PollT", bound=Poll)


def nba_policies(tier: BalldontliePlan) -> dict[PollSource, PollPolicy]:
    """How NBA's reads are asked on ``tier``: the slate at its live pace and once a minute when nothing presses, the
    lines every 5 s, and the season a page every 10 s."""
    return {
        PollSource.SLATE_STATES: PollPolicy(
            every=_LIVE_EVERY[tier], background_every=QUIET_EVERY, keep_for=timedelta(minutes=5), at_once=2
        ),
        PollSource.PLAYER_LINES: PollPolicy(every=LINES_EVERY, keep_for=timedelta(seconds=90)),
        PollSource.SEASON_RESULTS: PollPolicy(every=_SEASON_PAGE_EVERY, keep_for=timedelta(minutes=10)),
    }


def nba_accounts(source: SourceRef = BALLDONTLIE_SOURCE) -> AccountMap:
    """The account NBA's reads spend: every one of them BALLDONTLIE's."""
    return AccountMap(sources={source: ProviderAccount.BALLDONTLIE})


def season_of(day: date) -> int:
    """The season a date falls in, by the year it began: one that tips off in October and ends in June."""
    return day.year if day.month >= 7 else day.year - 1


@dataclass(frozen=True, slots=True, kw_only=True)
class NbaSchedulePolicy:
    """How NBA's scopes are judged and paced: how long an answer counts as fresh past the slate's next read, how close
    to its tip a game is read at the live pace, how long after its final a game's lines are read, and how long past
    midnight yesterday's games may still be being played."""

    fresh_for: timedelta = timedelta(minutes=2)
    tip_lead: timedelta = timedelta(minutes=5)
    lines_after_final: timedelta = timedelta(minutes=3)
    late_span: timedelta = timedelta(hours=6)

    def __post_init__(self) -> None:
        spans = (self.fresh_for, self.tip_lead, self.lines_after_final, self.late_span)
        if any(span <= timedelta(0) for span in spans):
            raise ValueError("a schedule policy's spans must be positive")


class MeteredBdl:
    """BALLDONTLIE's fetch, each request admitted and charged on the ledger, the provider's word on its minute heeded.

    A request the ledger won't admit is never made (`omni.app.quota.QuotaRefused`, which the provider takes as no
    answer). One that got no answer at all is counted, since it may have reached the server. An answer that says the
    minute is spent makes the account wait (`Meter.pause`): a 429 its ``retry-after``, or when it names none until
    the window resets; a window with nothing left until it resets. No wait runs past a minute, whatever a clock says.
    """

    def __init__(self, fetch: BdlFetch, meter: Meter, clock: Clock) -> None:
        self._fetch = fetch
        self._meter = meter
        self._clock = clock

    def __call__(self, path: str, query: BdlQuery) -> BdlAnswer:
        self._meter.admit()
        try:
            answer = self._fetch(path, query)
        except Exception:
            self._meter.charge(None)
            raise
        self._meter.charge(answer.headers)
        wait = _wait(answer, self._clock.now())
        if wait is not None:
            self._meter.pause(wait)
        return answer


def _wait(answer: BdlAnswer, now: datetime) -> timedelta | None:
    """How long an answer says to wait before the next request, if it says to."""
    headers = {name.lower(): value for name, value in answer.headers.items()}
    window = RateLimit.of(headers)
    reset = window.resets_at - now if window is not None else _LONGEST_WAIT
    if answer.status == 429:
        raw = headers.get("retry-after", "")
        wait = timedelta(seconds=int(raw)) if raw.isdigit() else reset
    elif window is not None and window.remaining <= 0:
        wait = reset
    else:
        return None
    return min(max(wait, timedelta(0)), _LONGEST_WAIT)


@dataclass(slots=True)
class _Game:
    """What the plan knows of one game: the date it's filed under, its slate row, its newest read and the reads not
    yet taken, and when a slate read first had it over."""

    day: date
    row: SlateGame
    latest: NbaReading | None = None
    untaken: deque[NbaReading] = field(default_factory=lambda: deque(maxlen=_KEEP))
    ended: MonotonicTime | None = None

    @property
    def under_way(self) -> bool:
        """Whether it's being played: begun, and not over."""
        return self.row.read is not None and self.row.phase is not BasketballPhase.FINAL

    @property
    def over(self) -> bool:
        """Whether nothing is left to play: final, postponed or called off."""
        return self.row.phase is None or self.row.phase in _OVER


@dataclass(frozen=True, slots=True)
class _Day:
    """One date's slate: when it last answered, and the rows it skipped."""

    listed: ClockReading
    warnings: tuple[str, ...] = ()


@dataclass(slots=True)
class _Season:
    """The day's season read, page by page: the cursor of the page to ask next, and the finals so far."""

    day: date
    cursor: str | None = None
    results: list[GameResult] = field(default_factory=list)
    done: bool = False


@dataclass(frozen=True, slots=True)
class _Want:
    """A read this pass wants: how to make it, its class, and the date whose scope it serves."""

    fetch: Callable[[datetime], object]
    priority: PollPriority
    day: date


class PlannedNba:
    """NBA's schedule and feeds on the shared board, answered from a plan."""

    league = League.NBA

    def __init__(
        self,
        plan: PollPlan,
        clock: Clock,
        provider: BalldontlieProvider,
        *,
        tier: BalldontliePlan = BalldontliePlan.FREE,
        policy: NbaSchedulePolicy = NbaSchedulePolicy(),
        log: Callable[[str], None] = print,
    ) -> None:
        missing = [kind.value for kind in _READS if kind not in plan.capacity()]
        if missing:
            raise ValueError(f"the plan has no policy for NBA's {', '.join(missing)} asks")
        accounts = plan.accounts
        if accounts is not None:
            for kind in _READS:  # every read NBA makes spends BALLDONTLIE's account, or the board isn't built
                account = accounts.account(kind, BALLDONTLIE_SOURCE)
                if account is not ProviderAccount.BALLDONTLIE:
                    raise ValueError(f"NBA's {kind.value} asks spend BALLDONTLIE's account, not {account.label}'s")
        self._plan = plan
        self._clock = clock
        self._provider = provider
        self._policy = policy
        self._log = log
        self._lines = tier.lines
        self._games: dict[LeagueScopedId, _Game] = {}
        self._days: dict[date, _Day] = {}
        self._season: _Season | None = None
        self._results: SeasonResults | None = None
        self._wanted: dict[PollKey[Any], _Want] = {}
        self._lock = threading.Lock()  # what's known, shared with the poller's workers as their reads return
        self._epoch = 0  # host interruptions taken in: a read asked before the latest files nothing

    @property
    def source(self) -> SourceRef:
        return BALLDONTLIE_SOURCE

    @property
    def lines(self) -> bool:
        """Whether the players' lines are read: on ALL-STAR, until BALLDONTLIE refuses them."""
        return self._lines

    def observe(self, at: ClockReading, held: Collection[PollScope]) -> ScopeReport:
        """Want the reads today's and yesterday's games and the season need now, and drop the rest; report each date's
        games as of its slate's newest answer, and its failure while its reads are failing."""
        today = local_date(at.utc, EASTERN)
        yesterday = today - timedelta(days=1)
        kept = {today, yesterday} | {
            scope.window.day
            for scope in held
            if (scope.source, scope.league) == (self.source, self.league) and isinstance(scope.window, LocalDate)
        }
        with self._lock:
            self._games = {key: game for key, game in self._games.items() if game.day in kept}
            self._days = {day: listing for day, listing in self._days.items() if day in kept}
            wants = self._wants(today, yesterday, at)
            for key in self._wanted.keys() - wants.keys():
                self._plan.drop(key)
            for key, want in wants.items():
                self._plan.want(key, want.fetch, priority=want.priority, at=at)
            self._wanted = wants
            span = ScopeSpan(
                current=self._scope(today), earlier=(self._scope(yesterday),), max_age=self._max_age(today)
            )
            observations = [*self._observe(today), *self._observe(yesterday)]
        return ScopeReport(span=span, observations=tuple(observations))

    def held_back(self, scope: PollScope) -> str | None:
        """Why the quota ledger is holding a read of ``scope`` back, while it is: the most important such read's."""
        with self._lock:
            wanted = sorted(self._wanted.items(), key=lambda item: -item[1].priority)
            keys = [key for key, want in wanted if self._scope(want.day) == scope]
        for key in keys:
            admission = self._plan.held_back(key)
            if admission is not None:
                return admission.reason
        return None

    def take(self, game: TeamGame) -> list[NbaReading]:
        """The reads of ``game`` that arrived since the last take, in arrival order; each is taken once."""
        with self._lock:
            known = self._games.get(game.id)
            if known is None:
                return []
            taken = sorted(known.untaken, key=lambda reading: reading.read.at)
            known.untaken.clear()
            return taken

    def latest(self, game: TeamGame) -> NbaReading | None:
        """The newest read of ``game``, taken or not; None before the first."""
        with self._lock:
            known = self._games.get(game.id)
            return known.latest if known is not None else None

    def slate_game(self, game: TeamGame) -> SlateGame | None:
        """The slate's newest row for ``game``; None before the slate has read it."""
        with self._lock:
            known = self._games.get(game.id)
            return known.row if known is not None else None

    def season(self) -> SeasonResults | None:
        """The newest complete read of the season's finals; None before the first."""
        with self._lock:
            return self._results

    def interrupted(
        self, interruption: Interruption, *, held: Collection[PollScope], contests: Collection[LeagueScopedId]
    ) -> None:
        """The host slept or stalled: every read NBA wants is dropped from the plan (what an ask out for it brings back
        is ignored) and wanted afresh at the next pass, due at once; an ask begun before the gap files nothing when it
        returns; and the reads not yet taken go. Each game's slate row and newest read stay known, for its schedule;
        the pipeline judges its freshness."""
        with self._lock:
            self._epoch += 1
            for key in self._wanted:
                self._plan.drop(key)
            self._wanted = {}
            for game in self._games.values():
                game.untaken.clear()

    # --- what to read --------------------------------------------------------------------------------------

    def _wants(self, today: date, yesterday: date, at: ClockReading) -> dict[PollKey[Any], _Want]:
        """Every read the two dates and the season need now."""
        wants: dict[PollKey[Any], _Want] = {}
        for day in (today, yesterday):
            if day == today or self._still_playing(yesterday, today, at):
                key: PollKey[Any] = PollKey(PollSource.SLATE_STATES, self._scope(day))
                priority = PollPriority.NORMAL if self._pressing(day, at) else PollPriority.BACKGROUND
                wants[key] = _Want(self._slate(day, self._current_for(key, priority)), priority, day)
        if self._lines:
            playing = [game.row.game.id for game in self._games.values() if self._reading_lines(game, at.monotonic)]
            if playing:
                key = PollKey(PollSource.PLAYER_LINES, self._scope(today))
                current_for = self._current_for(key, PollPriority.NORMAL)
                wants[key] = _Want(self._lines_read(key, tuple(playing), current_for), PollPriority.NORMAL, today)
        season = self._season
        if season is None or season.day != today:
            season = self._season = _Season(today)
        if not season.done:
            key = PollKey(PollSource.SEASON_RESULTS, self._scope(today))
            wants[key] = _Want(self._season_page(key, season), PollPriority.BACKFILL, today)
        return wants

    def _still_playing(self, yesterday: date, today: date, at: ClockReading) -> bool:
        """Whether yesterday's slate is still worth reading: to find its games, early enough after midnight that one
        may still be under way; and while one of them is still to finish."""
        if yesterday not in self._days:
            midnight = datetime.combine(today, time(), EASTERN)
            return at.utc < midnight + self._policy.late_span
        return any(not game.over for game in self._filed(yesterday))

    def _pressing(self, day: date, at: ClockReading) -> bool:
        """Whether a date's slate is read at the live pace: a game under way, or one within its lead of its tip."""
        lead = at.utc + self._policy.tip_lead
        return any(
            game.under_way or (game.row.phase is BasketballPhase.SCHEDULED and game.row.game.scheduled_start <= lead)
            for game in self._filed(day)
        )

    def _reading_lines(self, game: _Game, now: MonotonicTime) -> bool:
        """Whether a game's lines are read: under way, or final within the span after its final."""
        if game.under_way:
            return True
        return game.ended is not None and now < game.ended + self._policy.lines_after_final

    def _current_for(self, key: PollKey[Any], priority: PollPriority) -> timedelta:
        """How long a read of ``key``, asked as ``priority``, counts as current: two of the cadence it's read at."""
        return self._plan.policy(key.source).cadence(priority) * _CURRENT_CADENCES

    def _max_age(self, today: date) -> timedelta:
        """How long today's answer counts as fresh: `NbaSchedulePolicy.fresh_for` past the slate's next read."""
        want = self._wanted.get(PollKey(PollSource.SLATE_STATES, self._scope(today)))
        assert want is not None  # today's slate is always read
        return self._policy.fresh_for + self._plan.policy(PollSource.SLATE_STATES).cadence(want.priority)

    # --- what each date said -------------------------------------------------------------------------------

    def _observe(self, day: date) -> list[ScopeObservation]:
        """A date's games as of its slate's newest answer, then its newest failure if nothing has answered since."""
        scope = self._scope(day)
        observed: list[ScopeObservation] = []
        listing = self._days.get(day)
        listed = listing.listed if listing is not None else None
        if listing is not None:
            update = ProviderUpdate(
                source=self.source,
                observed_at=listing.listed.utc,
                contests=tuple(game.row.game for game in self._filed(day)),
                warnings=listing.warnings,
            )
            observed.append(ScopeObservation.answer(scope, update, listing.listed))
        failure = self._plan.failure(PollKey(PollSource.SLATE_STATES, scope))
        if failure is not None and (listed is None or failure.at.monotonic > listed.monotonic):
            observed.append(ScopeObservation.failure(scope, failure.error, failure.at))
        return observed

    def _filed(self, day: date) -> list[_Game]:
        """The games filed under ``day``, by tip."""
        games = (game for game in self._games.values() if game.day == day)
        return sorted(games, key=lambda game: (game.row.game.scheduled_start, game.row.game.id.raw))

    def _scope(self, day: date) -> PollScope:
        return PollScope(self.source, self.league, LocalDate(day))

    # --- the reads, filed as they return -------------------------------------------------------------------

    def _slate(self, day: date, current_for: timedelta) -> Callable[[datetime], SlatePoll]:
        epoch = self._epoch

        def fetch(asked: datetime) -> SlatePoll:
            poll = _answered(self._provider.poll_slate(day), "the slate")
            arrived = self._clock.read()
            with self._lock:
                if epoch != self._epoch:
                    return poll  # asked before the host's gap: its answer is history
                self._days[day] = _Day(arrived, tuple(each.line for each in poll.unreadable))
                listed: set[LeagueScopedId] = set()
                for row in poll.games:
                    if row.game.provider_date != day:
                        continue  # another date's game is that date's to list
                    listed.add(row.game.id)
                    self._file(row, day, arrived, current_for)
                unread = {each.game for each in poll.unreadable}
                for game_id, game in list(self._games.items()):
                    if game.day == day and game.row.read is None and game_id not in listed | unread:
                        del self._games[game_id]  # off the date before its tip: it has left it, not ended
            return poll

        return fetch

    def _lines_read(
        self, key: PollKey[Any], games: tuple[LeagueScopedId, ...], current_for: timedelta
    ) -> Callable[[datetime], LinesPoll]:
        epoch = self._epoch

        def fetch(asked: datetime) -> LinesPoll:
            poll = self._provider.poll_lines(games)
            if poll.outcome is FetchOutcome.UNAUTHORIZED:
                self._lines_refused(key, poll)
                return poll
            _answered(poll, "the players' lines")
            arrived = self._clock.read()
            with self._lock:
                if epoch != self._epoch:
                    return poll  # asked before the host's gap: its answer is history
                for lines in poll.games:
                    game = self._games.get(lines.game)
                    if game is not None:  # a game that has left its date since isn't brought back
                        self._keep(game, NbaReading(lines.read, arrived, current_for))
            return poll

        return fetch

    def _lines_refused(self, key: PollKey[Any], poll: LinesPoll) -> None:
        """BALLDONTLIE won't give the players' lines on this key: they're off for the run, said once, and never asked
        again."""
        with self._lock:
            self._lines = False
            self._wanted.pop(key, None)
            self._plan.drop(key)
        self._log(
            f"omni.app: NBA runs on the slate alone: BALLDONTLIE refused the players' lines ({poll.detail}), which"
            " its key's tier doesn't read, so NBA's cards have no captions or stat panels"
        )

    def _season_page(self, key: PollKey[Any], season: _Season) -> Callable[[datetime], SeasonPage]:
        epoch = self._epoch

        def fetch(asked: datetime) -> SeasonPage:
            page = self._provider.read_season_page(season_of(season.day), cursor=season.cursor, before=season.day)
            _answered(page, "the season")
            arrived = self._clock.read()
            with self._lock:
                if epoch != self._epoch or self._season is not season:
                    return page  # asked before the host's gap, or for another day: its answer is history
                season.results.extend(page.results)
                season.cursor = page.cursor
                if page.cursor is None:
                    season.done = True
                    self._results = SeasonResults(tuple(season.results), arrived)
                    self._wanted.pop(key, None)
                    self._plan.drop(key)
            return page

        return fetch

    def _file(self, row: SlateGame, day: date, arrived: ClockReading, current_for: timedelta) -> None:
        """Keep a slate row: the game's row, and its read when it's under way or over."""
        game = self._games.get(row.game.id)
        if game is None:
            game = self._games[row.game.id] = _Game(day=day, row=row)
        game.row = row
        if row.read is not None:
            self._keep(game, NbaReading(row.read, arrived, current_for))
        if row.phase is BasketballPhase.FINAL and game.ended is None:
            game.ended = arrived.monotonic

    @staticmethod
    def _keep(game: _Game, reading: NbaReading) -> None:
        """Keep one read of a game: its newest, and one more to take."""
        if game.latest is None or reading.read.at >= game.latest.read.at:
            game.latest = reading
        game.untaken.append(reading)


def _answered(poll: PollT, what: str) -> PollT:
    """``poll``, once it has answered; a `ProviderError` saying why when it hasn't, which the plan files as the ask's
    failure."""
    if not poll.answered:
        raise ProviderError(f"BALLDONTLIE {what}: {poll.detail or poll.outcome}")
    return poll
