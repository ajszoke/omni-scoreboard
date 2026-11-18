"""Mixed replay: one day of two leagues' slates, run through the real board.

A `MixedTimeline` is the world one board watches over a day, in its viewer's time zone:
- **MLB:** each game's `GameFrame`s, as `omni.replay.timeline` has them: its schedule entry, and while it's live
  or final, its feed's state, plays and decisions. A fake StatsAPI serves both, and `PlannedMlb` asks it through
  the plan, date by date.
- **NFL:** each game's `SlateFrame`s: its schedule entry, its status changing over the day, and while it's live or
  final its feed's answer: its state, the plays written at that frame, and when a halftime it's in ends. NFL's own
  scope source asks its slate, date by date, through the same plan. A fake of NFL's feeds reads each live game every
  `NFL_FEED_EVERY` on its own pace, off the plan (so MLB's asks are what they'd be without NFL), and once more when
  it's final; NFL's own pipeline cards its games beside MLB's, into the one queue.
- **Dates.** A game is listed on its `provider_date`'s slate from its first frame. `AlsoListed` lists it on another
  date's slate too, as around midnight.
- **Faults**, typed:
  - `SourceOutage`: every request to one league's source fails for a span: its slates, and its games' feeds;
  - `PartialSlate`: a date's slate skips a row it can't read, with a warning;
  - `ClockStep`: the board's wall clock jumps back or ahead;
  - `Correction`: a final's score corrected by a later answer.

  Quota exhaustion joins these with the quota ledger.

**Time.** The world keeps its own time, which runs with the replay's monotonic clock from ``start``. The sources
answer as of the world's time. The board reads its own wall clock, which a `ClockStep` moves alone, as a bad clock
correction would. The decisions are timed in seconds since the start on the running clock, so a step of the wall
clock doesn't move them.

**The runner** (`run_mixed`) wires the production spine around the fakes: `PlannedMlb` over one `PollPlan`, an
inline `Poller`, NFL's scope source on the same plan, and an `AppLoop` built as `build_planned_loop` builds it, with
NFL's schedule and pipeline beside MLB's. It warms up as the running board does, then steps at every instant
something can change: a frame or fault, the poller's next check with an ask due, NFL's next read, and the loop's own
deadlines (on a grid of 1/64 s, just past each). The poller checks the plan on a steady beat of its own, every quarter
second, as the running one does, and asks what's due; NFL's feeds read what's due; then the loop refreshes and paints.
So when a source is asked doesn't depend on what else steps the replay. With ``nfl=False`` the same world runs
without NFL's source.

**The decisions** (`mixed_decisions`), one line per change:
- the board's lines, as `omni.replay.harness.replay_decisions` writes them: ``up``, ``down``, ``drop``, ``frame``,
  ``stale``, ``unseen``, ``note``, and ``final`` (with the game's end on the board's wall clock), plus ``corrected``
  when a later answer changes a final shown;
- ``scope``: a scope's membership or outcome changed: its league and window, its outcome (``gone`` once the store
  drops it), and the ids it added and removed;
- ``health``: a league's health changed: fresh, stale, offline or never loaded, and why.

**What a run measures** (`MixedRun`): every request each source got, and each card, of either league, drawn with a
score or a final before its own league's TV-delay release. Each league's airtime share, its cards' waits and lease
overruns join these with the board policy; requests and rows against each account's quota, with the quota ledger.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from omni.app.contest_store import ContestStore, ProviderStatus
from omni.app.loop import AppLoop, Painted, Refresh
from omni.app.nfl_feeds import NflReading
from omni.app.nfl_pipeline import LiveFootballPipeline
from omni.app.pipeline import LiveBaseballPipeline
from omni.app.planned_mlb import MLB_POLICIES, PlannedMlb
from omni.app.poll_plan import PollKey, PollPlan, PollPriority, PollSource
from omni.app.poller import Poller
from omni.app.runner import warm_up
from omni.cards.base import CardId, CardKind, ScoreboardCard
from omni.cards.baseball import BigPlayCardPayload, FinalCardPayload, LiveBaseballCardPayload
from omni.cards.factory import CardFactory
from omni.cards.football import LiveFootballCardPayload
from omni.core.clock import ClockReading, FakeClock, MonotonicTime
from omni.core.enum import GameStatus, League, PanelProfile
from omni.core.ids import LeagueScopedId, SourceRef
from omni.core.time import DurationSeconds, local_date
from omni.domain.baseball import PregameDetails, WinProbability
from omni.domain.contest import TeamGame
from omni.domain.football import FootballGameState
from omni.events.baseball import LiveBaseballFeed
from omni.events.football import FootballEvent
from omni.preferences.favorites import Favorites
from omni.providers.base import (
    LocalDate,
    PollScope,
    ProviderError,
    ProviderUpdate,
    ScopeObservation,
    ScopeReport,
    ScopeSource,
    ScopeSpan,
)
from omni.providers.nflmeta import DEFAULT_POLL, NFLMETA_SOURCE, NflLiveSnapshot, NflPhase
from omni.queue.delay_policy import DelayPolicy
from omni.queue.priority import PriorityScorer
from omni.queue.scheduler import CardView, InterleavedCardQueue
from omni.renderers.canvas import Canvas
from omni.renderers.context import RenderContext
from omni.renderers.registry import RendererRegistry, default_registry
from omni.replay.harness import _describe, _Discarding, replay_versions
from omni.replay.timeline import GameFrame, Timeline

__all__ = [
    "MLB_STATSAPI",
    "NFL_FEED_EVERY",
    "NFL_SLATE_MAX_AGE",
    "AlsoListed",
    "ClockStep",
    "Correction",
    "EarlyReveal",
    "Fault",
    "Fetched",
    "MixedRun",
    "MixedStep",
    "MixedTimeline",
    "PartialSlate",
    "SlateFrame",
    "SourceOutage",
    "mixed_decisions",
    "run_mixed",
]

MLB_STATSAPI = SourceRef("mlb_statsapi", "https://statsapi.mlb.com")

# How long an answer of NFL's slate counts as fresh: past this its health goes stale.
NFL_SLATE_MAX_AGE = timedelta(minutes=2)

# How often the replay's NFL feeds read each live game. Each answer is current for two of these.
NFL_FEED_EVERY = timedelta(seconds=30)

# The replay steps on a grid of 1/64 s, which both floating point and the clock's whole microseconds hold exactly, so
# an answer's receipt, and every span counted from it, lands exactly on a step.
_TICK = timedelta(microseconds=15625)
_TICKS_PER_SECOND = 64
_BEAT = 16  # ticks: the running poller checks the plan every quarter second, on a steady beat of its own
_LEAGUES = (League.MLB, League.NFL)


@dataclass(frozen=True, slots=True, kw_only=True)
class SlateFrame:
    """One NFL game as its slate lists it, from ``at`` until its next frame, and, while it's live or final, as its
    feed answers: its state, the plays written at ``at``, and when the halftime it's in ends (by the world's time).
    A frame with no state is a schedule entry alone: its feed has nothing to say."""

    at: datetime
    game: TeamGame
    state: FootballGameState | None = None
    plays: tuple[FootballEvent, ...] = ()
    break_ends_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.at.tzinfo is None:
            raise ValueError("frame `at` must be timezone-aware")


@dataclass(frozen=True, slots=True)
class AlsoListed:
    """A game another date's slate lists too, besides its own: one running past midnight, say."""

    game: LeagueScopedId
    day: date


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceOutage:
    """A span, ``start`` up to ``end``, when every request to one league's source fails: each date's slate, and each
    game's feed."""

    league: League
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        _check_span(self.start, self.end)


@dataclass(frozen=True, slots=True, kw_only=True)
class PartialSlate:
    """A span when one date's slate of a league can't read one game's row: it skips the row, with a warning."""

    league: League
    day: date
    skipped: LeagueScopedId
    warning: str
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        _check_span(self.start, self.end)


@dataclass(frozen=True, slots=True, kw_only=True)
class ClockStep:
    """At ``at``, by the world's time, the board's wall clock jumps by ``by``, back or ahead. Monotonic time and the
    world run on."""

    at: datetime
    by: timedelta


@dataclass(frozen=True, slots=True, kw_only=True)
class Correction:
    """From ``at``, a final's feed answers with a corrected score: a later answer than the final's own."""

    at: datetime
    game: LeagueScopedId
    away: int
    home: int


# The faults a mixed day can carry.
Fault = SourceOutage | PartialSlate | ClockStep | Correction


@dataclass(frozen=True, slots=True, kw_only=True)
class MixedTimeline:
    """A day of MLB and NFL as their sources see it, with its faults. ``zone`` is the board's local calendar."""

    zone: ZoneInfo
    start: datetime
    end: datetime
    mlb: tuple[GameFrame, ...]
    nfl: tuple[SlateFrame, ...] = ()
    also_listed: tuple[AlsoListed, ...] = ()
    faults: tuple[Fault, ...] = ()
    # MLB's frames with each correction as a frame of its own; NFL's frames by game, in time order.
    _feeds: Timeline = field(init=False, repr=False, compare=False, hash=False)
    _slates: dict[LeagueScopedId, tuple[list[datetime], list[SlateFrame]]] = field(
        init=False, repr=False, compare=False, hash=False
    )

    def __post_init__(self) -> None:
        _check_span(self.start, self.end)
        games = [(League.MLB, frame.game) for frame in self.mlb] + [(League.NFL, frame.game) for frame in self.nfl]
        for league, game in games:
            if game.league is not league:
                raise ValueError(f"{game.id} is listed among {league}'s games")
            if game.provider_date is None:
                raise ValueError(f"{game.id} needs the date its slate files it under")
        listed = Timeline(frames=self.mlb)
        corrected = tuple(_corrected(listed, fault) for fault in self.faults if isinstance(fault, Correction))
        object.__setattr__(self, "_feeds", Timeline(frames=self.mlb + corrected))
        slates: dict[LeagueScopedId, list[SlateFrame]] = {}
        for frame in self.nfl:
            slates.setdefault(frame.game.id, []).append(frame)
        index = {}
        for game_id, frames in slates.items():
            frames.sort(key=lambda frame: frame.at)
            index[game_id] = ([frame.at for frame in frames], frames)
        object.__setattr__(self, "_slates", index)

    @property
    def changes(self) -> tuple[datetime, ...]:
        """Every instant something in the world changes, or a fault starts, ends or strikes, in order."""
        instants = {frame.at for frame in self._feeds.frames} | {frame.at for frame in self.nfl}
        for fault in self.faults:
            if isinstance(fault, (SourceOutage, PartialSlate)):
                instants.update((fault.start, fault.end))
            else:
                instants.add(fault.at)
        return tuple(sorted(instants))

    def clock_steps(self) -> list[ClockStep]:
        """The wall-clock steps, in order."""
        return sorted((fault for fault in self.faults if isinstance(fault, ClockStep)), key=lambda step: step.at)

    def nfl_games_at(self, now: datetime) -> list[TeamGame]:
        """Each NFL game as its slate entry stands at ``now``, from its first frame on, in id order."""
        return sorted(self._effective(now), key=lambda game: str(game.id))

    def nfl_feed_at(
        self, game_id: LeagueScopedId, now: datetime, *, since: datetime | None = None
    ) -> tuple[SlateFrame | None, tuple[FootballEvent, ...]]:
        """An NFL game's frame as of ``now`` (None before its first), and the plays written after ``since`` up to
        then. Raises `ProviderError` while NFL's source is out."""
        self._check_source(League.NFL, now)
        instants, frames = self._slates[game_id]
        found = bisect_right(instants, now)
        first = bisect_right(instants, since) if since is not None else 0
        plays = tuple(play for frame in frames[first:found] for play in frame.plays)
        return (frames[found - 1] if found else None), plays

    def feed_at(self, game_id: LeagueScopedId, now: datetime) -> GameFrame | None:
        """An MLB game's frame as of ``now``, corrections included; None before its first. Raises `ProviderError`
        while MLB's source is out."""
        self._check_source(League.MLB, now)
        return self._feeds.frame_at(game_id, now)

    def slate(self, league: League, day: date, now: datetime, *, source: SourceRef, asked: datetime) -> ProviderUpdate:
        """``league``'s slate for ``day`` as of ``now``, as its source answers it when asked at ``asked`` by the
        board's clock. Raises `ProviderError` while the league's source is out."""
        self._check_source(league, now)
        games = self._feeds.schedule_at(now) if league is League.MLB else tuple(self.nfl_games_at(now))
        extra = {listing.game for listing in self.also_listed if listing.day == day}
        listed = [game for game in games if game.provider_date == day or game.id in extra]
        warnings: list[str] = []
        for fault in self.faults:
            if isinstance(fault, PartialSlate) and (fault.league, fault.day) == (league, day) and _within(fault, now):
                listed = [game for game in listed if game.id != fault.skipped]
                warnings.append(fault.warning)
        return ProviderUpdate(source=source, observed_at=asked, contests=tuple(listed), warnings=tuple(warnings))

    def _check_source(self, league: League, now: datetime) -> None:
        """Raise `ProviderError` while ``league``'s source is out."""
        if any(
            isinstance(fault, SourceOutage) and fault.league is league and _within(fault, now) for fault in self.faults
        ):
            raise ProviderError(f"{league} source down")

    def _effective(self, now: datetime) -> Iterable[TeamGame]:
        """Each NFL game as of ``now``, from its first frame on."""
        for instants, frames in self._slates.values():
            found = bisect_right(instants, now)
            if found:
                yield frames[found - 1].game


@dataclass(frozen=True, slots=True, kw_only=True)
class Fetched:
    """One request a source got: what was asked, and when, in seconds since the start on the running clock."""

    at: timedelta
    key: PollKey[Any]


@dataclass(frozen=True, slots=True, kw_only=True)
class EarlyReveal:
    """A card drawn with a score, or a final, before that answer cleared the TV delay: a spoiler."""

    card: CardId
    at: timedelta  # since the start, on the running clock
    away: int
    home: int


@dataclass(frozen=True, slots=True, kw_only=True)
class MixedStep:
    """One step of a mixed run, as a watcher hears it. ``store`` is the board's own, as it stands at this step: read
    it during the call."""

    at: ClockReading  # the board's clock: its wall clock may have stepped
    elapsed: timedelta  # since the start, on the running clock
    world: datetime  # the world's own time, which the sources answer as of
    refresh: Refresh
    painted: Painted
    drawn: ScoreboardCard[Any] | None  # the card this step's frame drew, if one was
    store: ContestStore
    view: CardView  # the cards this step's refresh gave the board to air, both leagues'


@dataclass(frozen=True, slots=True, kw_only=True)
class MixedRun:
    """What a mixed run decided and measured."""

    decisions: tuple[str, ...]
    fetched: tuple[Fetched, ...]  # every request each source got, in order
    early: tuple[EarlyReveal, ...]  # cards drawn with a score or a final before its release: none, ever


def mixed_decisions(
    timeline: MixedTimeline,
    *,
    profile: PanelProfile,
    favorites: Favorites = Favorites(),
    broadcast_lag: DurationSeconds = DurationSeconds(45),
    nfl: bool = True,
    nfl_lag: DurationSeconds | None = None,
) -> list[str]:
    """Every change across ``timeline``, one line each, timed in seconds since its start, under a header naming
    what the replay decided with (`replay_versions`)."""
    run = run_mixed(
        timeline, profile=profile, favorites=favorites, broadcast_lag=broadcast_lag, nfl=nfl, nfl_lag=nfl_lag
    )
    header = replay_versions(profile, broadcast_lag=broadcast_lag, favorites=favorites).header()
    return [header, *run.decisions]


def run_mixed(
    timeline: MixedTimeline,
    *,
    profile: PanelProfile,
    favorites: Favorites = Favorites(),
    broadcast_lag: DurationSeconds = DurationSeconds(45),
    nfl: bool = True,
    nfl_lag: DurationSeconds | None = None,
    watch: Callable[[MixedStep], None] | None = None,
) -> MixedRun:
    """Run the board through ``timeline`` from its start to its end, and say what it decided and measured.

    ``favorites`` feeds the scorers and the plan's priorities, ``broadcast_lag`` is MLB's TV delay and ``nfl_lag``
    NFL's (MLB's unless given), and ``nfl=False`` leaves NFL's source and cards off the board. ``watch`` hears every
    step. Pure and deterministic: no real time, no I/O.
    """
    clock = FakeClock(timeline.start)
    world = _World(timeline.start, clock)
    plan = PollPlan(MLB_POLICIES)
    mlb = _ReplayMlb(timeline, world)
    planned = PlannedMlb(plan, clock, mlb, favorites=favorites)
    schedules: tuple[ScopeSource, ...] = (planned, _NflSlates(timeline, world, plan)) if nfl else (planned,)
    nfl_lag = nfl_lag if nfl_lag is not None else broadcast_lag
    feeds = _ReplayNfl(timeline, world, served=mlb.served) if nfl else None
    witness = _Witness()
    queue = InterleavedCardQueue()
    football = None
    if feeds is not None:
        football = LiveFootballPipeline(
            feeds=feeds, delay_policy=DelayPolicy(broadcast_lag=nfl_lag), queue=queue, favorites=favorites
        )
    store = ContestStore()
    loop = AppLoop(
        schedules=schedules,
        store=store,
        pipeline=LiveBaseballPipeline(
            scorer=PriorityScorer(favorites=favorites),
            delay_policy=DelayPolicy(broadcast_lag=broadcast_lag),
            factory=CardFactory(),
            queue=queue,
        ),
        queue=queue,
        registry=witness,
        sink=_Discarding(profile),
        fetch_feed=planned.fetch_feed,
        fetch_pregame_details=planned.fetch_pregame_details,
        fetch_game_lines=planned.fetch_game_lines,
        football=football,
    )
    poller = Poller(plan, clock)
    warm_up(loop, poller, clock)

    lines: list[str] = []
    early: list[EarlyReveal] = []
    seen = _Seen()
    lags = {League.MLB: broadcast_lag.as_timedelta(), League.NFL: nfl_lag.as_timedelta()}
    end = _ticks_from(world.monotonic(timeline.end))
    changes = sorted({_ticks_from(world.monotonic(instant)) for instant in timeline.changes})
    steps = timeline.clock_steps()
    tick = 0
    while tick <= end:
        while steps and steps[0].at <= world.now():
            clock.jump(steps.pop(0).by)
        if tick % _BEAT == 0:
            poller.run_due()
        at = clock.read()
        if feeds is not None:
            feeds.read_due(at)
        refresh = loop.refresh(at)
        witness.drawn = None
        painted = loop.paint(at)
        step = MixedStep(
            at=at,
            elapsed=world.elapsed(),
            world=world.now(),
            refresh=refresh,
            painted=painted,
            drawn=witness.drawn if painted.shown is not None else None,
            store=store,
            view=queue.published,
        )
        lines.extend(_lines(step, seen, timeline.zone))
        early.extend(_spoiled(step, mlb.served, lags))
        if watch is not None:
            watch(step)

        while changes and changes[0] <= tick:
            changes.pop(0)
        due = plan.next_due()
        assert due is not None  # the slates are always wanted
        deadline = loop.next_deadline(clock.read())
        beat = max(_ticks_from(due), tick + 1)
        upcoming = [-(-beat // _BEAT) * _BEAT]  # the poller's first check with an ask due
        upcoming += changes[:1]
        upcoming += [max(_ticks_from(deadline, past=True), tick + 1)] if deadline is not None else []
        read = feeds.next_due() if feeds is not None else None
        upcoming += [max(_ticks_from(read), tick + 1)] if read is not None else []
        following = min(upcoming)
        clock.advance(_TICK * (following - tick))
        tick = following
    return MixedRun(decisions=tuple(lines), fetched=tuple(world.fetched), early=tuple(early))


@dataclass(slots=True)
class _World:
    """The world's time: the replay's start, moved on by its monotonic clock."""

    start: datetime
    clock: FakeClock
    fetched: list[Fetched] = field(default_factory=list)  # every request any source got, in order

    def now(self) -> datetime:
        return self.start + self.elapsed()

    def elapsed(self) -> timedelta:
        return timedelta(seconds=self.clock.monotonic().seconds)

    def monotonic(self, instant: datetime) -> MonotonicTime:
        """The running clock's reading when the world reaches ``instant``."""
        return MonotonicTime((instant - self.start).total_seconds())

    def fetch(self, key: PollKey[Any]) -> datetime:
        """Note a request for ``key``; the world's time it's answered as of."""
        self.fetched.append(Fetched(at=self.elapsed(), key=key))
        return self.now()


class _ReplayMlb:
    """StatsAPI over the timeline: each date's slate, each game's feed, as of the world's time."""

    source = MLB_STATSAPI

    def __init__(self, timeline: MixedTimeline, world: _World) -> None:
        self._timeline = timeline
        self._world = world
        # Each score a game's feed has answered with, and when it first did: once with any status, once as a final.
        self.served: dict[tuple[LeagueScopedId, int, int, bool], MonotonicTime] = {}

    @property
    def schedule_timezone(self) -> ZoneInfo:
        return self._timeline.zone

    def fetch_schedule(self, day: date, *, now: datetime) -> ProviderUpdate:
        world = self._world.fetch(PollKey(PollSource.SLATE, PollScope(self.source, League.MLB, LocalDate(day))))
        return self._timeline.slate(League.MLB, day, world, source=self.source, asked=now)

    def fetch_live_feed(self, game: TeamGame, *, now: datetime) -> LiveBaseballFeed:
        frame = self._timeline.feed_at(game.id, self._world.fetch(PollKey(PollSource.GAME_FEED, game.id)))
        if frame is None or frame.state is None:
            raise ProviderError(f"no feed for {game.id}")
        received = self._world.clock.monotonic()
        scores = {(frame.state.away_score, frame.state.home_score)}
        plays = [event.payload for event in frame.events]  # each play's own score is in the answer too
        scores.update(
            (play.away_score, play.home_score)
            for play in plays
            if play.away_score is not None and play.home_score is not None
        )
        for away, home in sorted(scores):
            self.served.setdefault((game.id, away, home, False), received)
        if frame.game.status is GameStatus.FINAL:
            self.served.setdefault((game.id, frame.state.away_score, frame.state.home_score, True), received)
        return LiveBaseballFeed(
            state=frame.state, events=frame.events, decisions=frame.decisions, status=frame.game.status
        )

    def fetch_win_probability(self, game: TeamGame) -> WinProbability | None:
        return None

    def fetch_pregame_details(self, game: TeamGame) -> PregameDetails:
        self._world.fetch(PollKey(PollSource.PREGAME_DETAILS, game.id))
        return PregameDetails()


class _NflSlates:
    """NFL's schedule, one local date at a time, asked through the board's plan beside MLB's slates, at its
    background cadence."""

    def __init__(self, timeline: MixedTimeline, world: _World, plan: PollPlan) -> None:
        self._timeline = timeline
        self._world = world
        self._plan = plan

    def observe(self, at: ClockReading, held: Collection[PollScope]) -> ScopeReport:
        """Ask today's slate; report its standing answer, and its failure if it's failing."""
        today = local_date(at.utc, self._timeline.zone)
        scope = PollScope(NFLMETA_SOURCE, League.NFL, LocalDate(today))
        key: PollKey[ProviderUpdate] = PollKey(PollSource.SLATE, scope)

        def fetch(asked: datetime) -> ProviderUpdate:
            world = self._world.fetch(key)
            return self._timeline.slate(League.NFL, today, world, source=NFLMETA_SOURCE, asked=asked)

        self._plan.want(key, fetch, priority=PollPriority.BACKGROUND, at=at)
        observed: list[ScopeObservation] = []
        answer = self._plan.latest(key, at)
        if answer is not None:
            observed.append(ScopeObservation.answer(scope, answer.value, answer.received))
        failure = self._plan.failure(key)
        if failure is not None:
            observed.append(ScopeObservation.failure(scope, failure.error, failure.at))
        return ScopeReport(span=ScopeSpan(current=scope, max_age=NFL_SLATE_MAX_AGE), observations=tuple(observed))


class _ReplayNfl:
    """NFL's feeds over the timeline, an `NflFeeds`: each game its slate lists live is read every `NFL_FEED_EVERY`
    from the first step it's live, as its feed answers at the world's time, and once more when it's final; then it's
    left alone. A read brings the plays written since the last one. Each answer is taken once, and says it's current
    for two of those reads.

    ``served`` is each score a game's feed has answered with, and when it first did, once with any status and once
    as a final: the replay's MLB feed keeps the same, and the ids keep the leagues apart.
    """

    def __init__(
        self,
        timeline: MixedTimeline,
        world: _World,
        *,
        served: dict[tuple[LeagueScopedId, int, int, bool], MonotonicTime],
    ) -> None:
        self._timeline = timeline
        self._world = world
        self._served = served
        self._due: dict[LeagueScopedId, MonotonicTime] = {}  # each live game's next read
        self._read_to: dict[LeagueScopedId, datetime] = {}  # the world's time each game's plays were read to
        self._done: set[LeagueScopedId] = set()  # read final: nothing left to ask
        self._waiting: dict[LeagueScopedId, list[NflReading]] = {}
        self._latest: dict[LeagueScopedId, NflReading] = {}

    def read_due(self, at: ClockReading) -> None:
        """Read each live or final game whose read is due as of ``at``."""
        for game in self._timeline.nfl_games_at(self._world.now()):
            due = self._due.get(game.id)
            if game.id in self._done or game.status not in _READ or (due is not None and due > at.monotonic):
                continue
            self._due[game.id] = at.monotonic + NFL_FEED_EVERY
            self._read(game.id, at)

    def next_due(self) -> MonotonicTime | None:
        """When a game is next read, on the running clock; None when none is being read."""
        return min((due for game_id, due in self._due.items() if game_id not in self._done), default=None)

    def take(self, game: TeamGame) -> list[NflReading]:
        return self._waiting.pop(game.id, [])

    def latest(self, game: TeamGame) -> NflReading | None:
        return self._latest.get(game.id)

    def _read(self, game_id: LeagueScopedId, at: ClockReading) -> None:
        """One read of a game: its answer, unless the source is out or has nothing to say."""
        world = self._world.fetch(PollKey(PollSource.GAME_FEED, game_id))
        try:
            frame, plays = self._timeline.nfl_feed_at(game_id, world, since=self._read_to.get(game_id))
        except ProviderError:
            return
        if frame is None or frame.state is None:
            return
        self._read_to[game_id] = world
        final = frame.game.status is GameStatus.FINAL
        ends = frame.break_ends_at
        snapshot = NflLiveSnapshot(
            game=frame.game,
            phase=NflPhase.POST if final else NflPhase.IN,
            state=frame.state,
            events=plays,
            caught_up=True,
            break_ends_at=ends + (at.utc - world) if ends is not None else None,  # on the board's clock, as asked
            observed_at=world,
            poll_every=DEFAULT_POLL,
            settled=final,
        )
        reading = NflReading(snapshot, at, current_for=NFL_FEED_EVERY * 2)
        self._waiting.setdefault(game_id, []).append(reading)
        self._latest[game_id] = reading
        score = (frame.state.away_score, frame.state.home_score)
        self._served.setdefault((game_id, *score, False), at.monotonic)
        if final:
            self._served.setdefault((game_id, *score, True), at.monotonic)
            self._done.add(game_id)


_READ = frozenset({GameStatus.LIVE, GameStatus.FINAL})  # the NFL games whose feed the replay reads


class _Witness(RendererRegistry):
    """Today's renderers, chosen and checked as for drawing, but not drawn; it keeps the card each frame drew."""

    def __init__(self) -> None:
        super().__init__()
        self._real = default_registry()
        self.drawn: ScoreboardCard[Any] | None = None

    def render(self, card: ScoreboardCard[Any], context: RenderContext, canvas: Canvas) -> None:
        self._real.renderer_for(card)
        self.drawn = card


@dataclass(slots=True)
class _Seen:
    """What the decisions last said: each scope's outcome and members, each league's health, the stale card."""

    scopes: dict[PollScope, tuple[str, frozenset[LeagueScopedId]]] = field(default_factory=dict)
    leagues: dict[League, str] = field(default_factory=dict)
    stale: str | None = None


def _lines(step: MixedStep, seen: _Seen, zone: ZoneInfo) -> list[str]:
    """One step's decision lines: each league's health and each scope that changed, then the board's lines. A final's
    end is told on the board's wall clock, in ``zone``."""
    seconds = f"{step.elapsed.total_seconds():8.1f}"
    lines: list[str] = []
    for league in step.refresh.leagues:
        status = league.scope.status
        state = status.value if status is ProviderStatus.NEVER_LOADED else league.source.value
        said = f"{league.league}  {state}" + (f"  ({league.reason})" if league.reason else "")
        if seen.leagues.get(league.league) != said:
            seen.leagues[league.league] = said
            lines.append(f"{seconds}  health  {said}")
    store, now = step.store, step.at.monotonic
    current: dict[PollScope, tuple[str, frozenset[LeagueScopedId]]] = {}
    for scope in store.scopes:
        health = store.health(scope, now)
        assert health is not None  # the store holds it
        outcome = health.outcome.value if health.outcome is not None else "-"
        current[scope] = (outcome, frozenset(contest.id for contest in store.membership(scope)))
    for scope in sorted(current.keys() | seen.scopes.keys(), key=_scope_order):
        before = seen.scopes.get(scope, ("-", frozenset()))
        after = current.get(scope, ("gone", frozenset()))
        if after != before:
            added = sorted(str(game) for game in after[1] - before[1])
            removed = sorted(str(game) for game in before[1] - after[1])
            ids = "".join(f"  +{game}" for game in added) + "".join(f"  -{game}" for game in removed)
            lines.append(f"{seconds}  scope   {_scope_name(scope)}  {after[0]}{ids}")
    seen.scopes = current

    pipeline = step.refresh.pipeline
    lines.extend(f"{seconds}  unseen  {missed.card.id.raw}  ({missed.reason.value})" for missed in step.refresh.unseen)
    lines.extend(
        f"{seconds}  final   {card.raw}  (ended {ended.at.astimezone(zone):%H:%M:%S}, {ended.basis.value})"
        for card, ended in pipeline.ends
    )
    lines.extend(f"{seconds}  corrected  {card.raw}" for card in pipeline.corrections)
    painted = step.painted
    chosen = painted.presented.reason if painted.presented is not None else None
    lines.extend(f"{seconds}  {line}" for line in (_describe(change, chosen) for change in painted.changes) if line)
    lines.extend(f"{seconds}  note    {note.value}" for note in painted.notes)
    stale = painted.shown.raw if painted.shown is not None and painted.stale else None
    if stale is not None and stale != seen.stale:
        lines.append(f"{seconds}  stale   {stale}")
    seen.stale = stale
    return lines


def _spoiled(
    step: MixedStep,
    served: dict[tuple[LeagueScopedId, int, int, bool], MonotonicTime],
    lags: dict[League, timedelta],
) -> list[EarlyReveal]:
    """The card drawn this step, if it shows a score its feed hadn't answered with its league's full TV delay
    before."""
    card = step.drawn
    if card is None:
        return []
    payload = card.payload
    if isinstance(payload, LiveBaseballCardPayload):
        score, final = (payload.away_line.runs, payload.home_line.runs), False
    elif isinstance(payload, (BigPlayCardPayload, FinalCardPayload)):
        score, final = (payload.away_score, payload.home_score), isinstance(payload, FinalCardPayload)
    elif isinstance(payload, LiveFootballCardPayload):
        score, final = (payload.state.away_score, payload.state.home_score), card.kind is CardKind.FINAL
    else:
        return []
    received = served.get((card.contest.id, *score, final))
    if received is not None and received + lags[card.league] <= step.at.monotonic:
        return []
    return [EarlyReveal(card=card.id, at=step.elapsed, away=score[0], home=score[1])]


def _ticks_from(instant: MonotonicTime, *, past: bool = False) -> int:
    """The first tick of the replay's grid at ``instant`` or after it; with ``past``, the first after it."""
    ticks = instant.seconds * _TICKS_PER_SECOND
    return math.floor(ticks) + 1 if past else math.ceil(ticks)


def _corrected(listed: Timeline, correction: Correction) -> GameFrame:
    """A correction as a frame of its game's: its final, with the corrected score."""
    frame = listed.frame_at(correction.game, correction.at)
    if frame is None or frame.game.status is not GameStatus.FINAL or frame.state is None:
        raise ValueError(f"a correction of {correction.game} corrects a final")
    state = replace(frame.state, away_score=correction.away, home_score=correction.home)
    return replace(frame, at=correction.at, state=state)


def _scope_order(scope: PollScope) -> tuple[int, str]:
    return _LEAGUES.index(scope.league), _scope_name(scope)


def _scope_name(scope: PollScope) -> str:
    window = scope.window
    return f"{scope.league} {window.day.isoformat() if isinstance(window, LocalDate) else 'current'}"


def _within(fault: SourceOutage | PartialSlate, now: datetime) -> bool:
    return fault.start <= now < fault.end


def _check_span(start: datetime, end: datetime) -> None:
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("a span's instants must be timezone-aware")
    if end <= start:
        raise ValueError("a span must end after it starts")
