"""The MLB board's sources, answered from a `PollPlan` instead of the network.

`PlannedMlb` stands between the decision pass and the network. The pass asks it for the slate, a
game's feed, a game's win probability, or an upcoming game's details and odds, and gets the plan's
latest answer back at once. Each call also tells the plan the key is still wanted, and at what
priority: a favorite's live game is focus, a finished one background. The poller does the fetching,
so no read here ever waits on the network.

- **The slate** covers the previous and current local dates, deduplicated by game. Each date is asked
  on its own and keeps its own membership: the games its last answer listed. A date whose answer fails
  or expires keeps that membership as it was, and a new answer replaces it. Today's slate is the
  snapshot's freshness: without a standing answer for it, the refresh fails and the board's supervisor
  keeps the last snapshot.
- **The previous date** adds its games that aren't final, and its finals this session has already
  handed out, so a game running past midnight keeps its card and still gets its final. On a fresh
  start, yesterday's finished games stay away.
- **Retirement.** When a date leaves the window at local midnight, its games retire with it, except a
  game this session has handed out that is still being played, or whose final hasn't had its time on
  the board yet (`FINAL_HOLD` since it was first handed out final). Those stay, and their date is
  still asked, until each is final and shown.
- **A game's feed** carries the time its answer arrived, which dates the state for the TV delay.
- **Win probability**, from the market or the model, is read as of an instant: the newest answer that
  arrived no later than it. The pipeline asks as of the arrival of the feed answer whose state is on
  the card, so the meter never knows more than the score beside it, and each reading carries its own
  arrival, which ages it.
- **Pregame details and odds** are asked slowly, on their own workers; a game without an answer yet
  simply shows less.

A slate or feed with no standing answer raises `ProviderError`, with the last failure if there was
one, which the pipeline reports just as it reported a failed fetch.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Protocol, TypeVar
from zoneinfo import ZoneInfo

from omni.core.clock import Clock, ClockReading, MonotonicTime
from omni.app.pipeline import MeterReading
from omni.app.poll_plan import Answer, PollKey, PollPlan, PollPolicy, PollPriority, PollSource
from omni.app.supervisor import BackoffPolicy
from omni.core.enum import GameStatus, League
from omni.core.ids import LeagueScopedId, SourceRef
from omni.core.time import DurationSeconds, local_date
from omni.domain.baseball import GameLines, PregameDetails, WinProbability
from omni.domain.contest import Contest, TeamGame
from omni.events.baseball import LiveBaseballFeed
from omni.providers.base import ProviderError, ProviderUpdate

__all__ = ["FINAL_HOLD", "MLB_POLICIES", "NO_BACKOFF", "MarketReader", "MlbSources", "OddsReader", "PlannedMlb"]

T = TypeVar("T")

# The supervisor's backoff for a planned schedule: the plan already paces retries, and a supervisor
# backing off on top would keep reading a stale answer after the plan has a fresh one.
NO_BACKOFF = BackoffPolicy(base=DurationSeconds(0), cap=DurationSeconds(0))

# How long a game from a date that has left the window stays once first handed out final: time for its
# final to clear the TV delay and live out its post-game window.
FINAL_HOLD = timedelta(hours=1)

# A game still being played: kept past its date's window until it ends.
_IN_PLAY = frozenset({GameStatus.LIVE, GameStatus.DELAYED})

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


@dataclass(frozen=True, slots=True)
class _Membership:
    """What one date's slate last said: its games and warnings, and when that answer arrived."""

    games: tuple[Contest, ...]
    warnings: tuple[str, ...]
    received: MonotonicTime


class PlannedMlb:
    """The pipeline's MLB sources and schedule provider, answered from a plan."""

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
        self._dates: dict[date, _Membership] = {}  # each date's games, as its last answer listed them
        self._shown: set[LeagueScopedId] = set()  # games in the last slate handed out
        self._final_since: dict[LeagueScopedId, MonotonicTime] = {}  # when each was first handed out final

    @property
    def source(self) -> SourceRef:
        return self._provider.source

    def refresh(self, now: datetime) -> ProviderUpdate:
        """The slate across the previous and current local dates (the schedule provider's call)."""
        at = self._clock.read()
        today = local_date(now, self._provider.schedule_timezone)
        yesterday = today - timedelta(days=1)
        current_key = self._slate(today, PollPriority.NORMAL, at)
        earlier = sorted({yesterday, *(day for day in self._dates if day < yesterday)}, reverse=True)
        for day in earlier:
            self._remember(day, self._plan.latest(self._slate(day, PollPriority.BACKGROUND, at), at))
        current = self._plan.latest(current_key, at)
        if current is None:
            raise ProviderError(self._plan.last_error(current_key) or "MLB schedule: no answer yet")
        self._remember(today, current)
        contests: list[Contest] = list(current.value.contests)
        warnings = list(current.value.warnings)
        listed = {contest.id for contest in contests}
        for day in earlier:
            membership = self._dates.get(day)
            if membership is None:
                continue  # no answer for it yet
            kept = [
                contest
                for contest in membership.games
                if contest.id not in listed  # on a later date too: that entry is the current one
                and self._keeps(contest, day == yesterday, at.monotonic)
            ]
            contests.extend(kept)
            listed.update(contest.id for contest in kept)
            warnings.extend(membership.warnings)
            if day < yesterday and not kept:
                del self._dates[day]  # every game it held has retired: it's no longer asked
        self._shown = listed
        self._final_since = {
            contest.id: self._final_since.get(contest.id, at.monotonic)
            for contest in contests
            if contest.status is GameStatus.FINAL
        }
        return ProviderUpdate(
            source=current.value.source,
            observed_at=current.received.utc,
            contests=tuple(contests),
            warnings=tuple(warnings),
        )

    def fetch_feed(self, game: TeamGame, now: datetime) -> LiveBaseballFeed:
        at = self._clock.read()
        key: PollKey[LiveBaseballFeed] = PollKey(PollSource.GAME_FEED, game.id)
        self._plan.want(
            key, lambda asked: self._provider.fetch_live_feed(game, now=asked), priority=self._priority(game), at=at
        )
        answer = self._plan.latest(key, at)
        if answer is None:
            raise ProviderError(self._plan.last_error(key) or "no answer yet")
        return dataclasses.replace(answer.value, received_at=answer.received.utc)

    def fetch_win_probability(self, game: TeamGame, as_of: datetime) -> MeterReading | None:
        """The market's probability as it stood at ``as_of``: the newest reading that arrived by then."""
        market = self._market
        if market is None:
            return None
        key: PollKey[WinProbability | None] = PollKey(PollSource.MARKET_WIN_PROBABILITY, game.id)
        return self._reading(key, game, lambda asked: market(game, asked), as_of)

    def fetch_fallback_win_probability(self, game: TeamGame, as_of: datetime) -> MeterReading | None:
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

    def _slate(self, day: date, priority: PollPriority, at: ClockReading) -> PollKey[ProviderUpdate]:
        key: PollKey[ProviderUpdate] = PollKey(PollSource.SLATE, day)
        self._plan.want(key, lambda asked: self._provider.fetch_schedule(day, now=asked), priority=priority, at=at)
        return key

    def _remember(self, day: date, answer: Answer[ProviderUpdate] | None) -> None:
        """Take a date's standing answer as its membership, if it's newer than the one kept."""
        if answer is None:
            return  # missed or expired: the date keeps the membership it had
        kept = self._dates.get(day)
        if kept is None or answer.received.monotonic > kept.received:
            self._dates[day] = _Membership(answer.value.contests, answer.value.warnings, answer.received.monotonic)

    def _keeps(self, contest: Contest, in_window: bool, now: MonotonicTime) -> bool:
        """Whether a game from an earlier date belongs in the slate."""
        final = contest.status is GameStatus.FINAL
        if in_window:
            return not final or contest.id in self._shown
        if contest.id not in self._shown:
            return False  # its date has left the window, and the board never had it
        if final:
            since = self._final_since.get(contest.id)
            return since is None or now.since(since) < FINAL_HOLD  # its final hasn't had its time yet
        return contest.status in _IN_PLAY

    def _reading(
        self,
        key: PollKey[WinProbability | None],
        game: TeamGame,
        fetch: Callable[[datetime], WinProbability | None],
        as_of: datetime,
    ) -> MeterReading | None:
        at = self._clock.read()
        self._plan.want(key, fetch, priority=self._priority(game), at=at)
        answer = self._plan.latest_by(key, as_of, at)
        if answer is not None:
            return MeterReading(answer.value, answer.received.utc) if answer.value is not None else None
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

    def _priority(self, game: TeamGame) -> PollPriority:
        if game.status is not GameStatus.LIVE:
            return PollPriority.BACKGROUND
        favorite = game.away.abbreviation in self._favorites or game.home.abbreviation in self._favorites
        return PollPriority.FOCUS if favorite else PollPriority.NORMAL
