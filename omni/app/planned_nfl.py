"""NFL on the shared board: NFLMeta's slate and game feeds, asked through a `PollPlan` and its quota ledger.

`PlannedNfl` stands between the board's decision pass and NFLMeta. It is NFL's schedule (a `ScopeSource`) and
NFL's feeds (`NflFeeds`). Each pass (`observe`) it decides what NFLMeta should be asked, and the poller asks it, so
nothing here waits on the network. NFLMeta bills requests and rows by the month, so the reads are the cheapest
that keep every live score current (strategy E of `omni.replay.nfl_budget`):

- **The day's games** (`PollSource.DAY_LIST`, normal): one unfiltered read once the date is current, for its
  games and their ids. It bills every game it lists, so it's made once. A date that lists no game of its own is
  listed again after `NflSchedulePolicy.recheck_empty`, in case NFLMeta hadn't turned to it yet.
- **A favorite's game** (`PollSource.FOLLOWED_GAME`, focus): read by id at the live pace with its plays on change,
  by the provider's `poll`, as the single-game runner reads its game. Nothing is asked through its halftime hold,
  and nothing once its final has settled.
- **The other games** (`PollSource.LIVE_SCORES`, normal): one ``phase=in`` read of the date's other games that
  have kicked off, named by id, every 45 s while any is in progress. A game it stops listing has ended.
- **Each other final** (`PollSource.FINAL_SCORE`, background): one read of the game by id once it has ended,
  for its final score.

Nothing is read before a game's kickoff, or after its final. Each read is a key the plan asks at its own cadence
(`NFL_POLICIES`), dropped as soon as it isn't needed, so a read made once is asked once.

**Scopes.** NFL's scope is (NFLMeta, NFL, the date NFLMeta files a game under: its kickoff's date in the league's
own time zone). A date's games are those its day list listed, and each one's status is its freshest answer's. Each
pass hands the board today's scope and yesterday's (for a game played past midnight): the date's games as of its
freshest answer, partial when the day list skipped a row it couldn't read; and its newest failure, while a read
is failing and nothing has answered since.

**Freshness.** A scope's answer counts as fresh for `NflSchedulePolicy.fresh_for` from when a read of it falls
due. Between reads (before the first kickoff, through a lone game's halftime hold, after the last final) nothing
is due, so the answer counts as fresh until the next read is (`ScopeSpan.max_age` says how long, each pass).

**The quota.** The plan's ledger admits each ask on NFLMeta's account by its class, and NFLMeta's own counts reach
the ledger through the provider's metered fetcher (`omni.app.quota.MeteredHttp`). An ask refused stays due, and
`held_back` gives the reason for the league's health. When the month is spent, nothing is asked: NFL's answers
age, and its health goes stale naming the quota. MLB's asks spend their own account.

**Feeds.** Each answer about a game is kept as its read returns, with when it arrived: `take` hands each over
once, oldest first, and `latest` gives the newest. An answer the plan had given up on still counts, since the
provider has already moved past the plays it brought. Each counts as current for two of its own read's cadence
(`NflReading.current_for`): a favorite's game read every 10 s for 20 s, the others' read together every 45 s for
90 s, so a card read at the others' pace isn't dimmed between its reads.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

from omni.app.nfl_feeds import NflReading
from omni.app.poll_plan import AccountMap, PollKey, PollPlan, PollPolicy, PollPriority, PollSource
from omni.app.quota import ProviderAccount
from omni.core.clock import Clock, ClockReading, MonotonicTime
from omni.core.enum import League
from omni.core.ids import LeagueScopedId, SourceRef
from omni.core.time import local_date
from omni.domain.contest import TeamGame
from omni.preferences.favorites import Favorites
from omni.providers.base import LocalDate, PollScope, ProviderUpdate, ScopeObservation, ScopeReport, ScopeSpan
from omni.providers.nflmeta import (
    DEFAULT_POLL,
    NFLMETA_SOURCE,
    NflLiveSnapshot,
    NflMetaLiveProvider,
    NflPhase,
    NflSlate,
)

__all__ = ["LIVE_EVERY", "NFL_POLICIES", "NflSchedulePolicy", "PlannedNfl", "nfl_accounts"]

LIVE_EVERY = timedelta(seconds=45)  # the other games' scores: each live score within 45 s
_CURRENT_CADENCES = 2  # an answer counts as current for this many of its read's cadence
_KEEP = 2048  # answers kept per game until taken: hours of a followed game's; a game nobody takes from keeps no more

# The day list and a final's read are each made once; their cadence is only how often a failing one is asked
# again, once its quick retries are spent.
NFL_POLICIES = {
    PollSource.DAY_LIST: PollPolicy(every=timedelta(minutes=1), keep_for=timedelta(minutes=5), rows=16),
    PollSource.LIVE_SCORES: PollPolicy(every=LIVE_EVERY, keep_for=timedelta(minutes=3)),
    PollSource.FOLLOWED_GAME: PollPolicy(every=DEFAULT_POLL.as_timedelta(), keep_for=timedelta(seconds=90), at_once=2),
    PollSource.FINAL_SCORE: PollPolicy(every=timedelta(minutes=1), keep_for=timedelta(minutes=5), at_once=2),
}
_READS = tuple(NFL_POLICIES)


@dataclass(frozen=True, slots=True, kw_only=True)
class NflSchedulePolicy:
    """How NFL's scopes are judged: how long an answer counts as fresh once a read falls due, and when a date that
    listed no game of its own is listed again."""

    fresh_for: timedelta = timedelta(minutes=2)
    recheck_empty: timedelta = timedelta(hours=2)

    def __post_init__(self) -> None:
        if self.fresh_for <= timedelta(0) or self.recheck_empty <= timedelta(0):
            raise ValueError("a schedule policy's spans must be positive")


def nfl_accounts(source: SourceRef = NFLMETA_SOURCE) -> AccountMap:
    """The account NFL's asks spend: every one of them NFLMeta's."""
    return AccountMap(sources={source: ProviderAccount.NFLMETA})


@dataclass(slots=True)
class _Game:
    """What the plan knows of one game: the date it's filed under, its freshest answer, and the answers not yet
    taken."""

    day: date
    contest: TeamGame
    favorite: bool
    latest: NflReading
    untaken: deque[NflReading] = field(default_factory=lambda: deque(maxlen=_KEEP))
    seen_in: bool = False  # an answer has had it in progress
    ended: bool = False  # it has left the games in progress since: its final is to be read, once
    done: bool = False  # its final is in hand and settled: nothing is left to read
    hold_ends: MonotonicTime | None = None  # a followed game's halftime hold, once it has begun


@dataclass(slots=True)
class _Day:
    """One date's listing: when its day list last answered and what it skipped, and since when a read of the date
    has been due without a break."""

    listed: ClockReading | None = None
    warnings: tuple[str, ...] = ()
    due_since: MonotonicTime | None = None


@dataclass(frozen=True, slots=True)
class _Want:
    """A read this pass wants: how to make it, its class, and the date whose scope it serves."""

    fetch: Callable[[datetime], object]
    priority: PollPriority
    day: date


class PlannedNfl:
    """NFL's schedule and feeds on the shared board, answered from a plan."""

    league = League.NFL

    def __init__(
        self,
        plan: PollPlan,
        clock: Clock,
        provider: NflMetaLiveProvider,
        *,
        favorites: Favorites = Favorites(),
        policy: NflSchedulePolicy = NflSchedulePolicy(),
    ) -> None:
        missing = [kind.value for kind in _READS if kind not in plan.capacity()]
        if missing:
            raise ValueError(f"the plan has no policy for NFL's {', '.join(missing)} asks")
        accounts = plan.accounts
        if accounts is not None:
            for kind in _READS:  # every ask NFL makes spends NFLMeta's account, or the board isn't built
                account = accounts.account(kind, NFLMETA_SOURCE)
                if account is not ProviderAccount.NFLMETA:
                    raise ValueError(f"NFL's {kind.value} asks spend NFLMeta's account, not {account.label}'s")
        self._plan = plan
        self._clock = clock
        self._provider = provider
        self._favorites = favorites
        self._policy = policy
        self._zone = provider.schedule_timezone
        self._games: dict[LeagueScopedId, _Game] = {}
        self._days: dict[date, _Day] = {}
        self._wanted: dict[PollKey[Any], _Want] = {}
        self._lock = threading.Lock()  # what's known, shared with the poller's workers as their reads return
        self._polling = threading.Lock()  # one followed-game poll at a time: the provider keeps each game's place

    @property
    def source(self) -> SourceRef:
        return NFLMETA_SOURCE

    def observe(self, at: ClockReading, held: Collection[PollScope]) -> ScopeReport:
        """Want the reads today's and yesterday's games need now, and drop the rest; report each date's games as of
        its freshest answer, and its failure while a read is failing."""
        today = local_date(at.utc, self._zone)
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
                current=self._scope(today),
                earlier=(self._scope(yesterday),),
                max_age=self._max_age(today, yesterday, at),
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

    def take(self, game: TeamGame) -> list[NflReading]:
        """The answers about ``game`` that arrived since the last take, oldest first; each is taken once."""
        with self._lock:
            known = self._games.get(game.id)
            if known is None:
                return []
            taken = list(known.untaken)
            known.untaken.clear()
            return taken

    def latest(self, game: TeamGame) -> NflReading | None:
        """The newest answer about ``game``, taken or not; None before the first."""
        with self._lock:
            known = self._games.get(game.id)
            return known.latest if known is not None else None

    # --- what to read --------------------------------------------------------------------------------------

    def _wants(self, today: date, yesterday: date, at: ClockReading) -> dict[PollKey[Any], _Want]:
        """Every read the two dates need now."""
        wants: dict[PollKey[Any], _Want] = {}
        key: PollKey[Any]
        listing = self._days.setdefault(today, _Day())
        if listing.listed is None or (
            not self._filed(today) and at.monotonic >= listing.listed.monotonic + self._policy.recheck_empty
        ):
            key = PollKey(PollSource.DAY_LIST, self._scope(today))
            wants[key] = _Want(
                self._day_list(today, self._current_for(key, PollPriority.NORMAL)), PollPriority.NORMAL, today
            )
        for day in (today, yesterday):
            playing: list[int] = []
            for game in self._filed(day):
                if game.done or not (game.seen_in or game.contest.scheduled_start <= at.utc):
                    continue  # nothing left to read, or not kicked off yet
                if game.favorite:
                    if game.hold_ends is None or at.monotonic >= game.hold_ends:
                        key = PollKey(PollSource.FOLLOWED_GAME, game.contest.id)
                        wants[key] = _Want(
                            self._followed(game, self._current_for(key, PollPriority.FOCUS)), PollPriority.FOCUS, day
                        )
                elif game.ended:
                    key = PollKey(PollSource.FINAL_SCORE, game.contest.id)
                    wants[key] = _Want(
                        self._final(game, self._current_for(key, PollPriority.BACKGROUND)), PollPriority.BACKGROUND, day
                    )
                else:
                    playing.append(int(game.contest.id.raw))
            if playing:
                key = PollKey(PollSource.LIVE_SCORES, self._scope(day))
                wants[key] = _Want(
                    self._live(day, tuple(playing), self._current_for(key, PollPriority.NORMAL)),
                    PollPriority.NORMAL,
                    day,
                )
        return wants

    def _current_for(self, key: PollKey[Any], priority: PollPriority) -> timedelta:
        """How long an answer to ``key``, asked as ``priority``, counts as current: two of the cadence it's read at."""
        return self._plan.policy(key.source).cadence(priority) * _CURRENT_CADENCES

    def _max_age(self, today: date, yesterday: date, at: ClockReading) -> timedelta:
        """How long today's answer counts as fresh: `NflSchedulePolicy.fresh_for` past when a read fell due, or
        will next fall due while none is."""
        listing = self._days[today]
        if any(want.day == today for want in self._wanted.values()):
            if listing.due_since is None:
                listing.due_since = at.monotonic
            due = listing.due_since
        else:
            listing.due_since = None
            due = self._next_read(today, at)
        fresh = self._freshest(today) or self._freshest(yesterday)  # what the board stands on until today answers
        lead = due.since(fresh.monotonic) if fresh is not None else timedelta(0)
        return self._policy.fresh_for + max(lead, timedelta(0))

    def _next_read(self, today: date, at: ClockReading) -> MonotonicTime:
        """When a read of today next falls due, while none is: a kickoff, a hold's end, the empty date's recheck, or
        the date's end."""
        listing = self._days[today]
        assert listing.listed is not None  # until the day list answers, it's due
        games = self._filed(today)
        ahead = [
            at.monotonic + (game.contest.scheduled_start - at.utc)
            for game in games
            if not game.done and game.contest.scheduled_start > at.utc
        ]
        ahead += [game.hold_ends for game in games if game.hold_ends is not None and game.hold_ends > at.monotonic]
        if not games:
            ahead.append(listing.listed.monotonic + self._policy.recheck_empty)
        ends = datetime.combine(today + timedelta(days=1), time(), self._zone)
        return min([*ahead, at.monotonic + (ends - at.utc)])

    # --- what each date said -------------------------------------------------------------------------------

    def _observe(self, day: date) -> list[ScopeObservation]:
        """A date's games as of its freshest answer, then its newest failure if nothing has answered since."""
        scope = self._scope(day)
        observed: list[ScopeObservation] = []
        fresh = self._freshest(day)
        if fresh is not None:
            listing = self._days.get(day)
            update = ProviderUpdate(
                source=self.source,
                observed_at=fresh.utc,
                contests=tuple(game.contest for game in self._filed(day)),
                warnings=listing.warnings if listing is not None else (),
            )
            observed.append(ScopeObservation.answer(scope, update, fresh))
        failures = [
            failure
            for key, want in self._wanted.items()
            if want.day == day and (failure := self._plan.failure(key)) is not None
        ]
        failure = max(failures, key=lambda each: each.at.monotonic, default=None)
        if failure is not None and (fresh is None or failure.at.monotonic > fresh.monotonic):
            observed.append(ScopeObservation.failure(scope, failure.error, failure.at))
        return observed

    def _freshest(self, day: date) -> ClockReading | None:
        """The newest answer about a date: its day list, or a read of one of its games."""
        arrivals = [game.latest.arrived for game in self._filed(day)]
        listing = self._days.get(day)
        if listing is not None and listing.listed is not None:
            arrivals.append(listing.listed)
        return max(arrivals, key=lambda reading: reading.monotonic, default=None)

    def _filed(self, day: date) -> list[_Game]:
        """The games filed under ``day``, by kickoff."""
        games = (game for game in self._games.values() if game.day == day)
        return sorted(games, key=lambda game: (game.contest.scheduled_start, game.contest.id.raw))

    def _scope(self, day: date) -> PollScope:
        return PollScope(self.source, self.league, LocalDate(day))

    # --- the reads, filed as they return -------------------------------------------------------------------

    def _day_list(self, day: date, current_for: timedelta) -> Callable[[datetime], NflSlate]:
        def fetch(asked: datetime) -> NflSlate:
            slate = self._provider.day_list()
            arrived = self._clock.read()
            with self._lock:
                listing = self._days.setdefault(day, _Day())
                listing.listed, listing.warnings = arrived, slate.warnings
                for snapshot in slate.snapshots:
                    filed = local_date(snapshot.game.scheduled_start, self._zone)
                    if filed in (day, day - timedelta(days=1)):  # another date's game is that date's to list
                        self._file(snapshot, filed, arrived, current_for)
            return slate

        return fetch

    def _live(self, day: date, game_ids: tuple[int, ...], current_for: timedelta) -> Callable[[datetime], NflSlate]:
        def fetch(asked: datetime) -> NflSlate:
            slate = self._provider.live_scores(game_ids)
            arrived = self._clock.read()
            unlisted = set(game_ids) - slate.listed
            with self._lock:
                for snapshot in slate.snapshots:
                    self._file(snapshot, day, arrived, current_for)
                for game in self._games.values():
                    if game.seen_in and int(game.contest.id.raw) in unlisted:
                        game.ended = True  # no longer in progress: its final is read next
            return slate

        return fetch

    def _final(self, game: _Game, current_for: timedelta) -> Callable[[datetime], NflLiveSnapshot]:
        game_id, day = int(game.contest.id.raw), game.day

        def fetch(asked: datetime) -> NflLiveSnapshot:
            snapshot = self._provider.scoreboard(game_id)
            arrived = self._clock.read()
            with self._lock:
                self._file(snapshot, day, arrived, current_for)
            return snapshot

        return fetch

    def _followed(self, game: _Game, current_for: timedelta) -> Callable[[datetime], NflLiveSnapshot]:
        game_id, day = int(game.contest.id.raw), game.day

        def fetch(asked: datetime) -> NflLiveSnapshot:
            with self._polling:
                snapshot = self._provider.poll(game_id, self._clock.read())
                hold_ends = self._provider.hold_ends(game_id)
            arrived = self._clock.read()
            with self._lock:
                self._file(snapshot, day, arrived, current_for).hold_ends = hold_ends
            return snapshot

        return fetch

    def _file(self, snapshot: NflLiveSnapshot, day: date, arrived: ClockReading, current_for: timedelta) -> _Game:
        """Keep an answer about a game, current for ``current_for``: its newest, one more to take, and where the game
        stands."""
        reading = NflReading(snapshot, arrived, current_for)
        game = self._games.get(snapshot.game.id)
        if game is None:
            favorite = self._favorites.of(snapshot.game) is not None
            game = self._games[snapshot.game.id] = _Game(
                day=day, contest=snapshot.game, favorite=favorite, latest=reading
            )
        game.contest, game.latest = snapshot.game, reading
        game.untaken.append(reading)
        game.ended = game.ended and snapshot.phase is NflPhase.POST  # an answer that has it still on calls it back
        game.seen_in = game.seen_in or snapshot.phase is NflPhase.IN
        game.done = game.done or snapshot.settled
        return game
