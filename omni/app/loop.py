"""AppLoop: the deterministic orchestration tick (ROADMAP A0 spine capstone).

``run_once(now)`` is one pass of the appliance, wiring the spine end to end:

1. poll the provider (`ProviderSupervisor` — last-known-good on failure),
2. reconcile the contest set (`ContestStore`),
3. run the live-card pipeline (`LiveBaseballPipeline` — delay-safe, no spoilers),
4. pick the next card fairly (`InterleavedCardQueue`),
5. render it through the `RendererRegistry` and commit it to the `DisplaySink`.

It is deterministic — the same ``now`` + collaborators always produce the same
`TickResult` — so the infinite loop is a thin wrapper and a `FakeClock` + fixture
timeline yields stable traces. Render/display failures are isolated: the loop
reports them in `TickResult.render_error` and keeps running, never crashing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, tzinfo

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
from omni.cards.base import CardId
from omni.domain.contest import TeamGame
from omni.queue.scheduler import InterleavedCardQueue
from omni.renderers.context import RenderContext
from omni.renderers.image import LogoStore
from omni.renderers.registry import RendererRegistry

__all__ = ["TickResult", "AppLoop"]


@dataclass(frozen=True, slots=True, kw_only=True)
class TickResult:
    """A trace of one ``run_once`` pass (deterministic for a given input)."""

    provider_status: ProviderStatus
    reconciliation: Reconciliation | None  # None when no snapshot was available to apply
    pipeline: PipelineResult
    shown: CardId | None  # the card committed to the display this tick
    render_error: str | None  # an isolated render/display failure, if any


class AppLoop:
    """Holds the spine's collaborators and runs one deterministic tick at a time."""

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
        self._logos = logos  # ambient tile store handed to every RenderContext; None -> colour-bar fallback
        self._display_timezone = display_timezone  # the zone board clocks read in; None -> the render clock's

    def run_once(self, now: datetime) -> TickResult:
        """One full pass of the appliance as of ``now``."""
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

        card = self._queue.next_card(now, self._sink.profile)
        shown: CardId | None = None
        render_error: str | None = None
        if card is not None:
            try:
                frame = self._sink.new_frame()
                context = RenderContext(
                    profile=self._sink.profile, now=now, logos=self._logos, timezone=self._display_timezone
                )
                self._registry.render(card, context, frame)
                self._sink.commit(frame)
                shown = card.id
            except Exception as exc:  # isolate render/display failures — never crash the loop
                render_error = f"{type(exc).__name__}: {exc}"

        return TickResult(
            provider_status=snapshot.status,
            reconciliation=reconciliation,
            pipeline=pipeline_result,
            shown=shown,
            render_error=render_error,
        )
