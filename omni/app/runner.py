"""Assemble and drive the running app.

`build_loop` wires the spine's collaborators into an `AppLoop` from a provider, a
feed fetcher, and a display sink — the one place the standard wiring lives, so the
emulator entry, a future hardware entry, and tests all build the loop the same way.
`run_forever` is the thin real-time wrapper: decisions on a thread of their own, once a
tick, and frames on this one at a steady rate, so a slow fetch never freezes the panel.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta, tzinfo

from omni.app.background import Repeating
from omni.app.clock import Clock, Pacer
from omni.app.contest_store import ContestStore
from omni.app.display import DisplaySink
from omni.app.loop import AppLoop
from omni.app.pipeline import (
    FeedFetcher,
    GameLinesFetcher,
    LiveBaseballPipeline,
    PregameDetailsFetcher,
    WinProbFetcher,
)
from omni.app.supervisor import BackoffPolicy, ProviderSupervisor
from omni.app.trace import DecisionTracer
from omni.cards.factory import CardFactory
from omni.core.time import DurationSeconds
from omni.providers.base import Provider
from omni.queue.delay_policy import DelayPolicy
from omni.queue.priority import PriorityScorer
from omni.queue.scheduler import InterleavedCardQueue
from omni.renderers.image import LogoStore
from omni.renderers.registry import default_registry
from omni.renderers.text_lane import DEFAULT_MARQUEE, MarqueeStyle

__all__ = ["build_loop", "run_forever"]


def build_loop(
    provider: Provider,
    fetch_feed: FeedFetcher,
    sink: DisplaySink,
    *,
    favorites: frozenset[str] = frozenset(),
    broadcast_lag: DurationSeconds = DurationSeconds(45),
    max_age: DurationSeconds = DurationSeconds(120),
    backoff: BackoffPolicy | None = None,
    fetch_win_probability: WinProbFetcher | None = None,
    fetch_fallback_win_probability: WinProbFetcher | None = None,
    fetch_pregame_details: PregameDetailsFetcher | None = None,
    fetch_game_lines: GameLinesFetcher | None = None,
    logos: LogoStore | None = None,
    display_timezone: tzinfo | None = None,
    marquee: MarqueeStyle = DEFAULT_MARQUEE,
) -> AppLoop:
    """Wire the standard spine into an `AppLoop` (deterministic; no I/O performed here).

    `fetch_win_probability`, when given, feeds each live game's win-prob meter (delayed in
    the pipeline so it never leads the shown score); omitted, the live card carries no meter.
    `fetch_fallback_win_probability` takes over when that source goes quiet or has no reading.
    `fetch_pregame_details` and `fetch_game_lines`, when given, fill out the pregame card with
    records + starters and odds; omitted, it stays at matchup + countdown.
    `display_timezone` is the zone board clocks read in, since the render clock runs in UTC.
    `logos`, when given, is the ambient tile store every render uses; left None (a
    test/replay that doesn't care) the renderers fall back to a plain colour bar.
    """
    queue = InterleavedCardQueue()
    pipeline = LiveBaseballPipeline(
        scorer=PriorityScorer(favorites=favorites),
        delay_policy=DelayPolicy(broadcast_lag=broadcast_lag),
        factory=CardFactory(),
        queue=queue,
    )
    return AppLoop(
        supervisor=ProviderSupervisor(provider, max_age=max_age, backoff=backoff),
        store=ContestStore(),
        pipeline=pipeline,
        queue=queue,
        registry=default_registry(),
        sink=sink,
        fetch_feed=fetch_feed,
        fetch_win_probability=fetch_win_probability,
        fetch_fallback_win_probability=fetch_fallback_win_probability,
        fetch_pregame_details=fetch_pregame_details,
        fetch_game_lines=fetch_game_lines,
        logos=logos,
        display_timezone=display_timezone,
        marquee=marquee,
    )


def run_forever(
    loop: AppLoop,
    clock: Clock,
    *,
    tick: DurationSeconds,
    fps: float = 10.0,
    log: Callable[[str], None] = print,
    timezone: tzinfo | None = None,
) -> None:  # pragma: no cover - real-time loop
    """Decide once a `tick` on a worker thread, and paint the current pick `fps` times a second here."""
    tracer = DecisionTracer(log, timezone=timezone)
    every = tick.as_timedelta()

    def decide() -> None:
        started = clock.read()
        decision = loop.decide(started.utc)
        tracer.decided(decision, took=clock.monotonic().since(started.monotonic), next_at=started.utc + every)

    Repeating("decide", clock, every, decide, on_error=tracer.failed).start()
    frames = Pacer(clock, timedelta(seconds=1.0 / fps))
    while True:
        tracer.painted(loop.paint(clock.now()))
        frames.wait()
