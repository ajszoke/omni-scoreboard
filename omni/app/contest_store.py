"""ContestStore: the board's contests, kept scope by scope.

A schedule source asks its scopes (one source's contests in one league over a window, such as one local
date's slate) and hands the store what each said, as `ScopeObservation`s. Each scope keeps its own
membership:
- **Order.** A scope takes its observations in the order they arrived, on the monotonic clock, never by
  UTC. One older than the scope's last is ignored, for that scope alone, so a late answer can't rewind it
  and a wall-clock step back can't freeze it.
- **Complete.** The answer is the scope's membership. A game it leaves out leaves the scope, and an empty
  complete answer (a day without games) empties the scope, and nothing else.
- **Partial.** The games it lists are updated or join, and the rest stay, since nothing refuted them.
- **Failed.** The membership stays as it was, and the failure counts against the scope's health.

The board's contests are the union across scopes, by full id. A game two scopes list (yesterday's and
today's slates, around midnight) is there once, as the scope that heard of it last has it; a tie goes to
the later window. `reconcile` names what changed in the union since it last ran, and the pipeline prunes
the cards of the games that left.

**Rolling windows.** Each pass a source names the scopes it covers (`cover`): its current one, and earlier
ones still in its window. One source covers each league, so a league has one current scope. A scope its
source no longer names has rolled off and is retired (`retire`). `RollingDates` says which of a scope's
games the board keeps:
- of its current scope, all of them;
- of an earlier scope, those not final, and the finals the board already had: a game running past
  midnight gets its final, and on a fresh start yesterday's finished games stay away;
- of a retired scope, only those the board had that are still being played, or whose final hasn't had
  `FINAL_HOLD` on the board since it was first handed out final. A retired scope that keeps none is
  dropped. Until then the store still holds it, and its source keeps asking it.

**Health**, one envelope per scope (`ScopeHealth`): its last outcome, its last answer and last complete
answer, its consecutive failures and last error, and whether it's fresh, stale or never loaded by its own
max age, on the monotonic clock. A league's health is its current scope's. A current scope with no answer
yet (a new date, just past midnight) stands on the last answer of the scope it took over from, so the board
doesn't go offline while the new date is first asked.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from enum import Enum

from omni.core.clock import ClockReading, MonotonicTime
from omni.core.enum import GameStatus, League, StrEnumMixin
from omni.core.ids import LeagueScopedId
from omni.domain.contest import Contest
from omni.providers.base import (
    Coverage,
    CoverageWindow,
    LocalDate,
    PollScope,
    ScopeObservation,
    ScopeOutcome,
    ScopeReport,
)

__all__ = [
    "FINAL_HOLD",
    "ContestStore",
    "ProviderStatus",
    "Reconciliation",
    "RollingDates",
    "ScopeHealth",
    "ScopeRole",
]

# How long a game from a scope that has rolled off stays once first handed out final: time for its final to
# clear the TV delay and live out its post-game window.
FINAL_HOLD = timedelta(hours=1)

# A game still being played: kept past its scope's window until it ends.
_IN_PLAY = frozenset({GameStatus.LIVE, GameStatus.DELAYED})


class ProviderStatus(StrEnumMixin, str, Enum):
    """How far a scope's answers can be trusted now."""

    NEVER_LOADED = "never_loaded"  # no answer yet: nothing to show
    FRESH = "fresh"  # its last answer is within its max age
    STALE = "stale"  # its last answer is older than its max age: consider an offline card


class ScopeRole(StrEnumMixin, str, Enum):
    """Where a scope stands in its source's window."""

    CURRENT = "current"  # the source's current scope: today's slate
    EARLIER = "earlier"  # still in the window, before the current one: yesterday's
    RETIRED = "retired"  # rolled off the window: held only for the games it still keeps


@dataclass(frozen=True, slots=True)
class RollingDates:
    """Which of a scope's games the board keeps, by where the scope stands in its window."""

    final_hold: timedelta = FINAL_HOLD

    def keeps(
        self,
        contest: Contest,
        role: ScopeRole,
        *,
        shown: bool,
        final_since: MonotonicTime | None,
        now: MonotonicTime,
    ) -> bool:
        """Whether ``contest``, listed by a scope in ``role``, belongs on the board. ``shown`` says the board had it
        after the last reconcile, and ``final_since`` when it was first handed out final, if it was."""
        if role is ScopeRole.CURRENT:
            return True
        final = contest.status is GameStatus.FINAL
        if role is ScopeRole.EARLIER:
            return not final or shown
        if not shown:
            return False  # its window has rolled off, and the board never had it
        if final:
            return final_since is None or now.since(final_since) < self.final_hold  # its final hasn't had its time
        return contest.status in _IN_PLAY


@dataclass(frozen=True, slots=True, kw_only=True)
class ScopeHealth:
    """How one scope stands as of a moment."""

    scope: PollScope
    status: ProviderStatus
    max_age: timedelta
    outcome: ScopeOutcome | None = None  # its last observation's; None before any
    answered: ClockReading | None = None  # its last answer, complete or partial
    complete: ClockReading | None = None  # its last complete answer
    fresh_from: ClockReading | None = None  # the answer its freshness counts from: its own last, or a predecessor's
    age: timedelta | None = None  # since ``fresh_from``, in whole seconds; None while never loaded
    failures: int = 0  # failed observations since its last answer
    error: str | None = None  # the last failure's reason, while it's failing


@dataclass(frozen=True, slots=True, kw_only=True)
class Reconciliation:
    """What changed in the board's contests (ids sorted for deterministic traces)."""

    added: tuple[LeagueScopedId, ...] = ()
    updated: tuple[LeagueScopedId, ...] = ()
    removed: tuple[LeagueScopedId, ...] = ()

    @property
    def changed(self) -> bool:
        return bool(self.added or self.updated or self.removed)


@dataclass(frozen=True, slots=True)
class _Member:
    """A game as its scope last heard of it, and when that was."""

    contest: Contest
    received: MonotonicTime


@dataclass(slots=True)
class _Scope:
    """One scope's place in its window, membership and health."""

    role: ScopeRole
    max_age: timedelta
    members: dict[LeagueScopedId, _Member] = field(default_factory=dict)
    last: MonotonicTime | None = None  # the receipt of the last observation taken: an older one is ignored
    outcome: ScopeOutcome | None = None
    answered: ClockReading | None = None
    complete: ClockReading | None = None
    failures: int = 0
    error: str | None = None

    def take(self, observation: ScopeObservation) -> None:
        received = observation.received.monotonic
        repeated = self.last == received and self.outcome is observation.outcome  # handed again, unchanged
        self.last, self.outcome = received, observation.outcome
        if observation.outcome is ScopeOutcome.FAILED:
            if not repeated:
                self.failures += 1
            self.error = observation.error
            return
        listed = {contest.id: _Member(contest, received) for contest in observation.contests}
        self.members = listed if observation.outcome is ScopeOutcome.COMPLETE else {**self.members, **listed}
        self.answered, self.failures, self.error = observation.received, 0, None
        if observation.outcome is ScopeOutcome.COMPLETE:
            self.complete = observation.received

    def health(self, scope: PollScope, fresh_from: ClockReading | None, at: MonotonicTime) -> ScopeHealth:
        age: timedelta | None = None
        status = ProviderStatus.NEVER_LOADED
        if fresh_from is not None:
            # Whole seconds, and never negative: a limit is passed only once a full second over it.
            age = timedelta(seconds=max(0, int(at.since(fresh_from.monotonic).total_seconds())))
            status = ProviderStatus.STALE if age > self.max_age else ProviderStatus.FRESH
        return ScopeHealth(
            scope=scope,
            status=status,
            max_age=self.max_age,
            outcome=self.outcome,
            answered=self.answered,
            complete=self.complete,
            fresh_from=fresh_from,
            age=age,
            failures=self.failures,
            error=self.error,
        )


class ContestStore:
    """The board's contests, kept scope by scope and reconciled into one set."""

    def __init__(self, rolling: RollingDates = RollingDates()) -> None:
        self._rolling = rolling
        self._scopes: dict[PollScope, _Scope] = {}
        self._current: dict[League, PollScope] = {}  # each league's current scope, leagues in the order first covered
        self._carried: dict[League, ClockReading | None] = {}  # what a new current scope stands on until it answers
        self._contests: dict[LeagueScopedId, Contest] = {}  # the union handed out by the last reconcile
        self._final_since: dict[LeagueScopedId, MonotonicTime] = {}  # when each was first handed out final

    def cover(self, coverage: Coverage) -> None:
        """A source covers these scopes now: its current one, and the earlier ones still in its window. Its other
        scopes have rolled off, and are retired."""
        current = coverage.current
        previous = self._current.get(current.league)
        if previous != current:
            self._carried[current.league] = self._fresh_from(previous) if previous is not None else None
            self._current[current.league] = current
        roles = {current: ScopeRole.CURRENT, **{scope: ScopeRole.EARLIER for scope in coverage.earlier}}
        for scope, role in roles.items():
            state = self._scopes.get(scope)
            if state is None:
                self._scopes[scope] = _Scope(role=role, max_age=coverage.max_age)
            else:
                state.role, state.max_age = role, coverage.max_age
        for scope in self._scopes:
            if scope not in roles and (scope.source, scope.league) == (current.source, current.league):
                self.retire(scope)

    def retire(self, scope: PollScope) -> None:
        """``scope``'s window has rolled off: it keeps only what a retired scope keeps, and goes once that's
        nothing. A league's current scope can't retire; covering a new one moves it into the window."""
        if self._current.get(scope.league) == scope:
            raise ValueError("a league's current scope can't retire")
        state = self._scopes.get(scope)
        if state is not None:
            state.role = ScopeRole.RETIRED

    def apply(self, observation: ScopeObservation) -> bool:
        """Take ``observation`` into its scope. False when it's ignored: older than the scope's last, or for a scope
        no source has covered."""
        state = self._scopes.get(observation.scope)
        if state is None or (state.last is not None and observation.received.monotonic < state.last):
            return False
        state.take(observation)
        return True

    def take(self, report: ScopeReport) -> tuple[ScopeObservation, ...]:
        """One pass of a source: cover its window, then apply what each scope said. Returns the observations
        ignored."""
        self.cover(report.coverage)
        return tuple(observation for observation in report.observations if not self.apply(observation))

    def reconcile(self, now: MonotonicTime) -> Reconciliation:
        """Rebuild the board's contests from every scope as of ``now``, drop the retired scopes that keep nothing,
        and name what changed since the last reconcile."""
        order = self._ordered()
        newest: dict[LeagueScopedId, _Member] = {}
        for scope in order:
            for game_id, member in self._scopes[scope].members.items():
                known = newest.get(game_id)
                if known is None or member.received > known.received:  # a tie stays with the later window
                    newest[game_id] = member
        union: dict[LeagueScopedId, Contest] = {}
        for scope in order:
            state = self._scopes[scope]
            kept = False
            for game_id in state.members:
                if game_id in union:
                    continue  # a later window has it
                contest = newest[game_id].contest
                shown, final_since = game_id in self._contests, self._final_since.get(game_id)
                if self._rolling.keeps(contest, state.role, shown=shown, final_since=final_since, now=now):
                    union[game_id] = contest
                    kept = True
            if state.role is ScopeRole.RETIRED and not kept:
                del self._scopes[scope]  # every game it held has gone: it's no longer asked
        previous = self._contests
        added = [game_id for game_id in union if game_id not in previous]
        removed = [game_id for game_id in previous if game_id not in union]
        updated = [
            game_id for game_id, contest in union.items() if game_id in previous and contest != previous[game_id]
        ]
        self._contests = union
        self._final_since = {
            game_id: self._final_since.get(game_id, now)
            for game_id, contest in union.items()
            if contest.status is GameStatus.FINAL
        }
        return Reconciliation(
            added=tuple(sorted(added, key=str)),
            updated=tuple(sorted(updated, key=str)),
            removed=tuple(sorted(removed, key=str)),
        )

    @property
    def contests(self) -> tuple[Contest, ...]:
        """The board's contests, as the last reconcile left them."""
        return tuple(self._contests.values())

    @property
    def scopes(self) -> tuple[PollScope, ...]:
        """Every scope the store holds: those in a source's window, and retired ones still keeping a game."""
        return tuple(self._scopes)

    def membership(self, scope: PollScope) -> tuple[Contest, ...]:
        """``scope``'s own games, as it last heard of them; none for a scope the store doesn't hold."""
        state = self._scopes.get(scope)
        return tuple(member.contest for member in state.members.values()) if state is not None else ()

    def health(self, scope: PollScope, at: MonotonicTime) -> ScopeHealth | None:
        """How ``scope`` stands at ``at``, from its own answers; None for a scope the store doesn't hold."""
        state = self._scopes.get(scope)
        return state.health(scope, state.answered, at) if state is not None else None

    def league_health(self, league: League, at: MonotonicTime) -> ScopeHealth | None:
        """How ``league`` stands at ``at``: its current scope's health, standing on its predecessor's last answer
        until it has one of its own. None for a league no source covers."""
        current = self._current.get(league)
        return self._scopes[current].health(current, self._fresh_from(current), at) if current is not None else None

    def league_healths(self, at: MonotonicTime) -> tuple[ScopeHealth, ...]:
        """Every covered league's health at ``at``, in the order the leagues were first covered."""
        return tuple(
            self._scopes[current].health(current, self._fresh_from(current), at) for current in self._current.values()
        )

    def get(self, contest_id: LeagueScopedId) -> Contest | None:
        return self._contests.get(contest_id)

    def __contains__(self, contest_id: object) -> bool:
        return contest_id in self._contests

    def __len__(self) -> int:
        return len(self._contests)

    def _fresh_from(self, scope: PollScope) -> ClockReading | None:
        """The answer a league's current ``scope`` counts its freshness from: its own last, or what it took over."""
        state = self._scopes.get(scope)
        answered = state.answered if state is not None else None
        return answered if answered is not None else self._carried.get(scope.league)

    def _ordered(self) -> list[PollScope]:
        """Every scope, league by league in the order first covered; within a league, its current scope first,
        then the rest by window, newest first."""
        rank = {league: index for index, league in enumerate(self._current)}

        def key(scope: PollScope) -> tuple[int, bool, int, str]:
            current = self._scopes[scope].role is ScopeRole.CURRENT
            return rank[scope.league], not current, _newest_first(scope.window), scope.source.name

        return sorted(self._scopes, key=key)


def _newest_first(window: CoverageWindow) -> int:
    """A window's place among its league's, the newest first."""
    return -window.day.toordinal() if isinstance(window, LocalDate) else 0
