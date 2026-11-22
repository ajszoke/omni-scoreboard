"""NFL on the shared board: NFLMeta's slate and game feeds, asked through a `PollPlan` and its quota ledger.

`PlannedNfl` stands between the board's decision pass and NFLMeta. It is NFL's schedule (a `ScopeSource`) and
NFL's feeds (`NflFeeds`). Each pass (`observe`) it decides what NFLMeta should be asked, and the poller asks it, so
nothing here waits on the network. NFLMeta bills requests and rows by the month, so the reads are the cheapest
that keep every live score current (strategy E of `omni.replay.nfl_budget`):

- **The day's games** (`PollSource.DAY_LIST`): one unfiltered read once the date is current, for its games and their
  ids. It bills every game it lists. Until it answers no game is known, so nothing else can be read: it goes as
  focus on a board with an NFL favorite (the favorite's reads need it, and focus runs until the month is spent),
  as normal on one without. A date that lists no game of its own is listed again, the same way, after
  `NflSchedulePolicy.recheck_empty`, in case NFLMeta hadn't turned to it yet.
- **The day's games again** (`PollSource.DAY_LIST`, background): a schedule moves after it's first listed (a
  kickoff moved, a game postponed or added), so the day is listed again, within a budget (`NflSchedulePolicy`):
  each hour over the three before its first kickoff, and half an hour past each window's last kickoff (kickoffs
  that close together are one window) while a later window is still to kick off, or a game of the window hasn't
  been seen to start. A listing again updates the games that haven't kicked off and adds new ones; the games
  being played are their own reads'. A game it leaves out before its kickoff has left the date, and leaves the
  board without a final: an omitted game isn't over. One it leaves out once kicked off is read on, since only its
  own final says it's over. Being due doesn't age the date's answer, so a listing again that the quota sheds
  never takes the date stale.
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
is due, so the answer counts as fresh until the next read is (`ScopeSpan.max_age` says how long, each pass). A
listing again isn't such a read.

**The quota.** The plan's ledger admits each ask on NFLMeta's account by its class, and NFLMeta's own counts reach
the ledger through the provider's metered fetcher (`omni.app.quota.MeteredHttp`). An ask refused stays due, and
`held_back` gives the reason for the league's health. When the month is spent, nothing is asked: NFL's answers
age, and its health goes stale naming the quota. MLB's asks spend their own account.

**Feeds.** Each answer about a game is kept as its read returns, with when it arrived: `take` hands each over
once, oldest first, and `latest` gives the newest. An answer the plan had given up on still counts, since the
provider has already moved past the plays it brought. Each counts as current for two of its own read's cadence
(`NflReading.current_for`): a favorite's game read every 10 s for 20 s, the others' read together every 45 s for
90 s, so a card read at the others' pace isn't dimmed between its reads.

**The meter**, when the board has a market, is read as the pipeline asks for it, on the exchange's own account: a
game's win probability from its public quotes (`PollSource.PUBLIC_MARKET`, every 15 s while the game is wanted), read
as of a moment: the newest reading that arrived by then. A failed read just leaves the card without it.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

from omni.app.nfl_feeds import NflReading
from omni.app.pipeline import MeterReading
from omni.app.planned_nba import PUBLIC_MARKET_POLICY, MarketReader
from omni.app.poll_plan import AccountMap, PollKey, PollPlan, PollPolicy, PollPriority, PollSource
from omni.app.quota import ProviderAccount
from omni.core.clock import Clock, ClockReading, MonotonicTime
from omni.core.enum import League
from omni.core.ids import LeagueScopedId, SourceRef
from omni.core.time import local_date
from omni.domain.baseball import WinProbability
from omni.domain.contest import TeamGame
from omni.preferences.favorites import Favorites
from omni.providers.base import LocalDate, PollScope, ProviderUpdate, ScopeObservation, ScopeReport, ScopeSpan
from omni.tower.interruption import Interruption
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
# again, once its quick retries are spent. The exchange's quotes are the meter's, asked as NBA's are: a plan holds
# one policy a source.
NFL_POLICIES = {
    PollSource.DAY_LIST: PollPolicy(every=timedelta(minutes=1), keep_for=timedelta(minutes=5), rows=16),
    PollSource.LIVE_SCORES: PollPolicy(every=LIVE_EVERY, keep_for=timedelta(minutes=3)),
    PollSource.FOLLOWED_GAME: PollPolicy(every=DEFAULT_POLL.as_timedelta(), keep_for=timedelta(seconds=90), at_once=2),
    PollSource.FINAL_SCORE: PollPolicy(every=timedelta(minutes=1), keep_for=timedelta(minutes=5), at_once=2),
    PollSource.PUBLIC_MARKET: PUBLIC_MARKET_POLICY,
}
_READS = (PollSource.DAY_LIST, PollSource.LIVE_SCORES, PollSource.FOLLOWED_GAME, PollSource.FINAL_SCORE)  # NFLMeta's


@dataclass(frozen=True, slots=True, kw_only=True)
class NflSchedulePolicy:
    """How NFL's scopes are judged: how long an answer counts as fresh once a read falls due, when a date that listed
    no game of its own is listed again, and when a date's games are listed again to see its schedule move."""

    fresh_for: timedelta = timedelta(minutes=2)
    recheck_empty: timedelta = timedelta(hours=2)
    recheck_before: timedelta = timedelta(hours=1)  # the day is listed this often before its first kickoff
    recheck_from: timedelta = timedelta(hours=3)  # from this long before it
    recheck_after: timedelta = timedelta(minutes=30)  # and this long past each window's last kickoff; kickoffs this
    # close together are one window

    def __post_init__(self) -> None:
        spans = (self.fresh_for, self.recheck_empty, self.recheck_before, self.recheck_from, self.recheck_after)
        if any(span <= timedelta(0) for span in spans):
            raise ValueError("a schedule policy's spans must be positive")


def nfl_accounts(source: SourceRef = NFLMETA_SOURCE) -> AccountMap:
    """The accounts NFL's asks spend: NFLMeta's for the games, and the exchange's public quotes for the meter."""
    return AccountMap(
        sources={source: ProviderAccount.NFLMETA}, kinds={PollSource.PUBLIC_MARKET: ProviderAccount.POLYMARKET_US}
    )


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

    @property
    def started(self) -> bool:
        """Whether it has been played: in progress, or over. From then on its own reads say how it stands."""
        return self.seen_in or self.ended or self.done


@dataclass(slots=True)
class _Day:
    """One date's listing: when its day list last answered and what it skipped, and since when a read of the date
    has been due without a break."""

    listed: ClockReading | None = None
    warnings: tuple[str, ...] = ()
    due_since: MonotonicTime | None = None


@dataclass(frozen=True, slots=True)
class _Want:
    """A read this pass wants: how to make it, its class, the date whose scope it serves, and whether it's a listing
    again, whose being due doesn't age the date's answer."""

    fetch: Callable[[datetime], object]
    priority: PollPriority
    day: date
    again: bool = False


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
        market: MarketReader | None = None,
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
        self._market = market
        # Until the day's list answers, no game is known to read: on a board with an NFL favorite it's what focus
        # reads need, so it goes as focus.
        self._listing = PollPriority.FOCUS if favorites.teams(League.NFL) else PollPriority.NORMAL
        self._zone = provider.schedule_timezone
        self._games: dict[LeagueScopedId, _Game] = {}
        self._days: dict[date, _Day] = {}
        self._wanted: dict[PollKey[Any], _Want] = {}
        self._lock = threading.Lock()  # what's known, shared with the poller's workers as their reads return
        self._epoch = 0  # host interruptions taken in: a read asked before the latest files nothing
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

    def win_probability(self, game: TeamGame, as_of: ClockReading) -> MeterReading | None:
        """``game``'s win probability as it stood at ``as_of``: the newest reading that arrived by then; None without a
        market, or with no reading by then."""
        market = self._market
        if market is None:
            return None
        key: PollKey[WinProbability | None] = PollKey(PollSource.PUBLIC_MARKET, game.id)
        at = self._clock.read()
        self._plan.want(key, lambda asked: market(game, asked), priority=PollPriority.NORMAL, at=at)
        answer = self._plan.latest_by(key, as_of.monotonic, at)
        if answer is None or answer.value is None:
            return None
        return MeterReading(answer.value, answer.received)

    def interrupted(
        self, interruption: Interruption, *, held: Collection[PollScope], contests: Collection[LeagueScopedId]
    ) -> None:
        """The host slept or stalled: every read NFL wants is dropped from the plan (what an ask out for it brings back
        is ignored) and wanted afresh at the next pass, due at once, within the account's budget; an ask begun before
        the gap files nothing when it returns; and the answers not yet taken go, with each game's market readings.
        Each game's newest answer stays known, for its schedule; the pipeline judges its freshness."""
        with self._lock:
            self._epoch += 1
            for key in self._wanted:
                self._plan.drop(key)
            self._wanted = {}
            for game in self._games.values():
                game.untaken.clear()
        for contest in contests:
            if contest.league is self.league:
                self._plan.drop(PollKey(PollSource.PUBLIC_MARKET, contest))

    # --- what to read --------------------------------------------------------------------------------------

    def _wants(self, today: date, yesterday: date, at: ClockReading) -> dict[PollKey[Any], _Want]:
        """Every read the two dates need now."""
        wants: dict[PollKey[Any], _Want] = {}
        key: PollKey[Any] = PollKey(PollSource.DAY_LIST, self._scope(today))
        listing = self._days.setdefault(today, _Day())
        if listing.listed is None or (
            not self._filed(today) and at.monotonic >= listing.listed.monotonic + self._policy.recheck_empty
        ):
            wants[key] = _Want(self._day_list(today, self._current_for(key, self._listing)), self._listing, today)
        elif self._again(today, listing.listed, at):
            background = PollPriority.BACKGROUND
            wants[key] = _Want(self._day_list(today, self._current_for(key, background)), background, today, again=True)
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

    def _again(self, today: date, listed: ClockReading, at: ClockReading) -> bool:
        """Whether today's games are due to be listed again: each `NflSchedulePolicy.recheck_before` over the
        `NflSchedulePolicy.recheck_from` before the first kickoff; and once a window's last kickoff is
        `NflSchedulePolicy.recheck_after` past, if the last listing came before then and a later window is still to
        kick off or one of the window's games hasn't been seen to start."""
        policy = self._policy
        games = self._filed(today)
        kickoffs = sorted({game.contest.scheduled_start for game in games})
        if not kickoffs:
            return False  # a date with no game of its own is listed again on its own schedule
        if at.utc < kickoffs[0]:
            return (
                at.utc >= kickoffs[0] - policy.recheck_from and at.monotonic >= listed.monotonic + policy.recheck_before
            )
        windows = [[kickoffs[0]]]
        for kickoff in kickoffs[1:]:
            if kickoff - windows[-1][-1] > policy.recheck_after:
                windows.append([kickoff])
            else:
                windows[-1].append(kickoff)
        for n, window in enumerate(windows):
            due = at.monotonic + (window[-1] + policy.recheck_after - at.utc)
            later = n + 1 < len(windows)
            if listed.monotonic < due <= at.monotonic and (
                later or any(not game.started for game in games if game.contest.scheduled_start in window)
            ):
                return True
        return False

    def _current_for(self, key: PollKey[Any], priority: PollPriority) -> timedelta:
        """How long an answer to ``key``, asked as ``priority``, counts as current: two of the cadence it's read at."""
        return self._plan.policy(key.source).cadence(priority) * _CURRENT_CADENCES

    def _max_age(self, today: date, yesterday: date, at: ClockReading) -> timedelta:
        """How long today's answer counts as fresh: `NflSchedulePolicy.fresh_for` past when a read fell due, or
        will next fall due while none is."""
        listing = self._days[today]
        if any(want.day == today and not want.again for want in self._wanted.values()):
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
        epoch = self._epoch

        def fetch(asked: datetime) -> NflSlate:
            slate = self._provider.day_list()
            arrived = self._clock.read()
            with self._lock:
                if epoch != self._epoch:
                    return slate  # asked before the host's gap: its answer is history
                listing = self._days.setdefault(day, _Day())
                listing.listed, listing.warnings = arrived, slate.warnings
                listed: set[LeagueScopedId] = set()
                for snapshot in slate.snapshots:
                    filed = local_date(snapshot.game.scheduled_start, self._zone)
                    if filed not in (day, day - timedelta(days=1)):
                        continue  # another date's game is that date's to list
                    listed.add(snapshot.game.id)
                    known = self._games.get(snapshot.game.id)
                    if known is None or not known.started:  # a game being played is its own reads' to update
                        self._file(snapshot, filed, arrived, current_for)
                unread = slate.listed - {int(snapshot.game.id.raw) for snapshot in slate.snapshots}
                for game_id, game in list(self._games.items()):
                    if (
                        game.day == day
                        and not game.started
                        and game_id not in listed
                        and int(game_id.raw) not in unread
                    ):
                        del self._games[game_id]  # off the date before its kickoff: it has left it, not ended
            return slate

        return fetch

    def _live(self, day: date, game_ids: tuple[int, ...], current_for: timedelta) -> Callable[[datetime], NflSlate]:
        epoch = self._epoch

        def fetch(asked: datetime) -> NflSlate:
            slate = self._provider.live_scores(game_ids)
            arrived = self._clock.read()
            unlisted = set(game_ids) - slate.listed
            with self._lock:
                if epoch != self._epoch:
                    return slate  # asked before the host's gap: its answer is history
                for snapshot in slate.snapshots:
                    self._update(snapshot, arrived, current_for)
                for game in self._games.values():
                    if game.seen_in and int(game.contest.id.raw) in unlisted:
                        game.ended = True  # no longer in progress: its final is read next
            return slate

        return fetch

    def _final(self, game: _Game, current_for: timedelta) -> Callable[[datetime], NflLiveSnapshot]:
        game_id, epoch = int(game.contest.id.raw), self._epoch

        def fetch(asked: datetime) -> NflLiveSnapshot:
            snapshot = self._provider.scoreboard(game_id)
            arrived = self._clock.read()
            with self._lock:
                if epoch != self._epoch:
                    return snapshot  # asked before the host's gap: its answer is history
                self._update(snapshot, arrived, current_for)
            return snapshot

        return fetch

    def _followed(self, game: _Game, current_for: timedelta) -> Callable[[datetime], NflLiveSnapshot]:
        game_id, epoch = int(game.contest.id.raw), self._epoch

        def fetch(asked: datetime) -> NflLiveSnapshot:
            with self._polling:
                snapshot = self._provider.poll(game_id, self._clock.read())
                hold_ends = self._provider.hold_ends(game_id)
            arrived = self._clock.read()
            with self._lock:
                if epoch != self._epoch:
                    return snapshot  # asked before the host's gap: its answer is history
                followed = self._update(snapshot, arrived, current_for)
                if followed is not None:
                    followed.hold_ends = hold_ends
            return snapshot

        return fetch

    def _update(self, snapshot: NflLiveSnapshot, arrived: ClockReading, current_for: timedelta) -> _Game | None:
        """Keep a game's own read, while the game is still the plan's: one that has left its date since (or rolled
        off) isn't brought back by a late answer."""
        game = self._games.get(snapshot.game.id)
        return self._file(snapshot, game.day, arrived, current_for) if game is not None else None

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
