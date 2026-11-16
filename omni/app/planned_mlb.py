"""The MLB board's sources, answered from a `PollPlan` instead of the network.

`PlannedMlb` stands between the decision pass and the network. The pass asks it for the slate, a
game's feed or a game's win probability with the same calls it made of the provider before, and gets
the plan's latest answer back at once. Each call also tells the plan the key is still wanted, and at
what priority: a favorite's live game is focus, a finished one background. The poller does the
fetching.

- **The slate** covers the previous and current local dates, deduplicated by game. A game from the
  previous date stays while it isn't final. It also stays if this session has already shown it, so a
  game running past midnight keeps its card and still gets its final. On a fresh start, yesterday's
  finished games stay away.
- **A game's feed** carries the time its answer arrived, which dates the state for the TV delay.
- **Win probability**, from the market or the model, is read only from answers that arrived no later
  than the game's feed answer. So the meter never knows more than the score beside it.

A key with no standing answer raises `ProviderError`, with the last failure if there was one, which
the pipeline reports just as it reported a failed fetch.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from omni.app.clock import Clock, ClockReading
from omni.app.pipeline import WinProbFetcher
from omni.app.poll_plan import PollKey, PollPlan, PollPolicy, PollPriority, PollSource
from omni.app.supervisor import BackoffPolicy
from omni.core.enum import GameStatus, League
from omni.core.ids import LeagueScopedId, SourceRef
from omni.core.time import DurationSeconds, local_date
from omni.domain.baseball import WinProbability
from omni.domain.contest import Contest, TeamGame
from omni.events.baseball import LiveBaseballFeed
from omni.providers.base import ProviderError, ProviderUpdate

__all__ = ["MLB_POLICIES", "NO_BACKOFF", "MlbSources", "PlannedMlb"]

# The supervisor's backoff for a planned schedule: the plan already paces retries, and a supervisor
# backing off on top would keep reading a stale answer after the plan has a fresh one.
NO_BACKOFF = BackoffPolicy(base=DurationSeconds(0), cap=DurationSeconds(0))

MLB_POLICIES = {
    PollSource.SLATE: PollPolicy(
        every=timedelta(seconds=15), background_every=timedelta(seconds=60), keep_for=timedelta(minutes=5)
    ),
    PollSource.GAME_FEED: PollPolicy(
        every=timedelta(seconds=15),
        focus_every=timedelta(seconds=8),
        background_every=timedelta(seconds=30),
        keep_for=timedelta(seconds=90),
        per_minute=90,
    ),
    PollSource.MARKET_WIN_PROBABILITY: PollPolicy(
        every=timedelta(seconds=15), keep_for=timedelta(seconds=60), per_minute=120
    ),
    PollSource.MODEL_WIN_PROBABILITY: PollPolicy(
        every=timedelta(seconds=20), keep_for=timedelta(seconds=60), per_minute=60
    ),
}


class MlbSources(Protocol):
    """What the plan asks of the MLB provider."""

    @property
    def source(self) -> SourceRef: ...

    @property
    def schedule_timezone(self) -> ZoneInfo: ...

    def fetch_schedule(self, day: date, *, now: datetime) -> ProviderUpdate: ...

    def fetch_live_feed(self, game: TeamGame, *, now: datetime) -> LiveBaseballFeed: ...

    def fetch_win_probability(self, game: TeamGame) -> WinProbability | None: ...


class PlannedMlb:
    """The pipeline's MLB fetchers and schedule provider, answered from a plan."""

    league = League.MLB

    def __init__(
        self,
        plan: PollPlan,
        clock: Clock,
        provider: MlbSources,
        *,
        market: WinProbFetcher | None = None,
        favorites: frozenset[str] = frozenset(),
    ) -> None:
        self._plan = plan
        self._clock = clock
        self._provider = provider
        self._market = market
        self._favorites = favorites
        self._shown: set[LeagueScopedId] = set()  # games in the last slate handed out

    @property
    def source(self) -> SourceRef:
        return self._provider.source

    def refresh(self, now: datetime) -> ProviderUpdate:
        """The slate across the previous and current local dates (the schedule provider's call)."""
        at = self._clock.read()
        today = local_date(now, self._provider.schedule_timezone)
        current_key = self._slate(today, PollPriority.NORMAL, at)
        previous_key = self._slate(today - timedelta(days=1), PollPriority.BACKGROUND, at)
        current = self._plan.latest(current_key, at)
        if current is None:
            raise ProviderError(self._plan.last_error(current_key) or "MLB schedule: no answer yet")
        contests: list[Contest] = list(current.value.contests)
        warnings = list(current.value.warnings)
        previous = self._plan.latest(previous_key, at)
        if previous is not None:
            today_ids = {contest.id for contest in contests}
            for contest in previous.value.contests:
                if contest.id in today_ids:
                    continue  # on both dates (a resumed game): today's entry is the current one
                if contest.status is not GameStatus.FINAL or contest.id in self._shown:
                    contests.append(contest)
            warnings.extend(previous.value.warnings)
        self._shown = {contest.id for contest in contests}
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

    def fetch_win_probability(self, game: TeamGame, now: datetime) -> WinProbability | None:
        """The market's probability, from no later than the game's feed answer."""
        market = self._market
        if market is None:
            return None
        return self._gated(PollKey(PollSource.MARKET_WIN_PROBABILITY, game.id), game, lambda asked: market(game, asked))

    def fetch_fallback_win_probability(self, game: TeamGame, now: datetime) -> WinProbability | None:
        """The model's probability, from no later than the game's feed answer."""
        return self._gated(
            PollKey(PollSource.MODEL_WIN_PROBABILITY, game.id),
            game,
            lambda asked: self._provider.fetch_win_probability(game),
        )

    def _slate(self, day: date, priority: PollPriority, at: ClockReading) -> PollKey[ProviderUpdate]:
        key: PollKey[ProviderUpdate] = PollKey(PollSource.SLATE, day)
        self._plan.want(key, lambda asked: self._provider.fetch_schedule(day, now=asked), priority=priority, at=at)
        return key

    def _gated(
        self,
        key: PollKey[WinProbability | None],
        game: TeamGame,
        fetch: Callable[[datetime], WinProbability | None],
    ) -> WinProbability | None:
        at = self._clock.read()
        self._plan.want(key, fetch, priority=self._priority(game), at=at)
        feed_key: PollKey[LiveBaseballFeed] = PollKey(PollSource.GAME_FEED, game.id)
        feed = self._plan.latest(feed_key, at)
        if feed is not None:
            answer = self._plan.latest_by(key, feed.received.utc, at)
            if answer is not None:
                return answer.value
        error = self._plan.last_error(key)
        if error is not None:
            raise ProviderError(error)
        return None

    def _priority(self, game: TeamGame) -> PollPriority:
        if game.status is not GameStatus.LIVE:
            return PollPriority.BACKGROUND
        favorite = game.away.abbreviation in self._favorites or game.home.abbreviation in self._favorites
        return PollPriority.FOCUS if favorite else PollPriority.NORMAL
