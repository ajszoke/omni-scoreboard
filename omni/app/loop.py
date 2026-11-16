"""AppLoop: the deterministic orchestration passes (ROADMAP A0 spine capstone).

The board decides and paints on separate cadences:

- **`decide(now)`**, the decision pass, wires the spine end to end:
  1. poll the provider (`ProviderSupervisor`, last-known-good on failure);
  2. reconcile the contest set (`ContestStore`);
  3. run the live-card pipeline (`LiveBaseballPipeline`, delay-safe, no spoilers);
  4. pick the next card fairly (`InterleavedCardQueue`), which the board then shows until the next
     decision.
- **`paint(now)`** draws the picked card through the `RendererRegistry` and commits it to the
  `DisplaySink`. It runs at a frame rate, so scrolling lines, countdowns and blinks move between
  decisions. Painting never fetches.
- **`run_once(now)`** is one decision then one paint: the single deterministic pass replay drives.

Both passes are deterministic for a given ``now`` and collaborators, so the real-time runner is a thin
wrapper and a `FakeClock` with a fixture timeline yields stable traces. Render and display failures are
isolated: a paint reports its failure in `Painted.render_error` and the loop keeps going.

Each pick that reaches the panel is traced once, at its first committed frame, with how its matchup
was drawn (`Presentation`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, tzinfo
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
from omni.app.supervisor import ProviderStatus, ProviderSupervisor
from omni.cards.base import CardId, ScoreboardCard
from omni.domain.contest import TeamGame
from omni.queue.scheduler import InterleavedCardQueue, Pick, SelectionReason
from omni.renderers.context import RenderContext
from omni.renderers.image import LogoStore
from omni.renderers.registry import RendererRegistry
from omni.renderers.text_lane import DEFAULT_MARQUEE, LaneClock, Marquee, MarqueeStyle
from omni.renderers.visual_treatment import MatchupVisualTreatment, VisualContrastPolicy, resolve_matchup_treatment

__all__ = ["AppLoop", "Decision", "Painted", "Presentation", "TickResult"]


@dataclass(frozen=True, slots=True, kw_only=True)
class Decision:
    """What one decision pass found and chose (deterministic for a given input)."""

    at: datetime
    provider_status: ProviderStatus
    reconciliation: Reconciliation | None  # None when no snapshot was available to apply
    pipeline: PipelineResult
    pick: Pick | None  # what the board shows until the next decision; None when nothing is eligible


@dataclass(frozen=True, slots=True, kw_only=True)
class Presentation:
    """A pick's first committed frame: what went up, why, and how its teams were drawn."""

    at: datetime
    card: CardId
    reason: SelectionReason
    treatment: MatchupVisualTreatment | None  # None for a card without two team rows
    contrast: VisualContrastPolicy  # the legibility policy the treatment was resolved under


@dataclass(frozen=True, slots=True, kw_only=True)
class Painted:
    """What one paint did."""

    shown: CardId | None  # the card committed this frame; None when nothing was drawn
    render_error: str | None  # an isolated render or display failure, if any
    presented: Presentation | None  # set on the first committed frame of a newly picked card


@dataclass(frozen=True, slots=True, kw_only=True)
class TickResult:
    """A trace of one ``run_once`` pass: a decision, then a paint (deterministic for a given input)."""

    provider_status: ProviderStatus
    reconciliation: Reconciliation | None  # None when no snapshot was available to apply
    pipeline: PipelineResult
    shown: CardId | None  # the card committed to the display this tick
    render_error: str | None  # an isolated render/display failure, if any


class AppLoop:
    """Holds the spine's collaborators; decides and paints one pass at a time."""

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
        self._marquee = Marquee(style=marquee, clock=LaneClock())  # how lines scroll, and when each went up
        self._display_timezone = display_timezone  # the zone board clocks read in; None -> the render clock's
        # The decision pass hands its pick to the paint pass through this one reference; a pick is
        # immutable, so the two can run on different threads.
        self._pick: Pick | None = None
        self._presented: Pick | None = None  # the last pick to reach the panel

    def decide(self, now: datetime) -> Decision:
        """One decision pass as of ``now``: refresh what's known, then pick what the board shows."""
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

        pick = self._queue.pick(now, self._sink.profile)
        self._pick = pick  # with nothing eligible, painting stops and the panel keeps its last frame
        return Decision(
            at=now,
            provider_status=snapshot.status,
            reconciliation=reconciliation,
            pipeline=pipeline_result,
            pick=pick,
        )

    def paint(self, now: datetime) -> Painted:
        """Draw the current pick as of ``now`` and commit it; nothing while there's no pick."""
        pick = self._pick
        if pick is None:
            return Painted(shown=None, render_error=None, presented=None)
        context = RenderContext(
            profile=self._sink.profile,
            now=now,
            logos=self._logos,
            timezone=self._display_timezone,
            marquee=self._marquee,
        )
        try:
            frame = self._sink.new_frame()
            self._registry.render(pick.card, context, frame)
            self._sink.commit(frame)
        except Exception as exc:  # isolate render/display failures — never crash the loop
            return Painted(shown=None, render_error=f"{type(exc).__name__}: {exc}", presented=None)
        presented = None
        if pick is not self._presented:
            self._presented = pick
            presented = Presentation(
                at=now,
                card=pick.card.id,
                reason=pick.reason,
                treatment=_treatment(pick.card, context),
                contrast=context.contrast,
            )
        return Painted(shown=pick.card.id, render_error=None, presented=presented)

    def run_once(self, now: datetime) -> TickResult:
        """One decision, then one paint, as of ``now``."""
        decision = self.decide(now)
        painted = self.paint(now)
        return TickResult(
            provider_status=decision.provider_status,
            reconciliation=decision.reconciliation,
            pipeline=decision.pipeline,
            shown=painted.shown,
            render_error=painted.render_error,
        )


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
