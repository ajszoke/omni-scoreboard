"""AppLoop: the deterministic orchestration passes (ROADMAP A0 spine capstone).

The board refreshes and paints on separate cadences:

- **`refresh(at)`**, the decision pass, wires the spine end to end:
  1. ask each schedule source (a `ScopeSource`) what its scopes said: the scopes its window covers, and what
     each one it asked last answered, or why it failed;
  2. take each answer into its scope and reconcile the board's contests across scopes (`ContestStore`), so a
     scope that fails keeps what it had and another league's scopes carry on regardless;
  3. run each league's card pipeline (delay-safe, no spoilers) over its own games, both staging into the one card
     queue, and drop what can no longer air: `LiveBaseballPipeline` over the baseball games, and, when the board
     has NFL, `LiveFootballPipeline` over the NFL games. No game reaches a pipeline that can't draw it: another
     league's games stay members, and count toward its health, but have no cards until a pipeline of their own
     joins the board. Each pipeline gets the whole clock reading: its holds are pinned, and released, on the
     monotonic clock, so a wall-clock jump can't release one early. What they did is reported as one result;
  4. publish the staged cards to the board together with how far their source can be trusted, how
     many games have nothing to show because they wait out the TV delay or their feed has stopped
     answering, and what the board's policy reads (`omni.policy.view.PolicyView`: both leagues' games
     carded live and their offers, from released state only, each offer naming a card still staged): one
     numbered input, so a paint on the frame thread sees all of a refresh or none of it.
     Each league's health is its current scope's, judged on the monotonic clock. The board's is its
     healthiest league's, so one league going offline doesn't take another's cards down; with one league
     it is that league's. While a quota holds a league's current scope back, its reason says so.
     A league is offline once `offline_after` has passed since a read of its scope was due. A scope's answer
     counts as fresh for its `max_age`, which runs `FRESH_PAST_DUE` past when its next read falls due, so a
     scope that is quiet by plan (NFL before kickoff, through a lone game's halftime hold, after the last final)
     is never offline, and one that missed its due read goes offline `offline_after` later.
- **`paint(at)`** asks the board (`BoardSession`) what should be up: a card, chosen by the queue and
  held by its dwell, or an explicit frame (idle, held, offline). It draws that through the
  `RendererRegistry`, a stale card dimmed, and commits it to the `DisplaySink`. The commit starts a
  card's dwell, books its turn in the queue and starts its lines scrolling (each line's start is kept
  by the card's time on the board and its lane), so a card that never reached the panel spends none of
  its turn. All three count from when the display took the frame: given the clock, the paint reads it
  again once the commit returns, so a slow draw or swap doesn't eat into the card's time. Painting runs
  at a frame rate, so scrolling lines, countdowns and blinks move, and never fetches.
- **`run_once(at)`** is one refresh, then one paint: the single deterministic pass replay drives.
- **`refresh_deadline(at)`** says when a refresh next has something to do by itself (a play clearing
  the delay, the source going stale, a card running out of time to air); **`next_deadline(at)`** adds
  the board's own (a card's minimum, its turn, its expiry).

Both passes are deterministic for a given clock reading and collaborators, so the real-time runner is a
thin wrapper and a `FakeClock` with a fixture timeline yields stable traces. Render and display failures
are isolated: a paint reports its failure in `Painted.render_error` and the loop keeps going.

Each card that reaches the panel is traced at its first committed frame, with how its matchup was drawn
(`Presentation`).
"""

from __future__ import annotations

from collections.abc import Callable, Hashable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from types import MappingProxyType
from typing import Any

from omni.app.contest_store import ContestStore, ProviderStatus, Reconciliation, ScopeHealth
from omni.app.display import DisplaySink
from omni.app.nfl_pipeline import LiveFootballPipeline
from omni.app.pipeline import (
    FeedFetcher,
    GameLinesFetcher,
    LiveBaseballPipeline,
    PipelineResult,
    PregameDetailsFetcher,
    WinProbFetcher,
)
from omni.cards.base import CardId, ScoreboardCard
from omni.core.clock import Clock, ClockReading, MonotonicTime
from omni.core.enum import League, Sport
from omni.domain.contest import TeamGame
from omni.policy.view import EMPTY_POLICY_VIEW, PolicyView
from omni.providers.base import PollScope, ScopeSource
from omni.queue.scheduler import InterleavedCardQueue, Missed, SelectionReason
from omni.renderers.board_frame import BoardFrameRenderer
from omni.renderers.canvas import DimmedCanvas
from omni.renderers.context import RenderContext
from omni.renderers.image import LogoStore
from omni.renderers.registry import RendererRegistry
from omni.renderers.text_lane import DEFAULT_MARQUEE, LaneClock, LaneFrame, Marquee, MarqueeStyle, TextLane
from omni.renderers.visual_treatment import MatchupVisualTreatment, VisualContrastPolicy, resolve_matchup_treatment
from omni.tower.board import (
    BoardFrame,
    BoardHealth,
    BoardNote,
    BoardPayload,
    BoardPolicy,
    BoardSession,
    FrameKind,
    SourceHealth,
)
from omni.tower.lane import ChangeKind, LaneChange, earliest
from omni.tower.segment import Surface
from omni.policy.settings import PolicySettings

__all__ = [
    "FRESH_PAST_DUE",
    "OFFLINE_AFTER",
    "AppLoop",
    "LeagueHealth",
    "Painted",
    "Presentation",
    "QuotaNote",
    "Refresh",
    "TickResult",
]

# How long past when a read of the schedule was due the board waits for an answer before it takes its cards down
# and says it's offline.
OFFLINE_AFTER = timedelta(minutes=10)

# How long past when its next read falls due a scope's answer counts as fresh: every source's today (MLB's slate,
# NFL's scopes, a supervised provider's). A scope's `max_age` is that much past its due read.
FRESH_PAST_DUE = timedelta(minutes=2)

# Why a quota is holding a scope's asks back, while it is: e.g. "quota: NFLMeta rows 8% left, background slowed".
QuotaNote = Callable[[PollScope], str | None]

# The board's health is its healthiest league's.
_HEALTHIEST_FIRST = {SourceHealth.FRESH: 0, SourceHealth.STALE: 1, SourceHealth.OFFLINE: 2}


@dataclass(frozen=True, slots=True, kw_only=True)
class LeagueHealth:
    """How far the board can trust one league's schedule, judged from its current scope's health."""

    league: League
    source: SourceHealth
    scope: ScopeHealth  # the league's current scope, as the store has it
    reason: str = ""  # why it's stale or offline, or that a quota is holding its schedule back
    changes_at: MonotonicTime | None = None  # when this next changes by itself


@dataclass(frozen=True, slots=True, kw_only=True)
class Refresh:
    """What one refresh found (deterministic for a given input)."""

    at: datetime
    provider_status: ProviderStatus  # the status of the league whose health the board was told
    health: BoardHealth  # what the board was told: its source's health, and games held in the TV delay
    reconciliation: Reconciliation  # what changed in the board's contests
    pipeline: PipelineResult  # what every league's pipeline did, together
    unseen: tuple[Missed, ...] = ()  # cards that left before they ever aired, and why
    leagues: tuple[LeagueHealth, ...] = ()  # each covered league's health, in the order first covered
    policy: PolicyView = EMPTY_POLICY_VIEW  # what the board's policy was told: both leagues', its offers' cards staged


@dataclass(frozen=True, slots=True, kw_only=True)
class Presentation:
    """A card's first committed frame: what went up, why, and how its teams were drawn."""

    at: datetime
    card: CardId
    reason: SelectionReason
    treatment: MatchupVisualTreatment | None  # None for a card without two team rows
    contrast: VisualContrastPolicy  # the legibility policy the treatment was resolved under


@dataclass(frozen=True, slots=True, kw_only=True)
class Painted:
    """What one paint did."""

    at: datetime
    shown: CardId | None  # the card committed this frame; None for a frame, or when the paint failed
    reason: SelectionReason | None = None  # why the queue chose the card shown
    frame: FrameKind | None = None  # the explicit frame committed, when that's what is up
    stale: bool = False  # the card was drawn marked stale
    render_error: str | None = None  # an isolated render or display failure, if any
    presented: Presentation | None = None  # set on a card's first committed frame
    changes: tuple[LaneChange[BoardPayload], ...] = ()  # what the board did since the last paint
    notes: tuple[BoardNote, ...] = ()  # what the board noted since the last paint


@dataclass(frozen=True, slots=True, kw_only=True)
class TickResult:
    """A trace of one ``run_once`` pass: a refresh, then a paint (deterministic for a given input)."""

    provider_status: ProviderStatus
    reconciliation: Reconciliation  # what changed in the board's contests
    pipeline: PipelineResult
    shown: CardId | None  # the card committed to the display this tick
    frame: FrameKind | None  # the explicit frame committed this tick
    render_error: str | None  # an isolated render/display failure, if any


class AppLoop:
    """Holds the spine's collaborators; refreshes and paints one pass at a time."""

    def __init__(
        self,
        *,
        schedules: Sequence[ScopeSource],
        store: ContestStore,
        pipeline: LiveBaseballPipeline,
        queue: InterleavedCardQueue,
        registry: RendererRegistry,
        sink: DisplaySink,
        fetch_feed: FeedFetcher,
        fetch_win_probability: WinProbFetcher | None = None,
        fetch_fallback_win_probability: WinProbFetcher | None = None,
        fetch_pregame_details: PregameDetailsFetcher | None = None,
        fetch_game_lines: GameLinesFetcher | None = None,
        logos: LogoStore | None = None,
        display_timezone: tzinfo | None = None,
        marquee: MarqueeStyle = DEFAULT_MARQUEE,
        policy: BoardPolicy = BoardPolicy(),
        offline_after: timedelta = OFFLINE_AFTER,
        quota: QuotaNote | None = None,
        football: LiveFootballPipeline | None = None,
        settings: PolicySettings | None = None,
        fresh_past_due: timedelta = FRESH_PAST_DUE,
    ) -> None:
        if not schedules:
            raise ValueError("a board needs at least one schedule source")
        if settings is None:
            settings = PolicySettings()
        self._settings = settings  # what the user set about how the board shares its airtime
        self._schedules = tuple(schedules)
        self._store = store
        self._pipeline = pipeline
        self._football = football  # NFL's cards, staged into the same queue; None: NFL's games have no cards
        self._queue = queue
        self._registry = registry
        self._sink = sink
        self._fetch_feed = fetch_feed
        self._fetch_win_probability = fetch_win_probability  # optional per-game win-prob fetch; None -> no meter
        self._fetch_fallback_win_probability = fetch_fallback_win_probability  # takes over when that goes quiet
        self._fetch_pregame_details = fetch_pregame_details  # optional records + starters; None -> a plain pregame
        self._fetch_game_lines = fetch_game_lines  # optional pregame odds; None -> no odds
        self._logos = logos  # ambient tile store handed to every RenderContext; None -> color-bar fallback
        self._marquee = marquee  # how lines scroll
        self._lanes = LaneClock()  # when each line went up, by the card's time on the board
        self._display_timezone = display_timezone  # the zone board clocks read in; None -> the render clock's
        self._offline_after = offline_after
        self._fresh_past_due = fresh_past_due
        self._quota = quota  # why a quota holds a scope back; None: no quota is consulted
        self._board = BoardSession(queue, sink.profile, policy=policy)
        self._frames = BoardFrameRenderer()
        self._health_changes_at: MonotonicTime | None = None  # when a league's health next changes by itself

    @property
    def settings(self) -> PolicySettings:
        """The board policy's user settings this board runs with."""
        return self._settings

    def refresh(self, at: ClockReading) -> Refresh:
        """One refresh as of ``at``: bring the cards up to date, and tell the board how its source stands."""
        now = at.utc
        held = self._store.scopes
        for schedule in self._schedules:
            self._store.take(schedule.observe(at, held))
        reconciliation = self._store.reconcile(at.monotonic)

        games = [contest for contest in self._store.contests if isinstance(contest, TeamGame)]
        pipeline_result = self._pipeline.refresh(
            [game for game in games if game.sport is Sport.BASEBALL],
            at=at,
            fetch_feed=self._fetch_feed,
            fetch_win_probability=self._fetch_win_probability,
            fetch_fallback_win_probability=self._fetch_fallback_win_probability,
            fetch_pregame_details=self._fetch_pregame_details,
            fetch_game_lines=self._fetch_game_lines,
        )
        if self._football is not None:
            football = self._football.refresh([game for game in games if game.league is League.NFL], at=at)
            pipeline_result = _together(pipeline_result, football)
        unseen = tuple(self._queue.prune(at))
        leagues = tuple(self._judge(league) for league in self._store.league_healths(at.monotonic))
        health, status = self._health(leagues, pipeline_result)
        view = pipeline_result.policy
        offers = tuple(offer for offer in view.offers if self._queue.card(offer.card.raw) is not None)
        policy = PolicyView(view.games, offers)  # an offer whose card the prune just took has nothing to promote
        self._board.publish(health, policy=policy)
        return Refresh(
            at=now,
            provider_status=status,
            health=health,
            reconciliation=reconciliation,
            pipeline=pipeline_result,
            unseen=unseen,
            leagues=leagues,
            policy=policy,
        )

    def paint(self, at: ClockReading, *, clock: Clock | None = None) -> Painted:
        """Draw what the board says should be up at ``at``, and commit it.

        The frame counts from when the display took it: ``clock``, when given, is read again once the commit
        returns, and that reading starts a card's dwell, books it and starts its lines. Without one (a replay,
        a test), no time passes between the two.
        """
        cue = self._board.cue(at)
        payload = cue.payload
        lanes = _ShownLines(self._lanes, (Surface.BOARD, cue.occurrence), at.monotonic)
        context = RenderContext(
            profile=self._sink.profile,
            now=at.utc,
            logos=self._logos,
            timezone=self._display_timezone,
            marquee=Marquee(style=self._marquee, clock=lanes),
        )
        # Isolate render and display failures: never crash the loop. A card that can't be drawn is set
        # aside so the board moves on; a display that fails spends no airtime, and the cue is tried again.
        try:
            frame = self._sink.new_frame()
        except Exception as exc:
            return self._failed(at, exc)
        canvas = DimmedCanvas(frame) if cue.stale else frame
        try:
            if isinstance(payload, BoardFrame):
                self._frames.render(payload, context, canvas)
            else:
                self._registry.render(payload, context, canvas)
        except Exception as exc:
            self._board.failed(cue, at)
            return self._failed(at, exc)
        try:
            self._sink.commit(frame)
        except Exception as exc:
            return self._failed(at, exc)
        shown = at
        if clock is None:
            lanes.commit()
        else:
            shown = clock.read()
            lanes.shown(shown.monotonic)
        self._board.committed(cue, shown)
        changes, notes = tuple(self._board.changes()), tuple(self._board.notes())
        if isinstance(payload, BoardFrame):
            return Painted(at=shown.utc, shown=None, frame=payload.kind, changes=changes, notes=notes)
        presented = None
        if cue.reason is not None and any(change.kind is ChangeKind.UP for change in changes):
            presented = Presentation(
                at=shown.utc,
                card=payload.id,
                reason=cue.reason,
                treatment=_treatment(payload, context),
                contrast=context.contrast,
            )
        return Painted(
            at=shown.utc,
            shown=payload.id,
            reason=cue.reason,
            stale=cue.stale,
            presented=presented,
            changes=changes,
            notes=notes,
        )

    def _failed(self, at: ClockReading, exc: Exception) -> Painted:
        changes, notes = tuple(self._board.changes()), tuple(self._board.notes())
        error = f"{type(exc).__name__}: {exc}"
        return Painted(at=at.utc, shown=None, render_error=error, changes=changes, notes=notes)

    def run_once(self, at: ClockReading) -> TickResult:
        """One refresh, then one paint, at ``at``."""
        refresh = self.refresh(at)
        painted = self.paint(at)
        return TickResult(
            provider_status=refresh.provider_status,
            reconciliation=refresh.reconciliation,
            pipeline=refresh.pipeline,
            shown=painted.shown,
            frame=painted.frame,
            render_error=painted.render_error,
        )

    def refresh_deadline(self, at: ClockReading) -> MonotonicTime | None:
        """When a refresh would next find something changed by itself, as of ``at``, in either league: a state,
        play or result clearing the delay, a caption's time running out, a live card going stale or a stale game's
        cards coming down, the source going stale or offline, or a card that hasn't aired running out of time to
        (it's dropped then, and traced as missed)."""
        missed = self._queue.next_miss(at)
        football = self._football.next_deadline(at) if self._football is not None else None
        return earliest((self._pipeline.next_deadline(at), football, self._health_changes_at, missed))

    def next_deadline(self, at: ClockReading) -> MonotonicTime | None:
        """When either pass next has something to do by itself, as of ``at``."""
        return earliest((self.refresh_deadline(at), self._board.next_deadline(at)))

    def _health(self, leagues: Sequence[LeagueHealth], result: PipelineResult) -> tuple[BoardHealth, ProviderStatus]:
        """The board's view of its source (its healthiest league's schedule) and that league's status, with how
        many games have nothing on the board because their card is still inside the TV delay or their feed has
        stopped answering. Notes when any league's health next changes by itself."""
        carded = self._queue.contests()
        held = sum(1 for game_id in result.held if game_id not in carded)
        held_until = min((at for game_id, at in result.releases if game_id not in carded), default=None)
        unfed = sum(1 for game_id in result.stale if game_id not in carded)
        self._health_changes_at = earliest(league.changes_at for league in leagues)
        healthiest = min(leagues, key=lambda league: _HEALTHIEST_FIRST[league.source])
        health = BoardHealth(
            source=healthiest.source, reason=healthiest.reason, held=held, held_until=held_until, unfed=unfed
        )
        return health, healthiest.scope.status

    def _judge(self, scope: ScopeHealth) -> LeagueHealth:
        """How far the board can trust a league's schedule, from its current scope's health. A quota holding
        the scope back is the reason, fresh or not: it's why no newer answer came. It's offline `offline_after`
        past when a read of the scope was due."""
        league = scope.scope.league
        held = self._quota(scope.scope) if self._quota is not None else None
        error = held or scope.error or "no answer"
        if scope.fresh_from is None or scope.age is None:
            return LeagueHealth(
                league=league, source=SourceHealth.OFFLINE, scope=scope, reason=f"no schedule yet: {error}"
            )
        # Ages are whole seconds, and a limit is passed only once a full second over it.
        second = timedelta(seconds=1)
        since = scope.fresh_from.monotonic
        offline = max(scope.max_age - self._fresh_past_due, timedelta(0)) + self._offline_after
        if scope.age > offline:
            reason = f"no schedule for over {_span(self._offline_after)}: {error}"
            return LeagueHealth(league=league, source=SourceHealth.OFFLINE, scope=scope, reason=reason)
        if scope.status is ProviderStatus.STALE:
            reason = f"no schedule for over {_span(scope.max_age)}: {error}"
            changes_at = since + offline + second
            return LeagueHealth(
                league=league, source=SourceHealth.STALE, scope=scope, reason=reason, changes_at=changes_at
            )
        return LeagueHealth(
            league=league,
            source=SourceHealth.FRESH,
            scope=scope,
            reason=held or "",
            changes_at=since + scope.max_age + second,
        )


class _ShownLines(LaneFrame):
    """A frame's view of the board's lane clock that remembers the lines drawn through it, so a line new in the
    frame starts when the display took the frame rather than when it was drawn.

    It draws through an ordinary view as of the frame's moment. Once the frame is shown, the same lines are
    asked again through a view as of that moment, and that view's starts are the ones kept.
    """

    def __init__(self, lanes: LaneClock, presentation: Hashable, at: MonotonicTime) -> None:
        super().__init__(lanes, presentation, at, {})
        self._lanes = lanes
        self._of = presentation
        self._drawn = lanes.frame(presentation, at)
        self._asked: list[tuple[TextLane, str, bool]] = []

    def elapsed(self, lane: TextLane, text: str, *, overflows: bool) -> float | None:
        self._asked.append((lane, text, overflows))
        return self._drawn.elapsed(lane, text, overflows=overflows)

    def commit(self) -> None:
        """The display took this frame the moment it was drawn: its lines' starts stand as drawn."""
        self._drawn.commit()

    def shown(self, at: MonotonicTime) -> None:
        """The display took this frame at ``at``: its lines' starts stand, a new one's counted from then."""
        view = self._lanes.frame(self._of, at)
        for lane, text, overflows in self._asked:
            view.elapsed(lane, text, overflows=overflows)
        view.commit()


def _together(first: PipelineResult, second: PipelineResult) -> PipelineResult:
    """Two pipelines' passes as one: each kind of card, hold and final from both, ids sorted where a pass sorts
    them."""
    return PipelineResult(
        pregames=first.pregames + second.pregames,
        statuses=first.statuses + second.statuses,
        ingested=first.ingested + second.ingested,
        big_plays=first.big_plays + second.big_plays,
        no_hitters=first.no_hitters + second.no_hitters,
        finals=first.finals + second.finals,
        held=tuple(sorted(first.held + second.held, key=str)),
        removed=tuple(sorted(first.removed + second.removed, key=str)),
        skipped=first.skipped + second.skipped,
        warnings=first.warnings + second.warnings,
        stale=tuple(sorted(first.stale + second.stale, key=str)),
        releases=tuple(sorted(first.releases + second.releases, key=lambda release: str(release[0]))),
        ends=first.ends + second.ends,
        unconfirmed=tuple(sorted(first.unconfirmed + second.unconfirmed, key=str)),
        corrections=first.corrections + second.corrections,
        policy=PolicyView(
            MappingProxyType({**first.policy.games, **second.policy.games}), first.policy.offers + second.policy.offers
        ),
    )


def _span(span: timedelta) -> str:
    """A limit as a log reads it: ``2m``, or ``90s`` when it isn't whole minutes."""
    seconds = int(span.total_seconds())
    return f"{seconds // 60}m" if seconds % 60 == 0 else f"{seconds}s"


def _treatment(card: ScoreboardCard[Any], context: RenderContext) -> MatchupVisualTreatment | None:
    """How a card's two team rows were drawn: logo variants, tile or bar, meter colors.

    The variants and colors don't depend on where a renderer puts its rows, so the rows' tops here
    are nominal; the trace reads only what's independent of them.
    """
    if not isinstance(card.contest, TeamGame):
        return None
    return resolve_matchup_treatment(
        card.contest.away,
        card.contest.home,
        profile=context.profile,
        logos=context.logos,
        policy=context.contrast,
        away_top=0,
        home_top=0,
    )
