"""Assemble and drive the running app.

`build_loop` wires the spine's collaborators into an `AppLoop` from a provider, a
state fetcher, and a display sink — the one place the standard wiring lives, so the
emulator entry, a future hardware entry, and tests all build the loop the same way.
`run_forever` is the thin real-time wrapper the verdict calls for: tick the clock,
run one deterministic pass, sleep.
"""

from __future__ import annotations

import time
from datetime import datetime

from omni.app.clock import Clock
from omni.app.contest_store import ContestStore
from omni.app.display import DisplaySink
from omni.app.loop import AppLoop
from omni.app.pipeline import FeedFetcher, LiveBaseballPipeline
from omni.app.supervisor import BackoffPolicy, ProviderSupervisor
from omni.cards.factory import CardFactory
from omni.core.time import DurationSeconds
from omni.providers.base import Provider
from omni.queue.delay_policy import DelayPolicy
from omni.queue.priority import PriorityScorer
from omni.queue.scheduler import InterleavedCardQueue
from omni.renderers.registry import default_registry

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
) -> AppLoop:
    """Wire the standard spine into an `AppLoop` (deterministic; no I/O performed here)."""
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
    )


def run_forever(loop: AppLoop, clock: Clock, *, tick: DurationSeconds) -> None:  # pragma: no cover - real-time loop
    """Drive `loop.run_once` on real wall-time forever, sleeping `tick` between passes."""
    interval = tick.as_timedelta().total_seconds()
    while True:
        now: datetime = clock.now()
        loop.run_once(now)
        time.sleep(interval)
