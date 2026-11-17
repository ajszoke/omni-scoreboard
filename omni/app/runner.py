"""Assemble and drive the running app.

`build_loop` wires the spine's collaborators into an `AppLoop` from a schedule, a
feed fetcher, and a display sink — the one place the standard wiring lives, so the
emulator entry, a future hardware entry, and tests all build the loop the same way. A plain
provider is asked inline, once a refresh, through a `ProviderSupervisor`; a scope source
hands the board its scopes itself.
`build_planned_loop` is the running MLB board's wiring on top of it: every source read from a
`PlannedMlb`, which hands the board its slate date by date, so a refresh never waits on the network,
and the live win meter off unless asked for. The plan does the retrying: nothing backs off on top of it.
`run_forever` is the thin real-time wrapper: refreshes on a thread of their own, and frames on
this one at a steady rate, so a slow fetch never freezes the panel. Each frame (`paint_frame`)
counts from when the display took it, so a slow draw or swap doesn't eat into a card's time. A
refresh runs whenever a poll answer lands, when the last one said something would change by itself
(a play clearing the delay, the source going stale), and at least every few seconds. Given a
`Poller`, it warms the plan up first (the slate, then its games, then a pass that can show them)
and then leaves the fetching to the poller's workers.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta, tzinfo

from omni.app.background import Waking
from omni.app.contest_store import ContestStore
from omni.app.display import DisplaySink
from omni.app.loop import AppLoop, Painted
from omni.app.planned_mlb import PlannedMlb
from omni.app.poller import Poller
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
from omni.core.clock import Clock, MonotonicTime, Pacer
from omni.core.time import DurationSeconds
from omni.providers.base import Provider, ScopeSource
from omni.queue.delay_policy import DelayPolicy
from omni.queue.priority import PriorityScorer
from omni.queue.scheduler import InterleavedCardQueue
from omni.renderers.image import LogoStore
from omni.renderers.registry import RendererRegistry, default_registry
from omni.renderers.text_lane import DEFAULT_MARQUEE, MarqueeStyle
from omni.tower.board import BoardPolicy

__all__ = ["build_loop", "build_planned_loop", "paint_frame", "run_forever", "warm_up"]

_WARM_UP_PASSES = 3  # the slate, then its games' feeds, then a pass that can show them
_REFRESH_AT_LEAST_EVERY = timedelta(seconds=5)  # for what changes with nothing to wake it (an answer expiring)


def build_loop(
    schedule: Provider | ScopeSource,
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
    policy: BoardPolicy = BoardPolicy(),
    registry: RendererRegistry | None = None,
) -> AppLoop:
    """Wire the standard spine into an `AppLoop` (deterministic; no I/O performed here).

    A plain `Provider` ``schedule`` is supervised: its answers count as fresh for `max_age`, and after a
    failure it's asked again only after `backoff`. A `ScopeSource` paces and ages its own scopes.

    `fetch_win_probability`, when given, feeds each live game's win-prob meter (delayed in
    the pipeline so it never leads the shown score); omitted, the live card carries no meter.
    `fetch_fallback_win_probability` takes over when that source goes quiet or has no reading.
    `fetch_pregame_details` and `fetch_game_lines`, when given, fill out the pregame card with
    records + starters and odds; omitted, it stays at matchup + countdown.
    `display_timezone` is the zone board clocks read in, since the render clock runs in UTC.
    `logos`, when given, is the ambient tile store every render uses; left None (a
    test/replay that doesn't care) the renderers fall back to a plain color bar.
    `policy` sets how the board shares its airtime: a rotation card's turn, and the cap on
    takeovers back to back. `registry` replaces today's renderers (a replay that only lists decisions).
    """
    queue = InterleavedCardQueue()
    pipeline = LiveBaseballPipeline(
        scorer=PriorityScorer(favorites=favorites),
        delay_policy=DelayPolicy(broadcast_lag=broadcast_lag),
        factory=CardFactory(),
        queue=queue,
    )
    if isinstance(schedule, Provider):
        schedule = ProviderSupervisor(schedule, max_age=max_age, backoff=backoff)
    return AppLoop(
        schedules=(schedule,),
        store=ContestStore(),
        pipeline=pipeline,
        queue=queue,
        registry=registry if registry is not None else default_registry(),
        sink=sink,
        fetch_feed=fetch_feed,
        fetch_win_probability=fetch_win_probability,
        fetch_fallback_win_probability=fetch_fallback_win_probability,
        fetch_pregame_details=fetch_pregame_details,
        fetch_game_lines=fetch_game_lines,
        logos=logos,
        display_timezone=display_timezone,
        marquee=marquee,
        policy=policy,
    )


def build_planned_loop(
    planned: PlannedMlb,
    sink: DisplaySink,
    *,
    meter: bool = False,
    favorites: frozenset[str] = frozenset(),
    broadcast_lag: DurationSeconds = DurationSeconds(45),
    logos: LogoStore | None = None,
    display_timezone: tzinfo | None = None,
    marquee: MarqueeStyle = DEFAULT_MARQUEE,
    policy: BoardPolicy = BoardPolicy(),
    registry: RendererRegistry | None = None,
) -> AppLoop:
    """The running MLB board: the slate, feeds, pregame details and odds all read from ``planned``'s plan.

    The live win meter (the market, then MLB's model) is on only with ``meter``. Each reading stands
    beside a score only if it arrived no later than that score's feed answer.
    """
    return build_loop(
        planned,  # the slate, date by date, from the plan
        planned.fetch_feed,
        sink,
        favorites=favorites,
        broadcast_lag=broadcast_lag,
        fetch_win_probability=planned.fetch_win_probability if meter else None,
        fetch_fallback_win_probability=planned.fetch_fallback_win_probability if meter else None,
        fetch_pregame_details=planned.fetch_pregame_details,
        fetch_game_lines=planned.fetch_game_lines,
        logos=logos,
        display_timezone=display_timezone,
        marquee=marquee,
        policy=policy,
        registry=registry,
    )


def warm_up(loop: AppLoop, poller: Poller, clock: Clock) -> None:
    """Fill the plan before the board starts: refresh, then ask what that refresh wanted, a few times."""
    for _ in range(_WARM_UP_PASSES):
        loop.refresh(clock.read())
        poller.run_due()


def paint_frame(loop: AppLoop, clock: Clock) -> Painted:
    """One of the running board's frames: what should be up now, counted from when the display took it."""
    return loop.paint(clock.read(), clock=clock)


def run_forever(
    loop: AppLoop,
    clock: Clock,
    *,
    fps: float = 10.0,
    log: Callable[[str], None] = print,
    timezone: tzinfo | None = None,
    poller: Poller | None = None,
) -> None:  # pragma: no cover - real-time loop
    """Refresh on a worker thread whenever there's reason to, and paint `fps` times a second here."""
    tracer = DecisionTracer(log, timezone=timezone)

    def refresh() -> MonotonicTime | None:
        started = clock.read()
        result = loop.refresh(started)
        deadline = loop.refresh_deadline(clock.read())
        next_at = started.utc + deadline.since(started.monotonic) if deadline is not None else None
        tracer.refreshed(result, took=clock.monotonic().since(started.monotonic), next_at=next_at)
        return deadline

    refresher = Waking("refresh", clock, refresh, at_least_every=_REFRESH_AT_LEAST_EVERY, on_error=tracer.failed)
    if poller is not None:
        warm_up(loop, poller, clock)
        poller.listen(refresher.wake)
        poller.start()
    refresher.start()
    frames = Pacer(clock, timedelta(seconds=1.0 / fps))
    while True:
        tracer.painted(paint_frame(loop, clock))
        frames.wait()
