"""AppLoop: the deterministic orchestration passes (ROADMAP A0 spine capstone).

The board refreshes and paints on separate cadences:

- **`refresh(now)`**, the decision pass, wires the spine end to end:
  1. poll the provider (`ProviderSupervisor`, last-known-good on failure);
  2. reconcile the contest set (`ContestStore`);
  3. run the live-card pipeline (`LiveBaseballPipeline`, delay-safe, no spoilers), which keeps the card
     queue current;
  4. tell the board how far its source can be trusted, and how many games have nothing to show because
     they wait out the TV delay or their feed has stopped answering.
- **`paint(at)`** asks the board (`BoardSession`) what should be up: a card, chosen by the queue and
  held by its dwell, or an explicit frame (idle, held, offline). It draws that through the
  `RendererRegistry`, a stale card dimmed, and commits it to the `DisplaySink`. The commit starts a
  card's dwell, books its turn in the queue and starts its lines scrolling (each line's start is kept
  by the card's time on the board and its lane), so a card that never reached the panel spends none of
  its turn. Painting runs at a frame rate, so scrolling lines, countdowns and blinks move, and never
  fetches.
- **`run_once(at)`** is one refresh, then one paint: the single deterministic pass replay drives.
- **`refresh_deadline(at)`** says when a refresh next has something to do by itself (a play clearing
  the delay, the source going stale); **`next_deadline(at)`** adds the board's own (a card's minimum,
  its turn, its expiry).

Both passes are deterministic for a given clock reading and collaborators, so the real-time runner is a
thin wrapper and a `FakeClock` with a fixture timeline yields stable traces. Render and display failures
are isolated: a paint reports its failure in `Painted.render_error` and the loop keeps going.

Each card that reaches the panel is traced at its first committed frame, with how its matchup was drawn
(`Presentation`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from typing import Any

from omni.app.contest_store import ContestStore, Reconciliation
from omni.app.display import DisplaySink
from omni.app.pipeline import (
    FeedFetcher,
    GameLinesFetcher,
    LiveBaseballPipeline,
    PipelineResult,
    PregameDetailsFetcher,
    WinProbFetcher,
)
from omni.app.supervisor import ProviderStatus, ProviderSupervisor, SupervisedSnapshot
from omni.cards.base import CardId, ScoreboardCard
from omni.core.clock import ClockReading, MonotonicTime
from omni.domain.contest import TeamGame
from omni.queue.scheduler import InterleavedCardQueue, SelectionReason
from omni.renderers.board_frame import BoardFrameRenderer
from omni.renderers.canvas import DimmedCanvas
from omni.renderers.context import RenderContext
from omni.renderers.image import LogoStore
from omni.renderers.registry import RendererRegistry
from omni.renderers.text_lane import DEFAULT_MARQUEE, LaneClock, Marquee, MarqueeStyle
from omni.renderers.visual_treatment import MatchupVisualTreatment, VisualContrastPolicy, resolve_matchup_treatment
from omni.tower.board import (
    BoardFrame,
    BoardHealth,
    BoardPayload,
    BoardPolicy,
    BoardSession,
    FrameKind,
    SourceHealth,
)
from omni.tower.lane import ChangeKind, LaneChange, earliest
from omni.tower.segment import Surface

__all__ = ["OFFLINE_AFTER", "AppLoop", "Painted", "Presentation", "Refresh", "TickResult"]

# How long the schedule may go unanswered before the board takes its cards down and says it's offline.
OFFLINE_AFTER = timedelta(minutes=10)


@dataclass(frozen=True, slots=True, kw_only=True)
class Refresh:
    """What one refresh found (deterministic for a given input)."""

    at: datetime
    provider_status: ProviderStatus
    health: BoardHealth  # what the board was told: its source's health, and games held in the TV delay
    reconciliation: Reconciliation | None  # None when no snapshot was available to apply
    pipeline: PipelineResult


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


@dataclass(frozen=True, slots=True, kw_only=True)
class TickResult:
    """A trace of one ``run_once`` pass: a refresh, then a paint (deterministic for a given input)."""

    provider_status: ProviderStatus
    reconciliation: Reconciliation | None  # None when no snapshot was available to apply
    pipeline: PipelineResult
    shown: CardId | None  # the card committed to the display this tick
    frame: FrameKind | None  # the explicit frame committed this tick
    render_error: str | None  # an isolated render/display failure, if any


class AppLoop:
    """Holds the spine's collaborators; refreshes and paints one pass at a time."""

    def __init__(
        self,
        *,
        supervisor: ProviderSupervisor,
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
    ) -> None:
        self._supervisor = supervisor
        self._store = store
        self._pipeline = pipeline
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
        self._board = BoardSession(queue, sink.profile, policy=policy)
        self._frames = BoardFrameRenderer()
        self._health_changes_at: datetime | None = None  # when the source's health next changes by itself

    def refresh(self, now: datetime) -> Refresh:
        """One refresh as of ``now``: bring the cards up to date, and tell the board how its source stands."""
        snapshot = self._supervisor.poll(now)
        reconciliation = self._store.apply(snapshot.update) if snapshot.update is not None else None

        team_games = [contest for contest in self._store.contests if isinstance(contest, TeamGame)]
        pipeline_result = self._pipeline.refresh(
            team_games,
            now=now,
            fetch_feed=self._fetch_feed,
            fetch_win_probability=self._fetch_win_probability,
            fetch_fallback_win_probability=self._fetch_fallback_win_probability,
            fetch_pregame_details=self._fetch_pregame_details,
            fetch_game_lines=self._fetch_game_lines,
        )
        self._queue.prune(now)
        health = self._health(snapshot, pipeline_result)
        self._board.health = health
        return Refresh(
            at=now,
            provider_status=snapshot.status,
            health=health,
            reconciliation=reconciliation,
            pipeline=pipeline_result,
        )

    def paint(self, at: ClockReading) -> Painted:
        """Draw what the board says should be up at ``at``, and commit it."""
        cue = self._board.cue(at)
        payload = cue.payload
        lanes = self._lanes.frame((Surface.BOARD, cue.occurrence), at.monotonic)
        context = RenderContext(
            profile=self._sink.profile,
            now=at.utc,
            logos=self._logos,
            timezone=self._display_timezone,
            marquee=Marquee(style=self._marquee, clock=lanes),
        )
        try:
            frame = self._sink.new_frame()
            canvas = DimmedCanvas(frame) if cue.stale else frame
            if isinstance(payload, BoardFrame):
                self._frames.render(payload, context, canvas)
            else:
                self._registry.render(payload, context, canvas)
            self._sink.commit(frame)
        except Exception as exc:  # isolate render/display failures — never crash the loop
            changes = tuple(self._board.changes())
            return Painted(at=at.utc, shown=None, render_error=f"{type(exc).__name__}: {exc}", changes=changes)
        lanes.commit()
        self._board.committed(cue, at)
        changes = tuple(self._board.changes())
        if isinstance(payload, BoardFrame):
            return Painted(at=at.utc, shown=None, frame=payload.kind, changes=changes)
        presented = None
        if cue.reason is not None and any(change.kind is ChangeKind.UP for change in changes):
            presented = Presentation(
                at=at.utc,
                card=payload.id,
                reason=cue.reason,
                treatment=_treatment(payload, context),
                contrast=context.contrast,
            )
        return Painted(
            at=at.utc, shown=payload.id, reason=cue.reason, stale=cue.stale, presented=presented, changes=changes
        )

    def run_once(self, at: ClockReading) -> TickResult:
        """One refresh, then one paint, at ``at``."""
        refresh = self.refresh(at.utc)
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
        """When a refresh would next find something changed by itself, as of ``at``: a state or play clearing
        the delay, a final's reveal, a stale game's cards coming down, the source going stale or offline."""
        instants = [self._pipeline.next_deadline(at.utc), self._health_changes_at]
        return earliest(at.monotonic + (instant - at.utc) for instant in instants if instant is not None)

    def next_deadline(self, at: ClockReading) -> MonotonicTime | None:
        """When either pass next has something to do by itself, as of ``at``."""
        return earliest((self.refresh_deadline(at), self._board.next_deadline(at)))

    def _health(self, snapshot: SupervisedSnapshot, result: PipelineResult) -> BoardHealth:
        """The board's view of its source, from the schedule's last-known-good, and how many games have nothing
        on the board because their card is still inside the TV delay."""
        carded = self._queue.contests()
        held = sum(1 for game_id in result.held if game_id not in carded)
        unfed = sum(1 for game_id in result.stale if game_id not in carded)
        update, age = snapshot.update, snapshot.age
        error = snapshot.last_error or "no answer"
        if update is None or age is None:
            self._health_changes_at = None
            return BoardHealth(source=SourceHealth.OFFLINE, reason=f"no schedule yet: {error}", held=held, unfed=unfed)
        # Ages are whole seconds, and a limit is passed only once a full second over it.
        second = timedelta(seconds=1)
        max_age = self._supervisor.max_age.as_timedelta()
        if age.as_timedelta() > self._offline_after:
            self._health_changes_at = None
            reason = f"no schedule for over {_span(self._offline_after)}: {error}"
            return BoardHealth(source=SourceHealth.OFFLINE, reason=reason, held=held, unfed=unfed)
        if snapshot.status is ProviderStatus.STALE:
            self._health_changes_at = update.observed_at + self._offline_after + second
            reason = f"no schedule for over {_span(max_age)}: {error}"
            return BoardHealth(source=SourceHealth.STALE, reason=reason, held=held, unfed=unfed)
        self._health_changes_at = update.observed_at + max_age + second
        return BoardHealth(source=SourceHealth.FRESH, held=held, unfed=unfed)


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
