"""The MLB board's sources, answered from a `PollPlan` instead of the network.

`PlannedMlb` stands between the decision pass and the network. The pass asks it for the slate, a
game's feed, a game's win probability, or an upcoming game's details and odds, and gets the plan's
latest answer back at once. Each call also tells the plan the key is still wanted, and at what
priority: a favorite's live game is focus, a finished one background. The poller does the fetching,
so no read here ever waits on the network.

- **The slate** is asked date by date, each date its own scope (`PollScope` over a `LocalDate`): today's,
  yesterday's, and any older date the board still holds a game from. Each pass hands the board what each
  date it asks last said: its standing answer, complete or, when the parser skipped rows, partial; and
  its failure, while its latest ask is failing. A date asked but not answered yet hands nothing. The
  board's `ContestStore` keeps each date's membership, and its rolling-date rules decide what stays:
  yesterday's games until they're final and shown, and an older date's game still being played, or whose
  final hasn't had its time, until it has. Today is the current scope, whose health is the league's.
- **A game's feed** carries when its answer arrived, read both ways, which dates the state for the TV
  delay. It also says how long that answer counts as current (`CURRENT_FOR_CADENCES` of its cadence:
  the next answer is due one cadence after it, and has one more to arrive), and why the latest ask
  since failed, if it did. Past that the pipeline marks the game's card stale, however long the plan
  still keeps the answer as its last good one.
- **A game the schedule calls final** is asked as if it were still live (a favorite's sooner) until
  its feed says final too, since only the feed's word reveals a result; then in the background, so a
  late correction still arrives.
- **Win probability**, from the market or the model, is read as of a moment: the newest answer that
  arrived no later than it, on the monotonic clock. The pipeline asks as of the arrival of the feed
  answer whose state is on the card, so the meter never knows more than the score beside it, and each
  reading carries its own arrival, which ages it.
- **Pregame details and odds** are asked slowly, on their own workers; a game without an answer yet
  simply shows less.

A feed with no standing answer raises `ProviderError`, with the last failure if there was one, which the
pipeline reports just as it reported a failed fetch.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Collection
from datetime import date, datetime, timedelta
from typing import Protocol, TypeVar
from zoneinfo import ZoneInfo

from omni.core.clock import Clock, ClockReading
from omni.app.pipeline import MeterReading
from omni.app.poll_plan import PollKey, PollPlan, PollPolicy, PollPriority, PollSource
from omni.core.enum import GameStatus, League
from omni.core.ids import SourceRef
from omni.core.time import local_date
from omni.domain.baseball import GameLines, PregameDetails, WinProbability
from omni.domain.contest import TeamGame
from omni.events.baseball import LiveBaseballFeed
from omni.providers.base import (
    Coverage,
    LocalDate,
    PollScope,
    ProviderError,
    ProviderUpdate,
    ScopeObservation,
    ScopeReport,
)

__all__ = [
    "CURRENT_FOR_CADENCES",
    "MLB_POLICIES",
    "SLATE_MAX_AGE",
    "MarketReader",
    "MlbSources",
    "OddsReader",
    "PlannedMlb",
]

T = TypeVar("T")

# How long a date's slate answer counts as fresh. Today's is the league's health: past this the board's
# cards are drawn stale.
SLATE_MAX_AGE = timedelta(minutes=2)

# How many of its cadences a feed answer counts as current: the next answer is due one cadence after it,
# and has a whole further cadence to arrive.
CURRENT_FOR_CADENCES = 2

MLB_POLICIES = {
    PollSource.SLATE: PollPolicy(
        every=timedelta(seconds=15),
        background_every=timedelta(seconds=60),
        keep_for=timedelta(minutes=5),
        at_once=2,  # today's and yesterday's
    ),
    PollSource.GAME_FEED: PollPolicy(
        every=timedelta(seconds=15),
        focus_every=timedelta(seconds=8),
        background_every=timedelta(seconds=30),
        keep_for=timedelta(seconds=90),
        per_minute=90,
        at_once=3,
    ),
    # A reading must still stand when the state beside it clears the TV delay, so these stand long enough
    # for any delay; the pipeline ages each reading from its own arrival.
    PollSource.MARKET_WIN_PROBABILITY: PollPolicy(
        every=timedelta(seconds=15), keep_for=timedelta(minutes=10), per_minute=120
    ),
    PollSource.MODEL_WIN_PROBABILITY: PollPolicy(
        every=timedelta(seconds=20), keep_for=timedelta(minutes=10), per_minute=60
    ),
    # A club's record and starter, and a pregame line, barely move before first pitch.
    PollSource.PREGAME_DETAILS: PollPolicy(every=timedelta(minutes=5), keep_for=timedelta(minutes=30), per_minute=12),
    PollSource.GAME_LINES: PollPolicy(every=timedelta(minutes=5), keep_for=timedelta(minutes=30), per_minute=12),
}

# How the plan asks a market for one game's probability as of a UTC instant.
MarketReader = Callable[[TeamGame, datetime], WinProbability | None]

# How the plan asks a sportsbook for one game's odds as of a UTC instant.
OddsReader = Callable[[TeamGame, datetime], GameLines | None]


class MlbSources(Protocol):
    """What the plan asks of the MLB provider."""

    @property
    def source(self) -> SourceRef: ...

    @property
    def schedule_timezone(self) -> ZoneInfo: ...

    def fetch_schedule(self, day: date, *, now: datetime) -> ProviderUpdate: ...

    def fetch_live_feed(self, game: TeamGame, *, now: datetime) -> LiveBaseballFeed: ...

    def fetch_win_probability(self, game: TeamGame) -> WinProbability | None: ...

    def fetch_pregame_details(self, game: TeamGame) -> PregameDetails: ...


class PlannedMlb:
    """The pipeline's MLB sources and the board's MLB schedule, answered from a plan."""

    league = League.MLB

    def __init__(
        self,
        plan: PollPlan,
        clock: Clock,
        provider: MlbSources,
        *,
        market: MarketReader | None = None,
        odds: OddsReader | None = None,
        favorites: frozenset[str] = frozenset(),
    ) -> None:
        self._plan = plan
        self._clock = clock
        self._provider = provider
        self._market = market
        self._odds = odds
        self._favorites = favorites

    @property
    def source(self) -> SourceRef:
        return self._provider.source

    def observe(self, at: ClockReading, held: Collection[PollScope]) -> ScopeReport:
        """Ask today's and yesterday's slates, and any older date's the board still ``held``; report each one's
        standing answer and its failure, if it's failing."""
        today = local_date(at.utc, self._provider.schedule_timezone)
        yesterday = today - timedelta(days=1)
        older = sorted(
            (
                scope.window.day
                for scope in held
                if (scope.source, scope.league) == (self.source, self.league)
                and isinstance(scope.window, LocalDate)
                and scope.window.day < yesterday
            ),
            reverse=True,
        )
        observations = self._observe(today, PollPriority.NORMAL, at)
        for day in (yesterday, *older):
            observations.extend(self._observe(day, PollPriority.BACKGROUND, at))
        coverage = Coverage(current=self._scope(today), earlier=(self._scope(yesterday),), max_age=SLATE_MAX_AGE)
        return ScopeReport(coverage=coverage, observations=tuple(observations))

    def fetch_feed(self, game: TeamGame, now: datetime) -> LiveBaseballFeed:
        """A game's newest standing feed answer, with when it arrived, how long it counts as current, and why
        the latest ask since failed, if it did."""
        at = self._clock.read()
        key: PollKey[LiveBaseballFeed] = PollKey(PollSource.GAME_FEED, game.id)
        answer = self._plan.latest(key, at)
        unconfirmed = game.status is GameStatus.FINAL and (
            answer is None or answer.value.status is not GameStatus.FINAL
        )
        priority = self._priority(game, as_live=unconfirmed)
        self._plan.want(key, lambda asked: self._provider.fetch_live_feed(game, now=asked), priority=priority, at=at)
        if answer is None:
            raise ProviderError(self._plan.last_error(key) or "no answer yet")
        cadence = self._plan.policy(PollSource.GAME_FEED).cadence(priority)
        return dataclasses.replace(
            answer.value,
            received=answer.received,
            current_for=cadence * CURRENT_FOR_CADENCES,
            failing=self._plan.last_error(key),
        )

    def fetch_win_probability(self, game: TeamGame, as_of: ClockReading) -> MeterReading | None:
        """The market's probability as it stood at ``as_of``: the newest reading that arrived by then."""
        market = self._market
        if market is None:
            return None
        key: PollKey[WinProbability | None] = PollKey(PollSource.MARKET_WIN_PROBABILITY, game.id)
        return self._reading(key, game, lambda asked: market(game, asked), as_of)

    def fetch_fallback_win_probability(self, game: TeamGame, as_of: ClockReading) -> MeterReading | None:
        """The model's probability as it stood at ``as_of``: the newest reading that arrived by then."""
        key: PollKey[WinProbability | None] = PollKey(PollSource.MODEL_WIN_PROBABILITY, game.id)
        return self._reading(key, game, lambda asked: self._provider.fetch_win_probability(game), as_of)

    def fetch_pregame_details(self, game: TeamGame) -> PregameDetails | None:
        """An upcoming game's records and probable starters, as last heard; None until an answer stands."""
        key: PollKey[PregameDetails] = PollKey(PollSource.PREGAME_DETAILS, game.id)
        return self._optional(key, lambda asked: self._provider.fetch_pregame_details(game))

    def fetch_game_lines(self, game: TeamGame, now: datetime) -> GameLines | None:
        """An upcoming game's odds, as last heard; None without a sportsbook or until an answer stands."""
        odds = self._odds
        if odds is None:
            return None
        key: PollKey[GameLines | None] = PollKey(PollSource.GAME_LINES, game.id)
        return self._optional(key, lambda asked: odds(game, asked))

    def _scope(self, day: date) -> PollScope:
        return PollScope(self.source, self.league, LocalDate(day))

    def _observe(self, day: date, priority: PollPriority, at: ClockReading) -> list[ScopeObservation]:
        """Want a date's slate at ``priority``; its standing answer, then its failure if one came since."""
        scope = self._scope(day)
        key: PollKey[ProviderUpdate] = PollKey(PollSource.SLATE, scope)
        self._plan.want(key, lambda asked: self._provider.fetch_schedule(day, now=asked), priority=priority, at=at)
        observed: list[ScopeObservation] = []
        answer = self._plan.latest(key, at)
        if answer is not None:
            observed.append(ScopeObservation.answer(scope, answer.value, answer.received))
        failure = self._plan.failure(key)
        if failure is not None:
            observed.append(ScopeObservation.failure(scope, failure.error, failure.at))
        return observed

    def _reading(
        self,
        key: PollKey[WinProbability | None],
        game: TeamGame,
        fetch: Callable[[datetime], WinProbability | None],
        as_of: ClockReading,
    ) -> MeterReading | None:
        at = self._clock.read()
        self._plan.want(key, fetch, priority=self._priority(game), at=at)
        answer = self._plan.latest_by(key, as_of.monotonic, at)
        if answer is not None:
            return MeterReading(answer.value, answer.received) if answer.value is not None else None
        error = self._plan.last_error(key)
        if error is not None:
            raise ProviderError(error)
        return None

    def _optional(self, key: PollKey[T], fetch: Callable[[datetime], T]) -> T | None:
        """An optional read: the standing answer's value, or None. A failure only leaves the card showing less,
        and the plan retries it on its own schedule."""
        at = self._clock.read()
        self._plan.want(key, fetch, priority=PollPriority.BACKGROUND, at=at)
        answer = self._plan.latest(key, at)
        return answer.value if answer is not None else None

    def _priority(self, game: TeamGame, *, as_live: bool = False) -> PollPriority:
        """A favorite's live game is focus, another live game normal, the rest background; ``as_live`` asks a game
        as if it were live (a final its feed hasn't confirmed)."""
        if game.status is not GameStatus.LIVE and not as_live:
            return PollPriority.BACKGROUND
        favorite = game.away.abbreviation in self._favorites or game.home.abbreviation in self._favorites
        return PollPriority.FOCUS if favorite else PollPriority.NORMAL
